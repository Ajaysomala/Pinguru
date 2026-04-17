from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import hashlib
import json
import secrets
import logging
logger = logging.getLogger(__name__)

import httpx
import jwt
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from google.auth.transport import requests
from google.oauth2 import id_token
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator

from app.config import settings
from app.database import get_db
from app.models.models import PLAN_LIMITS, PlanType, UserCreate, get_plan_type
from app.security import limiter
from app.services.email import send_otp_email
from app.services.instagram import InstagramService

router = APIRouter()
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


class UserLoginRequest(BaseModel):
    email: EmailStr
    password: str


class InstagramTokenRequest(BaseModel):
    access_token: str
    user_id: str | None = None


class GoogleAuthRequest(BaseModel):
    id_token: str


class OTPVerifyRequest(BaseModel):
    email: EmailStr
    otp: str


class OTPResendRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: EmailStr) -> str:
        return str(v).strip().lower()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hash_password(pw: str) -> str:
    return pwd_ctx.hash(pw)


def verify_password(pw: str, hashed: str) -> bool:
    return pwd_ctx.verify(pw, hashed)


def create_jwt(user_id: str) -> str:
    expire = _utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="pg_token",
        value=token,
        httponly=True,
        secure=settings.ENVIRONMENT.lower() == "production",
        samesite="lax",
        max_age=604800,
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key="pg_token", path="/")


def generate_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def create_oauth_state(user_id: str) -> str:
    expire = _utcnow() + timedelta(minutes=10)
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


async def get_current_user(request: Request, db=Depends(get_db)):
    """Supports cookie-first auth with Bearer fallback for compatibility."""
    token = request.cookies.get("pg_token")

    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

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
    except (InvalidId, jwt.InvalidTokenError):
        raise HTTPException(status_code=401, detail="Invalid credentials")


# ── Register ──────────────────────────────────────────────────────────────────

@router.post("/register")
@limiter.limit("5/minute")
async def register(request: Request, data: UserCreate, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    existing = await db.users.find_one({"email": email})

    if existing and existing.get("email_verified", False):
        raise HTTPException(status_code=400, detail="Email already registered")

    otp = generate_otp()
    otp_expires = _utcnow() + timedelta(minutes=5)

    if existing and not existing.get("email_verified", False):
        await db.users.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    "hashed_password": hash_password(data.password),
                    "otp_hash": hash_otp(otp),
                    "otp_expires_at": otp_expires,
                    "otp_attempts": 0,
                    "otp_resend_window_started_at": _utcnow(),
                    "otp_resend_count": 1,
                    "updated_at": _utcnow(),
                }
            },
        )
    else:
        user_doc = {
            "email": email,
            "hashed_password": hash_password(data.password),
            "plan": PlanType.Free,
            "dm_limit": PLAN_LIMITS[PlanType.Free]["dm_limit"],
            "dm_count_this_month": 0,
            "is_active": True,
            "email_verified": False,
            "otp_hash": hash_otp(otp),
            "otp_expires_at": otp_expires,
            "otp_attempts": 0,
            "otp_resend_window_started_at": _utcnow(),
            "otp_resend_count": 1,
            "created_at": _utcnow(),
        }
        await db.users.insert_one(user_doc)

    otp_sent = await send_otp_email(email, otp)
    if not otp_sent:
        raise HTTPException(status_code=503, detail="Failed to send OTP email. Try again in a moment.")

    return {
        "message": "Account created. Check your email for verification code.",
        "email": email,
        "otp_expires_in_seconds": 300,
    }


# ── Email Verification ────────────────────────────────────────────────────────

@router.post("/verify-email")
@limiter.limit("10/minute")
async def verify_email(request: Request, data: OTPVerifyRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    otp = data.otp.strip()

    if len(otp) != 6 or not otp.isdigit():
        raise HTTPException(status_code=400, detail="OTP must be a 6-digit code")

    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.get("email_verified"):
        token = create_jwt(str(user["_id"]))
        response = Response(
            json.dumps({
                "message": "Already verified",
                "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
                "instagram_connected": bool(user.get("instagram_user_id")),
            }),
            media_type="application/json",
        )
        _set_auth_cookie(response, token)
        return response

    if int(user.get("otp_attempts", 0)) >= 3:
        raise HTTPException(status_code=429, detail="Too many invalid attempts. Request a new code.")

    otp_expires_at = _as_aware_utc(user.get("otp_expires_at"))
    if not otp_expires_at or _utcnow() > otp_expires_at:
        raise HTTPException(status_code=400, detail="Code expired. Request a new one.")

    expected_hash = user.get("otp_hash")
    if not expected_hash or hash_otp(otp) != expected_hash:
        await db.users.update_one({"_id": user["_id"]}, {"$inc": {"otp_attempts": 1}})
        raise HTTPException(status_code=400, detail="Invalid verification code")

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "email_verified": True,
                "otp_hash": None,
                "otp_expires_at": None,
                "otp_attempts": 0,
                "otp_resend_count": 0,
                "otp_resend_window_started_at": None,
            }
        },
    )

    token = create_jwt(str(user["_id"]))
    response = Response(
        json.dumps({
            "message": "Email verified",
            "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
            "instagram_connected": bool(user.get("instagram_user_id")),
        }),
        media_type="application/json",
    )
    _set_auth_cookie(response, token)
    return response


