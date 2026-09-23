import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.models.models import (
    AutomationRuleCreate,
    PlanType,
    TriggerType,
    UserInDB,
    get_plan_type,
)
from app.routes.automation import create_rule, update_rule
from app.routes.webhook import _send_rule_reply, handle_comment_event, handle_messaging_event
from app.services.instagram import InstagramService


# ── Test 1: Plan Resolution & UserInDB Model ──────────────────────────────────

def test_get_plan_type_case_insensitivity():
    assert get_plan_type("Starter") == PlanType.Starter
    assert get_plan_type("starter") == PlanType.Starter
    assert get_plan_type(" STARTER ") == PlanType.Starter
    assert get_plan_type("Pro") == PlanType.Pro
    assert get_plan_type("pro") == PlanType.Pro
    assert get_plan_type("PRO") == PlanType.Pro
    assert get_plan_type("Free") == PlanType.Free
    assert get_plan_type("free") == PlanType.Free
    assert get_plan_type(None) == PlanType.Free
    assert get_plan_type("") == PlanType.Free
    assert get_plan_type("unknown_tier") == PlanType.Free
    assert get_plan_type(PlanType.Starter) == PlanType.Starter


def test_user_in_db_normalizes_capitalized_plan():
    user = UserInDB(
        email="test@example.com",
        hashed_password="hashed_pwd_secret",
        plan="Starter",  # Capitalized string from MongoDB
    )
    assert user.plan == PlanType.Starter
    assert isinstance(user.plan, PlanType)


# ── Test 2: Automation Rules Preserve ask_follow_before_dm on Keyword Rules ──

class _FakeDbRules:
    def __init__(self, existing_rule=None):
        self.inserted_docs = []
        self.updated_docs = []
        self.existing_rule = existing_rule

    async def count_documents(self, _query):
        return 0

    async def insert_one(self, doc):
        new_id = ObjectId()
        doc_copy = dict(doc)
        doc_copy["_id"] = new_id
        self.inserted_docs.append(doc_copy)
        return SimpleNamespace(inserted_id=new_id)

    async def update_one(self, filter_query, update_query):
        self.updated_docs.append((filter_query, update_query))
        return SimpleNamespace(matched_count=1)

    async def find_one(self, filter_query):
        if self.existing_rule:
            return dict(self.existing_rule)
        if self.inserted_docs:
            return dict(self.inserted_docs[-1])
        return None


def test_starter_can_enable_ask_follow_on_keyword_rule():
    fake_db = SimpleNamespace(automation_rules=_FakeDbRules())
    user = {"_id": ObjectId(), "plan": "Starter"}
    payload = AutomationRuleCreate(
        name="Keyword Follow Gate",
        trigger_type=TriggerType.KEYWORD,
        keywords=["freebie"],
        reply_message="Here is your link: https://example.com",
        ask_follow_before_dm=True,
    )

    res = asyncio.run(create_rule(payload, db=fake_db, user=user))

    assert res["rule"]["ask_follow_before_dm"] is True
    assert fake_db.automation_rules.inserted_docs[0]["ask_follow_before_dm"] is True


def test_free_user_cannot_enable_ask_follow_on_keyword_rule():
    fake_db = SimpleNamespace(automation_rules=_FakeDbRules())
    user = {"_id": ObjectId(), "plan": "free"}
    payload = AutomationRuleCreate(
        name="Keyword Follow Gate",
        trigger_type=TriggerType.KEYWORD,
        keywords=["freebie"],
        reply_message="Here is your link: https://example.com",
        ask_follow_before_dm=True,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(payload, db=fake_db, user=user))

    assert exc.value.status_code == 403
    assert "available on Starter and Pro plans only" in exc.value.detail


def test_starter_can_update_keyword_rule_with_ask_follow():
    rule_id = ObjectId()
    existing = {
        "_id": rule_id,
        "user_id": "test_user_id",
        "name": "Keyword Rule",
        "trigger_type": "keyword",
        "keywords": ["freebie"],
        "reply_message": "Hello",
        "ask_follow_before_dm": False,
        "is_active": True,
    }
    fake_db = SimpleNamespace(automation_rules=_FakeDbRules(existing_rule=existing))
    user = {"_id": "test_user_id", "plan": "Starter"}
    payload = AutomationRuleCreate(
        name="Updated Keyword Rule",
        trigger_type=TriggerType.KEYWORD,
        keywords=["freebie"],
        reply_message="Updated response",
        ask_follow_before_dm=True,
    )

    res = asyncio.run(update_rule(str(rule_id), payload, db=fake_db, user=user))

    assert res["updated"] is True
    # Verify update query contained ask_follow_before_dm: True
    _, update_query = fake_db.automation_rules.updated_docs[0]
    assert update_query["$set"]["ask_follow_before_dm"] is True


# ── Test 3: InstagramService send_dm supports comment_id ──────────────────────

def test_send_dm_recipient_comment_id(monkeypatch):
    captured_payloads = []

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"recipient_id": "123", "message_id": "mid_456"}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, json=None):
            captured_payloads.append((url, json))
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: FakeClient())
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    async def _run():
        # Call with comment_id
        res = await InstagramService.send_dm(
            access_token="tok_abc",
            recipient_ig_id="ig_user_1",
            message="Hello commenter",
            ig_user_id="biz_page_id",
            comment_id="comment_999",
        )
        assert res["success"] is True
        url, payload = captured_payloads[0]
        assert payload["recipient"] == {"comment_id": "comment_999"}

        # Call without comment_id
        res2 = await InstagramService.send_dm(
            access_token="tok_abc",
            recipient_ig_id="ig_user_1",
            message="Hello direct",
            ig_user_id="biz_page_id",
        )
        assert res2["success"] is True
        url2, payload2 = captured_payloads[1]
        assert payload2["recipient"] == {"id": "ig_user_1"}

    asyncio.run(_run())


