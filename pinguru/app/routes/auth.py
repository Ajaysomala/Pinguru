from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.database import get_db
from app.models.models import UserCreate, PlanType, PLAN_LIMITS
from app.config import settings
from app.services.instagram import InstagramService
from datetime import datetime, timedelta
from passlib.context import CryptContext
from bson import ObjectId
import jwt

router = APIRouter()
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)

def verify_password(pw: str, hashed: str) -> bool:
    return pwd_ctx.verify(pw, hashed)

def create_jwt(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

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
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ── Register ──────────────────────────────────────────────────────────────────

@router.post("/register")
async def register(data: UserCreate, db=Depends(get_db)):
    existing = await db.users.find_one({"email": data.email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    user_doc = {
        "email": data.email,
        "hashed_password": hash_password(data.password),
        "plan": PlanType.FREE,
        "dm_limit": PLAN_LIMITS[PlanType.FREE]["dm_limit"],
        "dm_count_this_month": 0,
        "is_active": True,
        "created_at": datetime.utcnow(),
    }
    result = await db.users.insert_one(user_doc)
    token = create_jwt(str(result.inserted_id))
    return {"token": token, "message": "Account created ✅"}

# ── Login ─────────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(data: UserCreate, db=Depends(get_db)):
    user = await db.users.find_one({"email": data.email})
    if not user or not verify_password(data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_jwt(str(user["_id"]))
    return {"token": token, "plan": user["plan"], "instagram_connected": bool(user.get("instagram_user_id"))}

# ── Instagram OAuth Callback ──────────────────────────────────────────────────

@router.get("/instagram/callback")
async def instagram_callback(
    code: str,
    db=Depends(get_db),
    user=Depends(get_current_user)
):
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
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "instagram_user_id": profile.get("id"),
            "instagram_access_token": access_token,
            "ig_token_expires_at": expires_at,
        }}
    )
    return {"status": "Instagram connected ✅", "profile": profile}
