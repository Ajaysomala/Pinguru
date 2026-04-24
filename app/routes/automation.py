import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from app.database import get_db
from app.models.models import (
    AutomationRuleCreate,
    CommentMediaFilterType,
    CommentTargetType,
    PlanType,
    TriggerType,
    get_plan_limits,
    get_plan_type,
)
from app.routes.auth import get_current_user
from bson import ObjectId

router = APIRouter()


def _normalize_match_mode(match_mode: str | None) -> str:
    mode = (match_mode or "exact").strip().lower()
    return "hinglish" if mode == "hinglish" else "exact"


def _resolve_match_mode_for_plan(user_plan: PlanType, requested_mode: str | None) -> str:
    requested = _normalize_match_mode(requested_mode)
    if user_plan == PlanType.Pro:
        return requested
    return "exact"


def _sanitize_text(value: str) -> str:
    # Remove script blocks first, then strip all remaining HTML tags.
    no_scripts = re.sub(r"<script[^>]*>.*?</script>", "", value, flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r"<[^>]+>", "", no_scripts).strip()


def _sanitize_and_validate_rule_payload(data: AutomationRuleCreate) -> tuple[str, str, list[str]]:
    name = _sanitize_text(data.name)
    reply_message = _sanitize_text(data.reply_message)
    keywords = [_sanitize_text(k) for k in data.keywords if _sanitize_text(k)]

    if len(name) > 100:
        raise HTTPException(status_code=422, detail="Rule name must be 100 characters or fewer")
    if len(reply_message) > 1000:
        raise HTTPException(status_code=422, detail="Reply message must be 1000 characters or fewer")
    if len(keywords) > 20:
        raise HTTPException(status_code=422, detail="Maximum 20 keywords allowed")
    if any(len(keyword) > 50 for keyword in keywords):
        raise HTTPException(status_code=422, detail="Each keyword must be 50 characters or fewer")

    return name, reply_message, keywords


def _optional_text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _normalize_comment_target_type(value: str | CommentTargetType | None) -> str | None:
    if value is None:
        return None
    normalized = value.value if isinstance(value, CommentTargetType) else str(value)
    normalized = normalized.strip().lower()
    if normalized in {"specific", "any"}:
        return normalized
    return None


