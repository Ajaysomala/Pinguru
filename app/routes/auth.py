from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from urllib.parse import quote, urlencode
import base64
import hashlib
import hmac
import json
import re
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
from app.security import clear_session_cookies, client_ip, get_cookie, limiter, set_session_cookie
from app.services.email import send_otp_email, send_password_reset_email
from app.services.instagram import InstagramService, InstagramTokenExpiredError
from cryptography.fernet import InvalidToken

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


_DUMMY_HASH = "$2b$12$e80yVjJ8.VbI8hN8PuhN0.0XU6E.C1L.7lY0w/aR7s2wR1m6B8y1."


def verify_password(pw: str, hashed: str) -> bool:
    return pwd_ctx.verify(pw, hashed)


def create_jwt(user_id: str, session_version: int = 0) -> str:
    now = _utcnow()
    expire = now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {
        "sub": user_id,
        "exp": expire,
        "iat": int(now.timestamp()),
        "jti": secrets.token_hex(16),
        "typ": "session",
        "sv": int(session_version),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)



def _session_version(user_doc: dict[str, Any]) -> int:
    try:
        return int(user_doc.get("session_version", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _client_ip(request: Request) -> str:
    return client_ip(request)


async def _is_login_locked_for_email_ip(db, email: str, ip: str) -> bool:
    lockout_key = f"{email}:{ip}"
    record = await db.login_lockouts.find_one({"_id": lockout_key})
    if not record:
        return False
    locked_until = _as_aware_utc(record.get("locked_until"))
    return bool(locked_until and _utcnow() < locked_until)


async def _record_failed_login_attempt(db, email: str, ip: str, user: dict[str, Any] | None = None) -> None:
    now = _utcnow()
    lockout_key = f"{email}:{ip}"
    record = await db.login_lockouts.find_one({"_id": lockout_key})
    attempts = int((record or {}).get("failed_attempts", 0) or 0) + 1
    update: dict[str, Any] = {
        "email": email,
        "ip": ip,
        "failed_attempts": attempts,
        "last_attempt_at": now,
    }
    if attempts >= 5:
        update["locked_until"] = now + timedelta(minutes=15)

    await db.login_lockouts.update_one(
        {"_id": lockout_key},
        {"$set": update},
        upsert=True,
    )
    if user:
        await db.users.update_one({"_id": user["_id"]}, {"$set": {"failed_login_attempts": attempts}})


async def _clear_login_lockout(db, email: str, ip: str, user: dict[str, Any] | None = None) -> None:
    lockout_key = f"{email}:{ip}"
    await db.login_lockouts.delete_one({"_id": lockout_key})
    if user:
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"failed_login_attempts": 0, "login_lockout_until": None}},
        )


def _login_lockout_until(user_doc: dict[str, Any]) -> datetime | None:
    return _as_aware_utc(user_doc.get("login_lockout_until"))


def _is_login_locked(user_doc: dict[str, Any]) -> bool:
    locked_until = _login_lockout_until(user_doc)
    return bool(locked_until and _utcnow() < locked_until)



def _set_auth_cookie(response: Response, token: str, csrf_token: str | None = None) -> str:
    csrf_token = csrf_token or secrets.token_urlsafe(32)
    set_session_cookie(response, "pg_token", token, max_age=604800, httponly=True)
    set_session_cookie(response, "pg_csrf", csrf_token, max_age=604800, httponly=False)
    return csrf_token


def _auth_response(payload: dict[str, Any], token: str) -> Response:
    """JSON response that sets session cookies and returns the CSRF token in the body.

    The CSRF cookie is host-only on the API host, so the frontend (another host)
    cannot read it from document.cookie and must use this value instead.
    """
    csrf_token = secrets.token_urlsafe(32)
    response = Response(json.dumps({**payload, "csrf_token": csrf_token}), media_type="application/json")
    _set_auth_cookie(response, token, csrf_token)
    return response


def _clear_auth_cookie(response: Response) -> None:
    clear_session_cookies(response, "pg_token", "pg_csrf")


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


OAUTH_NONCE_COOKIE = "pg_oauth_nonce"
OAUTH_NONCE_PATH = "/auth/instagram"
OAUTH_STATE_TTL_SECONDS = 600


