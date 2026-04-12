from datetime import datetime, timedelta, timezone
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from passlib.exc import UnknownHashError
from pydantic import BaseModel, EmailStr
import jwt

from app.config import settings
from app.database import get_db

router = APIRouter()
admin_bearer = HTTPBearer(auto_error=False)
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
logger = logging.getLogger(__name__)


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str


def _create_admin_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = {"sub": email, "exp": expire, "type": "admin"}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


async def get_admin_user(credentials: HTTPAuthorizationCredentials | None = Depends(admin_bearer)):
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = credentials.credentials
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
async def admin_login(data: AdminLoginRequest):
    email = data.email.strip().lower()
    password = data.password
    admin_email = settings.ADMIN_EMAIL.strip().lower()
    admin_password_hash = settings.ADMIN_PASSWORD_HASH.strip()

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
    return {"token": token}


@router.get("/users")
async def admin_users(admin=Depends(get_admin_user), db=Depends(get_db)):
    users = await db.users.find({}, {"plan": 1, "dm_count_this_month": 1, "created_at": 1}).to_list(100000)
    sanitized_users = []
    for user in users:
        created_at = user.get("created_at")
        sanitized_users.append(
            {
                "plan": str(user.get("plan", "")),
                "dm_count": int(user.get("dm_count_this_month", 0)),
                "created_at": created_at.isoformat() if created_at else None,
            }
        )
    return {"users": sanitized_users}


@router.get("/stats")
async def admin_stats(admin=Depends(get_admin_user), db=Depends(get_db)):
    total_users = await db.users.count_documents({})
    total_dms_sent = await db.dm_logs.count_documents({"status": "sent"})

    pipeline = [
        {"$group": {"_id": "$plan", "count": {"$sum": 1}}}
    ]
    grouped = await db.users.aggregate(pipeline).to_list(100)
    plan_distribution = {str(item.get("_id", "unknown")): item.get("count", 0) for item in grouped}

    return {
        "total_users": total_users,
        "total_dms_sent": total_dms_sent,
        "plan_distribution": plan_distribution,
    }
