import asyncio
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.config import settings
from app.database import get_db
from app.models.models import PlanType, TriggerType, get_plan_limits, get_plan_type
from app.services.instagram import InstagramService
from bson import ObjectId

router = APIRouter()
logger = logging.getLogger(__name__)

FREE_BRAND_FOOTER = "\n\n© PinGuru"
FOLLOW_CONFIRMATION_TOKENS = {
    "followed",
    "done",
    "i followed",
    "following",
    "yes followed",
    "im following",
    "i am following",
    "i m following",
}


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


def _evaluate_keyword_match(
    message_text: str,
    keywords: list[str],
    match_mode: str = "contains",
    user_plan: PlanType = PlanType.Free,
) -> tuple[bool, str]:
    if not message_text or not keywords:
        return False, ""

    cleaned_msg = message_text.strip().lower()
    norm_mode = (match_mode or "contains").strip().lower()

    if norm_mode == "hinglish" and user_plan == PlanType.Pro:
        if hinglish_keyword_match(cleaned_msg, keywords):
            matched_kw = next((kw for kw in keywords if kw.lower() in cleaned_msg), (keywords[0] if keywords else ""))
            return True, matched_kw
        return False, ""

    for raw_kw in keywords:
        kw = str(raw_kw or "").strip().lower()
        if not kw:
            continue
        if norm_mode == "exact":
            if cleaned_msg == kw or cleaned_msg.strip("!?. ,#") == kw:
                return True, raw_kw
        elif norm_mode == "starts_with":
            if cleaned_msg.startswith(kw):
                return True, raw_kw
        else:
            # "contains" or default: word boundary match first
            pattern = rf"(?:\b|^){re.escape(kw)}(?:\b|$)"
            if re.search(pattern, cleaned_msg):
                return True, raw_kw
            elif kw in cleaned_msg:
                return True, raw_kw

    return False, ""



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
    return hashlib.sha256(compact.encode("utf-8")).hexdigest()


def _event_key_for_messaging(ig_id: str, messaging: dict[str, Any]) -> str:
    message_obj = messaging.get("message") or {}
    postback_obj = messaging.get("postback") or {}
    mid = (
        message_obj.get("mid")
        or message_obj.get("id")
        or postback_obj.get("mid")
        or str(messaging.get("timestamp") or "")
    )
    sender_id = (messaging.get("sender") or {}).get("id", "unknown")
    return f"msg:{ig_id}:{sender_id}:{mid}"


def _event_key_for_change(ig_id: str, change: dict[str, Any]) -> str:
    field = str(change.get("field") or "unknown")
    value = change.get("value") or {}
    native_id = value.get("comment_id") or value.get("id") or value.get("media_id") or value.get("created_time")
    if native_id:
        return f"chg:{ig_id}:{field}:{native_id}"
    return f"chg:{ig_id}:{field}:{_safe_event_hash(value)}"


def _normalize_comment_target_type(value: Any) -> str:
    normalized = str(value or "any").strip().lower()
    if normalized in {"specific", "any"}:
        return normalized
    return "any"


def _normalize_comment_media_filter(value: Any, trigger_type: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"post", "reel", "all"}:
        return normalized
    if trigger_type == TriggerType.POST_COMMENT.value:
        return "post"
    if trigger_type == TriggerType.REEL_COMMENT.value:
        return "reel"
    return "all"


def _extract_comment_media_context(value: dict[str, Any]) -> tuple[str, str | None]:
    media_id = str(value.get("media_id") or value.get("media", {}).get("id") or value.get("id") or "").strip()
    media_type_raw = str(
        value.get("media_type")
        or value.get("media", {}).get("media_type")
        or value.get("media_product_type")
        or value.get("product_type")
        or ""
    ).strip().lower()

    media_kind: str | None = None
    if media_type_raw in {"image", "photo", "carousel_album", "post"}:
        media_kind = "post"
    elif media_type_raw in {"video", "reel", "reels"}:
        media_kind = "reel"
    elif str(value.get("media_product_type") or "").strip().upper() == "REELS":
        media_kind = "reel"

    return media_id, media_kind


def _comment_rule_matches(rule: dict[str, Any], media_id: str, media_kind: str | None) -> bool:
    trigger_type = str(rule.get("trigger_type") or TriggerType.POST_COMMENT.value)
    target_type = _normalize_comment_target_type(rule.get("comment_target_type"))
    media_filter = _normalize_comment_media_filter(rule.get("comment_media_filter"), trigger_type)
    rule_media_id = str(rule.get("comment_media_id") or "").strip()

    if target_type == "specific":
        if not rule_media_id:
            return False
        if not media_id or media_id != rule_media_id:
            return False

    if media_filter == "post" and media_kind == "reel":
        return False
    if media_filter == "reel" and media_kind == "post":
        return False

    return True


def _render_comment_template(template: str, commenter_id: str, comment_text: str) -> str:
    rendered = (
        template
        .replace("{{username}}", commenter_id)
        .replace("{{name}}", commenter_id)
        .replace("{username}", commenter_id)
        .replace("{name}", commenter_id)
        .replace("{{comment}}", comment_text)
        .replace("{comment}", comment_text)
    )
    return rendered.strip()


def _apply_plan_footer(message: str, user_plan: PlanType) -> str:
    base = (message or "").strip()
    if user_plan == PlanType.Free and FREE_BRAND_FOOTER.strip().lower() not in base.lower():
        return f"{base}{FREE_BRAND_FOOTER}".strip()
    return base


async def _ensure_contact_create_allowed(db, user: dict[str, Any], ig_user_id: str) -> None:
    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    if user_plan != PlanType.Free:
        return

    existing = await db.contacts.find_one({"user_id": str(user["_id"]), "ig_user_id": ig_user_id})
    if existing:
        return

    contact_limit = int(get_plan_limits(PlanType.Free).get("contacts_limit") or 0)
    total_contacts = await db.contacts.count_documents({"user_id": str(user["_id"])})
    if contact_limit and total_contacts >= contact_limit:
        raise HTTPException(status_code=403, detail="Free plan contact limit reached. Upgrade to continue automations.")


def _is_follow_confirmation_message(message_text: str) -> bool:
    normalized = _normalize_text(message_text or "")
    if not normalized:
        return False
    if normalized in FOLLOW_CONFIRMATION_TOKENS:
        return True
    return any(token in normalized for token in FOLLOW_CONFIRMATION_TOKENS)