def _hash_oauth_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def create_oauth_state(user_id: str, nonce: str | None = None) -> str:
    expire = _utcnow() + timedelta(seconds=OAUTH_STATE_TTL_SECONDS)
    payload: dict[str, Any] = {"sub": user_id, "exp": expire, "type": "instagram_oauth_state"}
    if nonce is not None:
        payload["nh"] = _hash_oauth_nonce(nonce)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_oauth_state(state: str, nonce: str | None) -> str:
    """Decode the state JWT and require it to match the browser's nonce cookie.

    Binding state to a cookie in the initiating browser stops an attacker from
    sending a victim their own connect link (login CSRF on account linking).
    """
    try:
        payload = jwt.decode(state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    expected_hash = str(payload.get("nh") or "")
    if not nonce or not expected_hash or not hmac.compare_digest(_hash_oauth_nonce(nonce), expected_hash):
        raise HTTPException(status_code=400, detail="Instagram connection expired. Please start again from PinGuru.")
    return decode_oauth_state(state)


def _clear_oauth_nonce(response: Response) -> None:
    response.delete_cookie(
        key=OAUTH_NONCE_COOKIE,
        path=OAUTH_NONCE_PATH,
        secure=settings.ENVIRONMENT.lower() == "production",
        httponly=True,
        samesite="lax",
    )


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
    token = get_cookie(request, "pg_token")

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

        if payload.get("typ") != "session":
            raise HTTPException(status_code=401, detail="Invalid token type")

        user = await db.users.find_one({"_id": ObjectId(user_id)})

        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        if not user.get("is_active", True):
            raise HTTPException(status_code=401, detail="Account has been deactivated")
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
        if not existing.get("email_verified", False):
            now = _utcnow()
            # Per-email limit (3/hour) on re-register
            raw_attempts = existing.get("reregister_timestamps", [])
            valid_attempts = [
                _as_aware_utc(t) for t in raw_attempts
                if _as_aware_utc(t) and (now - _as_aware_utc(t)).total_seconds() < 3600
            ]
            if len(valid_attempts) >= 3:
                raise HTTPException(
                    status_code=429,
                    detail="Too many registration attempts for this email. Please try again later.",
                )

            valid_attempts.append(now)
            otp = generate_otp()
            otp_expires = now + timedelta(minutes=5)
            unverified_expires = now + timedelta(hours=24)

            update_data: dict[str, Any] = {
                "hashed_password": hash_password(data.password),
                "otp_hash": hash_otp(otp),
                "otp_expires_at": otp_expires,
                "otp_attempts": 0,
                "unverified_expires_at": unverified_expires,
                "reregister_timestamps": valid_attempts,
                "reregister_count": len(valid_attempts),
                "reregister_window_started_at": valid_attempts[0],
            }
            if data.first_name:
                update_data["first_name"] = _normalize_text(data.first_name, 80)
            if data.last_name:
                update_data["last_name"] = _normalize_text(data.last_name, 80)
            if data.business_category:
                update_data["business_category"] = _normalize_text(data.business_category, 100)
            if data.instagram_username:
                update_data["instagram_username"] = _normalize_text(data.instagram_username, 100)
            display_name = _build_display_name(
                update_data.get("first_name", existing.get("first_name")),
                update_data.get("last_name", existing.get("last_name")),
            )
            if display_name:
                update_data["display_name"] = display_name

            await db.users.update_one({"_id": existing["_id"]}, {"$set": update_data})

            otp_sent = await send_otp_email(email, otp)
            if not otp_sent:
                raise HTTPException(status_code=503, detail="Failed to send OTP email. Try again in a moment.")

            return {
                "message": "Verification code resent to your email.",
                "email": email,
                "otp_expires_in_seconds": 300,
            }

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
        "plan": PlanType.Free.value,
        "dm_limit": PLAN_LIMITS[PlanType.Free]["dm_limit"],
        "dm_count_this_month": 0,
        "is_active": True,
        "email_verified": False,
        "unverified_expires_at": _utcnow() + timedelta(hours=24),
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

    user = await db.users.find_one({"email": email})
    if not user:
        pwd_ctx.verify("dummy_password", _DUMMY_HASH)
        raise HTTPException(status_code=404, detail="Invalid request")

    if user.get("email_verified"):
        return {"message": "Already verified"}



    if len(otp) != 6 or not otp.isdigit():
        raise HTTPException(status_code=400, detail="OTP must be a 6-digit code")

    if int(user.get("otp_attempts", 0)) >= 3:
        raise HTTPException(status_code=429, detail="Too many invalid attempts. Request a new code.")

    otp_expires_at = _as_aware_utc(user.get("otp_expires_at"))
    if not otp_expires_at or _utcnow() > otp_expires_at:
        raise HTTPException(status_code=400, detail="Code expired. Request a new one.")

    expected_hash = user.get("otp_hash")
    if not expected_hash or not hmac.compare_digest(hash_otp(otp), str(expected_hash)):
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
            },
            "$unset": {
                "unverified_expires_at": "",
            },
        },
    )

    token = create_jwt(str(user["_id"]), _session_version(user))
    return _auth_response(
        {
            "message": "Email verified",
            "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
            "instagram_connected": bool(user.get("instagram_user_id")),
        },
        token,
    )


