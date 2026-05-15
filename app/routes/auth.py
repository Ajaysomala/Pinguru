from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from urllib.parse import quote, urlencode, urlparse
import hashlib
import json
import secrets
import logging
logger = logging.getLogger(__name__)

import httpx
import jwt
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from google.auth.transport import requests
from google.oauth2 import id_token
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator

from app.config import settings
from app.database import get_db
from app.models.models import PLAN_LIMITS, PlanType, UserCreate, get_plan_type
from app.security import limiter
from app.services.email import send_otp_email, send_password_reset_email
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


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    reset_token: str
    new_password: str


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


def create_jwt(user_id: str, session_version: int = 0) -> str:
    expire = _utcnow() + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {"sub": user_id, "exp": expire, "sv": int(session_version)}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _session_version(user_doc: dict[str, Any]) -> int:
    try:
        return int(user_doc.get("session_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _shared_cookie_domain() -> str | None:
    if settings.ENVIRONMENT.lower() != "production":
        return None
    frontend_url = (settings.FRONTEND_URL or "").strip()
    if not frontend_url:
        return None
    host = (urlparse(frontend_url).hostname or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1"}:
        return None
    host_parts = host.split(".")
    if len(host_parts) < 2:
        return None
    return f".{'.'.join(host_parts[-2:])}"


def _cookie_cleanup_domains() -> list[str | None]:
    domains: set[str] = set()

    shared_domain = _shared_cookie_domain()
    if shared_domain:
        domains.add(shared_domain)
        domains.add(shared_domain.lstrip("."))

    for source in (settings.FRONTEND_URL, settings.BASE_URL):
        host = (urlparse(source or "").hostname or "").strip().lower()
        if not host or host in {"localhost", "127.0.0.1"}:
            continue
        domains.add(host)
        if host.startswith("www."):
            domains.add(host[4:])

    return [None, *sorted(domains)]


def _login_lockout_until(user_doc: dict[str, Any]) -> datetime | None:
    return _as_aware_utc(user_doc.get("login_lockout_until"))


def _is_login_locked(user_doc: dict[str, Any]) -> bool:
    locked_until = _login_lockout_until(user_doc)
    return bool(locked_until and _utcnow() < locked_until)


def _set_auth_cookie(response: Response, token: str) -> None:
    csrf_token = secrets.token_urlsafe(32)
    cookie_domain = _shared_cookie_domain()
    response.set_cookie(
        key="pg_token",
        value=token,
        httponly=True,
        secure=settings.ENVIRONMENT.lower() == "production",
        samesite="lax",
        max_age=604800,
        path="/",
        domain=cookie_domain,
    )
    response.set_cookie(
        key="pg_csrf",
        value=csrf_token,
        httponly=False,
        secure=settings.ENVIRONMENT.lower() == "production",
        samesite="lax",
        max_age=604800,
        path="/",
        domain=cookie_domain,
    )


def _clear_auth_cookie(response: Response) -> None:
    for domain in _cookie_cleanup_domains():
        response.delete_cookie(key="pg_token", path="/", domain=domain)
        response.delete_cookie(key="pg_csrf", path="/", domain=domain)


def generate_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"


def hash_otp(otp: str) -> str:
    return hashlib.sha256(otp.encode("utf-8")).hexdigest()


def _normalize_text(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _build_display_name(first_name: str, last_name: str) -> str:
    return " ".join(part for part in [first_name, last_name] if part)


def _validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters long")
    if not any(char.isupper() for char in password):
        raise HTTPException(status_code=400, detail="Password must contain at least one uppercase letter (A-Z)")
    if not any(char.islower() for char in password):
        raise HTTPException(status_code=400, detail="Password must contain at least one lowercase letter (a-z)")
    if not any(char.isdigit() for char in password):
        raise HTTPException(status_code=400, detail="Password must contain at least one number (0-9)")
    if not any(char in "!@#$%^&*()-_=+[]{}|;:,.<>?/" for char in password):
        raise HTTPException(status_code=400, detail="Password must contain at least one special character (!@#$%^&*...)")


def _password_reset_frontend_base() -> str:
    return (settings.FRONTEND_URL or "http://localhost:5173").rstrip("/")


def _create_password_reset_token(email: str) -> str:
    expire = _utcnow() + timedelta(minutes=30)
    payload = {"sub": email, "exp": expire, "type": "password_reset"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _decode_password_reset_token(token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Reset token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    if payload.get("type") != "password_reset":
        raise HTTPException(status_code=401, detail="Invalid reset token")

    return payload


async def _ensure_unique_instagram_account(db, instagram_user_id: str, current_user_id: ObjectId | None = None) -> None:
    instagram_user_id = str(instagram_user_id or "").strip()
    if not instagram_user_id:
        return

    await _ensure_unique_instagram_accounts(db, [instagram_user_id], current_user_id)


def _dedupe_instagram_ids(ids: Sequence[str | None]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in ids:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


async def _ensure_unique_instagram_accounts(
    db,
    instagram_ids: list[str],
    current_user_id: ObjectId | None = None,
) -> None:
    normalized_ids = _dedupe_instagram_ids(instagram_ids)
    if not normalized_ids:
        return

    query: dict[str, object] = {
        "$or": [
            {"instagram_user_id": {"$in": normalized_ids}},
            {"instagram_account_ids": {"$in": normalized_ids}},
        ]
    }
    if current_user_id is not None:
        query["_id"] = {"$ne": current_user_id}

    linked_user = await db.users.find_one(query)
    if linked_user:
        raise HTTPException(status_code=409, detail="That Instagram account is already connected to another user")


def _collect_instagram_account_ids(
    token_data: dict[str, Any],
    profile: dict[str, Any],
    business_account_id: str | None,
) -> list[str]:
    # Prefer webhook-facing IDs first for stable webhook matching.
    return _dedupe_instagram_ids(
        [
            str(business_account_id or "").strip(),
            str(profile.get("user_id") or "").strip(),
            str(token_data.get("user_id") or "").strip(),
            str(profile.get("id") or "").strip(),
        ]
    )


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


def _oauth_frontend_base() -> str:
    return settings.FRONTEND_URL or "https://pinguru.me"


def _instagram_redirect_uri() -> str:
    explicit = (settings.INSTAGRAM_REDIRECT_URI or "").strip()
    if explicit:
        return explicit
    return f"{settings.BASE_URL.rstrip('/')}/auth/instagram/callback"


def _oauth_error_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(url=f"{_oauth_frontend_base()}/connect.html?ig_error={quote(message)}")


def _oauth_success_redirect() -> RedirectResponse:
    return RedirectResponse(url=f"{_oauth_frontend_base()}/connect.html?ig_connected=true")


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
        token_session_version = int(payload.get("sv") or 0)
        if token_session_version != _session_version(user):
            raise HTTPException(status_code=401, detail="Session expired")
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

    if existing:
        raise HTTPException(status_code=400, detail="Unable to create account. Please try again.")

    otp = generate_otp()
    otp_expires = _utcnow() + timedelta(minutes=5)

    first_name = _normalize_text(data.first_name, 80)
    last_name = _normalize_text(data.last_name, 80)
    business_category = _normalize_text(data.business_category, 100)
    instagram_username = _normalize_text(data.instagram_username, 100)
    display_name = _build_display_name(first_name, last_name)

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
        "failed_login_attempts": 0,
        "login_lockout_until": None,
        "session_version": 0,
        "created_at": _utcnow(),
        "first_name": first_name,
        "last_name": last_name,
        "business_category": business_category,
        "instagram_username": instagram_username,
        "display_name": display_name,
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
        token = create_jwt(str(user["_id"]), _session_version(user))
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
                "failed_login_attempts": 0,
                "login_lockout_until": None,
            }
        },
    )

    token = create_jwt(str(user["_id"]), _session_version(user))
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


@router.post("/forgot-password/request")
@limiter.limit("5/minute")
async def forgot_password_request(request: Request, data: ForgotPasswordRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    user = await db.users.find_one({"email": email})

    response: dict[str, Any] = {"message": "If an account exists, a password reset link has been sent."}
    if not user:
        return response

    reset_token = _create_password_reset_token(email)
    reset_url = f"{_password_reset_frontend_base()}/forgot-password?email={quote(email)}&token={quote(reset_token)}"

    email_sent = await send_password_reset_email(email, reset_url)
    if not email_sent:
        raise HTTPException(status_code=503, detail="Password reset email is temporarily unavailable")

    if settings.ENVIRONMENT.lower() != "production":
        response["reset_token"] = reset_token
        response["reset_url"] = reset_url

    return response


@router.post("/forgot-password/reset")
@limiter.limit("10/minute")
async def forgot_password_reset(request: Request, data: ResetPasswordRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    password = data.new_password or ""
    _validate_password_strength(password)

    payload = _decode_password_reset_token(data.reset_token)
    token_email = str(payload.get("sub") or "").strip().lower()
    if token_email != email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    user = await db.users.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "hashed_password": hash_password(password),
                "failed_login_attempts": 0,
                "login_lockout_until": None,
                "session_version": _session_version(user) + 1,
            }
        },
    )

    return {"message": "Password updated successfully. You can now sign in."}


# ── Login / Session ───────────────────────────────────────────────────────────

@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, data: UserLoginRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    user = await db.users.find_one({"email": email})

    if user and _is_login_locked(user):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    if not user or not verify_password(data.password, user["hashed_password"]):
        if user:
            failed_login_attempts = int(user.get("failed_login_attempts", 0) or 0) + 1
            update: dict[str, Any] = {"failed_login_attempts": failed_login_attempts}
            if failed_login_attempts >= 5:
                update["login_lockout_until"] = _utcnow() + timedelta(minutes=15)
            await db.users.update_one({"_id": user["_id"]}, {"$set": update})
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.get("email_verified", False):
        raise HTTPException(status_code=403, detail="Email not verified. Check your inbox for OTP.")

    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"failed_login_attempts": 0, "login_lockout_until": None}},
    )

    token = create_jwt(str(user["_id"]), _session_version(user))
    response_data = {
        "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
        "instagram_connected": bool(user.get("instagram_user_id")),
    }
    response = Response(json.dumps(response_data), media_type="application/json")
    _set_auth_cookie(response, token)
    return response


