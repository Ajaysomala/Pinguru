import asyncio
import hmac
import hashlib
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import get_db
from app.models.models import AutomationRuleCreate, TriggerType
from app.routes import billing as billing_module
from app.routes import webhook as webhook_module
from app.routes import admin as admin_module
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
        "/billing/cancel-pending",
        cookies={"pg_token": "dummy_token", "pg_csrf": "csrf_value"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid CSRF token"


def test_origin_rejected_for_cookie_post(client):
    response = client.post(
        "/billing/cancel-pending",
        headers={"Origin": "https://evil.example", "X-CSRF-Token": "csrf_value"},
        cookies={"pg_token": "dummy_token", "pg_csrf": "csrf_value"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid request origin"


def test_cookie_post_allows_valid_origin_and_csrf(client):
    response = client.post(
        "/billing/cancel-pending",
        headers={"Origin": "http://localhost:5173", "X-CSRF-Token": "csrf_value"},
        cookies={"pg_token": "dummy_token", "pg_csrf": "csrf_value"},
    )

    # Auth can still fail for dummy cookie, but middleware should not block with CSRF/origin errors.
    assert response.status_code != 403


def test_logout_allowed_without_csrf(client):
    response = client.post(
        "/auth/logout",
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
        trigger_type=TriggerType.KEYWORD,
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
        trigger_type=TriggerType.COMMENT,
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


def test_verify_email_constant_time_otp_verification():
    from app.routes.auth import verify_email, OTPVerifyRequest, hash_otp
    from datetime import datetime, timedelta, timezone
    from starlette.requests import Request

    otp = "123456"
    user_id = ObjectId()
    fake_user = {
        "_id": user_id,
        "email": "test@example.com",
        "email_verified": False,
        "otp_hash": hash_otp(otp),
        "otp_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "otp_attempts": 0,
    }

    class _FakeUsers:
        def __init__(self, doc):
            self.doc = doc
            self.updated = []
        async def find_one(self, q):
            return self.doc if q.get("email") == self.doc["email"] else None
        async def update_one(self, f, u):
            self.updated.append((f, u))
            return SimpleNamespace(matched_count=1)

    db = SimpleNamespace(users=_FakeUsers(fake_user))
    payload = OTPVerifyRequest(email="test@example.com", otp="123456")
    scope = {"type": "http", "method": "POST", "path": "/auth/verify-email", "headers": [], "client": ("127.0.0.1", 12345)}
    req = Request(scope)
    resp = asyncio.run(verify_email(request=req, data=payload, db=db))
    assert resp.status_code == 200


def test_me_returns_id_and_onboarding_complete():
    from app.routes.auth import me
    from starlette.requests import Request
    user_id = ObjectId()
    fake_user = {
        "_id": user_id,
        "email": "user@example.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "onboarding_complete": True,
        "plan": "pro",
    }
    scope = {"type": "http", "method": "GET", "path": "/auth/me", "headers": [], "client": ("127.0.0.1", 12345)}
    req = Request(scope)
    res = asyncio.run(me(request=req, user=fake_user, db=SimpleNamespace()))
    assert res["id"] == str(user_id)
    assert res["onboarding_complete"] is True
    assert res["display_name"] == "Jane Doe"


def test_profile_update_recomputes_display_name():
    from app.routes.auth import update_profile, ProfileUpdateRequest
    from fastapi import Response
    user_id = ObjectId()
    fake_user = {
        "_id": user_id,
        "email": "user@example.com",
        "first_name": "Old",
        "last_name": "Name",
        "display_name": "Old Name",
    }
    updated_doc = dict(fake_user)

    class _FakeUsers:
        async def update_one(self, f, u):
            if "$set" in u:
                updated_doc.update(u["$set"])
            return SimpleNamespace(matched_count=1)
        async def find_one(self, q):
            return updated_doc

    db = SimpleNamespace(users=_FakeUsers())
    payload = ProfileUpdateRequest(first_name="NewFirst", last_name="NewLast")
    res = asyncio.run(update_profile(data=payload, response=Response(), user=fake_user, db=db))
    assert updated_doc["display_name"] == "NewFirst NewLast"


def test_dashboard_contacts_aliases():
    from app.routes.dashboard import dashboard_contacts, dashboard_contact_stats

    user_id = ObjectId()
    fake_user = {"_id": user_id, "plan": "free"}

    class _FakeContacts:
        def __init__(self):
            pass
        async def count_documents(self, q):
            return 42
        def find(self, q):
            class _FakeCursor:
                def sort(self, *a, **kw): return self
                def skip(self, *a, **kw): return self
                def limit(self, *a, **kw): return self
                def __aiter__(self):
                    self._items = iter([{"_id": ObjectId(), "name": "tester"}])
                    return self
                async def __anext__(self):
                    try:
                        return next(self._items)
                    except StopIteration:
                        raise StopAsyncIteration
            return _FakeCursor()

    db = SimpleNamespace(contacts=_FakeContacts())
    res = asyncio.run(dashboard_contacts(page=1, limit=20, user=fake_user, db=db))
    assert res["total"] == 42
    assert len(res["contacts"]) == 1

    stats_res = asyncio.run(dashboard_contact_stats(user=fake_user, db=db))
    assert stats_res["total"] == 42
    assert stats_res["limit"] == 500


def test_admin_login_rate_limited(client):
    admin_module.settings.ADMIN_EMAIL = "admin@example.com"
    admin_module.settings.ADMIN_PASSWORD_HASH = "$2b$12$e80yVjJ8.VbI8hN8PuhN0.0XU6E.C1L.7lY0w/aR7s2wR1m6B8y1."

    responses = [
        client.post("/admin/login", json={"email": "wrong@example.com", "password": "wrong"})
        for _ in range(6)
    ]
    # First 5 should be 401 Unauthorized
    for r in responses[:5]:
        assert r.status_code == 401
    # 6th request must be 429 Too Many Requests
    assert responses[5].status_code == 429
    assert "Too many requests" in responses[5].json()["detail"]


def test_admin_auth_login_alias_rate_limited(client):
    admin_module.settings.ADMIN_EMAIL = "admin@example.com"
    pwd = "valid-admin-password"
    admin_module.settings.ADMIN_PASSWORD_HASH = admin_module.pwd_ctx.hash(pwd)

    responses = []
    for _ in range(6):
        resp = client.post("/admin/auth/login", json={"email": "admin@example.com", "password": pwd})
        client.cookies.clear()
        responses.append(resp)

    # First 5 should succeed (HTTP 200)
    for r in responses[:5]:
        assert r.status_code == 200
        assert r.json().get("ok") is True
    # 6th request must be 429 Too Many Requests
    assert responses[5].status_code == 429
    assert "Too many requests" in responses[5].json()["detail"]