@router.post("/resend-otp")
@limiter.limit("20/minute")
async def resend_otp(request: Request, data: OTPResendRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    user = await db.users.find_one({"email": email})

    if not user:
        pwd_ctx.verify("dummy_password", _DUMMY_HASH)
        raise HTTPException(status_code=404, detail="Invalid request")

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
        pwd_ctx.verify("dummy_password", _DUMMY_HASH)
        return response

    reset_token = _create_password_reset_token(email)
    token_hash = hashlib.sha256(reset_token.encode("utf-8")).hexdigest()
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_reset_token_hash": token_hash,
                "password_reset_token_used": False,
            }
        },
    )

    reset_url = f"{_password_reset_frontend_base()}/forgot-password?email={quote(email)}&token={quote(reset_token)}"

    if str(getattr(settings, "ENVIRONMENT", "")).strip().lower() == "development":
        print(f"[DEVELOPMENT ONLY] Password reset for {email}: reset_token={reset_token} reset_url={reset_url}")
        logger.info(f"[DEVELOPMENT ONLY] Password reset for {email}: reset_token={reset_token} reset_url={reset_url}")

    email_sent = await send_password_reset_email(email, reset_url)
    if not email_sent:
        raise HTTPException(status_code=503, detail="Password reset email is temporarily unavailable")

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
        raise HTTPException(status_code=400, detail="Invalid reset token")

    user = await db.users.find_one({"email": email})
    if not user:
        pwd_ctx.verify("dummy_password", _DUMMY_HASH)
        raise HTTPException(status_code=404, detail="Invalid request")

    # Single-use check: verify hash matches and used is False
    stored_hash = user.get("password_reset_token_hash")
    token_used = user.get("password_reset_token_used", False)
    incoming_hash = hashlib.sha256(data.reset_token.encode("utf-8")).hexdigest()

    if token_used or not stored_hash or not hmac.compare_digest(stored_hash, incoming_hash):
        raise HTTPException(status_code=400, detail="Password reset link is invalid or has already been used.")

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "hashed_password": hash_password(password),
                "failed_login_attempts": 0,
                "login_lockout_until": None,
                "session_version": _session_version(user) + 1,
                "password_reset_token_used": True,
            },
            "$unset": {
                "password_reset_token_hash": "",
            },
        },
    )

    return {"message": "Password updated successfully. You can now sign in."}



# ── Login / Session ───────────────────────────────────────────────────────────