def _build_follow_buttons(user: dict[str, Any]) -> list[dict[str, str]]:
    username = str(user.get("instagram_username") or "").strip().lstrip("@")
    profile_url = f"https://www.instagram.com/{username}/" if username else "https://www.instagram.com/"
    return [
        {
            "type": "web_url",
            "url": profile_url,
            "title": "Visit Profile",
        },
        {
            "type": "postback",
            "title": "I'm following ✅",
            "payload": "FOLLOWED",
        },
    ]


def _build_follow_prompt(user: dict[str, Any]) -> str:
    return (
        "Oh no! It seems you're not following me 😭 It would really mean a lot if you visit my profile and hit the follow button 🥺 . "
        "Once you have done that, click on the 'I'm following' button below and you will get the link ✨ ."
    )


def _build_follow_not_followed_reminder(user: dict[str, Any]) -> str:
    username = str(user.get("instagram_username") or "").strip().lstrip("@")
    handle_str = f"@{username} " if username else "our account "
    return (
        f"Oops! We checked and you're not following {handle_str}yet 🥺\n\n"
        "Please tap 'Visit Profile' below to follow, then tap 'I'm following ✅' again to unlock your message! ✨"
    )


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


async def _find_user_for_ig_account(db, ig_account_id: str) -> dict[str, Any] | None:
    # Step 1: Direct match in database
    user = await db.users.find_one(
        {
            "$or": [
                {"instagram_user_id": ig_account_id},
                {"instagram_account_ids": ig_account_id},
            ]
        }
    )
    if user:
        current_primary = str(user.get("instagram_user_id") or "").strip()
        account_ids = user.get("instagram_account_ids") or []
        if current_primary != ig_account_id or ig_account_id not in account_ids:
            await db.users.update_one(
                {"_id": user["_id"]},
                {
                    "$set": {"instagram_user_id": ig_account_id},
                    "$addToSet": {"instagram_account_ids": ig_account_id},
                },
            )
            user["instagram_user_id"] = ig_account_id
            if "instagram_account_ids" not in user:
                user["instagram_account_ids"] = []
            if ig_account_id not in user["instagram_account_ids"]:
                user["instagram_account_ids"].append(ig_account_id)
            logger.info(
                "Aligned instagram_user_id to webhook ig_account_id=%s for user_id=%s",
                ig_account_id,
                str(user.get("_id")),
            )
        return user

    # Step 2: Fetch candidate users who have an Instagram access token (newest users first)
    candidates = await db.users.find(
        {
            "instagram_access_token": {"$exists": True, "$nin": [None, ""]},
            "$or": [
                {"instagram_user_id": {"$exists": True, "$nin": [None, ""]}},
                {"instagram_account_ids": {"$exists": True, "$ne": []}},
            ],
        }
    ).sort("created_at", -1).to_list(50)

    if not candidates:
        logger.warning("No candidate users with Instagram tokens found for ig_account_id=%s", ig_account_id)
        return None

    # Step 3: Try active verification via Instagram Graph API
    for candidate in candidates:
        try:
            token = candidate.get("instagram_access_token")
            if not token:
                continue
            ownership = await InstagramService.verify_account_ownership(token, ig_account_id)
            if ownership:
                returned_id = str(ownership.get("id") or "").strip()
                returned_username = str(ownership.get("username") or "").strip().lower()
                cand_ig_id = str(candidate.get("instagram_user_id") or "").strip()
                cand_account_ids = [str(x).strip() for x in (candidate.get("instagram_account_ids") or [])]
                cand_username = str(candidate.get("instagram_username") or "").strip().lower()

                if (
                    (returned_id and (returned_id == cand_ig_id or returned_id in cand_account_ids))
                    or (returned_username and returned_username == cand_username)
                ):
                    await db.users.update_one(
                        {"_id": candidate["_id"]},
                        {
                            "$set": {"instagram_user_id": ig_account_id},
                            "$addToSet": {"instagram_account_ids": ig_account_id},
                        },
                    )
                    candidate["instagram_user_id"] = ig_account_id
                    candidate.setdefault("instagram_account_ids", []).append(ig_account_id)
                    logger.info(
                        "Verified and auto-mapped ig_account_id=%s to user_id=%s (%s) via Graph API",
                        ig_account_id,
                        str(candidate["_id"]),
                        candidate.get("email"),
                    )
                    return candidate
        except Exception:
            logger.exception("Error checking account ownership for candidate user %s", candidate.get("_id"))

    # Step 4: Fallback to non-expired active candidate if Graph API verification was inconclusive
    now = datetime.now(timezone.utc)
    active_candidates = []
    for c in candidates:
        exp = c.get("ig_token_expires_at")
        if exp is not None:
            if getattr(exp, "tzinfo", None) is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= now:
                continue
        active_candidates.append(c)

    fallback_candidates = active_candidates if len(active_candidates) == 1 else (candidates if len(candidates) == 1 else [])
    if len(fallback_candidates) == 1:
        fallback_user = fallback_candidates[0]
        old_id = str(fallback_user.get("instagram_user_id") or "")
        await db.users.update_one(
            {"_id": fallback_user["_id"]},
            {
                "$set": {"instagram_user_id": ig_account_id},
                "$addToSet": {"instagram_account_ids": ig_account_id},
            },
        )
        fallback_user["instagram_user_id"] = ig_account_id
        fallback_user.setdefault("instagram_account_ids", []).append(ig_account_id)
        logger.warning(
            "Auto-mapped instagram_user_id from %s to webhook ig_account_id=%s for user_id=%s (candidates=%s, active=%s)",
            old_id,
            ig_account_id,
            str(fallback_user.get("_id")),
            len(candidates),
            len(active_candidates),
        )
        return fallback_user

    logger.warning(
        "No user found for webhook ig_account_id=%s; connected_candidates=%s; active_candidates=%s",
        ig_account_id,
        len(candidates),
        len(active_candidates),
    )
    return None


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


