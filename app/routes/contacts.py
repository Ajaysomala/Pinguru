from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query
from app.database import get_db
from app.routes.auth import get_current_user
from app.models.models import get_plan_limits

router = APIRouter()


@router.get("")
async def list_contacts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(user["_id"])
    skip = (page - 1) * limit
    total = await db.contacts.count_documents({"user_id": user_id})
    cursor = db.contacts.find({"user_id": user_id}).sort("last_seen_at", -1).skip(skip).limit(limit)
    contacts = []
    async for c in cursor:
        c["id"] = str(c.pop("_id"))
        contacts.append(c)
    return {"contacts": contacts, "total": total, "page": page, "limit": limit}


@router.get("/stats")
async def contact_stats(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(user["_id"])
    plan_limits = get_plan_limits(user.get("plan", "free"))
    total = await db.contacts.count_documents({"user_id": user_id})
    return {
        "total": total,
        "limit": plan_limits.get("contacts_limit"),
    }