# ── Test 4: Webhook Follow-Gate Execution & Confirmation ─────────────────────

class _MockCollection:
    def __init__(self, data=None):
        self.data = list(data or [])
        self.inserts = []
        self.updates = []

    def _matches_filter(self, item, query):
        for k, v in query.items():
            if k == "$or" and isinstance(v, list):
                if not any(self._matches_filter(item, sub) for sub in v):
                    return False
            elif k == "trigger_type" and isinstance(v, dict) and "$in" in v:
                if item.get("trigger_type") not in v["$in"]:
                    return False
            elif item.get(k) != v:
                return False
        return True

    async def find_one(self, query):
        for item in self.data:
            if self._matches_filter(item, query):
                return item
        return None

    async def count_documents(self, query):
        return sum(1 for item in self.data if self._matches_filter(item, query))

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
            if "$set" in update_query:
                existing.update(update_query["$set"])
            return SimpleNamespace(matched_count=1)
        elif upsert:
            new_doc = dict(filter_query)
            if "$set" in update_query:
                new_doc.update(update_query["$set"])
            if "$setOnInsert" in update_query:
                new_doc.update(update_query["$setOnInsert"])
            new_doc["_id"] = ObjectId()
            self.data.append(new_doc)
            return SimpleNamespace(matched_count=0, upserted_id=new_doc["_id"])
        return SimpleNamespace(matched_count=0)

    def find(self, query):
        matching = [item for item in self.data if self._matches_filter(item, query)]
        return _MockCursor(matching)


class _MockCursor:
    def __init__(self, items):
        self.items = items

    async def to_list(self, _length):
        return list(self.items)


def test_keyword_follow_gate_prompts_user_then_unblocks_on_followed(monkeypatch):
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
        "name": "Keyword Rule",
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["guide"],
        "reply_message": "Here is your VIP guide: https://link.com",
        "ask_follow_before_dm": True,
        "is_active": True,
    }

    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()
    rules_col = _MockCollection([rule])
    users_col = _MockCollection([user])

    db = SimpleNamespace(
        contacts=contacts_col,
        dm_logs=dm_logs_col,
        automation_rules=rules_col,
        users=users_col,
    )

    sent_messages = []

    async def fake_send_dm(access_token, recipient_ig_id, message, ig_user_id, attachment_url=None, attachment_type="image", comment_id=None):
        sent_messages.append({
            "recipient_ig_id": recipient_ig_id,
            "message": message,
            "comment_id": comment_id,
        })
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Fan", "username": "fan_001"}))

    async def _run():
        # 1. Trigger rule reply for keyword
        await _send_rule_reply(db, user, "fan_001", rule, TriggerType.KEYWORD, matched_keyword="guide")

        # Should have sent follow prompt
        assert len(sent_messages) == 1
        assert "Please follow @my_brand first" in sent_messages[0]["message"]
        assert "reply here with: FOLLOWED" in sent_messages[0]["message"]

        # Contact should now be awaiting
        contact = await contacts_col.find_one({"user_id": str(user_id), "ig_user_id": "fan_001"})
        assert contact is not None
        assert contact["follow_gate_status"] == "awaiting"
        assert contact["follow_gate_rule_id"] == str(rule_id)

        # 2. Fan responds in DM with "FOLLOWED"
        messaging_event = {
            "sender": {"id": "fan_001"},
            "recipient": {"id": "biz_123"},
            "message": {"text": "FOLLOWED"},
        }

        await handle_messaging_event(db, "biz_123", messaging_event)

        # Should have triggered the un-gated final reply
        assert len(sent_messages) == 2
        assert "Here is your VIP guide: https://link.com" in sent_messages[1]["message"]

        # Contact should now have follow_gate_status = completed
        contact_after = await contacts_col.find_one({"user_id": str(user_id), "ig_user_id": "fan_001"})
        assert contact_after["follow_gate_status"] == "completed"

        # 3. Subsequent keyword trigger by the same contact should NOT prompt again
        await _send_rule_reply(db, user, "fan_001", rule, TriggerType.KEYWORD, matched_keyword="guide")

        assert len(sent_messages) == 3
        assert "Here is your VIP guide: https://link.com" in sent_messages[2]["message"]

    asyncio.run(_run())


def test_comment_event_passes_comment_id_to_send_dm(monkeypatch):
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
        "name": "Comment Rule",
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["price"],
        "reply_message": "Price is $10",
        "ask_follow_before_dm": False,
        "any_comment_keyword": True,
        "is_active": True,
    }

    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()
    rules_col = _MockCollection([rule])
    users_col = _MockCollection([user])

    db = SimpleNamespace(
        contacts=contacts_col,
        dm_logs=dm_logs_col,
        automation_rules=rules_col,
        users=users_col,
    )

    sent_messages = []

    async def fake_send_dm(access_token, recipient_ig_id, message, ig_user_id, attachment_url=None, attachment_type="image", comment_id=None):
        sent_messages.append({
            "recipient_ig_id": recipient_ig_id,
            "message": message,
            "comment_id": comment_id,
        })
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Fan", "username": "fan_001"}))

    async def _run():
        comment_payload = {
            "from": {"id": "commenter_888"},
            "text": "price please",
            "comment_id": "comm_unique_777",
            "media": {"id": "media_555"},
        }
        await handle_comment_event(db, "biz_123", comment_payload)

        assert len(sent_messages) == 1
        assert sent_messages[0]["recipient_ig_id"] == "commenter_888"
        assert sent_messages[0]["comment_id"] == "comm_unique_777"
        assert "Price is $10" in sent_messages[0]["message"]

    asyncio.run(_run())