async def _render_template(db, user: dict, recipient_id: str, rule: dict, matched_keyword: str = "") -> str:
    """Replace {{name}}, {{username}}, {{keyword}} with real values."""
    template = str(rule.get("reply_message") or "").strip()
    if not template:
        return template

    # Try to get stored contact info for this recipient
    contact = await db.contacts.find_one(
        {"user_id": str(user["_id"]), "ig_user_id": recipient_id}
    )
    ig_name = str((contact or {}).get("display_name") or (contact or {}).get("ig_name") or "")
    ig_username = str((contact or {}).get("ig_username") or "")
    if not ig_name or not ig_username:
        profile = await InstagramService.get_messaging_user_profile(
            user["instagram_access_token"], recipient_id
        )
        fetched_name = str(profile.get("name") or "").strip()
        fetched_username = str(profile.get("username") or "").strip()

        new_info_fetched = False
        if not ig_name and fetched_name:
            ig_name = fetched_name
            new_info_fetched = True
        if not ig_username and fetched_username:
            ig_username = fetched_username
            new_info_fetched = True

        # Fallback display_name to username if display_name is not set
        if not ig_name and ig_username:
            ig_name = ig_username

        if new_info_fetched or isinstance(profile.get("is_user_follow_business"), bool):
            update_fields = {}
            if ig_name:
                update_fields["display_name"] = ig_name
            if ig_username:
                update_fields["ig_username"] = ig_username
            if isinstance(profile.get("is_user_follow_business"), bool):
                update_fields["is_following"] = profile["is_user_follow_business"]
            if update_fields:
                now = datetime.now(timezone.utc)
                await db.contacts.update_one(
                    {"user_id": str(user["_id"]), "ig_user_id": recipient_id},
                    {
                        "$set": update_fields,
                        "$setOnInsert": {
                            "user_id": str(user["_id"]),
                            "ig_user_id": recipient_id,
                            "first_seen_at": now,
                        },
                    },
                    upsert=True,
                )

    keyword_val = matched_keyword or (rule.get("keywords") or [""])[0]
    contact_email = str((contact or {}).get("captured_email") or "")
    contact_phone = str((contact or {}).get("captured_phone") or "")

    return (
        template
        .replace("{{name}}",     ig_name)
        .replace("{{username}}", ig_username)
        .replace("{{keyword}}",  keyword_val)
        .replace("{{email}}",    contact_email)
        .replace("{{phone}}",    contact_phone)
        # also handle single-brace variants (legacy)
        .replace("{name}",       ig_name)
        .replace("{username}",   ig_username)
        .replace("{keyword}",    keyword_val)
        .replace("{email}",      contact_email)
        .replace("{phone}",      contact_phone)
    )


