from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from pydantic import BaseModel, EmailStr
from bson import ObjectId
from bson.errors import InvalidId
import jwt

from app.config import settings
from app.database import get_db
from app.services.instagram import InstagramService

router = APIRouter()
admin_bearer = HTTPBearer(auto_error=False)
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
logger = logging.getLogger(__name__)


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


class AdminForgotPasswordRequest(BaseModel):
    email: EmailStr


class AdminResetPasswordRequest(BaseModel):
    email: EmailStr
    reset_token: str
    new_password: str


class AdminActionPayload(BaseModel):
    reason: str | None = None
    note: str | None = None


def _create_admin_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": email, "exp": expire, "type": "admin"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _create_admin_password_reset_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=20)
    payload = {"sub": email, "exp": expire, "type": "admin_password_reset"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _admin_display_name(email: str) -> str:
    local_part = email.split("@", 1)[0] if "@" in email else email
    return local_part.replace(".", " ").replace("_", " ").title() or "Admin"


async def _get_effective_admin_password_hash(db) -> str:
    override = await db.admin_config.find_one({"_id": "admin_credentials"}, {"password_hash": 1})
    override_hash = str((override or {}).get("password_hash", "")).strip()
    if override_hash:
        return override_hash
    return settings.ADMIN_PASSWORD_HASH.strip()


async def _write_admin_audit(db, admin_email: str, action: str, target: str):
    await db.admin_audit.insert_one(
        {
            "adminName": _admin_display_name(admin_email),
            "action": action,
            "target": target,
            "createdAt": datetime.now(timezone.utc),
        }
    )


def _to_iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _to_status(user: dict[str, Any]) -> str:
    if user.get("deleted_at"):
        return "deleted"
    if user.get("is_active") is False or user.get("admin_status") == "suspended":
        return "suspended"
    if user.get("is_flagged") is True:
        return "flagged"
    return "active"


def _to_admin_user(user: dict[str, Any]) -> dict[str, Any]:
    email = str(user.get("email") or "")
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    display_name = (f"{first} {last}".strip() or email.split("@", 1)[0] or "User").strip()

    ig_username = str(user.get("instagram_username") or "").strip()
    if ig_username and not ig_username.startswith("@"):
        ig_username = f"@{ig_username}"

    return {
        "id": str(user.get("_id")),
        "name": display_name,
        "email": email,
        "plan": str(user.get("plan") or "free"),
        "status": _to_status(user),
        "instagramHandle": ig_username or None,
        "createdAt": _to_iso(user.get("created_at")) or datetime.now(timezone.utc).isoformat(),
        "lastSeenAt": _to_iso(user.get("last_seen_at")),
        "dmCount30d": int(user.get("dm_count_this_month") or 0),
        "spamScore": int(user.get("spam_score") or 0),
    }


def _user_selector(user_id: str) -> dict[str, Any]:
    try:
        return {"_id": ObjectId(user_id)}
    except (InvalidId, TypeError):
        return {"_id": user_id}


def _set_admin_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="pg_admin_token",
        value=token,
        httponly=True,
        secure=settings.ENVIRONMENT.lower() == "production",
        samesite="lax",
        max_age=3600,
        path="/",
    )


def _clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(key="pg_admin_token", path="/")


async def get_admin_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(admin_bearer),
):
    # Cookie-first (httpOnly) — XSS safe
    token = request.cookies.get("pg_admin_token")

    # Bearer fallback for backward compatibility
    if not token and credentials and credentials.credentials:
        token = credentials.credentials

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    if payload.get("type") != "admin":
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    email = payload.get("sub")
    admin_email = settings.ADMIN_EMAIL.strip().lower()
    if not email or email.strip().lower() != admin_email:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    return {"email": email}


@router.post("/login")
async def admin_login(data: AdminLoginRequest, response: Response, db=Depends(get_db)):
    email = data.email.strip().lower()
    password = data.password
    admin_email = settings.ADMIN_EMAIL.strip().lower()
    admin_password_hash = await _get_effective_admin_password_hash(db)

    if not admin_email or not admin_password_hash:
        raise HTTPException(status_code=503, detail="Admin credentials are not configured")

    if email != admin_email:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    try:
        password_valid = pwd_ctx.verify(password, admin_password_hash)
    except UnknownHashError:
        logger.error("ADMIN_PASSWORD_HASH is not a recognized hash format")
        raise HTTPException(status_code=503, detail="Admin credentials are misconfigured")

    if not password_valid:
        raise HTTPException(status_code=401, detail="Invalid admin credentials")

    token = _create_admin_token(email)
    # Set httpOnly cookie — XSS safe, no localStorage exposure
    _set_admin_cookie(response, token)
    # Also return token for backward compat (admin frontend will ignore once updated)
    return {"ok": True, "token": token}


