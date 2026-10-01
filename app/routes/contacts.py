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
    tag: str | None = Query(None),
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    user_id = str(user["_id"])
    skip = (page - 1) * limit
    query = {"user_id": user_id}
    tag_val = tag if isinstance(tag, str) else None
    if tag_val:
        query["tags"] = tag_val.strip().lower()

    total = await db.contacts.count_documents(query)
    cursor = db.contacts.find(query).sort("last_seen_at", -1).skip(skip).limit(limit)
    contacts = []
    now = datetime.now(timezone.utc)
    async for c in cursor:
        c["id"] = str(c.pop("_id"))
        last_seen = c.get("last_seen_at")
        if last_seen and getattr(last_seen, "tzinfo", None) is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        c["is_window_active"] = bool(last_seen and (now - last_seen).total_seconds() <= 86400)
        c.setdefault("tags", [])
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


@router.get("/export")
@router.get("/export-csv")
async def export_contacts_csv(
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    import csv
    import io
    from fastapi.responses import StreamingResponse

    user_id = str(user["_id"])
    cursor = db.contacts.find({"user_id": user_id}).sort("last_seen_at", -1)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Instagram Username",
        "Display Name",
        "Captured Email",
        "Captured Phone",
        "Tags",
        "24h Window Active",
        "Email Captured At",
        "Phone Captured At",
        "Instagram User ID",
        "DM Count",
        "Follow Gate Status",
        "First Seen At",
        "Last Seen At",
    ])

    now = datetime.now(timezone.utc)
    async for c in cursor:
        captured_at_str = c.get("email_captured_at").isoformat() if c.get("email_captured_at") and hasattr(c.get("email_captured_at"), "isoformat") else str(c.get("email_captured_at") or "")
        phone_captured_at_str = c.get("phone_captured_at").isoformat() if c.get("phone_captured_at") and hasattr(c.get("phone_captured_at"), "isoformat") else str(c.get("phone_captured_at") or "")
        first_seen_str = c.get("first_seen_at").isoformat() if c.get("first_seen_at") and hasattr(c.get("first_seen_at"), "isoformat") else str(c.get("first_seen_at") or "")
        last_seen_str = c.get("last_seen_at").isoformat() if c.get("last_seen_at") and hasattr(c.get("last_seen_at"), "isoformat") else str(c.get("last_seen_at") or "")
        
        last_seen = c.get("last_seen_at")
        if last_seen and getattr(last_seen, "tzinfo", None) is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        window_active = "Yes" if bool(last_seen and (now - last_seen).total_seconds() <= 86400) else "No"
        tags_str = ", ".join(c.get("tags") or [])

        writer.writerow([
            c.get("ig_username") or "",
            c.get("display_name") or "",
            c.get("captured_email") or "",
            c.get("captured_phone") or "",
            tags_str,
            window_active,
            captured_at_str,
            phone_captured_at_str,
            c.get("ig_user_id") or "",
            c.get("dm_count") or 0,
            c.get("follow_gate_status") or "none",
            first_seen_str,
            last_seen_str,
        ])

    csv_data = output.getvalue()
    filename = f"pinguru_contacts_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        io.StringIO(csv_data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