async def _send_rule_reply(
    db,
    user: dict[str, Any],
    recipient_id: str,
    rule: dict[str, Any],
    trigger_type: TriggerType,
    matched_keyword: str = "",
    comment_id: str | None = None,
    skip_follow_gate: bool = False,
    skip_email_capture: bool = False,
    skip_phone_capture: bool = False,
):
    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    base_reply = await _render_template(db, user, recipient_id, rule, matched_keyword)
    reply = _apply_plan_footer(base_reply, user_plan)

    allowed_follow_gate_triggers = {
        TriggerType.COMMENT,
        TriggerType.POST_COMMENT,
        TriggerType.REEL_COMMENT,
        TriggerType.KEYWORD,
        TriggerType.NEW_DM,
        TriggerType.STORY_REPLY,
        TriggerType.STORY_MENTION,
    }

    follow_gate_requested = bool(rule.get("ask_follow_before_dm", False))
    if (
        not skip_follow_gate
        and follow_gate_requested
        and user_plan in {PlanType.Starter, PlanType.Pro}
        and trigger_type in allowed_follow_gate_triggers
    ):
        contact = await db.contacts.find_one({"user_id": str(user["_id"]), "ig_user_id": recipient_id})
        rule_id_str = str(rule.get("_id"))
        completed_rules = set(contact.get("completed_follow_gate_rule_ids") or [])
        if contact and str(contact.get("follow_gate_status") or "") == "completed":
            saved_rule_id = str(contact.get("follow_gate_rule_id") or "").strip()
            if saved_rule_id:
                completed_rules.add(saved_rule_id)

        is_completed = rule_id_str in completed_rules
        is_awaiting_for_rule = (
            bool(contact)
            and str(contact.get("follow_gate_status") or "") == "awaiting"
            and str(contact.get("follow_gate_rule_id") or "") == rule_id_str
        )

        if not is_awaiting_for_rule and not is_completed:
            # Check if recipient already follows the business account
            is_already_following = False
            if contact and contact.get("is_following") is True:
                is_already_following = True
            else:
                profile = await InstagramService.get_messaging_user_profile(
                    user["instagram_access_token"], recipient_id
                )
                if profile and profile.get("is_user_follow_business") is True:
                    is_already_following = True

            if is_already_following:
                logger.info(
                    "Recipient %s is already following %s. Completing follow gate without prompt.",
                    recipient_id,
                    user.get("instagram_username"),
                )
                now = datetime.now(timezone.utc)
                await _ensure_contact_create_allowed(db, user, recipient_id)
                await db.contacts.update_one(
                    {"user_id": str(user["_id"]), "ig_user_id": recipient_id},
                    {
                        "$set": {
                            "is_following": True,
                            "follow_gate_status": "completed",
                            "follow_gate_completed_at": now,
                            "last_seen_at": now,
                        },
                        "$addToSet": {"completed_follow_gate_rule_ids": rule_id_str},
                        "$setOnInsert": {
                            "user_id": str(user["_id"]),
                            "ig_user_id": recipient_id,
                            "first_seen_at": now,
                        },
                    },
                    upsert=True,
                )
                is_completed = True
            else:
                prompt_message = _build_follow_prompt(user)
                follow_buttons = _build_follow_buttons(user)
                prompt_result = await InstagramService.send_dm(
                    access_token=user["instagram_access_token"],
                    recipient_ig_id=recipient_id,
                    message=prompt_message,
                    ig_user_id=user["instagram_user_id"],
                    comment_id=comment_id,
                    buttons=follow_buttons,
                )

                await db.dm_logs.insert_one(
                    {
                        "user_id": str(user["_id"]),
                        "rule_id": str(rule["_id"]),
                        "recipient_ig_id": recipient_id,
                        "message_sent": prompt_message,
                        "trigger_type": trigger_type,
                        "status": "sent" if prompt_result["success"] else "failed",
                        "sent_at": datetime.now(timezone.utc),
                    }
                )

                if prompt_result["success"]:
                    now = datetime.now(timezone.utc)
                    await _ensure_contact_create_allowed(db, user, recipient_id)
                    await db.contacts.update_one(
                        {"user_id": str(user["_id"]), "ig_user_id": recipient_id},
                        {
                            "$set": {
                                "last_seen_at": now,
                                "last_triggered_rule_id": str(rule["_id"]),
                                "trigger_type": trigger_type,
                                "follow_gate_status": "awaiting",
                                "follow_gate_rule_id": str(rule["_id"]),
                                "follow_gate_trigger_type": trigger_type.value,
                                "follow_gate_prompted_at": now,
                                "is_following": False,
                            },
                            "$inc": {"dm_count": 1},
                            "$setOnInsert": {"user_id": str(user["_id"]), "ig_user_id": recipient_id, "first_seen_at": now},
                        },
                        upsert=True,
                    )
                    await db.users.update_one({"_id": user["_id"]}, {"$inc": {"dm_count_this_month": 1}})
                return

    # Check In-DM Email Capture (if enabled and not skipped)
    email_capture_requested = bool(rule.get("capture_email_enabled", False))
    if (
        not skip_email_capture
        and email_capture_requested
        and user_plan in {PlanType.Starter, PlanType.Pro}
    ):
        contact = await db.contacts.find_one({"user_id": str(user["_id"]), "ig_user_id": recipient_id})
        has_email = bool(contact and contact.get("captured_email"))
        is_awaiting_email = (
            bool(contact)
            and str(contact.get("email_capture_status") or "") == "awaiting"
            and str(contact.get("email_capture_rule_id") or "") == str(rule.get("_id"))
        )

        if not has_email and not is_awaiting_email:
            prompt_message = str(rule.get("email_capture_prompt") or "What's the best email address to send your link to? 📩").strip()
            prompt_message = _apply_plan_footer(prompt_message, user_plan)
            prompt_result = await InstagramService.send_dm(
                access_token=user["instagram_access_token"],
                recipient_ig_id=recipient_id,
                message=prompt_message,
                ig_user_id=user["instagram_user_id"],
                comment_id=comment_id,
            )

            await db.dm_logs.insert_one(
                {
                    "user_id": str(user["_id"]),
                    "rule_id": str(rule["_id"]),
                    "recipient_ig_id": recipient_id,
                    "message_sent": prompt_message,
                    "trigger_type": trigger_type,
                    "status": "sent" if prompt_result["success"] else "failed",
                    "sent_at": datetime.now(timezone.utc),
                }
            )

            if prompt_result["success"]:
                now = datetime.now(timezone.utc)
                await _ensure_contact_create_allowed(db, user, recipient_id)
                await db.contacts.update_one(
                    {"user_id": str(user["_id"]), "ig_user_id": recipient_id},
                    {
                        "$set": {
                            "last_seen_at": now,
                            "last_triggered_rule_id": str(rule["_id"]),
                            "trigger_type": trigger_type,
                            "email_capture_status": "awaiting",
                            "email_capture_rule_id": str(rule["_id"]),
                            "email_capture_trigger_type": trigger_type.value,
                            "email_capture_prompted_at": now,
                        },
                        "$inc": {"dm_count": 1},
                        "$setOnInsert": {"user_id": str(user["_id"]), "ig_user_id": recipient_id, "first_seen_at": now},
                    },
                    upsert=True,
                )
                await db.users.update_one({"_id": user["_id"]}, {"$inc": {"dm_count_this_month": 1}})
            return

    # Check In-DM Phone Capture (if enabled and not skipped)
    phone_capture_requested = bool(rule.get("capture_phone_enabled", False))
    if (
        not skip_phone_capture
        and phone_capture_requested
        and user_plan in {PlanType.Starter, PlanType.Pro}
    ):
        contact = await db.contacts.find_one({"user_id": str(user["_id"]), "ig_user_id": recipient_id})
        has_phone = bool(contact and contact.get("captured_phone"))
        is_awaiting_phone = (
            bool(contact)
            and str(contact.get("phone_capture_status") or "") == "awaiting"
            and str(contact.get("phone_capture_rule_id") or "") == str(rule.get("_id"))
        )

        if not has_phone and not is_awaiting_phone:
            prompt_message = str(rule.get("capture_phone_prompt") or "What's your WhatsApp or phone number so we can text you the details? 📱").strip()
            prompt_message = _apply_plan_footer(prompt_message, user_plan)
            prompt_result = await InstagramService.send_dm(
                access_token=user["instagram_access_token"],
                recipient_ig_id=recipient_id,
                message=prompt_message,
                ig_user_id=user["instagram_user_id"],
                comment_id=comment_id,
            )

            await db.dm_logs.insert_one(
                {
                    "user_id": str(user["_id"]),
                    "rule_id": str(rule["_id"]),
                    "recipient_ig_id": recipient_id,
                    "message_sent": prompt_message,
                    "trigger_type": trigger_type,
                    "status": "sent" if prompt_result["success"] else "failed",
                    "sent_at": datetime.now(timezone.utc),
                }
            )

            if prompt_result["success"]:
                now = datetime.now(timezone.utc)
                await _ensure_contact_create_allowed(db, user, recipient_id)
                await db.contacts.update_one(
                    {"user_id": str(user["_id"]), "ig_user_id": recipient_id},
                    {
                        "$set": {
                            "last_seen_at": now,
                            "last_triggered_rule_id": str(rule["_id"]),
                            "trigger_type": trigger_type,
                            "phone_capture_status": "awaiting",
                            "phone_capture_rule_id": str(rule["_id"]),
                            "phone_capture_trigger_type": trigger_type.value,
                            "phone_capture_prompted_at": now,
                        },
                        "$inc": {"dm_count": 1},
                        "$setOnInsert": {"user_id": str(user["_id"]), "ig_user_id": recipient_id, "first_seen_at": now},
                    },
                    upsert=True,
                )
                await db.users.update_one({"_id": user["_id"]}, {"$inc": {"dm_count_this_month": 1}})
            return

    # Optional simulated human jitter delay
    delay_secs = int(rule.get("reply_delay_seconds") or 0)
    if delay_secs > 0:
        await asyncio.sleep(min(delay_secs, 15))

    # Format DM buttons if configured
    dm_buttons = None
    if rule.get("dm_buttons"):
        dm_buttons = []
        for btn in rule.get("dm_buttons", []):
            b_type = str(btn.get("type") or "web_url").strip().lower()
            if b_type == "web_url" and btn.get("url"):
                dm_buttons.append({"type": "web_url", "url": btn["url"], "title": str(btn.get("title") or "Open Link")[:20]})
            elif b_type == "postback":
                dm_buttons.append({"type": "postback", "title": str(btn.get("title") or "Select")[:20], "payload": str(btn.get("payload") or btn.get("title") or "Select")[:100]})

    result = await InstagramService.send_dm(
        access_token=user["instagram_access_token"],
        recipient_ig_id=recipient_id,
        message=reply,
        ig_user_id=user["instagram_user_id"],
        attachment_url=str(rule.get("dm_attachment_url") or "").strip() or None,
        attachment_type=str(rule.get("dm_attachment_type") or "image").strip().lower() or "image",
        comment_id=comment_id,
        buttons=dm_buttons,
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

    # Assign contact tags if configured on rule
    rule_tags = [str(t).strip().lower() for t in (rule.get("add_contact_tags") or []) if str(t).strip()]
    if rule_tags:
        await db.contacts.update_one(
            {"user_id": str(user["_id"]), "ig_user_id": recipient_id},
            {"$addToSet": {"tags": {"$each": rule_tags}}},
            upsert=True,
        )

    if not result["success"] and result.get("status_code") == 401:
        logger.warning(
            "DM failed with 401/token error for user %s (%s). Flagging token as needing reauth.",
            user.get("_id"),
            result.get("error"),
        )
        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "instagram_access_token": None,
                    "instagram_user_id": None,
                    "instagram_account_ids": [],
                    "ig_token_expires_at": None,
                }
            },
        )

    if result["success"]:
        await db.users.update_one({"_id": user["_id"]}, {"$inc": {"dm_count_this_month": 1}})
        await db.automation_rules.update_one({"_id": rule["_id"]}, {"$inc": {"sent_count": 1}})
        now = datetime.now(timezone.utc)
        await _ensure_contact_create_allowed(db, user, recipient_id)
        await db.contacts.update_one(
            {"user_id": str(user["_id"]), "ig_user_id": recipient_id},
            {
                "$set": {"last_seen_at": now, "last_triggered_rule_id": str(rule["_id"]), "trigger_type": trigger_type},
                "$inc": {"dm_count": 1},
                "$setOnInsert": {"user_id": str(user["_id"]), "ig_user_id": recipient_id, "first_seen_at": now},
            },
            upsert=True,
        )


