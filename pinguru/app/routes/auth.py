from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from app.database import get_db
from app.models.models import UserCreate, PlanType, PLAN_LIMITS, get_plan_type
from app.config import settings
from app.services.instagram import InstagramService
from passlib.context import CryptContext
from bson import ObjectId
from bson.errors import InvalidId
from app.security import limiter
import jwt

router = APIRouter()
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()


class InstagramTokenRequest(BaseModel):
    access_token: str

def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)

def verify_password(pw: str, hashed: str) -> bool:
    return pwd_ctx.verify(pw, hashed)

def create_jwt(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_oauth_state(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=10)
    payload = {"sub": user_id, "exp": expire, "type": "instagram_oauth_state"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_oauth_state(state: str) -> str:
    try:
        payload = jwt.decode(state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    if payload.get("type") != "instagram_oauth_state":
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    return user_id

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db=Depends(get_db)
):
    """Proper FastAPI dependency — use with Depends(get_current_user) in any route."""
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except InvalidId:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid credentials")

# ── Register ──────────────────────────────────────────────────────────────────

@router.post("/register")
@limiter.limit("5/minute")
async def register(request: Request, data: UserCreate, db=Depends(get_db)):
    existing = await db.users.find_one({"email": data.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_doc = {
        "email": data.email,
        "hashed_password": hash_password(data.password),
        "plan": PlanType.Free,
        "dm_limit": PLAN_LIMITS[PlanType.Free]["dm_limit"],
        "dm_count_this_month": 0,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.users.insert_one(user_doc)
    token = create_jwt(str(result.inserted_id))
    return {"token": token, "message": "Account created ✅"}

# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, data: UserCreate, db=Depends(get_db)):
    user = await db.users.find_one({"email": data.email})
    if not user or not verify_password(data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_jwt(str(user["_id"]))
    return {"token": token, "plan": get_plan_type(user["plan"]).name, "instagram_connected": bool(user.get("instagram_user_id"))}

# ── Instagram OAuth ───────────────────────────────────────────────────────────

@router.get("/instagram/initiate")
async def instagram_initiate(user=Depends(get_current_user)):
    state = create_oauth_state(str(user["_id"]))
    redirect_uri = f"{settings.BASE_URL}/auth/instagram/callback"
    params = urlencode(
        {
            "client_id": settings.META_APP_ID,
            "redirect_uri": redirect_uri,
            "scope": "instagram_business_basic,instagram_business_manage_messages,instagram_business_manage_comments",
            "response_type": "code",
            "state": state,
        }
    )
    oauth_url = f"https://www.facebook.com/v19.0/dialog/oauth?{params}"
    return {"auth_url": oauth_url}

@router.get("/instagram/callback")
async def instagram_callback(
    code: str,
    state: str | None = None,
    db=Depends(get_db),
):
    if not state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    user_id = decode_oauth_state(state)

    try:
        user_object_id = ObjectId(user_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    user = await db.users.find_one({"_id": user_object_id})
    if not user:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    redirect_uri = f"{settings.BASE_URL}/auth/instagram/callback"
    result = await InstagramService.exchange_code_for_token(code, redirect_uri)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {result.get('error')}")

    token_data = result["token_data"]
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="No access token returned")

    profile = await InstagramService.get_user_profile(access_token)
    expires_in = token_data.get("expires_in", 5183944)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    encrypted_access_token = InstagramService.encrypt_access_token(access_token)

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "instagram_user_id": profile.get("id"),
            "instagram_access_token": encrypted_access_token,
            "ig_token_expires_at": expires_at,
        }}
    )
    return {"status": "Instagram connected ✅", "profile": profile}


@router.post("/instagram/token")
async def save_instagram_token(
    data: InstagramTokenRequest,
    db=Depends(get_db),
    x_admin_key: str = Header(None),
):
    if x_admin_key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Forbidden")

    access_token = data.access_token.strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="access_token is required")

    url = (
        "https://graph.facebook.com/v19.0/me/accounts"
        f"?fields=instagram_business_account&access_token={access_token}"
    )
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        if response.status_code >= 400:
            raise HTTPException(status_code=400, detail="Invalid or expired access token")
        profile = response.json()

    ig_user_id = None
    for account in profile.get("data", []):
        instagram_business_account = account.get("instagram_business_account") or {}
        ig_user_id = instagram_business_account.get("id")
        if ig_user_id:
            break

    if not ig_user_id:
        raise HTTPException(status_code=400, detail="Failed to fetch Instagram user ID from token")

    encrypted_access_token = InstagramService.encrypt_access_token(access_token)
    result = await db.users.update_one(
        {"instagram_user_id": ig_user_id},
        {"$set": {
            "instagram_access_token": encrypted_access_token,
            "instagram_user_id": ig_user_id,
            "instagram_connected": True
        }},
        upsert=True,
    )

    return {"status": "Instagram token saved ✅", "instagram_user_id": ig_user_id}
