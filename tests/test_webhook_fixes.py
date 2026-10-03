import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import ObjectId

from app.models.models import PlanType, TriggerType
from app.routes.webhook import (
    _ensure_contact_create_allowed,
    _extract_comment_media_context,
    _is_follow_confirmation_message,
    _send_rule_reply,
    handle_comment_event,
    handle_dm_event,
    handle_messaging_event,
    handle_story_mention_event,
)
from app.services.instagram import InstagramService


# These tests call DM handlers directly, bypassing the inbound webhook that opens
# Meta's 24h messaging window (see conftest.open_messaging_window).
pytestmark = pytest.mark.usefixtures("open_messaging_window")


class _MockCollection:
    def __init__(self, data=None):
        self.data = list(data or [])
        self.inserts = []
        self.updates = []

    def _matches_filter(self, doc, query):
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches_filter(doc, clause) for clause in v):
                    return False
            elif isinstance(v, dict):
                field_val = doc.get(k)
                for op, op_val in v.items():
                    if op == "$in" and field_val not in op_val:
                        return False
                    if op == "$ne" and field_val == op_val:
                        return False
                    if op == "$exists":
                        exists = k in doc and doc[k] is not None
                        if exists != op_val:
                            return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    async def find_one(self, query):
        for item in self.data:
            if self._matches_filter(item, query):
                return dict(item)
        return None

    async def insert_one(self, doc):
        doc_copy = dict(doc)
        if "_id" not in doc_copy:
            doc_copy["_id"] = ObjectId()
        self.inserts.append(doc_copy)
        self.data.append(doc_copy)
        return SimpleNamespace(inserted_id=doc_copy["_id"])

    async def update_one(self, filter_query, update_query, upsert=False):
        self.updates.append((filter_query, update_query))
        existing = await self.find_one(filter_query)
        if existing:
            for i, d in enumerate(self.data):
                if d.get("_id") == existing.get("_id"):
                    if "$set" in update_query:
                        for sk, sv in update_query["$set"].items():
                            if "." in sk:
                                parts = sk.split(".")
                                cur = self.data[i]
                                for p in parts[:-1]:
                                    cur = cur.setdefault(p, {})
                                cur[parts[-1]] = sv
                            else:
                                self.data[i][sk] = sv
                    if "$inc" in update_query:
                        for ik, iv in update_query["$inc"].items():
                            self.data[i][ik] = self.data[i].get(ik, 0) + iv
                    if "$addToSet" in update_query:
                        for ak, av in update_query["$addToSet"].items():
                            cur = self.data[i].setdefault(ak, [])
                            if av not in cur:
                                cur.append(av)
                    break
            return SimpleNamespace(matched_count=1)
        elif upsert:
            new_doc = dict(filter_query)
            if "$set" in update_query:
                for sk, sv in update_query["$set"].items():
                    if "." in sk:
                        parts = sk.split(".")
                        cur = new_doc
                        for p in parts[:-1]:
                            cur = cur.setdefault(p, {})
                        cur[parts[-1]] = sv
                    else:
                        new_doc[sk] = sv
            if "$setOnInsert" in update_query:
                new_doc.update(update_query["$setOnInsert"])
            if "$inc" in update_query:
                for ik, iv in update_query["$inc"].items():
                    new_doc[ik] = new_doc.get(ik, 0) + iv
            if "$addToSet" in update_query:
                for ak, av in update_query["$addToSet"].items():
                    cur = new_doc.setdefault(ak, [])
                    if av not in cur:
                        cur.append(av)
            new_doc["_id"] = ObjectId()
            self.data.append(new_doc)
            return SimpleNamespace(matched_count=0, upserted_id=new_doc["_id"])
        return SimpleNamespace(matched_count=0)

    def find(self, query):
        matching = [dict(item) for item in self.data if self._matches_filter(item, query)]
        return _MockCursor(matching)

    async def count_documents(self, query):
        return len([item for item in self.data if self._matches_filter(item, query)])


class _MockCursor:
    def __init__(self, items):
        self.items = items

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, _length):
        return list(self.items)