def _sanitize_public_comment_reply_template(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _sanitize_text(value)
    if not cleaned:
        return None
    if len(cleaned) > 300:
        raise HTTPException(status_code=422, detail="Public comment reply must be 300 characters or fewer")
    return cleaned


def _sanitize_attachment_url(value: str | None) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    if len(text) > 2048:
        raise HTTPException(status_code=422, detail="Attachment URL must be 2048 characters or fewer")
    if not re.match(r"^https://", text, flags=re.IGNORECASE):
        raise HTTPException(status_code=422, detail="Attachment URL must use https")
    return text


def _require_comment_pro_features(user_plan: PlanType, enabled: bool, field_name: str) -> None:
    if enabled and user_plan != PlanType.Pro:
        raise HTTPException(status_code=403, detail=f"{field_name} is available on the Pro plan only")


def _require_comment_starter_or_pro_features(user_plan: PlanType, enabled: bool, field_name: str) -> None:
    if enabled and user_plan not in {PlanType.Starter, PlanType.Pro}:
        raise HTTPException(status_code=403, detail=f"{field_name} is available on Starter and Pro plans only")


def _reject_follow_up_feature(enabled: bool) -> None:
    if enabled:
        raise HTTPException(status_code=403, detail="Follow-up automation is not part of the current plan contract")


def _normalize_comment_media_filter(value: str | CommentMediaFilterType | None) -> str:
    if value is None:
        return "all"
    normalized = value.value if isinstance(value, CommentMediaFilterType) else str(value)
    normalized = normalized.strip().lower()
    if normalized in {"post", "reel", "all"}:
        return normalized
    return "all"


def _normalize_trigger_type(value: TriggerType | str) -> str:
    trigger = value.value if isinstance(value, TriggerType) else str(value)
    trigger = trigger.strip().lower()
    if trigger in {t.value for t in TriggerType}:
        return trigger
    return TriggerType.KEYWORD.value


def _comment_default_filter(trigger_type: str) -> str:
    if trigger_type == TriggerType.POST_COMMENT.value:
        return "post"
    if trigger_type == TriggerType.REEL_COMMENT.value:
        return "reel"
    return "all"


def _serialize_rule(rule: dict) -> dict:
    trigger_type = _normalize_trigger_type(rule.get("trigger_type", TriggerType.KEYWORD.value))
    comment_target_type = _normalize_comment_target_type(rule.get("comment_target_type"))
    comment_media_filter = _normalize_comment_media_filter(rule.get("comment_media_filter") or _comment_default_filter(trigger_type))

    serialized = dict(rule)
    serialized["trigger_type"] = trigger_type
    serialized["comment_target_type"] = comment_target_type if trigger_type in {TriggerType.COMMENT.value, TriggerType.POST_COMMENT.value, TriggerType.REEL_COMMENT.value} else None
    if serialized["comment_target_type"] is None and trigger_type in {TriggerType.COMMENT.value, TriggerType.POST_COMMENT.value, TriggerType.REEL_COMMENT.value}:
        serialized["comment_target_type"] = CommentTargetType.ANY.value
    serialized["comment_media_filter"] = comment_media_filter if trigger_type in {TriggerType.COMMENT.value, TriggerType.POST_COMMENT.value, TriggerType.REEL_COMMENT.value} else "all"
    serialized["comment_media_id"] = (rule.get("comment_media_id") or "").strip() or None
    serialized["comment_media_permalink"] = (rule.get("comment_media_permalink") or "").strip() or None
    serialized["comment_media_caption"] = (rule.get("comment_media_caption") or "").strip() or None
    serialized["comment_media_type"] = (rule.get("comment_media_type") or "").strip() or None
    serialized["dm_attachment_url"] = (rule.get("dm_attachment_url") or "").strip() or None
    serialized["dm_attachment_type"] = (rule.get("dm_attachment_type") or "").strip() or None
    serialized["any_comment_keyword"] = bool(rule.get("any_comment_keyword", True))
    serialized["public_comment_reply_enabled"] = bool(rule.get("public_comment_reply_enabled", False))
    serialized["public_comment_reply_template"] = (rule.get("public_comment_reply_template") or "").strip() or None
    serialized["ask_follow_before_dm"] = bool(rule.get("ask_follow_before_dm", False))
    serialized["send_follow_up_message"] = bool(rule.get("send_follow_up_message", False))
    if "created_at" in serialized and hasattr(serialized["created_at"], "isoformat"):
        serialized["created_at"] = serialized["created_at"].isoformat()
    return serialized

@router.post("/rules")
async def create_rule(data: AutomationRuleCreate, db=Depends(get_db), user=Depends(get_current_user)):
    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    plan_limits = get_plan_limits(user_plan)
    match_mode = _resolve_match_mode_for_plan(user_plan, data.match_mode)
    name, reply_message, keywords = _sanitize_and_validate_rule_payload(data)
    trigger_type = _normalize_trigger_type(data.trigger_type)
    existing = await db.automation_rules.count_documents({"user_id": str(user["_id"])})
    if plan_limits["rules"] is not None and existing >= plan_limits["rules"]:
        raise HTTPException(status_code=403, detail=f"Rule limit reached. Upgrade your plan.")

    is_comment_rule = trigger_type in {TriggerType.COMMENT.value, TriggerType.POST_COMMENT.value, TriggerType.REEL_COMMENT.value}
    is_new_dm_rule = trigger_type == TriggerType.NEW_DM.value
    comment_target_type = _normalize_comment_target_type(data.comment_target_type) if is_comment_rule else None
    if is_comment_rule and comment_target_type is None:
        comment_target_type = CommentTargetType.ANY.value
    comment_media_filter = _normalize_comment_media_filter(data.comment_media_filter) if is_comment_rule else "all"
    comment_media_id = _optional_text(data.comment_media_id) if is_comment_rule else None
    comment_media_permalink = _optional_text(data.comment_media_permalink) if is_comment_rule else None
    comment_media_caption = _optional_text(data.comment_media_caption) if is_comment_rule else None
    comment_media_type = _optional_text(data.comment_media_type) if is_comment_rule else None
    dm_attachment_url = _sanitize_attachment_url(data.dm_attachment_url) if is_comment_rule else None
    dm_attachment_type = (data.dm_attachment_type or "").strip().lower() if is_comment_rule and data.dm_attachment_type else None
    any_comment_keyword = bool(data.any_comment_keyword) if is_comment_rule and data.any_comment_keyword is not None else (True if is_comment_rule else False)
    public_comment_reply_enabled = bool(data.public_comment_reply_enabled) if is_comment_rule else False
    public_comment_reply_template = _sanitize_public_comment_reply_template(data.public_comment_reply_template) if is_comment_rule else None
    ask_follow_before_dm = bool(data.ask_follow_before_dm) if is_comment_rule else False
    send_follow_up_message = False

    if dm_attachment_type and dm_attachment_type not in {"image"}:
        raise HTTPException(status_code=422, detail="Only image attachments are supported for DM attachments")

    _require_comment_pro_features(user_plan, bool(dm_attachment_url), "DM image attachments")

    _require_comment_pro_features(user_plan, public_comment_reply_enabled, "Public comment reply")
    _require_comment_starter_or_pro_features(user_plan, ask_follow_before_dm, "Follow-before-DM automation")
    _reject_follow_up_feature(bool(data.send_follow_up_message))

    if is_comment_rule and not any_comment_keyword and len(keywords) == 0:
        raise HTTPException(status_code=422, detail="Add at least one keyword or enable any_comment_keyword")

    if is_new_dm_rule and len(keywords) > 0:
        # Keep new-DM automations simple: they fire on every incoming DM.
        keywords = []

    if is_comment_rule and public_comment_reply_enabled and not public_comment_reply_template:
        raise HTTPException(status_code=422, detail="public_comment_reply_template is required when public_comment_reply_enabled is true")

    rule_doc = {
        "user_id": str(user["_id"]),
        "name": name,
        "trigger_type": trigger_type,
        "keywords": keywords,
        "match_mode": match_mode,
        "reply_message": reply_message,
        "comment_target_type": comment_target_type,
        "comment_media_filter": comment_media_filter,
        "comment_media_id": comment_media_id,
        "comment_media_permalink": comment_media_permalink,
        "comment_media_caption": comment_media_caption,
        "comment_media_type": comment_media_type,
        "dm_attachment_url": dm_attachment_url,
        "dm_attachment_type": dm_attachment_type,
        "any_comment_keyword": any_comment_keyword,
        "public_comment_reply_enabled": public_comment_reply_enabled,
        "public_comment_reply_template": public_comment_reply_template,
        "ask_follow_before_dm": ask_follow_before_dm,
        "send_follow_up_message": send_follow_up_message,
        "is_active": True,
        "sent_count": 0,
        "created_at": datetime.now(timezone.utc),
    }
    result = await db.automation_rules.insert_one(rule_doc)
    rule_doc["_id"] = str(result.inserted_id)
    return {"rule": _serialize_rule(rule_doc)}

@router.get("/rules")
async def list_rules(db=Depends(get_db), user=Depends(get_current_user)):
    rules = await db.automation_rules.find({"user_id": str(user["_id"])}).sort("created_at", -1).to_list(100)
    for r in rules:
        r["_id"] = str(r["_id"])
    return {"rules": [_serialize_rule(r) for r in rules]}

@router.patch("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    if not ObjectId.is_valid(rule_id):
        raise HTTPException(status_code=400, detail="Invalid rule ID")
    rule_object_id = ObjectId(rule_id)
    rule = await db.automation_rules.find_one({"_id": rule_object_id, "user_id": str(user["_id"])})
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    new_status = not rule["is_active"]
    await db.automation_rules.update_one({"_id": rule_object_id}, {"$set": {"is_active": new_status}})
    return {"rule_id": rule_id, "is_active": new_status}

@router.put("/rules/{rule_id}")
async def update_rule(rule_id: str, data: AutomationRuleCreate, db=Depends(get_db), user=Depends(get_current_user)):
    if not ObjectId.is_valid(rule_id):
        raise HTTPException(status_code=400, detail="Invalid rule ID")
    rule_object_id = ObjectId(rule_id)
    user_plan = get_plan_type(user.get("plan", PlanType.Free))
    match_mode = _resolve_match_mode_for_plan(user_plan, data.match_mode)
    name, reply_message, keywords = _sanitize_and_validate_rule_payload(data)
    trigger_type = _normalize_trigger_type(data.trigger_type)
    is_comment_rule = trigger_type in {TriggerType.COMMENT.value, TriggerType.POST_COMMENT.value, TriggerType.REEL_COMMENT.value}
    is_new_dm_rule = trigger_type == TriggerType.NEW_DM.value
    comment_target_type = _normalize_comment_target_type(data.comment_target_type) if is_comment_rule else None
    if is_comment_rule and comment_target_type is None:
        comment_target_type = CommentTargetType.ANY.value
    comment_media_filter = _normalize_comment_media_filter(data.comment_media_filter) if is_comment_rule else "all"
    comment_media_id = _optional_text(data.comment_media_id) if is_comment_rule else None
    comment_media_permalink = _optional_text(data.comment_media_permalink) if is_comment_rule else None
    comment_media_caption = _optional_text(data.comment_media_caption) if is_comment_rule else None
    comment_media_type = _optional_text(data.comment_media_type) if is_comment_rule else None
    dm_attachment_url = _sanitize_attachment_url(data.dm_attachment_url) if is_comment_rule else None
    dm_attachment_type = (data.dm_attachment_type or "").strip().lower() if is_comment_rule and data.dm_attachment_type else None
    any_comment_keyword = bool(data.any_comment_keyword) if is_comment_rule and data.any_comment_keyword is not None else (True if is_comment_rule else False)
    public_comment_reply_enabled = bool(data.public_comment_reply_enabled) if is_comment_rule else False
    public_comment_reply_template = _sanitize_public_comment_reply_template(data.public_comment_reply_template) if is_comment_rule else None
    ask_follow_before_dm = bool(data.ask_follow_before_dm) if is_comment_rule else False
    send_follow_up_message = False

    if dm_attachment_type and dm_attachment_type not in {"image"}:
        raise HTTPException(status_code=422, detail="Only image attachments are supported for DM attachments")

    _require_comment_pro_features(user_plan, bool(dm_attachment_url), "DM image attachments")

    _require_comment_pro_features(user_plan, public_comment_reply_enabled, "Public comment reply")
    _require_comment_starter_or_pro_features(user_plan, ask_follow_before_dm, "Follow-before-DM automation")
    _reject_follow_up_feature(bool(data.send_follow_up_message))

    if is_comment_rule and not any_comment_keyword and len(keywords) == 0:
        raise HTTPException(status_code=422, detail="Add at least one keyword or enable any_comment_keyword")

    if is_new_dm_rule:
        keywords = []

    if is_comment_rule and public_comment_reply_enabled and not public_comment_reply_template:
        raise HTTPException(status_code=422, detail="public_comment_reply_template is required when public_comment_reply_enabled is true")

    result = await db.automation_rules.update_one(
        {"_id": rule_object_id, "user_id": str(user["_id"])},
        {"$set": {
            "name": name,
            "trigger_type": trigger_type,
            "keywords": keywords,
            "match_mode": match_mode,
            "reply_message": reply_message,
            "comment_target_type": comment_target_type,
            "comment_media_filter": comment_media_filter,
            "comment_media_id": comment_media_id,
            "comment_media_permalink": comment_media_permalink,
            "comment_media_caption": comment_media_caption,
            "comment_media_type": comment_media_type,
            "dm_attachment_url": dm_attachment_url,
            "dm_attachment_type": dm_attachment_type,
            "any_comment_keyword": any_comment_keyword,
            "public_comment_reply_enabled": public_comment_reply_enabled,
            "public_comment_reply_template": public_comment_reply_template,
            "ask_follow_before_dm": ask_follow_before_dm,
            "send_follow_up_message": send_follow_up_message,
        }}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    updated_rule = await db.automation_rules.find_one({"_id": rule_object_id, "user_id": str(user["_id"])} )
    if not updated_rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    updated_rule["_id"] = str(updated_rule["_id"])
    return {"updated": True, "rule": _serialize_rule(updated_rule)}

@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: str, db=Depends(get_db), user=Depends(get_current_user)):
    if not ObjectId.is_valid(rule_id):
        raise HTTPException(status_code=400, detail="Invalid rule ID")
    rule_object_id = ObjectId(rule_id)
    result = await db.automation_rules.delete_one({"_id": rule_object_id, "user_id": str(user["_id"])})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"deleted": True}
