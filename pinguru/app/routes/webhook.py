import hashlib
import hmac
import logging
import re

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from app.config import settings
from app.database import get_db
from app.models.models import DMLog, PlanType, TriggerType, get_plan_limits, get_plan_type
from app.services.instagram import InstagramService

router = APIRouter()
logger = logging.getLogger(__name__)


def _normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def hinglish_keyword_match(message: str, keywords: list[str]) -> bool:
    # PinGuru Pro — Smart Hinglish Trigger (unique feature)
    message_clean = _normalize_text(message)
    if not message_clean or not keywords:
        return False

    variants_by_root = {
        "link": [
            "link do",
            "bhai link",
            "link bhejo",
            "link chahiye",
            "send link",
            "link please",
            "link dena",
            "link de",
            "lnk",
        ],
        "price": [
            "price btao",
            "kitna hai",
            "rate kya hai",
            "cost kya hai",
            "kitne ka",
        ],
        "join": [
            "join karna",
            "join kaise",
            "kaise join",
            "join krna",
            "join chahiye",
        ],
    }

    phrase_candidates: set[str] = set()
    root_candidates: set[str] = set()

    for keyword in keywords:
        normalized_keyword = _normalize_text(keyword)
        if not normalized_keyword:
            continue

        phrase_candidates.add(normalized_keyword)
        for token in normalized_keyword.split():
            if len(token) >= 3:
                root_candidates.add(token)

        for root, variants in variants_by_root.items():
            if root in normalized_keyword or normalized_keyword in root:
                root_candidates.add(root)
                phrase_candidates.update(_normalize_text(v) for v in variants)

    for phrase in phrase_candidates:
        if phrase and phrase in message_clean:
            return True

    message_tokens = message_clean.split()
    for root in root_candidates:
        if any(token.startswith(root) or root.startswith(token) for token in message_tokens):
            return True

    return False

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
    signature = request.headers.get("X-Hub-Signature-256")
    raw_body = await request.body()

    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Missing signature")

    expected_signature = hmac.new(
        settings.META_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    provided_signature = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    body = await request.json()
    entries = body.get("entry", [])
    logger.info(f"Webhook received: {len(raw_body)} bytes, {len(entries)} entries")

    db = get_db()

    for entry in entries:
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

    user_plan = get_plan_type(user.get("plan", PlanType.Free))

    # Check DM limit
    plan_limits = get_plan_limits(user_plan)
    if user.get("dm_count_this_month", 0) >= plan_limits["dm_limit"]:
        logger.warning("DM limit reached")
        return

    # Find matching keyword automation rule
    rules = await db.automation_rules.find({
        "user_id": str(user["_id"]),
        "is_active": True,
        "trigger_type": TriggerType.KEYWORD
    }).to_list(100)

    for rule in rules:
        keywords = [k.lower() for k in rule.get("keywords", [])]
        match_mode = str(rule.get("match_mode", "exact")).lower()
        use_hinglish = match_mode == "hinglish" and user_plan == PlanType.Pro

        if use_hinglish:
            is_match = hinglish_keyword_match(message_text, keywords)
        else:
            # Free/Starter silently fall back to exact matching.
            is_match = any(kw in message_text for kw in keywords)

        if is_match:
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
                "sent_at": datetime.now(timezone.utc),
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