@router.get("/me")
@limiter.limit("60/minute")
async def me(request: Request, user=Depends(get_current_user), db: Any = Depends(get_db)):
    first_name = (user.get("first_name") or "").strip()
    last_name = (user.get("last_name") or "").strip()
    full_name = " ".join(part for part in [first_name, last_name] if part)
    instagram_username = str(user.get("instagram_username") or "").strip()

    if not instagram_username and user.get("instagram_access_token") and user.get("instagram_user_id"):
        try:
            profile = await InstagramService.get_user_profile(str(user.get("instagram_access_token") or ""))
            instagram_username = str(profile.get("username") or "").strip()
            if instagram_username:
                await db.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"instagram_username": instagram_username}},
                )
        except Exception:
            instagram_username = str(user.get("instagram_username") or "").strip()

    return {
        "email": user.get("email"),
        "first_name": first_name,
        "last_name": last_name,
        "business_category": user.get("business_category", ""),
        "display_name": user.get("display_name") or full_name,
        "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
        "instagram_connected": bool(user.get("instagram_user_id")),
        "instagram_user_id": user.get("instagram_user_id", ""),
        "instagram_username": instagram_username,
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
    redirect_uri = _instagram_redirect_uri()
    # Use IG_APP_ID (Instagram sub-app) for instagram.com/oauth/authorize
    # Fall back to META_APP_ID only if IG_APP_ID not set
    ig_client_id = settings.IG_APP_ID or settings.META_APP_ID
    params = urlencode(
        {
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
        return _oauth_error_redirect(error_message or "Instagram connection failed")

    try:
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

        redirect_uri = _instagram_redirect_uri()
        result = await InstagramService.exchange_code_for_token(code, redirect_uri)
        if not result["success"]:
            detail = str(result.get("error") or "Instagram connection failed. Please try again.")
            raise HTTPException(status_code=400, detail=detail)

        token_data = result["token_data"]
        access_token = token_data.get("access_token")
        if not access_token:
            raise HTTPException(status_code=400, detail="No access token returned")

        profile = await InstagramService.get_user_profile(access_token)
        ig_username = str(profile.get("username") or "").strip()
        business_account_id = await InstagramService.get_business_account_id(access_token, preferred_username=ig_username)

        account_ids = _collect_instagram_account_ids(token_data, profile, business_account_id)
        ig_user_id = account_ids[0] if account_ids else ""
        logger.info(
            "IG account ids resolved: primary=%s username=%s candidates=%s",
            ig_user_id,
            ig_username,
            account_ids,
        )

        if not ig_user_id:
            raise HTTPException(status_code=400, detail="Could not resolve Instagram user ID")

        await _ensure_unique_instagram_accounts(db, account_ids, user["_id"])

        expires_in = token_data.get("expires_in", 5183944)
        expires_at = _utcnow() + timedelta(seconds=expires_in)
        encrypted_access_token = InstagramService.encrypt_access_token(access_token)

        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "instagram_user_id": ig_user_id,
                    "instagram_account_ids": account_ids,
                    "instagram_username": ig_username,
                    "instagram_access_token": encrypted_access_token,
                    "ig_token_expires_at": expires_at,
                }
            },
        )
    except HTTPException as exc:
        return _oauth_error_redirect(str(exc.detail))
    except Exception:
        logger.exception("Unexpected Instagram callback failure")
        return _oauth_error_redirect("Instagram connection failed. Please try again.")

    return _oauth_success_redirect()


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

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        response = await client.get(url, headers=headers)
        if response.status_code >= 400:
            raise HTTPException(status_code=400, detail="Instagram connection failed. Invalid or expired access token.")
        profile = response.json()

    ig_user_id = None
    for account in profile.get("data", []):
        instagram_business_account = account.get("instagram_business_account") or {}
        ig_user_id = instagram_business_account.get("id")
        if ig_user_id:
            break

    if not ig_user_id:
        raise HTTPException(status_code=400, detail="Failed to fetch Instagram user ID from token")

    ig_username = ""
    try:
        profile = await InstagramService.get_user_profile(access_token)
        ig_username = str(profile.get("username") or "").strip()
    except Exception:
        ig_username = ""

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
        linked_user = await db.users.find_one(
            {
                "$or": [
                    {"instagram_user_id": ig_user_id},
                    {"instagram_account_ids": ig_user_id},
                ]
            }
        )
        if linked_user:
            update_filter = {"_id": linked_user["_id"]}

    if not update_filter:
        raise HTTPException(
            status_code=400,
            detail="No matching user found. Provide user_id to link token to an existing account.",
        )

    await _ensure_unique_instagram_accounts(db, [ig_user_id], update_filter["_id"])

    encrypted_access_token = InstagramService.encrypt_access_token(access_token)
    result = await db.users.update_one(
        update_filter,
        {
            "$set": {
                "instagram_access_token": encrypted_access_token,
                "instagram_user_id": ig_user_id,
                "instagram_account_ids": [ig_user_id],
                "instagram_username": ig_username or None,
            }
        },
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Update failed: User not found after verification")

    return {"status": "Instagram token saved", "instagram_user_id": ig_user_id, "instagram_username": ig_username}


@router.get("/instagram/media")
async def instagram_media(
    media_type: str = Query("all"),
    limit: int = Query(25, ge=1, le=50),
    user=Depends(get_current_user),
):
    access_token = str(user.get("instagram_access_token") or "").strip()
    instagram_user_id = str(user.get("instagram_user_id") or "").strip()

    if not access_token or not instagram_user_id:
        return {"media": [], "source": "unavailable", "connected": False}

    media = await InstagramService.get_user_media(access_token, limit=limit, media_type=media_type)
    return {
        "media": media,
        "source": "instagram" if media else "fallback",
        "connected": True,
        "media_type": media_type,
    }


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.post("/google/callback")
async def google_callback(data: GoogleAuthRequest, db=Depends(get_db)):
    try:
        idinfo = id_token.verify_oauth2_token(data.id_token, requests.Request(), settings.GOOGLE_CLIENT_ID)
        email = (idinfo.get("email") or "").strip().lower()
        first_name = _normalize_text(idinfo.get("given_name"), 80)
        last_name = _normalize_text(idinfo.get("family_name"), 80)
        display_name = _build_display_name(first_name, last_name) or _normalize_text(idinfo.get("name"), 160)

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
                "failed_login_attempts": 0,
                "login_lockout_until": None,
                "session_version": 0,
                "created_at": _utcnow(),
                "first_name": first_name,
                "last_name": last_name,
                "display_name": display_name,
            }
            result = await db.users.insert_one(user_doc)
            user = await db.users.find_one({"_id": result.inserted_id})

        if not user.get("email_verified", False):
            await db.users.update_one({"_id": user["_id"]}, {"$set": {"email_verified": True}})
            user["email_verified"] = True

        token = create_jwt(str(user["_id"]), _session_version(user))
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
        raise HTTPException(status_code=400, detail="Google authentication failed.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail="Google authentication failed.")


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


# ── Instagram Disconnect ───────────────────────────────────────────────────────

@router.post("/instagram/disconnect")
@limiter.limit("5/minute")
async def disconnect_instagram(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Remove Instagram connection from the user account.
    Does NOT delete automation rules — they stay saved for reconnection.
    """
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "instagram_user_id": None,
                "instagram_account_ids": [],
                "instagram_access_token": None,
                "ig_token_expires_at": None,
                "instagram_username": None,
            }
        },
    )
    return {"disconnected": True, "message": "Instagram account disconnected successfully."}


# ── Instagram Token Refresh ────────────────────────────────────────────────────

@router.post("/instagram/refresh-token")
@limiter.limit("10/minute")
async def refresh_instagram_token(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    """Refresh the long-lived Instagram access token (valid 60 days).
    Call this every 30–45 days to keep the connection alive.
    """
    encrypted_token = str(user.get("instagram_access_token") or "").strip()
    if not encrypted_token:
        raise HTTPException(status_code=400, detail="No Instagram account connected.")

    result = await InstagramService.refresh_long_lived_token(encrypted_token)
    new_token = result.get("access_token")
    if not new_token:
        raise HTTPException(
            status_code=400,
            detail="Token refresh failed. Please reconnect your Instagram account.",
        )

    expires_in = int(result.get("expires_in") or 5183944)
    expires_at = _utcnow() + timedelta(seconds=expires_in)
    encrypted_new_token = InstagramService.encrypt_access_token(new_token)

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "instagram_access_token": encrypted_new_token,
                "ig_token_expires_at": expires_at,
            }
        },
    )

    return {
        "refreshed": True,
        "expires_at": expires_at.isoformat(),
        "message": "Instagram token refreshed successfully.",
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
                "instagram_account_ids": [],
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
