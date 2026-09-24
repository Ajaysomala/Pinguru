import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone
import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import get_db
from app.models.models import (
    AutomationRuleCreate,
    RuleButton,
    TriggerType,
    PlanType,
)
from app.routes.automation import create_rule, update_rule
import app.main as main_module


@pytest.fixture
def client(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main_module, "connect_db", _noop)
    monkeypatch.setattr(main_module, "disconnect_db", _noop)

    with TestClient(main_module.app) as test_client:
        yield test_client


def test_comment_templates_rotation_and_serialization():
    inserted_docs = []

    class _FakeRules:
        async def count_documents(self, _q):
            return 0
        async def insert_one(self, doc):
            doc["_id"] = ObjectId()
            inserted_docs.append(doc)
            return SimpleNamespace(inserted_id=doc["_id"])

    db = SimpleNamespace(automation_rules=_FakeRules())
    pro_user = {"_id": ObjectId(), "plan": "pro"}

    templates = [
        "Sent you a DM! 🚀",
        "Check your inbox right now! 📩",
        "Just sent it to your DMs! 🙌",
    ]
    payload = AutomationRuleCreate(
        name="Comment Rotation Test",
        trigger_type=TriggerType.COMMENT,
        keywords=["freebie"],
        reply_message="Here is your freebie!",
        public_comment_reply_enabled=True,
        public_comment_reply_templates=templates,
    )

    res = asyncio.run(create_rule(data=payload, db=db, user=pro_user))
    rule = res["rule"]
    assert rule["public_comment_reply_enabled"] is True
    assert rule["public_comment_reply_templates"] == templates
    assert rule["public_comment_reply_template"] == templates[0]


def test_dm_buttons_validation_and_serialization():
    inserted_docs = []

    class _FakeRules:
        async def count_documents(self, _q):
            return 0
        async def insert_one(self, doc):
            doc["_id"] = ObjectId()
            inserted_docs.append(doc)
            return SimpleNamespace(inserted_id=doc["_id"])

    db = SimpleNamespace(automation_rules=_FakeRules())
    starter_user = {"_id": ObjectId(), "plan": "starter"}

    buttons = [
        RuleButton(type="web_url", title="Visit Site", url="https://pinguru.ai"),
        RuleButton(type="web_url", title="Watch Video", url="https://youtube.com/watch"),
    ]
    payload = AutomationRuleCreate(
        name="Buttons Rule Test",
        trigger_type=TriggerType.KEYWORD,
        keywords=["guide"],
        reply_message="Here are your links below:",
        dm_buttons=buttons,
        reply_delay_seconds=3,
    )

    res = asyncio.run(create_rule(data=payload, db=db, user=starter_user))
    rule = res["rule"]
    assert len(rule["dm_buttons"]) == 2
    assert rule["dm_buttons"][0]["title"] == "Visit Site"
    assert rule["dm_buttons"][0]["url"] == "https://pinguru.ai"
    assert rule["reply_delay_seconds"] == 3


def test_dm_buttons_reject_invalid_url_and_long_title():
    class _FakeRules:
        async def count_documents(self, _q): return 0

    db = SimpleNamespace(automation_rules=_FakeRules())
    starter_user = {"_id": ObjectId(), "plan": "starter"}

    # Non-https URL
    bad_url_payload = AutomationRuleCreate(
        name="Bad URL",
        trigger_type=TriggerType.KEYWORD,
        keywords=["test"],
        reply_message="Hi",
        dm_buttons=[RuleButton(type="web_url", title="Test", url="http://insecure.com")],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(data=bad_url_payload, db=db, user=starter_user))
    assert exc.value.status_code == 422
    assert "https" in exc.value.detail.lower()

    # Title > 20 chars
    long_title_payload = AutomationRuleCreate(
        name="Long Title",
        trigger_type=TriggerType.KEYWORD,
        keywords=["test"],
        reply_message="Hi",
        dm_buttons=[RuleButton(type="web_url", title="This title is way too long for Meta", url="https://pinguru.ai")],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(data=long_title_payload, db=db, user=starter_user))
    assert exc.value.status_code == 422
    assert "20 characters" in exc.value.detail.lower()


