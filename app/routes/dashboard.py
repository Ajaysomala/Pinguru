from datetime import datetime, timedelta, timezone

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
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    ]
    agg = await db.dm_logs.aggregate(pipeline).to_list(10)
    counts = {r["_id"]: r["count"] for r in agg}
    total_sent = counts.get("sent", 0)
    total_failed = counts.get("failed", 0)
    active_rules = await db.automation_rules.count_documents({"user_id": user_id, "is_active": True})
    plan_type = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(plan_type)
    dm_limit = plan_limits.get("dm_limit")
    rule_limit = plan_limits["rules"]
    analytics_tier = str(plan_limits.get("analytics_tier", "basic"))
    premium_analytics_enabled = analytics_tier == "premium"

    success_rate = None
    avg_dms_per_day_30d = None
    best_day_30d = None
    peak_hour_utc = None
    busiest_weekday = None

    if premium_analytics_enabled:
        now = datetime.now(timezone.utc)
        days_30_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        days_30_start = days_30_start.replace(day=1) if now.day == 1 else days_30_start
        days_30_start = max(days_30_start, now - timedelta(days=30))

        recent_totals_pipeline = [
            {"$match": {"user_id": user_id, "sent_at": {"$gte": days_30_start}}},
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ]
        recent_totals = await db.dm_logs.aggregate(recent_totals_pipeline).to_list(10)
        recent_counts = {r.get("_id"): int(r.get("count", 0)) for r in recent_totals}
        recent_sent = int(recent_counts.get("sent", 0))
        recent_failed = int(recent_counts.get("failed", 0))
        recent_total = recent_sent + recent_failed
        if recent_total > 0:
            success_rate = round((recent_sent / recent_total) * 100, 2)

        daily_pipeline = [
            {"$match": {"user_id": user_id, "sent_at": {"$gte": days_30_start}, "status": "sent"}},
            {
                "$group": {
                    "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$sent_at"}},
                    "count": {"$sum": 1},
                }
            },
            {"$sort": {"_id": 1}},
        ]
        daily_rows = await db.dm_logs.aggregate(daily_pipeline).to_list(60)
        if daily_rows:
            total_sent_30d = sum(int(row.get("count", 0)) for row in daily_rows)
            avg_dms_per_day_30d = round(total_sent_30d / 30, 2)
            best_day_row = max(daily_rows, key=lambda row: int(row.get("count", 0)))
            best_day_30d = {
                "date": str(best_day_row.get("_id")),
                "sent": int(best_day_row.get("count", 0)),
            }

        hourly_pipeline = [
            {"$match": {"user_id": user_id, "sent_at": {"$gte": days_30_start}, "status": "sent"}},
            {"$group": {"_id": {"$hour": "$sent_at"}, "count": {"$sum": 1}}},
        ]
        hourly_rows = await db.dm_logs.aggregate(hourly_pipeline).to_list(24)
        if hourly_rows:
            peak = max(hourly_rows, key=lambda row: int(row.get("count", 0)))
            peak_hour_utc = int(peak.get("_id", 0))

        weekday_pipeline = [
            {"$match": {"user_id": user_id, "sent_at": {"$gte": days_30_start}, "status": "sent"}},
            {"$group": {"_id": {"$dayOfWeek": "$sent_at"}, "count": {"$sum": 1}}},
        ]
        weekday_rows = await db.dm_logs.aggregate(weekday_pipeline).to_list(7)
        weekday_labels = {
            1: "Sunday",
            2: "Monday",
            3: "Tuesday",
            4: "Wednesday",
            5: "Thursday",
            6: "Friday",
            7: "Saturday",
        }
        if weekday_rows:
            busiest = max(weekday_rows, key=lambda row: int(row.get("count", 0)))
            busiest_weekday = weekday_labels.get(int(busiest.get("_id", 1)), "Sunday")

    return {
        "plan": plan_type.name,
        "dm_sent_this_month": total_sent,
        "dm_failed_this_month": total_failed,
        "dm_limit": dm_limit,
        "dm_remaining": None if dm_limit is None else max(0, dm_limit - total_sent),
        "active_rules": active_rules,
        "rule_limit": "Unlimited" if rule_limit is None else rule_limit,
        "analytics_tier": analytics_tier,
        "premium_analytics_enabled": premium_analytics_enabled,
        "success_rate": success_rate,
        "avg_dms_per_day_30d": avg_dms_per_day_30d,
        "best_day_30d": best_day_30d,
        "peak_hour_utc": peak_hour_utc,
        "busiest_weekday": busiest_weekday,
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
