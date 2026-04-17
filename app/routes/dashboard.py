from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from app.database import get_db
from app.routes.auth import get_current_user
from app.models.models import PlanType, get_plan_limits, get_plan_type

router = APIRouter()

@router.get("/stats")
async def get_stats(db=Depends(get_db), user=Depends(get_current_user)):
   user_id = str(user["_id"])
   start_of_month = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
   pipeline = [
      {"$match": {"user_id": user_id, "sent_at": {"$gte": start_of_month}}},
      {"$group": {"_id": "$status", "count": {"$sum": 1}}}
      ]
   agg = await db.dm_logs.aggregate(pipeline).to_list(10)
   counts = {r["_id"]: r["count"] for r in agg}
   total_sent   = counts.get("sent", 0)
   total_failed = counts.get("failed", 0)
   active_rules = await db.automation_rules.count_documents({"user_id": user_id, "is_active": True})
   plan_type = get_plan_type(user.get("plan", PlanType.Free))
   plan_limits  = get_plan_limits(plan_type)
   dm_limit = plan_limits.get("dm_limit")
   rule_limit = plan_limits["rules"]
   return {
       "plan": plan_type.name,
       "dm_sent_this_month": total_sent,
       "dm_failed_this_month": total_failed,
       "dm_limit": dm_limit,
       "dm_remaining": None if dm_limit is None else max(0, dm_limit - total_sent),
       "active_rules": active_rules,
       "rule_limit": "Unlimited" if rule_limit is None else rule_limit,
       "instagram_connected": bool(user.get("instagram_user_id")),
       "ig_token_expires_at": user.get("ig_token_expires_at", "").isoformat() if user.get("ig_token_expires_at") else None,
       }

@router.get("/dm-logs")
async def get_dm_logs(limit: int = Query(default=50, le=500), db=Depends(get_db), user=Depends(get_current_user)):
    logs = await db.dm_logs.find({"user_id": str(user["_id"])}).sort("sent_at", -1).limit(limit).to_list(limit)
    for log in logs:
        log["_id"] = str(log["_id"])
        if "sent_at" in log: log["sent_at"] = log["sent_at"].isoformat()
    return {"logs": logs, "total": len(logs)}