def test_in_dm_email_capture_plan_gating():
    class _FakeRules:
        async def count_documents(self, _q): return 0

    db = SimpleNamespace(automation_rules=_FakeRules())
    free_user = {"_id": ObjectId(), "plan": "free"}
    starter_user = {"_id": ObjectId(), "plan": "starter"}

    payload = AutomationRuleCreate(
        name="Lead Capture Rule",
        trigger_type=TriggerType.KEYWORD,
        keywords=["pdf"],
        reply_message="Here is your PDF",
        capture_email_enabled=True,
        email_capture_prompt="Drop your email to get the PDF: 📩",
        email_capture_success_message="Awesome! Sent to {{email}}! 🎉",
    )

    # Free user must be rejected with 403
    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(data=payload, db=db, user=free_user))
    assert exc.value.status_code == 403
    assert "starter and pro" in exc.value.detail.lower()

    # Starter user succeeds
    inserted_docs = []
    class _FakeRulesInsert:
        async def count_documents(self, _q): return 0
        async def insert_one(self, doc):
            doc["_id"] = ObjectId()
            inserted_docs.append(doc)
            return SimpleNamespace(inserted_id=doc["_id"])

    res = asyncio.run(create_rule(data=payload, db=SimpleNamespace(automation_rules=_FakeRulesInsert()), user=starter_user))
    rule = res["rule"]
    assert rule["capture_email_enabled"] is True
    assert rule["email_capture_prompt"] == "Drop your email to get the PDF: 📩"
    assert rule["email_capture_success_message"] == "Awesome! Sent to {{email}}! 🎉"


def test_funnel_analytics_computation():
    from app.routes.automation import _serialize_rule

    sample_rule = {
        "_id": ObjectId(),
        "name": "Funnel Analytics Test",
        "trigger_type": "comment",
        "reply_message": "Hello",
        "triggers_count": 100,
        "sent_count": 80,
        "follow_gate_completed_count": 45,
        "email_captured_count": 30,
    }
    serialized = _serialize_rule(sample_rule)
    analytics = serialized["analytics"]
    assert analytics["triggers"] == 100
    assert analytics["dms_sent"] == 80
    assert analytics["follows_unlocked"] == 45
    assert analytics["emails_captured"] == 30
    assert analytics["conversion_rate"] == 30.0


def test_contacts_csv_export():
    from app.routes.contacts import export_contacts_csv

    user_id = ObjectId()
    fake_user = {"_id": user_id, "plan": "pro"}

    sample_contacts = [
        {
            "_id": ObjectId(),
            "user_id": str(user_id),
            "ig_username": "john_doe",
            "display_name": "John Doe",
            "captured_email": "john@example.com",
            "email_captured_at": datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
            "ig_user_id": "1784140001",
            "dm_count": 5,
            "follow_gate_status": "completed",
            "first_seen_at": datetime(2026, 2, 28, 10, 0, tzinfo=timezone.utc),
            "last_seen_at": datetime(2026, 3, 1, 12, 5, tzinfo=timezone.utc),
        }
    ]

    class _FakeContacts:
        def find(self, q):
            class _FakeCursor:
                def sort(self, *a, **kw): return self
                def __aiter__(self):
                    self._items = iter(sample_contacts)
                    return self
                async def __anext__(self):
                    try:
                        return next(self._items)
                    except StopIteration:
                        raise StopAsyncIteration
            return _FakeCursor()

    db = SimpleNamespace(contacts=_FakeContacts())
    resp = asyncio.run(export_contacts_csv(user=fake_user, db=db))
    assert resp.media_type == "text/csv"
    assert "Content-Disposition" in resp.headers
    assert "contacts_" in resp.headers["Content-Disposition"]

    async def _read_chunks():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    content = asyncio.run(_read_chunks())
    assert "Instagram Username,Display Name,Captured Email" in content
    assert "john_doe,John Doe,john@example.com" in content