async def _process_webhook_payload(db, body: dict[str, Any], raw_body: bytes) -> dict[str, int]:
    # NOTE ON SCALING & DURABILITY:
    # When dispatched via FastAPI BackgroundTasks, this runs in-process on the active asyncio event loop.
    # This is non-durable (if the process restarts mid-task, that task is lost).
    # If PinGuru scales past a few hundred DMs/day, this should graduate to a real queue
    # (e.g. a simple Mongo-backed job collection polled by a worker loop, or Redis+arq if budget allows).
    try:
        entries = body.get("entry", [])
        logger.info(f"Webhook received: {len(raw_body)} bytes, {len(entries)} entries")

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

                sender_id = (messaging.get("sender") or {}).get("id")
                recipient_id = (messaging.get("recipient") or {}).get("id") or ig_id
                message_text = (messaging.get("message") or {}).get("text")
                logger.info("Messaging webhook event received: sender_id=%s, recipient_id=%s", sender_id, recipient_id)

                try:
                    await handle_messaging_event(db, str(recipient_id or ig_id), messaging)
                    processed_events += 1
                except Exception:
                    logger.exception(
                        "Failed to process messaging event in webhook: sender_id=%s, recipient_id=%s",
                        sender_id,
                        recipient_id,
                    )

            for change in change_events:
                event_key = _event_key_for_change(ig_id, change)
                if not await _mark_event_if_new(db, event_key, "change"):
                    deduped_events += 1
                    continue
                try:
                    await handle_change_event(db, ig_id, change)
                    processed_events += 1
                except Exception:
                    logger.exception("Failed to process change event in webhook: ig_id=%s", ig_id)

        return {"processed_events": processed_events, "deduped_events": deduped_events}
    except Exception:
        logger.exception("Failed to process webhook payload")
        return {"processed_events": 0, "deduped_events": 0}


@router.get("/instagram")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.META_WEBHOOK_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta")
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/instagram")
async def handle_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db=Depends(get_db),
):
    raw_body = await request.body()

    if settings.ENVIRONMENT.lower() == "production" and settings.DISABLE_WEBHOOK_SIGNATURE:
        raise HTTPException(status_code=503, detail="Webhook signature verification cannot be disabled in production")

    if not settings.DISABLE_WEBHOOK_SIGNATURE:
        signature = request.headers.get("X-Hub-Signature-256")
        _validate_signature(signature, raw_body)
    else:
        logger.warning("Webhook signature verification disabled in development")

    body = await request.json()
    database = db if db is not None else get_db()
    background_tasks.add_task(_process_webhook_payload, database, body, raw_body)
    return {"status": "ok"}


@router.post("/dev/simulate")
async def simulate_webhook(data: WebhookSimulateRequest, db=Depends(get_db)):
    if settings.ENVIRONMENT.lower() != "development":
        raise HTTPException(status_code=403, detail="Simulator is available only in development")

    payload_json = json.dumps(data.payload, separators=(",", ":"))
    raw_body = payload_json.encode("utf-8")
    signature_header = None
    if data.sign_with_app_secret:
        signature_header = f"sha256={_compute_signature(raw_body)}"

    result = {"processed_events": 0, "deduped_events": 0}
    if data.process_payload:
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
                                "from": {"id": "<customer_ig_id>", "username": "<customer_ig_username>"},
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
    message_obj = messaging.get("message") or {}
    postback_obj = messaging.get("postback") or {}
    has_content = bool(
        message_obj.get("text")
        or postback_obj.get("payload")
        or postback_obj.get("title")
        or (message_obj.get("quick_reply") or {}).get("payload")
    )

    if has_content:
        await handle_dm_event(db, ig_account_id, messaging)

    if _is_story_reply(messaging):
        await handle_story_reply_event(db, ig_account_id, messaging)


async def handle_change_event(db, ig_account_id: str, change: dict):
    field = str(change.get("field") or "")
    value = change.get("value") or {}

    if field == "comments":
        await handle_comment_event(db, ig_account_id, value)
    elif field == "mentions":
        await handle_story_mention_event(db, ig_account_id, value)