@router.post("/auth/login")
async def admin_login_alias(data: AdminLoginRequest, response: Response, db=Depends(get_db)):
    return await admin_login(data, response, db)


@router.post("/auth/logout")
async def admin_logout_alias(response: Response, _admin=Depends(get_admin_user)):
    _clear_admin_cookie(response)
    return {"ok": True}


@router.get("/me")
async def admin_me(admin=Depends(get_admin_user)):
    email = str(admin.get("email") or settings.ADMIN_EMAIL).strip().lower()
    return {
        "id": "admin-1",
        "name": _admin_display_name(email),
        "email": email,
        "role": "owner",
    }


@router.post("/auth/forgot-password/request")
async def admin_forgot_password_request(data: AdminForgotPasswordRequest):
    admin_email = settings.ADMIN_EMAIL.strip().lower()
    if not admin_email:
        raise HTTPException(status_code=503, detail="Admin credentials are not configured")

    # Keep response generic to avoid exposing account existence.
    response = {"ok": True, "message": "If the account exists, a reset token has been generated."}
    if data.email.strip().lower() == admin_email and settings.ENVIRONMENT.lower() != "production":
        response["reset_token"] = _create_admin_password_reset_token(admin_email)
    return response


@router.post("/auth/forgot-password/reset")
async def admin_forgot_password_reset(data: AdminResetPasswordRequest, db=Depends(get_db)):
    admin_email = settings.ADMIN_EMAIL.strip().lower()
    if not admin_email:
        raise HTTPException(status_code=503, detail="Admin credentials are not configured")

    new_password = data.new_password or ""
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    try:
        payload = jwt.decode(data.reset_token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Reset token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    token_email = str(payload.get("sub") or "").strip().lower()
    token_type = str(payload.get("type") or "")
    request_email = data.email.strip().lower()

    if token_type != "admin_password_reset" or token_email != admin_email or request_email != admin_email:
        raise HTTPException(status_code=401, detail="Invalid reset token")

    new_password_hash = pwd_ctx.hash(new_password)
    await db.admin_config.update_one(
        {"_id": "admin_credentials"},
        {"$set": {"password_hash": new_password_hash, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    await _write_admin_audit(db, admin_email, "Reset admin password", "admin account")
    return {"ok": True, "message": "Admin password updated"}


@router.get("/users")
async def admin_users(admin=Depends(get_admin_user), db=Depends(get_db)):
    raw_users = await db.users.find({}, {
        "email": 1,
        "first_name": 1,
        "last_name": 1,
        "plan": 1,
        "is_active": 1,
        "is_flagged": 1,
        "admin_status": 1,
        "instagram_username": 1,
        "created_at": 1,
        "last_seen_at": 1,
        "dm_count_this_month": 1,
        "spam_score": 1,
        "deleted_at": 1,
    }).to_list(100000)
    return [_to_admin_user(user) for user in raw_users]


@router.get("/stats")
async def admin_stats(admin=Depends(get_admin_user), db=Depends(get_db)):
    total_users = await db.users.count_documents({})
    total_dms_sent = await db.dm_logs.count_documents({"status": "sent"})
    active_users = await db.users.count_documents({"is_active": {"$ne": False}, "deleted_at": {"$exists": False}})
    flagged_users = await db.users.count_documents({"$or": [{"is_flagged": True}, {"spam_score": {"$gte": 70}}]})

    pipeline = [
        {"$group": {"_id": "$plan", "count": {"$sum": 1}}}
    ]
    grouped = await db.users.aggregate(pipeline).to_list(100)
    plan_distribution = {str(item.get("_id", "unknown")): item.get("count", 0) for item in grouped}

    summary = {
        "totalUsers": total_users,
        "activeUsers": active_users,
        "flaggedUsers": flagged_users,
        "spamEvents24h": 0,
        "openActions": flagged_users,
    }

    return {
        **summary,
        "total_users": total_users,
        "total_dms_sent": total_dms_sent,
        "plan_distribution": plan_distribution,
    }


@router.get("/dashboard/summary")
async def admin_dashboard_summary(admin=Depends(get_admin_user), db=Depends(get_db)):
    stats = await admin_stats(admin, db)
    return {
        "totalUsers": stats.get("totalUsers", 0),
        "activeUsers": stats.get("activeUsers", 0),
        "flaggedUsers": stats.get("flaggedUsers", 0),
        "spamEvents24h": stats.get("spamEvents24h", 0),
        "openActions": stats.get("openActions", 0),
    }


@router.get("/billing/summary")
async def admin_billing_summary(admin=Depends(get_admin_user), db=Depends(get_db)):
    grouped = await db.users.aggregate([
        {"$group": {"_id": "$plan", "count": {"$sum": 1}}}
    ]).to_list(10)
    distribution = {str(item.get("_id", "free")): int(item.get("count", 0)) for item in grouped}

    free_count = distribution.get("free", 0)
    starter_count = distribution.get("starter", 0)
    pro_count = distribution.get("pro", 0)
    mrr_inr = starter_count * 199 + pro_count * 499

    return {
        "mrrInr": mrr_inr,
        "failedPayments24h": 0,
        "churn30d": 0,
        "atRiskAccounts": 0,
        "planDistribution": {
            "free": free_count,
            "starter": starter_count,
            "pro": pro_count,
        },
    }


@router.get("/activity")
async def admin_activity(admin=Depends(get_admin_user), db=Depends(get_db)):
    logs = await db.dm_logs.find({}, {
        "user_id": 1,
        "status": 1,
        "message_sent": 1,
        "sent_at": 1,
    }).sort("sent_at", -1).limit(100).to_list(100)

    events = []
    for item in logs:
        status = str(item.get("status") or "sent")
        event_type = "dm_sent" if status == "sent" else "spam_flag"
        title = "DM sent" if event_type == "dm_sent" else "DM blocked"
        detail = str(item.get("message_sent") or "No message preview")
        created_at = _to_iso(item.get("sent_at")) or datetime.now(timezone.utc).isoformat()

        events.append(
            {
                "id": str(item.get("_id")),
                "userId": str(item.get("user_id") or ""),
                "type": event_type,
                "title": title,
                "detail": detail,
                "createdAt": created_at,
            }
        )

    return events


@router.get("/audit")
async def admin_audit(admin=Depends(get_admin_user), db=Depends(get_db)):
    logs = await db.admin_audit.find({}, {
        "adminName": 1,
        "action": 1,
        "target": 1,
        "createdAt": 1,
    }).sort("createdAt", -1).limit(200).to_list(200)

    return [
        {
            "id": str(item.get("_id")),
            "adminName": str(item.get("adminName") or "Admin"),
            "action": str(item.get("action") or ""),
            "target": str(item.get("target") or ""),
            "createdAt": _to_iso(item.get("createdAt")) or datetime.now(timezone.utc).isoformat(),
        }
        for item in logs
    ]


@router.get("/users/{user_id}/timeline")
async def admin_user_timeline(user_id: str, admin=Depends(get_admin_user), db=Depends(get_db)):
    selector = _user_selector(user_id)
    log_filter: dict[str, Any] = {"$or": [{"user_id": user_id}]}
    selector_id = selector.get("_id")
    if isinstance(selector_id, ObjectId):
        log_filter["$or"].append({"user_id": selector_id})

    logs = await db.dm_logs.find(log_filter, {
        "status": 1,
        "message_sent": 1,
        "sent_at": 1,
    }).sort("sent_at", -1).limit(100).to_list(100)

    return [
        {
            "id": str(item.get("_id")),
            "userId": user_id,
            "type": "dm_sent" if str(item.get("status") or "") == "sent" else "spam_flag",
            "title": "DM event",
            "detail": str(item.get("message_sent") or "No message preview"),
            "createdAt": _to_iso(item.get("sent_at")) or datetime.now(timezone.utc).isoformat(),
        }
        for item in logs
    ]


@router.post("/users/{user_id}/suspend")
async def admin_suspend_user(user_id: str, payload: AdminActionPayload, admin=Depends(get_admin_user), db=Depends(get_db)):
    reason = (payload.reason or "Suspended by admin").strip()
    selector = _user_selector(user_id)
    await db.users.update_one(
        selector,
        {
            "$set": {
                "is_active": False,
                "admin_status": "suspended",
                "suspended_reason": reason,
                "suspended_at": datetime.now(timezone.utc),
            }
        },
    )
    await _write_admin_audit(db, admin["email"], "Suspended account", user_id)
    return {"ok": True}


@router.post("/users/{user_id}/unsuspend")
async def admin_unsuspend_user(user_id: str, admin=Depends(get_admin_user), db=Depends(get_db)):
    selector = _user_selector(user_id)
    await db.users.update_one(
        selector,
        {
            "$set": {"is_active": True, "admin_status": "active"},
            "$unset": {"suspended_reason": "", "suspended_at": ""},
        },
    )
    await _write_admin_audit(db, admin["email"], "Unsuspended account", user_id)
    return {"ok": True}


@router.post("/users/{user_id}/flag")
async def admin_flag_user(user_id: str, payload: AdminActionPayload, admin=Depends(get_admin_user), db=Depends(get_db)):
    note = (payload.note or "Flagged by admin").strip()
    selector = _user_selector(user_id)
    await db.users.update_one(
        selector,
        {
            "$set": {
                "is_flagged": True,
                "flag_note": note,
                "flagged_at": datetime.now(timezone.utc),
            }
        },
    )
    await _write_admin_audit(db, admin["email"], "Flagged account", user_id)
    return {"ok": True}


@router.post("/users/{user_id}/instagram/reset")
async def admin_reset_instagram(user_id: str, admin=Depends(get_admin_user), db=Depends(get_db)):
    selector = _user_selector(user_id)
    await db.users.update_one(
        selector,
        {
            "$set": {
                "instagram_access_token": None,
                "instagram_user_id": None,
                "instagram_account_ids": [],
                "ig_token_expires_at": None,
            }
        },
    )
    await _write_admin_audit(db, admin["email"], "Reset Instagram connection", user_id)
    return {"ok": True}


@router.delete("/users/{user_id}")
async def admin_delete_user(user_id: str, admin=Depends(get_admin_user), db=Depends(get_db)):
    selector = _user_selector(user_id)
    await db.users.update_one(
        selector,
        {
            "$set": {
                "deleted_at": datetime.now(timezone.utc),
                "admin_status": "deleted",
                "is_active": False,
            }
        },
    )
    await _write_admin_audit(db, admin["email"], "Deleted account", user_id)
    return {"ok": True}


@router.post("/refresh-ig-tokens")
async def refresh_instagram_tokens(admin=Depends(get_admin_user), db=Depends(get_db)):
    """
    Refresh long-lived Instagram tokens expiring within 15 days.
    Call daily from a cron job:
      curl -s -X POST https://api.pinguru.me/admin/refresh-ig-tokens \
           -H "Authorization: Bearer <admin_jwt>"
    Safe to call repeatedly — only acts on tokens near expiry.
    """
    now = datetime.now(timezone.utc)
    threshold = now + timedelta(days=15)

    cursor = db.users.find(
        {
            "instagram_user_id": {"$exists": True, "$ne": None, "$ne": ""},
            "instagram_access_token": {"$exists": True, "$ne": None, "$ne": ""},
            "$or": [
                {"ig_token_expires_at": {"$lte": threshold}},
                {"ig_token_expires_at": {"$exists": False}},
                {"ig_token_expires_at": None},
            ],
        },
        {"_id": 1, "instagram_access_token": 1, "instagram_user_id": 1, "ig_token_expires_at": 1},
    )

    users = await cursor.to_list(10000)
    logger.info("Token refresh job: %d users need refresh", len(users))

    refreshed = 0
    skipped = 0
    failed = 0

    for user in users:
        user_id = user["_id"]
        encrypted_token = user.get("instagram_access_token", "")

        if not encrypted_token:
            skipped += 1
            continue

        try:
            result = await InstagramService.refresh_long_lived_token(encrypted_token)
        except Exception:
            logger.exception("Unexpected error refreshing token for user %s", user_id)
            failed += 1
            continue

        new_token = result.get("access_token")
        expires_in = result.get("expires_in")

        if not new_token:
            # Instagram returned an error — token revoked or user removed app access
            error_obj = result.get("error") or {}
            error_code = error_obj.get("code")
            logger.warning("Token refresh failed for user %s — %s", user_id, error_obj)

            # 190 = invalid/expired token, 102 = session invalidated by user
            # Clear so webhook stops sending DMs with a dead token
            if error_code in (190, 102):
                await db.users.update_one(
                    {"_id": user_id},
                    {"$set": {
                        "instagram_access_token": None,
                        "instagram_user_id": None,
                        "instagram_account_ids": [],
                        "ig_token_expires_at": None,
                    }},
                )
                logger.warning("Cleared revoked Instagram token for user %s", user_id)

            failed += 1
            continue

        new_expires_at = now + timedelta(seconds=expires_in) if expires_in else now + timedelta(days=60)
        new_encrypted = InstagramService.encrypt_access_token(new_token)

        await db.users.update_one(
            {"_id": user_id},
            {"$set": {
                "instagram_access_token": new_encrypted,
                "ig_token_expires_at": new_expires_at,
            }},
        )
        refreshed += 1
        logger.info("Refreshed IG token for user %s, expires %s", user_id, new_expires_at.isoformat())

    return {
        "status": "done",
        "users_checked": len(users),
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
    }