import asyncio
import hmac
import hashlib
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import get_db
from app.models.models import AutomationRuleCreate
from app.routes import billing as billing_module
from app.routes import webhook as webhook_module
from app.routes.automation import create_rule
from app.routes.webhook import _ensure_contact_create_allowed
import app.main as main_module


class _FakeInsertResult:
    def __init__(self):
        self.inserted_id = ObjectId()


class _FakeAutomationRules:
    def __init__(self, count: int):
        self._count = count

    async def count_documents(self, _query):
        return self._count

    async def insert_one(self, _doc):
        return _FakeInsertResult()


class _FakeContacts:
    def __init__(self, exists: bool, total: int):
        self._exists = exists
        self._total = total

    async def find_one(self, _query):
        return {"_id": "contact"} if self._exists else None

    async def count_documents(self, _query):
        return self._total


@pytest.fixture
def client(monkeypatch):
    async def _noop():
        return None

    async def _fake_db_override():
        yield SimpleNamespace()

    monkeypatch.setattr(main_module, "connect_db", _noop)
    monkeypatch.setattr(main_module, "disconnect_db", _noop)

    billing_module.settings.RAZORPAY_WEBHOOK_SECRET = "test_razorpay_secret"
    webhook_module.settings.META_APP_SECRET = "test_meta_secret"
    webhook_module.settings.DISABLE_WEBHOOK_SIGNATURE = False

    main_module.app.dependency_overrides[get_db] = _fake_db_override

    with TestClient(main_module.app) as test_client:
        yield test_client

    main_module.app.dependency_overrides.clear()


def test_csrf_required_for_cookie_post(client):
    response = client.post(
        "/auth/logout",
        cookies={"pg_token": "dummy_token", "pg_csrf": "csrf_value"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid CSRF token"


def test_origin_rejected_for_cookie_post(client):
    response = client.post(
        "/auth/logout",
        headers={"Origin": "https://evil.example", "X-CSRF-Token": "csrf_value"},
        cookies={"pg_token": "dummy_token", "pg_csrf": "csrf_value"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid request origin"


def test_cookie_post_allows_valid_origin_and_csrf(client):
    response = client.post(
        "/auth/logout",
        headers={"Origin": "http://localhost:5173", "X-CSRF-Token": "csrf_value"},
        cookies={"pg_token": "dummy_token", "pg_csrf": "csrf_value"},
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Logged out"


def test_meta_webhook_rejects_invalid_signature(client):
    response = client.post(
        "/webhook/instagram",
        json={"entry": []},
        headers={"X-Hub-Signature-256": "sha256=invalid"},
    )

    assert response.status_code == 403


def test_razorpay_webhook_rejects_invalid_signature(client):
    response = client.post(
        "/billing/razorpay-webhook",
        json={"event": "subscription.activated", "payload": {}},
        headers={"X-Razorpay-Signature": "invalid"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid webhook signature"


def test_razorpay_webhook_accepts_valid_signature(client):
    payload = b'{"event":"subscription.activated","payload":{"subscription":{"entity":{"notes":{}}}}}'
    signature = hmac.new(
        billing_module.settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()

    response = client.post(
        "/billing/razorpay-webhook",
        data=payload,
        headers={"Content-Type": "application/json", "X-Razorpay-Signature": signature},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_free_rule_limit_enforced():
    db = SimpleNamespace(automation_rules=_FakeAutomationRules(count=5))
    user = {"_id": ObjectId(), "plan": "free"}
    payload = AutomationRuleCreate(
        name="Free Limit Test",
        trigger_type="keyword",
        keywords=["price"],
        reply_message="Hello",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(payload, db=db, user=user))

    assert exc.value.status_code == 403
    assert "Rule limit reached" in str(exc.value.detail)


def test_follow_up_feature_rejected_even_for_pro():
    db = SimpleNamespace(automation_rules=_FakeAutomationRules(count=0))
    user = {"_id": ObjectId(), "plan": "pro"}
    payload = AutomationRuleCreate(
        name="Follow Up Rejection",
        trigger_type="comment",
        keywords=["price"],
        reply_message="Hello",
        any_comment_keyword=True,
        send_follow_up_message=True,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(payload, db=db, user=user))

    assert exc.value.status_code == 403
    assert "not part of the current plan contract" in str(exc.value.detail)


def test_free_contact_limit_enforced():
    db = SimpleNamespace(contacts=_FakeContacts(exists=False, total=500))
    user = {"_id": ObjectId(), "plan": "free"}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_ensure_contact_create_allowed(db, user, "ig_123"))

    assert exc.value.status_code == 403
    assert "contact limit reached" in str(exc.value.detail).lower()