@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, data: UserLoginRequest, db=Depends(get_db)):
    email = str(data.email).strip().lower()
    client_ip = _client_ip(request)

    user = await db.users.find_one({"email": email})

    if user and not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account has been deactivated.")

    if user and str(user.get("oauth_provider") or "").lower() == "google":
        raise HTTPException(
            status_code=400,
            detail="This account was registered using Google Sign-In. Please sign in with Google.",
        )

    # Key lockout on (email + IP)
    if await _is_login_locked_for_email_ip(db, email, client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")

    if not user or not verify_password(data.password, user["hashed_password"]):
        if not user:
            pwd_ctx.verify(data.password or "dummy", _DUMMY_HASH)
        await _record_failed_login_attempt(db, email, client_ip, user=user)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not user.get("email_verified", False):
        raise HTTPException(status_code=403, detail="Email not verified. Check your inbox for OTP.")

    await _clear_login_lockout(db, email, client_ip, user=user)

    token = create_jwt(str(user["_id"]), _session_version(user))
    response_data = {
        "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
        "instagram_connected": bool(user.get("instagram_user_id")),
    }
    return _auth_response(response_data, token)



@router.get("/me")
@limiter.limit("60/minute")
async def me(request: Request, user=Depends(get_current_user), db: Any = Depends(get_db)):
    first_name = (user.get("first_name") or "").strip()
    last_name = (user.get("last_name") or "").strip()
    full_name = " ".join(part for part in [first_name, last_name] if part)
    instagram_username = str(user.get("instagram_username") or "").strip()

    connection_status = user.get("ig_connection_status")
    if not connection_status and user.get("instagram_user_id"):
        expires_at = user.get("ig_token_expires_at")
        if expires_at:
            if isinstance(expires_at, str):
                try:
                    expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                except Exception:
                    expires_at = None
            if isinstance(expires_at, datetime):
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    connection_status = "expired"
        if not connection_status:
            connection_status = "active"

    return {
        "id": str(user["_id"]),
        "email": user.get("email"),
        "first_name": first_name,
        "last_name": last_name,
        "business_category": user.get("business_category", ""),
        "display_name": user.get("display_name") or full_name,
        "onboarding_complete": bool(user.get("onboarding_complete", False)),
        "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
        "instagram_connected": bool(user.get("instagram_user_id")),
        "instagram_user_id": user.get("instagram_user_id", ""),
        "instagram_username": instagram_username,
        "email_verified": bool(user.get("email_verified", False)),
        "ig_connection_status": connection_status,
        "ig_connected_at": user.get("ig_connected_at"),
        "ig_last_refreshed_at": user.get("ig_last_refreshed_at"),
        "ig_profile_picture_url": user.get("ig_profile_picture_url"),
        "ig_account_type": user.get("ig_account_type"),
        "ig_followers_count": user.get("ig_followers_count"),
        "webhook_subscribed": user.get("webhook_subscribed", False),
    }


@router.get("/csrf")
@limiter.limit("60/minute")
async def csrf_token(request: Request, user=Depends(get_current_user)):
    """Return the CSRF token for the X-CSRF-Token header (cookie is not readable cross-host)."""
    token = get_cookie(request, "pg_csrf")
    if token:
        return {"csrf_token": token}
    token = secrets.token_urlsafe(32)
    response = Response(json.dumps({"csrf_token": token}), media_type="application/json")
    set_session_cookie(response, "pg_csrf", token, max_age=604800, httponly=False)
    return response


@router.post("/logout")
async def logout(request: Request, db=Depends(get_db)):
    token = get_cookie(request, "pg_token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()
    if token:
        try:
            payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM], options={"verify_exp": False})
            user_id = payload.get("sub")
            # Only session tokens may revoke sessions; OAuth state, reset or other
            # signed tokens just get the cookies cleared.
            if payload.get("typ") != "session":
                logger.debug("Logout with non-session token type; session not revoked")
            elif user_id:
                await db.users.update_one(
                    {"_id": ObjectId(user_id)},
                    {"$inc": {"session_version": 1}},
                )
        except Exception as exc:
            # Logout always succeeds for the client; note why revocation was skipped.
            logger.debug("Logout session revocation skipped: %s", type(exc).__name__)

    response = Response(json.dumps({"message": "Logged out"}), media_type="application/json")
    _clear_auth_cookie(response)
    return response



# ── Instagram OAuth ───────────────────────────────────────────────────────────