# ==============================================================================
# FIX 1: _send_rule_reply guards `contact` None before contact.get(...)
# ==============================================================================
def test_send_rule_reply_guards_contact_none(monkeypatch):
    """Verify that when contact is None, _send_rule_reply does not crash with AttributeError."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
        "instagram_username": "my_brand",
    }
    rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "name": "Follow Gate Rule",
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["vip"],
        "reply_message": "VIP content",
        "ask_follow_before_dm": True,
        "is_active": True,
    }

    # DB with NO contact for recipient "new_fan"
    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([]),
        automation_rules=_MockCollection([rule]),
        dm_logs=_MockCollection([]),
    )

    sent = []
    async def fake_send_dm(access_token, recipient_ig_id, message, **kwargs):
        sent.append({"recipient": recipient_ig_id, "message": message, "buttons": kwargs.get("buttons")})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "New Fan", "username": "new_fan", "is_user_follow_business": False}))

    # Contact is initially None in DB -> should NOT raise AttributeError
    result = asyncio.run(_send_rule_reply(db, user, "new_fan", rule, TriggerType.KEYWORD, matched_keyword="vip"))
    assert result is True
    assert len(sent) == 1
    assert "Oh no! It seems you're not following me" in sent[0]["message"]

    # Contact record was created with awaiting status
    created = asyncio.run(db.contacts.find_one({"user_id": str(user_id), "ig_user_id": "new_fan"}))
    assert created is not None
    assert created["follow_gate_status"] == "awaiting"


# ==============================================================================
# FIX 2: _extract_comment_media_context reads value["media"]["media_product_type"]
# ==============================================================================
def test_extract_comment_media_context_media_product_type():
    """Verify that value["media"]["media_product_type"] is parsed correctly (FEED->post, REELS->reel, STORY->None)."""
    # FEED -> post
    mid, kind = _extract_comment_media_context({"media": {"id": "111", "media_product_type": "FEED"}})
    assert mid == "111"
    assert kind == "post"

    # REELS -> reel
    mid, kind = _extract_comment_media_context({"media": {"id": "222", "media_product_type": "REELS"}})
    assert mid == "222"
    assert kind == "reel"

    # STORY -> ignore (kind is None)
    mid, kind = _extract_comment_media_context({"media": {"id": "333", "media_product_type": "STORY"}})
    assert mid == "333"
    assert kind is None

    # Root-level media_product_type
    mid, kind = _extract_comment_media_context({"id": "444", "media_product_type": "FEED"})
    assert mid == "444"
    assert kind == "post"

    mid, kind = _extract_comment_media_context({"id": "555", "media_product_type": "REELS"})
    assert mid == "555"
    assert kind == "reel"

    mid, kind = _extract_comment_media_context({"id": "666", "media_product_type": "STORY"})
    assert mid == "666"
    assert kind is None

    # Fallbacks for legacy media_type
    mid, kind = _extract_comment_media_context({"media_type": "image"})
    assert kind == "post"
    mid, kind = _extract_comment_media_context({"media_type": "video"})
    assert kind == "reel"
    mid, kind = _extract_comment_media_context({"media_type": "carousel_album"})
    assert kind == "post"


# ==============================================================================
# FIX 3: handle_comment_event:
# - skip when commenter_id == ig_account_id or value has parent_id from our account
# - if rule has keywords, require a match; treat any_comment_keyword as "no keywords needed" only when keywords is empty
# - break after first matched rule
# - only write comment_dm_history if the DM actually sent
# ==============================================================================
def test_handle_comment_event_skips_own_account_and_parent_id(monkeypatch):
    """Verify handle_comment_event skips comments from our own account or replying to our parent_id."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_account_ids": ["biz_123", "biz_alias_456"],
        "instagram_access_token": "token_abc",
        "plan": "Starter",
    }
    rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["info"],
        "reply_message": "Info reply",
        "is_active": True,
    }
    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([]),
        automation_rules=_MockCollection([rule]),
        dm_logs=_MockCollection([]),
    )

    sent = []
    async def fake_send_dm(access_token, recipient_ig_id, message, **kwargs):
        sent.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    # 1. Comment from our own account ID
    comment_self = {"from": {"id": "biz_123"}, "text": "info please", "id": "c1"}
    asyncio.run(handle_comment_event(db, "biz_123", comment_self))
    assert len(sent) == 0

    # 2. Comment from our alias account ID
    comment_alias = {"from": {"id": "biz_alias_456"}, "text": "info please", "id": "c2"}
    asyncio.run(handle_comment_event(db, "biz_123", comment_alias))
    assert len(sent) == 0

    # 3. Comment replying to our parent_id
    comment_parent_biz = {"from": {"id": "fan_1"}, "text": "info please", "parent_id": "biz_123", "id": "c3"}
    asyncio.run(handle_comment_event(db, "biz_123", comment_parent_biz))
    assert len(sent) == 0

    # 4. Comment with parent dict from our account
    comment_parent_dict = {"from": {"id": "fan_1"}, "text": "info please", "parent": {"from": {"id": "biz_alias_456"}}, "id": "c4"}
    asyncio.run(handle_comment_event(db, "biz_123", comment_parent_dict))
    assert len(sent) == 0

    # 5. Normal comment from external user -> processes and sends DM
    comment_valid = {"from": {"id": "fan_external"}, "text": "info please", "id": "c5"}
    asyncio.run(handle_comment_event(db, "biz_123", comment_valid))
    assert len(sent) == 1
    assert sent[0]["recipient"] == "fan_external"