async def handle_dm_event(db, ig_account_id: str, messaging: dict):
    message_obj = messaging.get("message", {})
    postback_obj = messaging.get("postback", {})

    # Ignore echoes of our own sent messages
    if message_obj.get("is_echo"):
        logger.info("Ignoring echo message")
        return

    sender_id = messaging.get("sender", {}).get("id")
    raw_text = (
        postback_obj.get("payload")
        or (message_obj.get("quick_reply") or {}).get("payload")
        or message_obj.get("text")
        or postback_obj.get("title")
        or ""
    )
    message_text = raw_text.strip().lower()

    if not sender_id or not message_text:
        return

    logger.info("DM processing started: sender_id=%s", sender_id)

    if sender_id == ig_account_id:
        return

    user = await _find_user_for_ig_account(db, ig_account_id)
    if not user:
        return

    if not user.get("instagram_access_token") or not user.get("instagram_user_id"):
        logger.warning("Skipping DM automation due to missing Instagram credentials")
        return

    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    dm_limit = plan_limits.get("dm_limit")
    if dm_limit is not None and user.get("dm_count_this_month", 0) >= dm_limit:
        logger.warning("DM limit reached")
        return

    contact = await db.contacts.find_one({"user_id": str(user["_id"]), "ig_user_id": sender_id})
    if contact and str(contact.get("follow_gate_status") or "") == "awaiting" and _is_follow_confirmation_message(message_text):
        pending_rule_id = str(contact.get("follow_gate_rule_id") or "").strip()
        pending_trigger_raw = str(contact.get("follow_gate_trigger_type") or TriggerType.COMMENT.value)
        pending_trigger = TriggerType(pending_trigger_raw) if pending_trigger_raw in {t.value for t in TriggerType} else TriggerType.COMMENT

        # Fetch profile and verify real follower status from Instagram Graph API
        profile = await InstagramService.get_messaging_user_profile(
            user["instagram_access_token"], sender_id
        )
        is_following = profile.get("is_user_follow_business") if profile else None

        update_fields: dict[str, Any] = {"last_seen_at": datetime.now(timezone.utc)}
        if profile.get("username"):
            update_fields["ig_username"] = profile["username"]
        if profile.get("name"):
            update_fields["display_name"] = profile["name"]
        if isinstance(is_following, bool):
            update_fields["is_following"] = is_following

        # If Meta explicitly indicates the user is NOT following
        if is_following is False:
            logger.info(
                "Follow gate check rejected for sender_id=%s: user is NOT following %s",
                sender_id,
                user.get("instagram_username") or ig_account_id,
            )
            await db.contacts.update_one(
                {"_id": contact["_id"]},
                {"$set": update_fields},
            )
            reminder_msg = _build_follow_not_followed_reminder(user)
            reminder_msg = _apply_plan_footer(reminder_msg, user_plan)
            follow_buttons = _build_follow_buttons(user)
            prompt_res = await InstagramService.send_dm(
                access_token=user["instagram_access_token"],
                recipient_ig_id=sender_id,
                message=reminder_msg,
                ig_user_id=user["instagram_user_id"],
                buttons=follow_buttons,
            )
            await db.dm_logs.insert_one(
                {
                    "user_id": str(user["_id"]),
                    "rule_id": pending_rule_id,
                    "recipient_ig_id": sender_id,
                    "message_sent": reminder_msg,
                    "trigger_type": "follow_gate_reminder",
                    "status": "sent" if prompt_res.get("success") else "failed",
                    "sent_at": datetime.now(timezone.utc),
                }
            )
            if prompt_res.get("success"):
                await db.users.update_one({"_id": user["_id"]}, {"$inc": {"dm_count_this_month": 1}})
            return

        logger.info(
            "Follow gate check passed (is_following=%s) for sender_id=%s. Delivering rule reply.",
            is_following,
            sender_id,
        )

        if ObjectId.is_valid(pending_rule_id):
            pending_rule = await db.automation_rules.find_one(
                {
                    "_id": ObjectId(pending_rule_id),
                    "user_id": str(user["_id"]),
                    "is_active": True,
                }
            )
            if pending_rule:
                await db.automation_rules.update_one(
                    {"_id": pending_rule["_id"]},
                    {"$inc": {"follow_gate_completed_count": 1}},
                )
                await _send_rule_reply(
                    db,
                    user,
                    sender_id,
                    pending_rule,
                    pending_trigger,
                    skip_follow_gate=True,
                )

        now = datetime.now(timezone.utc)
        await db.contacts.update_one(
            {"_id": contact["_id"]},
            {
                "$set": {
                    "follow_gate_status": "completed",
                    "follow_gate_completed_at": now,
                    **update_fields,
                },
                "$addToSet": {
                    "completed_follow_gate_rule_ids": pending_rule_id,
                },
            },
        )
        return

    # Check awaiting email capture
    if contact and str(contact.get("email_capture_status") or "") == "awaiting":
        pending_rule_id = str(contact.get("email_capture_rule_id") or "").strip()
        email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', message_text)
        if email_match:
            captured_email = email_match.group(0).lower()
            now = datetime.now(timezone.utc)
            await db.contacts.update_one(
                {"_id": contact["_id"]},
                {
                    "$set": {
                        "email_capture_status": "captured",
                        "captured_email": captured_email,
                        "email_captured_at": now,
                    }
                },
            )
            if ObjectId.is_valid(pending_rule_id):
                pending_rule = await db.automation_rules.find_one(
                    {
                        "_id": ObjectId(pending_rule_id),
                        "user_id": str(user["_id"]),
                        "is_active": True,
                    }
                )
                if pending_rule:
                    await db.automation_rules.update_one(
                        {"_id": pending_rule["_id"]},
                        {"$inc": {"email_captured_count": 1}},
                    )
                    success_msg = str(pending_rule.get("email_capture_success_message") or "").strip()
                    if success_msg:
                        formatted_success = success_msg.replace("{{email}}", captured_email).replace("{email}", captured_email)
                        formatted_success = _apply_plan_footer(formatted_success, user_plan)
                        await InstagramService.send_dm(
                            access_token=user["instagram_access_token"],
                            recipient_ig_id=sender_id,
                            message=formatted_success,
                            ig_user_id=user["instagram_user_id"],
                        )
                    pending_trigger_raw = str(contact.get("email_capture_trigger_type") or TriggerType.COMMENT.value)
                    pending_trigger = TriggerType(pending_trigger_raw) if pending_trigger_raw in {t.value for t in TriggerType} else TriggerType.COMMENT
                    await _send_rule_reply(
                        db,
                        user,
                        sender_id,
                        pending_rule,
                        pending_trigger,
                        skip_follow_gate=True,
                        skip_email_capture=True,
                    )
            return
        else:
            retry_msg = "Please reply with a valid email address (e.g. name@example.com) to receive your link! 📩"
            retry_msg = _apply_plan_footer(retry_msg, user_plan)
            await InstagramService.send_dm(
                access_token=user["instagram_access_token"],
                recipient_ig_id=sender_id,
                message=retry_msg,
                ig_user_id=user["instagram_user_id"],
            )
            return

    # Check awaiting phone capture
    if contact and str(contact.get("phone_capture_status") or "") == "awaiting":
        pending_rule_id = str(contact.get("phone_capture_rule_id") or "").strip()
        phone_match = re.search(r'(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}|\+?\d{10,15}', raw_text)
        if phone_match:
            captured_phone = re.sub(r'[^\d+]', '', phone_match.group(0))
            now = datetime.now(timezone.utc)
            await db.contacts.update_one(
                {"_id": contact["_id"]},
                {
                    "$set": {
                        "phone_capture_status": "captured",
                        "captured_phone": captured_phone,
                        "phone_captured_at": now,
                    }
                },
            )
            if ObjectId.is_valid(pending_rule_id):
                pending_rule = await db.automation_rules.find_one(
                    {
                        "_id": ObjectId(pending_rule_id),
                        "user_id": str(user["_id"]),
                        "is_active": True,
                    }
                )
                if pending_rule:
                    await db.automation_rules.update_one(
                        {"_id": pending_rule["_id"]},
                        {"$inc": {"phone_captured_count": 1}},
                    )
                    success_msg = str(pending_rule.get("capture_phone_success_message") or "").strip()
                    if success_msg:
                        formatted_success = success_msg.replace("{{phone}}", captured_phone).replace("{phone}", captured_phone)
                        formatted_success = _apply_plan_footer(formatted_success, user_plan)
                        await InstagramService.send_dm(
                            access_token=user["instagram_access_token"],
                            recipient_ig_id=sender_id,
                            message=formatted_success,
                            ig_user_id=user["instagram_user_id"],
                        )
                    pending_trigger_raw = str(contact.get("phone_capture_trigger_type") or TriggerType.COMMENT.value)
                    pending_trigger = TriggerType(pending_trigger_raw) if pending_trigger_raw in {t.value for t in TriggerType} else TriggerType.COMMENT
                    await _send_rule_reply(
                        db,
                        user,
                        sender_id,
                        pending_rule,
                        pending_trigger,
                        skip_follow_gate=True,
                        skip_email_capture=True,
                        skip_phone_capture=True,
                    )
            return
        else:
            retry_msg = "Please reply with a valid phone or WhatsApp number to receive your details! 📱"
            retry_msg = _apply_plan_footer(retry_msg, user_plan)
            await InstagramService.send_dm(
                access_token=user["instagram_access_token"],
                recipient_ig_id=sender_id,
                message=retry_msg,
                ig_user_id=user["instagram_user_id"],
            )
            return

    dm_rules_query = {
        "user_id": str(user["_id"]),
        "is_active": True,
        "trigger_type": {"$in": [TriggerType.KEYWORD, TriggerType.NEW_DM]},
    }
    logger.info(f"DM rules Mongo query: {json.dumps(dm_rules_query, default=str)}")

    rules = await db.automation_rules.find(dm_rules_query).to_list(100)

    logger.info(
        f"DM rules fetched: count={len(rules)}, sender_id={sender_id}, rule_ids={[str(rule.get('_id')) for rule in rules]}"
    )

    def _rule_trigger_val(r):
        tt = r.get("trigger_type")
        return tt.value if hasattr(tt, "value") else str(tt or "")

    keyword_rules = [r for r in rules if _rule_trigger_val(r) != TriggerType.NEW_DM.value]
    new_dm_rules = [r for r in rules if _rule_trigger_val(r) == TriggerType.NEW_DM.value]

    matched_keyword_rule = False
    for rule in keyword_rules:
        keywords = [k for k in rule.get("keywords", []) if k]
        match_mode = str(rule.get("match_mode") or "contains").lower()

        logger.info(
            "DM keyword match run: sender_id=%s, rule_id=%s, match_mode=%s",
            sender_id,
            rule.get("_id"),
            match_mode,
        )

        is_match, matched_kw = _evaluate_keyword_match(message_text, keywords, match_mode, user_plan)

        logger.info("DM keyword match result: sender_id=%s, rule_id=%s, is_match=%s, matched_kw=%s", sender_id, rule.get("_id"), is_match, matched_kw)

        if is_match:
            matched_keyword_rule = True
            await db.automation_rules.update_one({"_id": rule["_id"]}, {"$inc": {"triggers_count": 1}})
            await _send_rule_reply(db, user, sender_id, rule, TriggerType.KEYWORD, matched_keyword=matched_kw)
            break

    # If no keyword rule matched, check for NEW_DM (welcome/default reply) rules
    if not matched_keyword_rule and new_dm_rules:
        new_dm_rule = new_dm_rules[0]
        await db.automation_rules.update_one({"_id": new_dm_rule["_id"]}, {"$inc": {"triggers_count": 1}})
        logger.info("Triggering NEW_DM rule %s for sender_id=%s", new_dm_rule.get("_id"), sender_id)
        await _send_rule_reply(db, user, sender_id, new_dm_rule, TriggerType.NEW_DM)
        
