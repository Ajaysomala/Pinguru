import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import settings
from app.database import get_db
from app.models.models import PlanType, TriggerType, get_plan_limits, get_plan_type
from app.services.instagram import InstagramService

router = APIRouter()
logger = logging.getLogger(__name__)


class WebhookSimulateRequest(BaseModel):
    payload: dict[str, Any]
    sign_with_app_secret: bool = True
    process_payload: bool = True


def _normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def hinglish_keyword_match(message: str, keywords: list[str]) -> bool:
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


def _compute_signature(raw_body: bytes) -> str:
    return hmac.new(
        settings.META_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def _validate_signature(signature_header: str | None, raw_body: bytes) -> None:
    if not signature_header or not signature_header.startswith("sha256="):
        raise HTTPException(status_code=403, detail="Missing signature")

    expected_signature = _compute_signature(raw_body)
    provided_signature = signature_header.removeprefix("sha256=")
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise HTTPException(status_code=403, detail="Invalid signature")


def _safe_event_hash(payload: dict[str, Any]) -> str:
    compact = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(compact.encode("utf-8")).hexdigest()


def _event_key_for_messaging(ig_id: str, messaging: dict[str, Any]) -> str:
    message_obj = messaging.get("message") or {}
    mid = message_obj.get("mid") or message_obj.get("id") or str(messaging.get("timestamp") or "")
    sender_id = (messaging.get("sender") or {}).get("id", "unknown")
    return f"msg:{ig_id}:{sender_id}:{mid}"


def _event_key_for_change(ig_id: str, change: dict[str, Any]) -> str:
    field = str(change.get("field") or "unknown")
    value = change.get("value") or {}
    native_id = value.get("comment_id") or value.get("id") or value.get("media_id") or value.get("created_time")
    if native_id:
        return f"chg:{ig_id}:{field}:{native_id}"
    return f"chg:{ig_id}:{field}:{_safe_event_hash(value)}"


async def _mark_event_if_new(db, event_key: str, source: str) -> bool:
    result = await db.webhook_events.update_one(
        {"_id": event_key},
        {
            "$setOnInsert": {
                "source": source,
                "received_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )
    return result.upserted_id is not None


def _is_story_reply(messaging: dict[str, Any]) -> bool:
    message_obj = messaging.get("message") or {}
    referral = messaging.get("referral") or {}
    source = str(referral.get("source") or "").lower()
    if source in {"story", "mention", "story_mention"}:
        return True
    if message_obj.get("is_story_reply"):
        return True
    if isinstance(message_obj.get("reply_to"), dict):
        return True
    return False


async def _send_rule_reply(db, user: dict[str, Any], recipient_id: str, rule: dict[str, Any], trigger_type: TriggerType):
    reply = rule["reply_message"].replace("{{username}}", recipient_id)
    result = await InstagramService.send_dm(
        access_token=user["instagram_access_token"],
        recipient_ig_id=recipient_id,
        message=reply,
        ig_user_id=user["instagram_user_id"],
    )

    await db.dm_logs.insert_one(
        {
            "user_id": str(user["_id"]),
            "rule_id": str(rule["_id"]),
            "recipient_ig_id": recipient_id,
            "message_sent": reply,
            "trigger_type": trigger_type,
            "status": "sent" if result["success"] else "failed",
            "sent_at": datetime.now(timezone.utc),
        }
    )

    if result["success"]:
        await db.users.update_one({"_id": user["_id"]}, {"$inc": {"dm_count_this_month": 1}})


async def _process_webhook_payload(db, body: dict[str, Any], raw_body: bytes) -> dict[str, int]:
    entries = body.get("entry", [])
    logger.info(f"Webhook received: {len(raw_body)} bytes, {len(entries)} entries")
    logger.info(f"Webhook payload: {body}")

    processed_events = 0
    deduped_events = 0

    for entry in entries:
        ig_id = entry.get("id")
        messaging_events = entry.get("messaging") or []
        change_events = entry.get("changes") or []

        for messaging in messaging_events:
            event_key = _event_key_for_messaging(ig_id, messaging)
            if not await _mark_event_if_new(db, event_key, "messaging"):
                deduped_events += 1
                continue
            await handle_messaging_event(db, ig_id, messaging)
            processed_events += 1

        for change in change_events:
            event_key = _event_key_for_change(ig_id, change)
            if not await _mark_event_if_new(db, event_key, "change"):
                deduped_events += 1
                continue
            await handle_change_event(db, ig_id, change)
            processed_events += 1

    return {"processed_events": processed_events, "deduped_events": deduped_events}


@router.get("/instagram")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.META_WEBHOOK_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta")
        return int(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/instagram")
async def handle_webhook(request: Request):
    raw_body = await request.body()

    if not settings.DISABLE_WEBHOOK_SIGNATURE:
        signature = request.headers.get("X-Hub-Signature-256")
        _validate_signature(signature, raw_body)
    else:
        logger.warning("Webhook signature verification disabled in development")

    body = await request.json()
    db = get_db()
    result = await _process_webhook_payload(db, body, raw_body)
    return {"status": "ok", **result}


@router.post("/dev/simulate")
async def simulate_webhook(data: WebhookSimulateRequest):
    if settings.ENVIRONMENT.lower() != "development":
        raise HTTPException(status_code=403, detail="Simulator is available only in development")

    payload_json = json.dumps(data.payload, separators=(",", ":"))
    raw_body = payload_json.encode("utf-8")
    signature_header = None
    if data.sign_with_app_secret:
        signature_header = f"sha256={_compute_signature(raw_body)}"

    result = {"processed_events": 0, "deduped_events": 0}
    if data.process_payload:
        db = get_db()
        result = await _process_webhook_payload(db, data.payload, raw_body)

    return {
        "status": "simulated",
        "signature_header": signature_header,
        "payload": data.payload,
        **result,
    }


@router.get("/dev/sample-payload")
async def sample_payloads():
    return {
        "dm_keyword": {
            "entry": [
                {
                    "id": "<instagram_business_id>",
                    "messaging": [
                        {
                            "sender": {"id": "<customer_ig_id>"},
                            "message": {"mid": "m_1", "text": "link bhejo"},
                            "timestamp": 1710000000,
                        }
                    ],
                }
            ]
        },
        "comment": {
            "entry": [
                {
                    "id": "<instagram_business_id>",
                    "changes": [
                        {
                            "field": "comments",
                            "value": {
                                "from": {"id": "<customer_ig_id>"},
                                "text": "price?",
                                "comment_id": "1789",
                            },
                        }
                    ],
                }
            ]
        },
        "story_reply": {
            "entry": [
                {
                    "id": "<instagram_business_id>",
                    "messaging": [
                        {
                            "sender": {"id": "<customer_ig_id>"},
                            "message": {"mid": "m_2", "text": "interested", "is_story_reply": True},
                            "timestamp": 1710000001,
                        }
                    ],
                }
            ]
        },
    }


async def handle_messaging_event(db, ig_account_id: str, messaging: dict):
    message_text = (messaging.get("message") or {}).get("text", "").strip()

    if message_text:
        await handle_dm_event(db, ig_account_id, messaging)

    if _is_story_reply(messaging):
        await handle_story_reply_event(db, ig_account_id, messaging)


async def handle_change_event(db, ig_account_id: str, change: dict):
    field = str(change.get("field") or "")
    value = change.get("value") or {}

    if field == "comments":
        await handle_comment_event(db, ig_account_id, value)


async def handle_dm_event(db, ig_account_id: str, messaging: dict):
    sender_id = messaging.get("sender", {}).get("id")
    message_text = messaging.get("message", {}).get("text", "").lower()

    if not sender_id or not message_text:
        return

    if sender_id == ig_account_id:
        return

    user = await db.users.find_one({"instagram_user_id": ig_account_id})
    if not user:
        return

    if not user.get("instagram_access_token") or not user.get("instagram_user_id"):
        logger.warning("Skipping DM automation due to missing Instagram credentials")
        return

    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    if user.get("dm_count_this_month", 0) >= plan_limits["dm_limit"]:
        logger.warning("DM limit reached")
        return

    rules = await db.automation_rules.find(
        {
            "user_id": str(user["_id"]),
            "is_active": True,
            "trigger_type": TriggerType.KEYWORD,
        }
    ).to_list(100)

    for rule in rules:
        keywords = [k.lower() for k in rule.get("keywords", [])]
        match_mode = str(rule.get("match_mode", "exact")).lower()
        use_hinglish = match_mode == "hinglish" and user_plan == PlanType.Pro

        if use_hinglish:
            is_match = hinglish_keyword_match(message_text, keywords)
        else:
            is_match = any(kw in message_text for kw in keywords)

        if is_match:
            await _send_rule_reply(db, user, sender_id, rule, TriggerType.KEYWORD)
            break


async def handle_story_reply_event(db, ig_account_id: str, messaging: dict):
    sender_id = (messaging.get("sender") or {}).get("id")
    message_text = ((messaging.get("message") or {}).get("text") or "").lower()

    if not sender_id:
        return

    user = await db.users.find_one({"instagram_user_id": ig_account_id})
    if not user:
        return

    if not user.get("instagram_access_token") or not user.get("instagram_user_id"):
        return

    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    if user.get("dm_count_this_month", 0) >= plan_limits["dm_limit"]:
        return

    rules = await db.automation_rules.find(
        {
            "user_id": str(user["_id"]),
            "is_active": True,
            "trigger_type": TriggerType.STORY_REPLY,
        }
    ).to_list(100)

    for rule in rules:
        keywords = [k.lower() for k in rule.get("keywords", [])]
        if not keywords or any(kw in message_text for kw in keywords):
            await _send_rule_reply(db, user, sender_id, rule, TriggerType.STORY_REPLY)
            break


async def handle_comment_event(db, ig_account_id: str, value: dict):
    commenter_id = value.get("from", {}).get("id")
    comment_text = value.get("text", "").lower()

    if not commenter_id or not comment_text:
        return

    user = await db.users.find_one({"instagram_user_id": ig_account_id})
    if not user:
        return

    if not user.get("instagram_access_token") or not user.get("instagram_user_id"):
        logger.warning("Skipping comment automation due to missing Instagram credentials")
        return

    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    if user.get("dm_count_this_month", 0) >= plan_limits["dm_limit"]:
        return

    rules = await db.automation_rules.find(
        {
            "user_id": str(user["_id"]),
            "is_active": True,
            "trigger_type": {"$in": [TriggerType.POST_COMMENT, TriggerType.REEL_COMMENT]},
        }
    ).to_list(100)

    for rule in rules:
        keywords = [k.lower() for k in rule.get("keywords", [])]
        if not keywords or any(kw in comment_text for kw in keywords):
            raw_trigger = str(rule.get("trigger_type") or TriggerType.POST_COMMENT)
            trigger_type = TriggerType(raw_trigger) if raw_trigger in {t.value for t in TriggerType} else TriggerType.POST_COMMENT
            await _send_rule_reply(db, user, commenter_id, rule, trigger_type)
            break