def test_handle_comment_event_keyword_requirement_and_any_comment_keyword(monkeypatch):
    """If rule has keywords, require a match even if any_comment_keyword=True. Treat any_comment_keyword as 'no keywords needed' only when keywords is empty."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
    }

    # Rule A: has keywords ["price"] AND any_comment_keyword=True
    rule_with_kws = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["price"],
        "any_comment_keyword": True,
        "reply_message": "Price is 99",
        "is_active": True,
    }

    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([]),
        automation_rules=_MockCollection([rule_with_kws]),
        dm_logs=_MockCollection([]),
    )

    sent = []
    async def fake_send_dm(access_token, recipient_ig_id, message, **kwargs):
        sent.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    # Comment does NOT contain "price" -> MUST NOT trigger rule_with_kws!
    asyncio.run(handle_comment_event(db, "biz_123", {"from": {"id": "fan_1"}, "text": "hello nice post", "id": "c1"}))
    assert len(sent) == 0

    # Comment DOES contain "price" -> MUST trigger!
    asyncio.run(handle_comment_event(db, "biz_123", {"from": {"id": "fan_1"}, "text": "what is the price?", "id": "c2"}))
    assert len(sent) == 1
    assert "Price is 99" in sent[0]["message"]

    # Now test rule with EMPTY keywords and any_comment_keyword=True -> triggers on any comment
    rule_no_kws_any = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT,
        "keywords": [],
        "any_comment_keyword": True,
        "reply_message": "General reply",
        "is_active": True,
    }
    db.automation_rules = _MockCollection([rule_no_kws_any])
    asyncio.run(handle_comment_event(db, "biz_123", {"from": {"id": "fan_2"}, "text": "random comment", "id": "c3"}))
    assert len(sent) == 2
    assert "General reply" in sent[1]["message"]

    # Test rule with EMPTY keywords and any_comment_keyword=False -> does NOT trigger
    rule_no_kws_no_any = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT,
        "keywords": [],
        "any_comment_keyword": False,
        "reply_message": "Should not fire",
        "is_active": True,
    }
    db.automation_rules = _MockCollection([rule_no_kws_no_any])
    asyncio.run(handle_comment_event(db, "biz_123", {"from": {"id": "fan_3"}, "text": "random comment", "id": "c4"}))
    assert len(sent) == 2  # No new message sent


def test_handle_comment_event_breaks_after_first_matched_rule(monkeypatch):
    """Verify handle_comment_event breaks after the first matched rule."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
    }
    rule1 = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["deal"],
        "reply_message": "First Rule Deal",
        "is_active": True,
        "triggers_count": 0,
    }
    rule2 = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["deal"],
        "reply_message": "Second Rule Deal",
        "is_active": True,
        "triggers_count": 0,
    }

    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([]),
        automation_rules=_MockCollection([rule1, rule2]),
        dm_logs=_MockCollection([]),
    )

    sent = []
    async def fake_send_dm(access_token, recipient_ig_id, message, **kwargs):
        sent.append(message)
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    asyncio.run(handle_comment_event(db, "biz_123", {"from": {"id": "fan_1"}, "text": "send deal", "id": "c1"}))
    assert len(sent) == 1
    assert "First Rule Deal" in sent[0]

    # Verify rule1 was incremented, but rule2 was NOT
    r1 = asyncio.run(db.automation_rules.find_one({"_id": rule1["_id"]}))
    r2 = asyncio.run(db.automation_rules.find_one({"_id": rule2["_id"]}))
    assert r1["triggers_count"] == 1
    assert r2["triggers_count"] == 0