@router.get("/instagram/initiate")
async def instagram_initiate(response: Response, user=Depends(get_current_user)):
    nonce = secrets.token_urlsafe(32)
    state = create_oauth_state(str(user["_id"]), nonce)
    # Host-only, httpOnly; SameSite=Lax is sent on the top-level redirect back from Instagram.
    response.set_cookie(
        key=OAUTH_NONCE_COOKIE,
        value=nonce,
        httponly=True,
        secure=settings.ENVIRONMENT.lower() == "production",
        samesite="lax",
        max_age=OAUTH_STATE_TTL_SECONDS,
        path=OAUTH_NONCE_PATH,
    )
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

        user_id = verify_oauth_state(state, request.cookies.get(OAUTH_NONCE_COOKIE))

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

        # 1. Verify granted scopes
        granted_scopes = await InstagramService.get_granted_permissions(access_token)
        logger.info("Instagram OAuth granted scopes for user %s: %s", user["_id"], granted_scopes)
        if granted_scopes:
            required_scopes = {"instagram_business_basic", "instagram_business_manage_messages"}
            missing_scopes = required_scopes - set(granted_scopes)
            if missing_scopes:
                missing_str = ", ".join(sorted(missing_scopes))
                raise HTTPException(
                    status_code=400,
                    detail=f"Missing required Instagram permissions: {missing_str}. Please reconnect and grant all permissions.",
                )

        # 2. Call POST graph.instagram.com/{ig_id}/subscribed_apps?subscribed_fields=messages,comments,messaging_postbacks (mentions if supported) and store webhook_subscribed
        webhook_subscribed = await InstagramService.subscribe_app_to_webhooks(access_token, ig_user_id)
        logger.info("Instagram webhook subscription for %s: %s", ig_user_id, webhook_subscribed)

        expires_in = token_data.get("expires_in", 5183944)
        expires_at = _utcnow() + timedelta(seconds=expires_in)
        now = _utcnow()
        encrypted_access_token = InstagramService.encrypt_access_token(access_token)

        profile_picture_url = profile.get("profile_picture_url")
        account_type = profile.get("account_type")
        followers_count = profile.get("followers_count")

        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "instagram_user_id": ig_user_id,
                    "instagram_account_ids": account_ids,
                    "meta_app_scoped_id": str(token_data.get("user_id") or "").strip() or None,
                    "instagram_username": ig_username,
                    "instagram_access_token": encrypted_access_token,
                    "ig_token_expires_at": expires_at,
                    "ig_connection_status": "active",
                    "ig_connected_at": now,
                    "ig_last_refreshed_at": now,
                    "ig_profile_picture_url": profile_picture_url,
                    "ig_account_type": account_type,
                    "ig_followers_count": followers_count,
                    "webhook_subscribed": webhook_subscribed,
                    "granted_scopes": granted_scopes,
                }
            },
        )
    except HTTPException as exc:
        redirect = _oauth_error_redirect(str(exc.detail))
        _clear_oauth_nonce(redirect)
        return redirect
    except Exception:
        logger.exception("Unexpected Instagram callback failure")
        redirect = _oauth_error_redirect("Instagram connection failed. Please try again.")
        _clear_oauth_nonce(redirect)
        return redirect

    redirect = _oauth_success_redirect()
    _clear_oauth_nonce(redirect)
    return redirect


