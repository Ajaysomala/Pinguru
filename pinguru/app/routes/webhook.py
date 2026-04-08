from fastapi import APIRouter, Request, Query, HTTPException
from app.config import settings
from app.database import get_db
from app.services.instagram import InstagramService
from app.models.models import DMLog, TriggerType
from datetime import datetime
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

# ── Meta Webhook Verification (GET) ──────────────────────────────────────────

@router.get("/instagram")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.META_WEBHOOK_VERIFY_TOKEN:
        logger.info("✅ Webhook verified by Meta")
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")

# ── Meta Webhook Events (POST) ────────────────────────────────────────────────

@router.post("/instagram")
async def handle_webhook(request: Request):
    body = await request.json()
    logger.info(f"Webhook received: {body}")

    db = get_db()

    for entry in body.get("entry", []):
        ig_id = entry.get("id")  # Instagram Business Account ID

        # --- Direct Messages ---
        for messaging in entry.get("messaging", []):
            await handle_dm_event(db, ig_id, messaging)

        # --- Comments on Posts/Reels ---
        for change in entry.get("changes", []):
            if change.get("field") == "comments":
                await handle_comment_event(db, ig_id, change["value"])

    return {"status": "ok"}

# ── DM Handler ────────────────────────────────────────────────────────────────

async def handle_dm_event(db, ig_account_id: str, messaging: dict):
    sender_id = messaging.get("sender", {}).get("id")
    message_text = messaging.get("message", {}).get("text", "").lower()

    if not sender_id or not message_text:
        return

    # Find the user who owns this Instagram account
    user = await db.users.find_one({"instagram_user_id": ig_account_id})
    if not user:
        return

    # Check DM limit
    if user.get("dm_count_this_month", 0) >= user.get("dm_limit", 50):
        logger.warning(f"DM limit reached for user {user['_id']}")
        return

    # Find matching keyword automation rule
    rules = await db.automation_rules.find({
        "user_id": str(user["_id"]),
        "is_active": True,
        "trigger_type": TriggerType.KEYWORD
    }).to_list(100)

    for rule in rules:
        keywords = [k.lower() for k in rule.get("keywords", [])]
        if any(kw in message_text for kw in keywords):
            # Replace template variables
            reply = rule["reply_message"].replace("{{username}}", sender_id)

            result = await InstagramService.send_dm(
                access_token=user["instagram_access_token"],
                recipient_ig_id=sender_id,
                message=reply,
            )

            # Log it
            log = {
                "user_id": str(user["_id"]),
                "rule_id": str(rule["_id"]),
                "recipient_ig_id": sender_id,
                "message_sent": reply,
                "trigger_type": TriggerType.KEYWORD,
                "status": "sent" if result["success"] else "failed",
                "sent_at": datetime.utcnow(),
            }
            await db.dm_logs.insert_one(log)

            if result["success"]:
                await db.users.update_one(
                    {"_id": user["_id"]},
                    {"$inc": {"dm_count_this_month": 1}}
                )
            break  # only one rule per message


# ── Comment Handler ───────────────────────────────────────────────────────────

async def handle_comment_event(db, ig_account_id: str, value: dict):
    commenter_id = value.get("from", {}).get("id")
    comment_text = value.get("text", "").lower()

    if not commenter_id or not comment_text:
        return

    user = await db.users.find_one({"instagram_user_id": ig_account_id})
    if not user:
        return

    rules = await db.automation_rules.find({
        "user_id": str(user["_id"]),
        "is_active": True,
        "trigger_type": TriggerType.POST_COMMENT,
    }).to_list(100)

    for rule in rules:
        keywords = [k.lower() for k in rule.get("keywords", [])]
        if not keywords or any(kw in comment_text for kw in keywords):
            reply = rule["reply_message"].replace("{{username}}", commenter_id)
            await InstagramService.send_dm(
                access_token=user["instagram_access_token"],
                recipient_ig_id=commenter_id,
                message=reply,
            )
            break