async def handle_story_mention_event(db, ig_account_id: str, value: dict):
    # Meta sends: {"media_id": "...", "comment_id": "...", "from": {"id": "..."}}
    sender_id = (value.get("from") or {}).get("id")
    if not sender_id:
        return

    user = await _find_user_for_ig_account(db, ig_account_id)
    if not user or not user.get("instagram_access_token"):
        return

    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    dm_limit = plan_limits.get("dm_limit")
    if dm_limit is not None and user.get("dm_count_this_month", 0) >= dm_limit:
        return

    rules = await db.automation_rules.find({
        "user_id": str(user["_id"]),
        "is_active": True,
        "trigger_type": {"$in": [
            TriggerType.STORY_MENTION.value,
            TriggerType.STORY_REPLY.value,
            TriggerType.STORY_MENTION,
            TriggerType.STORY_REPLY,
        ]},
    }).to_list(100)

    mention_rules = [r for r in rules if str(r.get("trigger_type")) in {TriggerType.STORY_MENTION.value, str(TriggerType.STORY_MENTION)}]
    target_rule = mention_rules[0] if mention_rules else (rules[0] if rules else None)

    if target_rule:
        matched_kw = (target_rule.get("keywords") or ["story_mention"])[0]
        actual_tt = TriggerType.STORY_MENTION if str(target_rule.get("trigger_type")) in {TriggerType.STORY_MENTION.value, str(TriggerType.STORY_MENTION)} else TriggerType.STORY_REPLY
        await db.automation_rules.update_one({"_id": target_rule["_id"]}, {"$inc": {"triggers_count": 1}})
        await _send_rule_reply(db, user, sender_id, target_rule, actual_tt, matched_keyword=matched_kw)