@router.post("/resend-otp")
@limiter.limit("20/minute")
async def resend_otp(request: Request, data: OTPResendRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.get("email_verified"):
        return {"message": "Already verified"}

    now = _utcnow()
    window_start = _as_aware_utc(user.get("otp_resend_window_started_at"))
    resend_count = int(user.get("otp_resend_count", 0))

    if not window_start or now > window_start + timedelta(hours=1):
        resend_count = 0
        window_start = now

    if resend_count >= 3:
        raise HTTPException(status_code=429, detail="Resend limit reached. Try again in 1 hour.")

    otp = generate_otp()
    otp_expires = now + timedelta(minutes=5)

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "otp_hash": hash_otp(otp),
                "otp_expires_at": otp_expires,
                "otp_attempts": 0,
                "otp_resend_window_started_at": window_start,
                "otp_resend_count": resend_count + 1,
            }
        },
    )

    otp_sent = await send_otp_email(email, otp)
    if not otp_sent:
        raise HTTPException(status_code=503, detail="Failed to send OTP email. Try again later.")

    return {"message": "New verification code sent", "otp_expires_in_seconds": 300}


# ── Login / Session ───────────────────────────────────────────────────────────

@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, data: UserLoginRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    user = await db.users.find_one({"email": email})

    if not user or not verify_password(data.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.get("email_verified", False):
        raise HTTPException(status_code=403, detail="Email not verified. Check your inbox for OTP.")

    token = create_jwt(str(user["_id"]))
    response_data = {
        "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
        "instagram_connected": bool(user.get("instagram_user_id")),
    }
    response = Response(json.dumps(response_data), media_type="application/json")
    _set_auth_cookie(response, token)
    return response


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return {
        "email": user.get("email"),
        "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
        "instagram_connected": bool(user.get("instagram_user_id")),
        "email_verified": bool(user.get("email_verified", False)),
    }


@router.post("/logout")
async def logout():
    response = Response(json.dumps({"message": "Logged out"}), media_type="application/json")
    _clear_auth_cookie(response)
    return response


# ── Instagram OAuth ───────────────────────────────────────────────────────────

@router.get("/instagram/initiate")
async def instagram_initiate(user=Depends(get_current_user)):
    state = create_oauth_state(str(user["_id"]))
    redirect_uri = f"{settings.BASE_URL}/auth/instagram/callback"
    # Use IG_APP_ID (Instagram sub-app) for instagram.com/oauth/authorize
    # Fall back to META_APP_ID only if IG_APP_ID not set
    ig_client_id = settings.IG_APP_ID or settings.META_APP_ID
    params = urlencode(
        {
            "force_reauth": "true",
            "client_id": ig_client_id,
            "redirect_uri": redirect_uri,
            "scope": "instagram_business_basic,instagram_business_manage_messages,instagram_business_manage_comments",
            "response_type": "code",
            "state": state,
        }
    )
    oauth_url = f"https://www.instagram.com/oauth/authorize?{params}"
    return {"auth_url": oauth_url}


@router.get("/instagram/callback")
async def instagram_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    db=Depends(get_db),
):
    # Meta sends error_code + error_message when redirect URI is blocked or user denies
    if error_code:
        frontend_url = settings.FRONTEND_URL or "https://pinguru.me"
        from fastapi.responses import RedirectResponse
        from urllib.parse import quote
        msg = quote(error_message or "Instagram connection failed")
        return RedirectResponse(url=f"{frontend_url}/connect.html?ig_error={msg}")

    if not code:
        raise HTTPException(status_code=400, detail="No authorization code received")

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

    # Prefer user_id baked into token_data (from short-lived token response).
    # Fall back to /me profile call only if missing.
    ig_user_id = str(token_data.get("user_id") or "").strip()
    if not ig_user_id:
        profile = await InstagramService.get_user_profile(access_token)
        ig_user_id = str(profile.get("id") or "").strip()
        logger.info(f"IG user_id from /me fallback: {ig_user_id}")
    else:
        logger.info(f"IG user_id from token_data: {ig_user_id}")

    if not ig_user_id:
        raise HTTPException(status_code=400, detail="Could not resolve Instagram user ID")

    expires_in = token_data.get("expires_in", 5183944)
    expires_at = _utcnow() + timedelta(seconds=expires_in)
    encrypted_access_token = InstagramService.encrypt_access_token(access_token)

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "instagram_user_id": ig_user_id,
                "instagram_access_token": encrypted_access_token,
                "ig_token_expires_at": expires_at,
            }
        },
    )
    # Redirect back to frontend connect page with success flag
    from fastapi.responses import RedirectResponse
    frontend_url = settings.FRONTEND_URL or "https://pinguru.me"
    return RedirectResponse(url=f"{frontend_url}/connect.html?ig_connected=true")


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

    url = f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_API_VERSION}/me/accounts?fields=instagram_business_account"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
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

    update_filter = None
    if data.user_id:
        try:
            user_object_id = ObjectId(data.user_id)
            user_exists = await db.users.find_one({"_id": user_object_id})
            if not user_exists:
                raise HTTPException(status_code=404, detail=f"User {data.user_id} not found in database")
            update_filter = {"_id": user_object_id}
        except InvalidId:
            raise HTTPException(status_code=400, detail="Invalid user_id format")
    else:
        linked_user = await db.users.find_one({"instagram_user_id": ig_user_id})
        if linked_user:
            update_filter = {"_id": linked_user["_id"]}

    if not update_filter:
        raise HTTPException(
            status_code=400,
            detail="No matching user found. Provide user_id to link token to an existing account.",
        )

    encrypted_access_token = InstagramService.encrypt_access_token(access_token)
    result = await db.users.update_one(
        update_filter,
        {
            "$set": {
                "instagram_access_token": encrypted_access_token,
                "instagram_user_id": ig_user_id,
            }
        },
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Update failed: User not found after verification")

    return {"status": "Instagram token saved", "instagram_user_id": ig_user_id}


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.post("/google/callback")
async def google_callback(data: GoogleAuthRequest, db=Depends(get_db)):
    try:
        idinfo = id_token.verify_oauth2_token(data.id_token, requests.Request(), settings.GOOGLE_CLIENT_ID)
        email = (idinfo.get("email") or "").strip().lower()

        if not email:
            raise HTTPException(status_code=400, detail="No email in Google profile")

        user = await db.users.find_one({"email": email})

        if not user:
            user_doc = {
                "email": email,
                "hashed_password": hash_password(settings.DEFAULT_OAUTH_PASSWORD),
                "plan": PlanType.Free,
                "dm_limit": PLAN_LIMITS[PlanType.Free]["dm_limit"],
                "dm_count_this_month": 0,
                "is_active": True,
                "email_verified": True,
                "oauth_provider": "google",
                "created_at": _utcnow(),
            }
            result = await db.users.insert_one(user_doc)
            user = await db.users.find_one({"_id": result.inserted_id})

        if not user.get("email_verified", False):
            await db.users.update_one({"_id": user["_id"]}, {"$set": {"email_verified": True}})
            user["email_verified"] = True

        token = create_jwt(str(user["_id"]))
        response = Response(
            json.dumps(
                {
                    "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
                    "instagram_connected": bool(user.get("instagram_user_id")),
                }
            ),
            media_type="application/json",
        )
        _set_auth_cookie(response, token)
        return response

    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid Google token: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Google authentication failed: {str(e)}")


# ── Profile Update ─────────────────────────────────────────────────────────────

class ProfileUpdateRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    business_category: str | None = None
    onboarding_complete: bool | None = None


@router.patch("/profile")
async def update_profile(
    data: ProfileUpdateRequest,
    response: Response,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    update: dict = {}
    if data.first_name is not None:
        update["first_name"] = data.first_name.strip()[:80]
    if data.last_name is not None:
        update["last_name"] = data.last_name.strip()[:80]
    if data.business_category is not None:
        update["business_category"] = data.business_category.strip()[:100]
    if data.onboarding_complete is not None:
        update["onboarding_complete"] = data.onboarding_complete

    if update:
        await db.users.update_one({"_id": user["_id"]}, {"$set": update})

    updated = await db.users.find_one({"_id": user["_id"]})
    return {
        "email": updated.get("email"),
        "first_name": updated.get("first_name", ""),
        "last_name": updated.get("last_name", ""),
        "business_category": updated.get("business_category", ""),
        "onboarding_complete": updated.get("onboarding_complete", False),
        "plan": get_plan_type(updated.get("plan", PlanType.Free)).name,
        "instagram_connected": bool(updated.get("instagram_user_id")),
        "email_verified": bool(updated.get("email_verified", False)),
    }


# ── Data Deletion ──────────────────────────────────────────────────────────────

@router.post("/data-deletion")
async def request_data_deletion(
    response: Response,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id_str = str(user["_id"])
    await db.automation_rules.delete_many({"user_id": user_id_str})
    await db.dm_logs.delete_many({"user_id": user_id_str})
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "instagram_user_id": None,
                "instagram_access_token": None,
                "ig_token_expires_at": None,
            },
            "$unset": {
                "first_name": "",
                "last_name": "",
                "business_category": "",
            },
        },
    )
    _clear_auth_cookie(response)
    return {"message": "Your data has been deleted. Account deactivated."}