@router.post("/instagram/token")
async def save_instagram_token(
    data: InstagramTokenRequest,
    db=Depends(get_db),
    x_admin_key: str = Header(None),
):
    if not settings.admin_api_key or not hmac.compare_digest(x_admin_key or "", settings.admin_api_key):
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
    now = _utcnow()
    webhook_subscribed = await InstagramService.subscribe_app_to_webhooks(access_token, ig_user_id)
    profile_picture_url = profile.get("profile_picture_url") if isinstance(profile, dict) else None
    account_type = profile.get("account_type") if isinstance(profile, dict) else None
    followers_count = profile.get("followers_count") if isinstance(profile, dict) else None

    result = await db.users.update_one(
        update_filter,
        {
            "$set": {
                "instagram_access_token": encrypted_access_token,
                "instagram_user_id": ig_user_id,
                "instagram_account_ids": [ig_user_id],
                "instagram_username": ig_username or None,
                "ig_connection_status": "active",
                "ig_connected_at": now,
                "ig_last_refreshed_at": now,
                "ig_profile_picture_url": profile_picture_url,
                "ig_account_type": account_type,
                "ig_followers_count": followers_count,
                "webhook_subscribed": webhook_subscribed,
            }
        },
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Update failed: User not found after verification")

    return {"status": "Instagram token saved", "instagram_user_id": ig_user_id, "instagram_username": ig_username}


@router.get("/instagram/media")
@limiter.limit("20/minute")
async def instagram_media(
    request: Request,
    media_type: str = Query("all"),
    limit: int = Query(25, ge=1, le=50),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    access_token = str(user.get("instagram_access_token") or "").strip()
    instagram_user_id = str(user.get("instagram_user_id") or "").strip()

    if not access_token or not instagram_user_id:
        return {"media": [], "source": "unavailable", "connected": False}

    expires_at = user.get("ig_token_expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            try:
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            except Exception:
                expires_at = None
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                logger.warning("Instagram token expired at %s for user %s", expires_at, user.get("_id"))
                return {"media": [], "source": "token_expired", "connected": True}

    try:
        media = await InstagramService.get_user_media(access_token, limit=limit, media_type=media_type)
    except (InstagramTokenExpiredError, InvalidToken, ValueError) as exc:
        error_code = getattr(exc, "error_code", 190)
        error_message = getattr(exc, "error_message", str(exc))
        logger.warning(
            "Instagram token invalid/expired (code %s) for user %s: %s. Flagging token as needing reauth.",
            error_code,
            user.get("_id"),
            error_message,
        )
        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "ig_connection_status": "needs_reauth",
                }
            },
        )
        return {"media": [], "source": "token_expired", "connected": True}

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
                "hashed_password": hash_password(secrets.token_urlsafe(32)),
                "plan": PlanType.Free.value,
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
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"email_verified": True}, "$unset": {"unverified_expires_at": ""}},
            )
            user["email_verified"] = True

        token = create_jwt(str(user["_id"]), _session_version(user))
        return _auth_response(
            {
                "plan": get_plan_type(user.get("plan", PlanType.Free)).name,
                "instagram_connected": bool(user.get("instagram_user_id")),
            },
            token,
        )

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
    if data.first_name is not None or data.last_name is not None:
        effective_first = update.get("first_name", user.get("first_name", ""))
        effective_last = update.get("last_name", user.get("last_name", ""))
        update["display_name"] = _build_display_name(effective_first, effective_last)
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
                "ig_connection_status": None,
                "ig_connected_at": None,
                "ig_last_refreshed_at": None,
                "ig_profile_picture_url": None,
                "ig_account_type": None,
                "ig_followers_count": None,
                "webhook_subscribed": None,
                "granted_scopes": [],
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
        error_obj = result.get("error") if isinstance(result.get("error"), dict) else {}
        error_code = error_obj.get("code")
        if error_code in (190, 102) or "token" in str(result).lower():
            await db.users.update_one(
                {"_id": user["_id"]},
                {"$set": {"ig_connection_status": "needs_reauth"}},
            )
        raise HTTPException(
            status_code=400,
            detail="Token refresh failed. Please reconnect your Instagram account.",
        )

    expires_in = int(result.get("expires_in") or 5183944)
    now = _utcnow()
    expires_at = now + timedelta(seconds=expires_in)
    encrypted_new_token = InstagramService.encrypt_access_token(new_token)

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "instagram_access_token": encrypted_new_token,
                "ig_token_expires_at": expires_at,
                "ig_connection_status": "active",
                "ig_last_refreshed_at": now,
            }
        },
    )

    return {
        "refreshed": True,
        "expires_at": expires_at.isoformat(),
        "message": "Instagram token refreshed successfully.",
    }


# ── Data Deletion & Meta Callbacks ───────────────────────────────────────────

