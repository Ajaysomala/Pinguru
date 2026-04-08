from fastapi import APIRouter, Depends
from app.database import get_db
from app.routes.auth import get_current_user
from app.models.models import PLAN_LIMITS
from datetime import datetime

router = APIRouter()

@router.get("/stats")
async def get_stats(db=Depends(get_db), user=Depends(get_current_user)):
    user_id = str(user["_id"])
    start_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    dm_logs = await db.dm_logs.find({"user_id": user_id, "sent_at": {"$gte": start_of_month}}).to_list(10000)
    total_sent   = len([d for d in dm_logs if d["status"] == "sent"])
    total_failed = len([d for d in dm_logs if d["status"] == "failed"])
    active_rules = await db.automation_rules.count_documents({"user_id": user_id, "is_active": True})
    plan_limits  = PLAN_LIMITS[user["plan"]]
    return {
        "plan": user["plan"],
        "dm_sent_this_month": total_sent,
        "dm_failed_this_month": total_failed,
        "dm_limit": plan_limits["dm_limit"],
        "dm_remaining": max(0, plan_limits["dm_limit"] - total_sent),
        "active_rules": active_rules,
        "rule_limit": plan_limits["rules"],
        "instagram_connected": bool(user.get("instagram_user_id")),
        "ig_token_expires_at": user.get("ig_token_expires_at", "").isoformat() if user.get("ig_token_expires_at") else None,
    }

@router.get("/dm-logs")
async def get_dm_logs(limit: int = 50, db=Depends(get_db), user=Depends(get_current_user)):
    logs = await db.dm_logs.find({"user_id": str(user["_id"])}).sort("sent_at", -1).limit(limit).to_list(limit)
    for log in logs:
        log["_id"] = str(log["_id"])
        if "sent_at" in log: log["sent_at"] = log["sent_at"].isoformat()
    return {"logs": logs, "total": len(logs)}