async def handle_story_reply_event(db, ig_account_id: str, messaging: dict):
    sender_id = (messaging.get("sender") or {}).get("id")
    message_text = ((messaging.get("message") or {}).get("text") or "").lower()

    if not sender_id:
        return

    user = await _find_user_for_ig_account(db, ig_account_id)
    if not user:
        return

    if not user.get("instagram_access_token") or not user.get("instagram_user_id"):
        return

    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    dm_limit = plan_limits.get("dm_limit")
    if dm_limit is not None and user.get("dm_count_this_month", 0) >= dm_limit:
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
            matched_kw = next((kw for kw in (rule.get("keywords") or []) if kw.lower() in message_text), "")
            if not matched_kw and rule.get("keywords"):
                matched_kw = rule["keywords"][0]
            if not matched_kw and (messaging.get("message") or {}).get("text"):
                first_word = ((messaging.get("message") or {}).get("text") or "").strip().split()[0]
                matched_kw = first_word
            await _send_rule_reply(db, user, sender_id, rule, TriggerType.STORY_REPLY, matched_keyword=matched_kw)
            break


async def handle_comment_event(db, ig_account_id: str, value: dict):
    from_obj = value.get("from", {}) or {}
    commenter_id = from_obj.get("id")
    commenter_username = str(from_obj.get("username") or "").strip()
    commenter_name = str(from_obj.get("name") or "").strip()
    raw_comment_text = str(value.get("text", "") or "")
    comment_text = raw_comment_text.lower()
    comment_id = str(value.get("comment_id") or value.get("id") or "").strip()
    media_id, media_kind = _extract_comment_media_context(value)

    if not commenter_id or not comment_text:
        return

    user = await _find_user_for_ig_account(db, ig_account_id)
    if not user:
        return

    if not user.get("instagram_access_token") or not user.get("instagram_user_id"):
        logger.warning("Skipping comment automation due to missing Instagram credentials")
        return

    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    dm_limit = plan_limits.get("dm_limit")
    if dm_limit is not None and user.get("dm_count_this_month", 0) >= dm_limit:
        return

    rules = await db.automation_rules.find(
        {
            "user_id": str(user["_id"]),
            "is_active": True,
            "trigger_type": {"$in": [TriggerType.COMMENT, TriggerType.POST_COMMENT, TriggerType.REEL_COMMENT]},
        }
    ).to_list(100)

    for rule in rules:
        if not _comment_rule_matches(rule, media_id, media_kind):
            continue

        any_comment_keyword = bool(rule.get("any_comment_keyword", True))
        rule_keywords = [k for k in rule.get("keywords", []) if k]
        match_mode = str(rule.get("match_mode") or "contains").lower()
        keyword_match, matched_kw = _evaluate_keyword_match(raw_comment_text, rule_keywords, match_mode, user_plan)

        if not keyword_match and any_comment_keyword:
            keyword_match = True
            if rule_keywords:
                matched_kw = rule_keywords[0]
            else:
                first_word = raw_comment_text.strip().split()[0] if raw_comment_text.strip() else ""
                matched_kw = first_word.strip("!?. ,#")

        if keyword_match:
            raw_trigger = str(rule.get("trigger_type") or TriggerType.POST_COMMENT)
            trigger_type = TriggerType(raw_trigger) if raw_trigger in {t.value for t in TriggerType} else TriggerType.POST_COMMENT

            await db.automation_rules.update_one({"_id": rule["_id"]}, {"$inc": {"triggers_count": 1}})

            # Enrich contact record with username/display_name directly from comment webhook payload
            if commenter_username or commenter_name:
                contact_update = {}
                display_val = commenter_name or commenter_username
                if display_val:
                    contact_update["display_name"] = display_val
                if commenter_username:
                    contact_update["ig_username"] = commenter_username
                now = datetime.now(timezone.utc)
                await db.contacts.update_one(
                    {"user_id": str(user["_id"]), "ig_user_id": commenter_id},
                    {
                        "$set": contact_update,
                        "$setOnInsert": {
                            "user_id": str(user["_id"]),
                            "ig_user_id": commenter_id,
                            "first_seen_at": now,
                        },
                    },
                    upsert=True,
                )

            # 1. Public comment reply (if enabled)
            if bool(rule.get("public_comment_reply_enabled", False)) and comment_id:
                templates = list(rule.get("public_comment_reply_templates") or [])
                if not templates and rule.get("public_comment_reply_template"):
                    templates = [rule["public_comment_reply_template"]]
                if templates:
                    import random
                    chosen_template = random.choice(templates)
                    commenter_handle = commenter_username or commenter_id
                    public_reply = _render_comment_template(chosen_template, commenter_handle, raw_comment_text)
                    if public_reply:
                        await InstagramService.reply_to_comment(
                            access_token=user["instagram_access_token"],
                            comment_id=comment_id,
                            message=public_reply,
                        )

            # 2. Smart Anti-Spam Cooldown check for In-DM delivery
            cooldown_hours = int(rule.get("comment_cooldown_hours") or 0)
            skip_dm_cooldown = False
            if cooldown_hours > 0 and media_id:
                contact = await db.contacts.find_one({"user_id": str(user["_id"]), "ig_user_id": commenter_id})
                if contact:
                    comment_dm_hist = contact.get("comment_dm_history", {}) or {}
                    media_key = str(media_id).replace(".", "_")
                    last_dm_time = comment_dm_hist.get(media_key)
                    if last_dm_time:
                        if isinstance(last_dm_time, str):
                            try:
                                last_dm_time = datetime.fromisoformat(last_dm_time.replace("Z", "+00:00"))
                            except Exception:
                                last_dm_time = None
                        if last_dm_time:
                            diff_seconds = (datetime.now(timezone.utc) - last_dm_time).total_seconds()
                            if diff_seconds < cooldown_hours * 3600:
                                skip_dm_cooldown = True
                                logger.info(
                                    "Comment DM skipped for commenter=%s on media=%s due to anti-spam cooldown (%s hrs)",
                                    commenter_id,
                                    media_id,
                                    cooldown_hours,
                                )

            if not skip_dm_cooldown:
                if not matched_kw and rule.get("keywords"):
                    matched_kw = rule["keywords"][0]
                if not matched_kw:
                    first_word = raw_comment_text.strip().split()[0] if raw_comment_text.strip() else ""
                    matched_kw = first_word

                await _send_rule_reply(
                    db,
                    user,
                    commenter_id,
                    rule,
                    trigger_type,
                    matched_keyword=matched_kw,
                    comment_id=comment_id,
                )

                if media_id:
                    media_key = str(media_id).replace(".", "_")
                    now = datetime.now(timezone.utc)
                    await db.contacts.update_one(
                        {"user_id": str(user["_id"]), "ig_user_id": commenter_id},
                        {"$set": {f"comment_dm_history.{media_key}": now}},
                        upsert=True,
                    )
            break