def parse_meta_signed_request(signed_request: str) -> dict[str, Any]:
    """Parse and verify a Meta/Facebook signed_request parameter using META_APP_SECRET."""
    if not signed_request or "." not in signed_request:
        raise HTTPException(status_code=400, detail="Invalid signed_request format")

    parts = signed_request.split(".", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Malformed signed_request")

    encoded_sig, payload = parts

    sig_padding = "=" * ((4 - len(encoded_sig) % 4) % 4)
    try:
        sig = base64.urlsafe_b64decode(encoded_sig + sig_padding)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature encoding in signed_request")

    payload_padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(payload + payload_padding)
        data = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payload JSON in signed_request")

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Payload must be a JSON object")

    algo = str(data.get("algorithm") or "")
    if algo != "HMAC-SHA256":
        raise HTTPException(status_code=400, detail="Unsupported signature algorithm in signed_request")

    expected_sig = hmac.new(
        settings.META_APP_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(sig, expected_sig):
        raise HTTPException(status_code=400, detail="Invalid signed_request signature")

    return data


async def _extract_signed_request(request: Request) -> str | None:
    # 1. Query parameter
    sr = request.query_params.get("signed_request")
    if sr:
        return sr

    # 2. Form data
    content_type = request.headers.get("content-type", "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
            if "signed_request" in form:
                return str(form["signed_request"])
        except Exception:
            pass

    # 3. JSON body
    if "application/json" in content_type:
        try:
            json_body = await request.json()
            if isinstance(json_body, dict) and "signed_request" in json_body:
                return str(json_body["signed_request"])
        except Exception:
            pass

    # 4. Fallback raw body inspection
    try:
        raw_body = await request.body()
        raw_str = raw_body.decode("utf-8", errors="replace")
        if "signed_request=" in raw_str:
            from urllib.parse import parse_qs
            parsed = parse_qs(raw_str)
            if "signed_request" in parsed and parsed["signed_request"]:
                return parsed["signed_request"][0]
        try:
            parsed_json = json.loads(raw_str)
            if isinstance(parsed_json, dict) and "signed_request" in parsed_json:
                return str(parsed_json["signed_request"])
        except Exception:
            pass
    except Exception:
        pass

    return None


@router.post("/data-deletion")
async def request_data_deletion(
    response: Response,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id_str = str(user["_id"])
    now = _utcnow()

    # 1. Automation rules
    await db.automation_rules.delete_many({"user_id": user_id_str})

    # 2. DM logs
    await db.dm_logs.delete_many({"user_id": user_id_str})

    # 3. Contacts
    await db.contacts.delete_many({"user_id": user_id_str})

    # 4. Webhook events (by user_id or by user's Instagram account IDs)
    ig_ids = [str(x) for x in (user.get("instagram_account_ids") or []) if x]
    if user.get("instagram_user_id"):
        ig_ids.append(str(user.get("instagram_user_id")))
    ig_ids = list(set(ig_ids))

    webhook_queries: list[dict[str, Any]] = [{"user_id": user_id_str}]
    for ig_id in ig_ids:
        escaped_id = re.escape(ig_id)
        webhook_queries.append({"_id": {"$regex": f"^(?:msg|chg):{escaped_id}:"}})
    await db.webhook_events.delete_many({"$or": webhook_queries})

    # 5. Deactivate user, clear tokens and personal details
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "is_active": False,
                "deleted_at": now,
                "instagram_user_id": None,
                "instagram_account_ids": [],
                "instagram_access_token": None,
                "ig_token_expires_at": None,
                "ig_connection_status": None,
                "ig_connected_at": None,
                "ig_last_refreshed_at": None,
                "ig_profile_picture_url": None,
                "ig_account_type": None,
                "ig_followers_count": None,
                "webhook_subscribed": None,
                "granted_scopes": [],
                "session_version": _session_version(user) + 1,
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


@router.post("/data-deletion-callback")
@router.post("/meta/data-deletion")
async def meta_data_deletion_callback(request: Request, db=Depends(get_db)):
    signed_req = await _extract_signed_request(request)
    if not signed_req:
        raise HTTPException(status_code=400, detail="Missing signed_request parameter")

    data = parse_meta_signed_request(signed_req)
    meta_user_id = str(data.get("user_id") or "").strip()
    now = _utcnow()
    confirmation_code = secrets.token_hex(16)

    matched_user = None
    if meta_user_id:
        matched_user = await db.users.find_one(
            {
                "$or": [
                    {"instagram_user_id": meta_user_id},
                    {"instagram_account_ids": meta_user_id},
                    {"meta_app_scoped_id": meta_user_id},
                    {"meta_user_id": meta_user_id},
                    {"facebook_user_id": meta_user_id},
                ]
            }
        )

    if not matched_user:
        logger.warning(
            "Meta data-deletion callback: no user matched signed_request user_id=%s (confirmation_code=%s)",
            meta_user_id or "<missing>",
            confirmation_code,
        )

    if matched_user:
        user_id_str = str(matched_user["_id"])
        await db.automation_rules.delete_many({"user_id": user_id_str})
        await db.dm_logs.delete_many({"user_id": user_id_str})
        await db.contacts.delete_many({"user_id": user_id_str})

        ig_ids = [str(x) for x in (matched_user.get("instagram_account_ids") or []) if x]
        if matched_user.get("instagram_user_id"):
            ig_ids.append(str(matched_user.get("instagram_user_id")))
        ig_ids = list(set(ig_ids))

        webhook_queries: list[dict[str, Any]] = [{"user_id": user_id_str}]
        for ig_id in ig_ids:
            escaped_id = re.escape(ig_id)
            webhook_queries.append({"_id": {"$regex": f"^(?:msg|chg):{escaped_id}:"}})
        await db.webhook_events.delete_many({"$or": webhook_queries})

        await db.users.update_one(
            {"_id": matched_user["_id"]},
            {
                "$set": {
                    "is_active": False,
                    "deleted_at": now,
                    "data_deletion_confirmation_code": confirmation_code,
                    "instagram_user_id": None,
                    "instagram_account_ids": [],
                    "instagram_access_token": None,
                    "ig_token_expires_at": None,
                    "ig_connection_status": None,
                    "ig_connected_at": None,
                    "ig_last_refreshed_at": None,
                    "ig_profile_picture_url": None,
                    "ig_account_type": None,
                    "ig_followers_count": None,
                    "webhook_subscribed": None,
                    "granted_scopes": [],
                    "session_version": _session_version(matched_user) + 1,
                },
                "$unset": {
                    "first_name": "",
                    "last_name": "",
                    "business_category": "",
                },
            },
        )

    await db.data_deletion_requests.insert_one(
        {
            "confirmation_code": confirmation_code,
            "meta_user_id": meta_user_id,
            "user_id": str(matched_user["_id"]) if matched_user else None,
            "status": "completed",
            "requested_at": now,
            "completed_at": now,
        }
    )

    base_url = str(request.base_url).rstrip("/")
    status_url = f"{base_url}/auth/data-deletion-status?code={confirmation_code}"
    return {
        "url": status_url,
        "confirmation_code": confirmation_code,
    }


@router.get("/data-deletion-status")
@router.get("/meta/data-deletion-status")
async def meta_data_deletion_status(code: str = Query(...), db=Depends(get_db)):
    req = await db.data_deletion_requests.find_one({"confirmation_code": code})
    if not req:
        raise HTTPException(status_code=404, detail="Data deletion request not found")

    return {
        "confirmation_code": code,
        "status": req.get("status", "completed"),
        "requested_at": req.get("requested_at").isoformat() if isinstance(req.get("requested_at"), datetime) else req.get("requested_at"),
        "completed_at": req.get("completed_at").isoformat() if isinstance(req.get("completed_at"), datetime) else req.get("completed_at"),
        "message": "Your data has been successfully deleted from PinGuru.",
    }


@router.post("/deauthorize")
@router.post("/meta/deauthorize")
async def meta_deauthorize_callback(request: Request, db=Depends(get_db)):
    signed_req = await _extract_signed_request(request)
    if not signed_req:
        raise HTTPException(status_code=400, detail="Missing signed_request parameter")

    data = parse_meta_signed_request(signed_req)
    meta_user_id = str(data.get("user_id") or "").strip()

    if meta_user_id:
        user = await db.users.find_one(
            {
                "$or": [
                    {"instagram_user_id": meta_user_id},
                    {"instagram_account_ids": meta_user_id},
                    {"meta_app_scoped_id": meta_user_id},
                    {"meta_user_id": meta_user_id},
                    {"facebook_user_id": meta_user_id},
                ]
            }
        )
        if user:
            await db.users.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {
                        "instagram_access_token": None,
                        "ig_token_expires_at": None,
                        "ig_connection_status": "expired",
                        "webhook_subscribed": False,
                    }
                },
            )
            logger.info("Meta deauthorized for user %s (meta_user_id=%s)", user["_id"], meta_user_id)
        else:
            logger.warning("Meta deauthorize callback: no user matched signed_request user_id=%s", meta_user_id)
    else:
        logger.warning("Meta deauthorize callback: signed_request has no user_id")

    return {"success": True}