def test_handle_comment_event_dm_history_only_written_if_dm_sent(monkeypatch):
    """Verify comment_dm_history is ONLY recorded if send_dm was actually successful."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
    }
    rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["ebook"],
        "reply_message": "Here is ebook",
        "comment_cooldown_hours": 24,
        "is_active": True,
    }

    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([]),
        automation_rules=_MockCollection([rule]),
        dm_logs=_MockCollection([]),
    )

    dm_success = False
    async def fake_send_dm(*args, **kwargs):
        return {"success": dm_success}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Fan", "username": "fan_999"}))

    # 1. When DM fails (e.g. Meta API 400 error):
    comment_payload = {
        "from": {"id": "fan_999"},
        "text": "ebook please",
        "id": "c1",
        "media": {"id": "post_777", "media_product_type": "FEED"},
    }
    asyncio.run(handle_comment_event(db, "biz_123", comment_payload))

    contact = asyncio.run(db.contacts.find_one({"user_id": str(user_id), "ig_user_id": "fan_999"}))
    # comment_dm_history must NOT have post_777 recorded
    assert contact is None or "post_777" not in (contact.get("comment_dm_history") or {})

    # 2. When DM succeeds:
    dm_success = True
    asyncio.run(handle_comment_event(db, "biz_123", comment_payload))
    contact_after = asyncio.run(db.contacts.find_one({"user_id": str(user_id), "ig_user_id": "fan_999"}))
    assert contact_after is not None
    assert "post_777" in (contact_after.get("comment_dm_history") or {})


# ==============================================================================
# FIX 4: handle_story_mention_event: only STORY_MENTION rules, no fallback
# ==============================================================================
def test_handle_story_mention_event_no_fallback_to_story_reply(monkeypatch):
    """Verify handle_story_mention_event uses only STORY_MENTION rules and does NOT fall back to STORY_REPLY."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
    }

    # Only a STORY_REPLY rule exists in DB
    story_reply_rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.STORY_REPLY,
        "reply_message": "Thanks for replying to our story!",
        "is_active": True,
    }

    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([]),
        automation_rules=_MockCollection([story_reply_rule]),
        dm_logs=_MockCollection([]),
    )

    sent = []
    async def fake_send_dm(access_token, recipient_ig_id, message, **kwargs):
        sent.append(message)
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    # Mention event occurs
    mention_value = {"from": {"id": "fan_mentioner"}, "media_id": "sm_100"}
    asyncio.run(handle_story_mention_event(db, "biz_123", mention_value))

    # Must NOT have fallen back to story_reply_rule!
    assert len(sent) == 0

    # Now add an explicit STORY_MENTION rule
    story_mention_rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.STORY_MENTION,
        "reply_message": "Thanks for mentioning us in your story! Here is 10% off: MENTION10",
        "is_active": True,
    }
    db.automation_rules = _MockCollection([story_reply_rule, story_mention_rule])
    asyncio.run(handle_story_mention_event(db, "biz_123", mention_value))

    assert len(sent) == 1
    assert "MENTION10" in sent[0]


# ==============================================================================
# FIX 5: NEW_DM rule fires once per contact per 24h
# ==============================================================================
def test_new_dm_rule_fires_once_per_contact_per_24h(monkeypatch):
    """Verify NEW_DM rule fires only once per contact per 24h period."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
    }
    new_dm_rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.NEW_DM,
        "reply_message": "Welcome! How can we help you?",
        "is_active": True,
        "triggers_count": 0,
    }

    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([]),
        automation_rules=_MockCollection([new_dm_rule]),
        dm_logs=_MockCollection([]),
    )

    sent = []
    async def fake_send_dm(access_token, recipient_ig_id, message, **kwargs):
        sent.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Alice", "username": "alice"}))

    msg1 = {"sender": {"id": "user_alice"}, "message": {"text": "hello"}}
    asyncio.run(handle_dm_event(db, "biz_123", msg1))

    # First DM fires NEW_DM rule
    assert len(sent) == 1
    assert "Welcome! How can we help you?" in sent[0]["message"]
    contact = asyncio.run(db.contacts.find_one({"user_id": str(user_id), "ig_user_id": "user_alice"}))
    assert contact is not None
    assert contact.get("last_new_dm_at") is not None

    # Second DM from same contact 2 hours later (< 24h) -> does NOT fire
    msg2 = {"sender": {"id": "user_alice"}, "message": {"text": "are you there?"}}
    asyncio.run(handle_dm_event(db, "biz_123", msg2))
    assert len(sent) == 1

    # After 25 hours (>= 24h) -> CAN fire again
    past_time = datetime.now(timezone.utc) - timedelta(hours=25)
    asyncio.run(db.contacts.update_one(
        {"user_id": str(user_id), "ig_user_id": "user_alice"},
        {"$set": {"last_new_dm_at": past_time}},
    ))

    msg3 = {"sender": {"id": "user_alice"}, "message": {"text": "good morning"}}
    asyncio.run(handle_dm_event(db, "biz_123", msg3))
    assert len(sent) == 2
    assert "Welcome! How can we help you?" in sent[1]["message"]


# ==============================================================================
# FIX 6: follow-confirmation exact-token match on short messages; reprompts if None
# ==============================================================================
def test_follow_confirmation_exact_token_short_messages_only():
    """Verify follow-confirmation matches exact tokens on short messages only."""
    # Valid short tokens
    assert _is_follow_confirmation_message("followed") is True
    assert _is_follow_confirmation_message("done") is True
    assert _is_follow_confirmation_message("Done!") is True
    assert _is_follow_confirmation_message("i followed") is True
    assert _is_follow_confirmation_message("following") is True
    assert _is_follow_confirmation_message("yes followed") is True
    assert _is_follow_confirmation_message("im following") is True
    assert _is_follow_confirmation_message("I'm following ✅") is True
    assert _is_follow_confirmation_message("i am following") is True
    assert _is_follow_confirmation_message("i m following") is True
    assert _is_follow_confirmation_message("FOLLOWED") is True

    # Long or unrelated messages must NOT match
    assert _is_follow_confirmation_message("done with everything, what is the cost?") is False
    assert _is_follow_confirmation_message("i was following your account last year") is False
    assert _is_follow_confirmation_message("well done on the video!") is False
    assert _is_follow_confirmation_message("have you done the update yet?") is False
    assert _is_follow_confirmation_message("can you tell me if this is done") is False
    assert _is_follow_confirmation_message("") is False


def test_follow_confirmation_reprompts_when_is_user_follow_business_is_none(monkeypatch):
    """If is_user_follow_business is None, reprompt instead of passing follow gate."""
    user_id = ObjectId()
    rule_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
        "instagram_username": "my_brand",
    }
    rule = {
        "_id": rule_id,
        "user_id": str(user_id),
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["secret"],
        "reply_message": "Unblocked secret link: https://secret.com",
        "ask_follow_before_dm": True,
        "is_active": True,
    }

    # Contact is awaiting follow gate
    contact = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "ig_user_id": "fan_awaiting",
        "follow_gate_status": "awaiting",
        "follow_gate_rule_id": str(rule_id),
        "follow_gate_trigger_type": "keyword",
    }

    db = SimpleNamespace(
        users=_MockCollection([user]),
        contacts=_MockCollection([contact]),
        automation_rules=_MockCollection([rule]),
        dm_logs=_MockCollection([]),
    )

    sent = []
    async def fake_send_dm(access_token, recipient_ig_id, message, **kwargs):
        sent.append({"recipient": recipient_ig_id, "message": message, "buttons": kwargs.get("buttons")})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    # 1. Profile returns is_user_follow_business as None (missing/unconfirmed)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Fan", "username": "fan"}))

    postback_event = {
        "sender": {"id": "fan_awaiting"},
        "recipient": {"id": "biz_123"},
        "postback": {"title": "I'm following ✅", "payload": "FOLLOWED"},
    }
    asyncio.run(handle_messaging_event(db, "biz_123", postback_event))

    # Must NOT deliver the unblocked secret link! Reprompts instead!
    assert len(sent) == 1
    assert "https://secret.com" not in sent[0]["message"]
    assert "you're not following @my_brand yet" in sent[0]["message"]

    c_after = asyncio.run(db.contacts.find_one({"user_id": str(user_id), "ig_user_id": "fan_awaiting"}))
    assert c_after["follow_gate_status"] == "awaiting"

    # 2. Profile now confirms is_user_follow_business is True
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Fan", "username": "fan", "is_user_follow_business": True}))
    asyncio.run(handle_messaging_event(db, "biz_123", postback_event))

    assert len(sent) == 2
    assert "Unblocked secret link: https://secret.com" in sent[1]["message"]
    c_final = asyncio.run(db.contacts.find_one({"user_id": str(user_id), "ig_user_id": "fan_awaiting"}))
    assert c_final["follow_gate_status"] == "completed"


# ==============================================================================
# FIX 7: _ensure_contact_create_allowed: check BEFORE sending, return bool, never raise HTTPException
# ==============================================================================
def test_ensure_contact_create_allowed_checks_before_sending_and_returns_bool(monkeypatch):
    """Verify _ensure_contact_create_allowed returns bool, never raises HTTPException, and prevents sending DMs if limit reached."""
    user_id = ObjectId()
    free_user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "free",
    }
    rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["link"],
        "reply_message": "Here is link",
        "is_active": True,
    }

    # Simulate free contact limit reached (500 contacts in DB)
    fake_contacts = [{"_id": ObjectId(), "user_id": str(user_id), "ig_user_id": f"ig_{i}"} for i in range(500)]
    db = SimpleNamespace(
        users=_MockCollection([free_user]),
        contacts=_MockCollection(fake_contacts),
        automation_rules=_MockCollection([rule]),
        dm_logs=_MockCollection([]),
    )

    # 1. _ensure_contact_create_allowed returns False and NEVER raises HTTPException
    allowed = asyncio.run(_ensure_contact_create_allowed(db, free_user, "brand_new_recipient"))
    assert allowed is False

    # 2. Calling _send_rule_reply for a new contact when limit reached returns False BEFORE sending any DM
    sent = []
    async def fake_send_dm(*args, **kwargs):
        sent.append(args)
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Fan", "username": "fan"}))

    dm_result = asyncio.run(_send_rule_reply(db, free_user, "brand_new_recipient", rule, TriggerType.KEYWORD, matched_keyword="link"))
    assert dm_result is False
    assert len(sent) == 0  # No DM sent to Meta!

    # 3. For existing contact, _ensure_contact_create_allowed returns True and DM sends
    existing_allowed = asyncio.run(_ensure_contact_create_allowed(db, free_user, "ig_0"))
    assert existing_allowed is True
    dm_result_existing = asyncio.run(_send_rule_reply(db, free_user, "ig_0", rule, TriggerType.KEYWORD, matched_keyword="link"))
    assert dm_result_existing is True
    assert len(sent) == 1

    # 4. For Pro user, contact limit does not apply
    pro_user = {"_id": user_id, "plan": "pro", "instagram_user_id": "biz_123", "instagram_access_token": "token_abc"}
    assert asyncio.run(_ensure_contact_create_allowed(db, pro_user, "brand_new_recipient")) is True
