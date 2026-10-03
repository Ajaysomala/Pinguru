import logging
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pymongo import ASCENDING, DESCENDING
from starlette.testclient import TestClient

import app.main as main_module
from app.config import settings
from app.database import get_db, _create_indexes
from app.models.models import PlanType, PLAN_LIMITS
from app.routes.auth import (
    hash_password,
    verify_password,
    create_jwt,
    parse_meta_signed_request,
)
from app.services.token_refresh import reset_monthly_dm_counts, cleanup_unverified_accounts
from app.routes.webhook import _ensure_monthly_dm_count_current


class MockCollection:
    def __init__(self, name="col", docs=None):
        self.name = name
        self.docs = list(docs or [])
        self.indexes = []

    def _matches(self, doc, query):
        if not query:
            return True
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches(doc, cond) for cond in v):
                    return False
            elif isinstance(v, dict):
                val = doc.get(k)
                for op, op_val in v.items():
                    if op == "$gt" and not (val is not None and val > op_val):
                        return False
                    elif op == "$gte" and not (val is not None and val >= op_val):
                        return False
                    elif op == "$lt" and not (val is not None and val < op_val):
                        return False
                    elif op == "$lte" and not (val is not None and val <= op_val):
                        return False
                    elif op == "$ne" and val == op_val:
                        return False
                    elif op == "$nin" and val in op_val:
                        return False
                    elif op == "$in" and val not in op_val:
                        return False
                    elif op == "$exists":
                        exists = k in doc and doc[k] is not None
                        if exists != op_val:
                            return False
                    elif op == "$regex":
                        if not re.search(op_val, str(val or "")):
                            return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    async def find_one(self, query):
        for d in self.docs:
            if self._matches(d, query):
                return dict(d)
        return None

    def find(self, query):
        matched = [dict(d) for d in self.docs if self._matches(d, query)]
        return MockCursor(matched)

    async def insert_one(self, doc):
        d = dict(doc)
        if "_id" not in d:
            d["_id"] = ObjectId()
        self.docs.append(d)
        return SimpleNamespace(inserted_id=d["_id"])

    async def update_one(self, filter_q, update_q, upsert=False):
        for i, d in enumerate(self.docs):
            if self._matches(d, filter_q):
                if "$set" in update_q:
                    self.docs[i].update(update_q["$set"])
                if "$unset" in update_q:
                    for uk in update_q["$unset"]:
                        self.docs[i].pop(uk, None)
                if "$inc" in update_q:
                    for ik, iv in update_q["$inc"].items():
                        self.docs[i][ik] = self.docs[i].get(ik, 0) + iv
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            new_doc = dict(filter_q)
            if "$set" in update_q:
                new_doc.update(update_q["$set"])
            if "$inc" in update_q:
                for ik, iv in update_q["$inc"].items():
                    new_doc[ik] = new_doc.get(ik, 0) + iv
            if "_id" not in new_doc:
                new_doc["_id"] = ObjectId()
            self.docs.append(new_doc)
            return SimpleNamespace(matched_count=0, modified_count=1, upserted_id=new_doc["_id"])
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, filter_q, update_q):
        mod = 0
        for i, d in enumerate(self.docs):
            if self._matches(d, filter_q):
                if "$set" in update_q:
                    self.docs[i].update(update_q["$set"])
                if "$unset" in update_q:
                    for uk in update_q["$unset"]:
                        self.docs[i].pop(uk, None)
                mod += 1
        return SimpleNamespace(modified_count=mod)

    async def delete_one(self, filter_q):
        for i, d in enumerate(self.docs):
            if self._matches(d, filter_q):
                self.docs.pop(i)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def delete_many(self, filter_q):
        initial_len = len(self.docs)
        self.docs = [d for d in self.docs if not self._matches(d, filter_q)]
        return SimpleNamespace(deleted_count=initial_len - len(self.docs))

    async def count_documents(self, filter_q):
        return len([d for d in self.docs if self._matches(d, filter_q)])

    async def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    async def drop_index(self, index_name):
        if not hasattr(self, "dropped_indexes"):
            self.dropped_indexes = []
        self.dropped_indexes.append(index_name)


class MockCursor:
    def __init__(self, items):
        self.items = items

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        return list(self.items)


class MockDB:
    def __init__(self):
        self.users = MockCollection("users")
        self.automation_rules = MockCollection("automation_rules")
        self.dm_logs = MockCollection("dm_logs")
        self.contacts = MockCollection("contacts")
        self.webhook_events = MockCollection("webhook_events")
        self.data_deletion_requests = MockCollection("data_deletion_requests")
        self.refund_requests = MockCollection("refund_requests")
        self.admin_audit = MockCollection("admin_audit")
        self.admin_config = MockCollection("admin_config")
        self.login_lockouts = MockCollection("login_lockouts")

    def __getattr__(self, name):
        col = MockCollection(name)
        setattr(self, name, col)
        return col


@pytest.fixture
def test_setup(monkeypatch):
    async def _noop():
        return None

    monkeypatch.setattr(main_module, "connect_db", _noop)
    monkeypatch.setattr(main_module, "disconnect_db", _noop)
    monkeypatch.setattr(settings, "META_APP_SECRET", "test_meta_app_secret_123")
    monkeypatch.setattr(main_module.limiter, "enabled", False)

    mock_db = MockDB()
    main_module.app.dependency_overrides[get_db] = lambda: mock_db

    with TestClient(main_module.app) as client:
        yield client, mock_db

    main_module.app.dependency_overrides.clear()


def _generate_meta_signed_request(payload: dict, secret: str = None) -> str:
    if secret is None:
        secret = settings.META_APP_SECRET
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    return f"{sig_b64}.{payload_b64}"


# ─────────────────────────────────────────────────────────────────────────────
# 1. Google signup & login blocking
# ─────────────────────────────────────────────────────────────────────────────

def test_google_signup_uses_unique_token_and_blocks_password_login(test_setup):
    """Verify Google signup stores random password, sets oauth_provider=google, and blocks password login."""
    client, mock_db = test_setup
    test_email = f"google_user_{secrets.token_hex(4)}@example.com"

    mock_id_info = {
        "email": test_email,
        "given_name": "Google",
        "family_name": "User",
        "name": "Google User",
    }
    with patch("app.routes.auth.id_token.verify_oauth2_token", return_value=mock_id_info):
        resp = client.post("/auth/google/callback", json={"id_token": "fake_google_token"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "plan" in data

    user = asyncio.run(mock_db.users.find_one({"email": test_email}))
    assert user is not None
    assert user.get("oauth_provider") == "google"
    assert user.get("email_verified") is True

    # Password should NOT match DEFAULT_OAUTH_PASSWORD
    assert not verify_password(settings.DEFAULT_OAUTH_PASSWORD, user["hashed_password"])

    # Clear the OAuth session cookie before attempting unauthenticated password login
    client.cookies.clear()

    # Attempting password login via /auth/login must be rejected
    login_resp = client.post(
        "/auth/login",
        json={"email": test_email, "password": "AnyPassword123!"},
    )
    assert login_resp.status_code == 400
    assert "Google Sign-In" in login_resp.json().get("detail", "")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Register unverified user resend & TTL index
# ─────────────────────────────────────────────────────────────────────────────

def test_register_resends_otp_for_unverified_account(test_setup):
    """If an unverified account already exists, registering again resends the OTP instead of returning 400."""
    client, mock_db = test_setup
    test_email = f"unverified_{secrets.token_hex(4)}@example.com"

    with patch("app.routes.auth.send_otp_email", new_callable=AsyncMock) as mock_send_email:
        mock_send_email.return_value = True

        # First registration
        resp1 = client.post(
            "/auth/register",
            json={
                "email": test_email,
                "password": "Password123!",
                "first_name": "Alice",
                "last_name": "Test",
            },
        )
        assert resp1.status_code == 200, resp1.text
        assert "Account created" in resp1.json().get("message", "")
        assert mock_send_email.call_count == 1

        user1 = asyncio.run(mock_db.users.find_one({"email": test_email}))
        assert user1 is not None
        assert user1.get("email_verified") is False
        assert user1.get("unverified_expires_at") is not None
        first_otp_hash = user1.get("otp_hash")

        # Second registration with same email (unverified)
        resp2 = client.post(
            "/auth/register",
            json={
                "email": test_email,
                "password": "NewPassword456!",
                "first_name": "Alice",
                "last_name": "Updated",
            },
        )
        assert resp2.status_code == 200, resp2.text
        assert "resent" in resp2.json().get("message", "").lower()
        assert mock_send_email.call_count == 2

        user2 = asyncio.run(mock_db.users.find_one({"email": test_email}))
        assert user2 is not None
        assert user2.get("otp_hash") != first_otp_hash
        assert user2.get("unverified_expires_at") is not None
        assert verify_password("NewPassword456!", user2["hashed_password"])


def test_verify_email_unsets_unverified_expires_at(test_setup):
    """Once email is verified, unverified_expires_at is unset so TTL index does not delete it."""
    client, mock_db = test_setup
    test_email = f"verify_{secrets.token_hex(4)}@example.com"

    with patch("app.routes.auth.send_otp_email", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = True
        client.post(
            "/auth/register",
            json={"email": test_email, "password": "Password123!"},
        )

    known_otp = "123456"
    from app.routes.auth import hash_otp
    asyncio.run(
        mock_db.users.update_one(
            {"email": test_email},
            {"$set": {"otp_hash": hash_otp(known_otp), "otp_expires_at": datetime.now(timezone.utc) + timedelta(minutes=5)}},
        )
    )

    verify_resp = client.post("/auth/verify-email", json={"email": test_email, "otp": known_otp})
    assert verify_resp.status_code == 200, verify_resp.text

    user = asyncio.run(mock_db.users.find_one({"email": test_email}))
    assert user.get("email_verified") is True
    assert "unverified_expires_at" not in user or user.get("unverified_expires_at") is None


def test_cleanup_unverified_accounts_task():
    """cleanup_unverified_accounts purges expired unverified users while preserving active/verified ones."""
    mock_db = MockDB()
    now = datetime.now(timezone.utc)

    # 1. Expired unverified account
    res1 = asyncio.run(
        mock_db.users.insert_one({
            "email": f"expired_unverified_{secrets.token_hex(4)}@example.com",
            "email_verified": False,
            "unverified_expires_at": now - timedelta(hours=2),
            "created_at": now - timedelta(hours=26),
        })
    )

    # 2. Fresh unverified account (still within 24h window)
    res2 = asyncio.run(
        mock_db.users.insert_one({
            "email": f"fresh_unverified_{secrets.token_hex(4)}@example.com",
            "email_verified": False,
            "unverified_expires_at": now + timedelta(hours=10),
            "created_at": now,
        })
    )

    # 3. Verified account
    res3 = asyncio.run(
        mock_db.users.insert_one({
            "email": f"verified_{secrets.token_hex(4)}@example.com",
            "email_verified": True,
            "created_at": now - timedelta(days=5),
        })
    )

    deleted = asyncio.run(cleanup_unverified_accounts(mock_db))
    assert deleted >= 1

    assert asyncio.run(mock_db.users.find_one({"_id": res1.inserted_id})) is None
    assert asyncio.run(mock_db.users.find_one({"_id": res2.inserted_id})) is not None
    assert asyncio.run(mock_db.users.find_one({"_id": res3.inserted_id})) is not None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Data Deletion (User initiated + Meta signed_request callbacks)
# ─────────────────────────────────────────────────────────────────────────────

def test_user_data_deletion_removes_contacts_webhook_events_and_deactivates(test_setup):
    """POST /auth/data-deletion deletes rules, logs, contacts, webhook_events, and sets is_active=False."""
    client, mock_db = test_setup
    now = datetime.now(timezone.utc)

    ig_id = f"ig_user_{secrets.token_hex(4)}"
    res = asyncio.run(
        mock_db.users.insert_one({
            "email": f"delete_me_{secrets.token_hex(4)}@example.com",
            "is_active": True,
            "email_verified": True,
            "instagram_user_id": ig_id,
            "instagram_account_ids": [ig_id],
            "instagram_access_token": "some_token",
            "session_version": 1,
            "created_at": now,
        })
    )
    user_id = res.inserted_id
    user_id_str = str(user_id)

    # Seed contacts, rules, dm_logs, and webhook_events
    asyncio.run(mock_db.contacts.insert_one({"user_id": user_id_str, "ig_user_id": "contact_1"}))
    asyncio.run(mock_db.automation_rules.insert_one({"user_id": user_id_str, "name": "rule 1"}))
    asyncio.run(mock_db.dm_logs.insert_one({"user_id": user_id_str, "recipient_ig_id": "contact_1", "sent_at": now}))
    asyncio.run(mock_db.webhook_events.insert_one({"_id": f"msg:{ig_id}:fan_1:mid_1", "source": "messaging"}))

    token = create_jwt(user_id_str, 1)
    client.cookies.set("pg_token", token)
    client.cookies.set("pg_csrf", "test_csrf_token")

    del_resp = client.post("/auth/data-deletion", headers={"X-CSRF-Token": "test_csrf_token"})
    assert del_resp.status_code == 200, del_resp.text
    assert "deleted" in del_resp.json().get("message", "").lower()

    # Verify everything was deleted/deactivated
    assert asyncio.run(mock_db.contacts.count_documents({"user_id": user_id_str})) == 0
    assert asyncio.run(mock_db.automation_rules.count_documents({"user_id": user_id_str})) == 0
    assert asyncio.run(mock_db.dm_logs.count_documents({"user_id": user_id_str})) == 0
    assert asyncio.run(mock_db.webhook_events.find_one({"_id": f"msg:{ig_id}:fan_1:mid_1"})) is None

    updated_user = asyncio.run(mock_db.users.find_one({"_id": user_id}))
    assert updated_user["is_active"] is False
    assert updated_user.get("deleted_at") is not None
    assert updated_user.get("instagram_access_token") is None

    # Deactivated account cannot access authenticated routes
    me_resp = client.get("/auth/me")
    assert me_resp.status_code == 401


def test_meta_data_deletion_callback_with_signed_request(test_setup):
    """POST /auth/data-deletion-callback verifies signed_request, deletes user data, and tracks status."""
    client, mock_db = test_setup
    now = datetime.now(timezone.utc)

    meta_user_id = f"meta_{secrets.token_hex(4)}"
    res = asyncio.run(
        mock_db.users.insert_one({
            "email": f"meta_user_{secrets.token_hex(4)}@example.com",
            "is_active": True,
            "email_verified": True,
            "instagram_user_id": meta_user_id,
            "instagram_account_ids": [meta_user_id],
            "instagram_access_token": "token123",
            "created_at": now,
        })
    )
    user_id = res.inserted_id
    user_id_str = str(user_id)

    # Seed contacts and webhook events
    asyncio.run(mock_db.contacts.insert_one({"user_id": user_id_str, "ig_user_id": "meta_contact"}))
    asyncio.run(mock_db.webhook_events.insert_one({"_id": f"chg:{meta_user_id}:comments:comm_1", "source": "change"}))

    # Test invalid signature rejection
    bad_signed_req = "bad_sig.eyJhbGdvcml0aG0iOiJITUFDLVNIQTI1NiIsInVzZXJfaWQiOiIxMjM0NSJ9"
    resp_bad = client.post("/auth/data-deletion-callback", data={"signed_request": bad_signed_req})
    assert resp_bad.status_code == 400

    # Test valid signed request
    valid_payload = {
        "algorithm": "HMAC-SHA256",
        "user_id": meta_user_id,
        "issued_at": int(now.timestamp()),
    }
    valid_signed_req = _generate_meta_signed_request(valid_payload)

    resp_valid = client.post(
        "/auth/data-deletion-callback",
        data={"signed_request": valid_signed_req},
    )
    assert resp_valid.status_code == 200, resp_valid.text
    data = resp_valid.json()
    assert "url" in data
    assert "confirmation_code" in data
    code = data["confirmation_code"]

    # Verify user deactivated and contacts deleted
    user_after = asyncio.run(mock_db.users.find_one({"_id": user_id}))
    assert user_after["is_active"] is False
    assert asyncio.run(mock_db.contacts.count_documents({"user_id": user_id_str})) == 0
    assert asyncio.run(mock_db.webhook_events.find_one({"_id": f"chg:{meta_user_id}:comments:comm_1"})) is None

    # Check status endpoint with the confirmation code
    status_resp = client.get(f"/auth/data-deletion-status?code={code}")
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["confirmation_code"] == code
    assert status_data["status"] == "completed"


def test_meta_deauthorize_callback(test_setup):
    """POST /auth/deauthorize handles signed_request and marks connection expired."""
    client, mock_db = test_setup
    now = datetime.now(timezone.utc)

    meta_user_id = f"meta_deauth_{secrets.token_hex(4)}"
    res = asyncio.run(
        mock_db.users.insert_one({
            "email": f"deauth_user_{secrets.token_hex(4)}@example.com",
            "is_active": True,
            "email_verified": True,
            "instagram_user_id": meta_user_id,
            "instagram_account_ids": [meta_user_id],
            "instagram_access_token": "token_active",
            "ig_connection_status": "active",
            "webhook_subscribed": True,
            "created_at": now,
        })
    )
    user_id = res.inserted_id

    valid_payload = {
        "algorithm": "HMAC-SHA256",
        "user_id": meta_user_id,
        "issued_at": int(now.timestamp()),
    }
    signed_req = _generate_meta_signed_request(valid_payload)

    resp = client.post("/auth/deauthorize", data={"signed_request": signed_req})
    assert resp.status_code == 200, resp.text
    assert resp.json().get("success") is True

    user_after = asyncio.run(mock_db.users.find_one({"_id": user_id}))
    assert user_after.get("instagram_access_token") is None
    assert user_after.get("ig_connection_status") == "expired"
    assert user_after.get("webhook_subscribed") is False


# ─────────────────────────────────────────────────────────────────────────────
# 4. Monthly DM count reset & TTL indexes
# ─────────────────────────────────────────────────────────────────────────────

def test_monthly_dm_count_reset_service():
    """reset_monthly_dm_counts resets counts when a new month begins."""
    mock_db = MockDB()
    now = datetime.now(timezone.utc)
    last_month = (now.replace(day=1) - timedelta(days=2)).replace(day=15)

    # User whose DM count was last reset in a previous month
    res1 = asyncio.run(
        mock_db.users.insert_one({
            "email": f"monthly_reset_{secrets.token_hex(4)}@example.com",
            "dm_count_this_month": 45,
            "dm_count_reset_at": last_month,
            "created_at": last_month,
        })
    )

    # User whose DM count was reset this month
    res2 = asyncio.run(
        mock_db.users.insert_one({
            "email": f"current_month_{secrets.token_hex(4)}@example.com",
            "dm_count_this_month": 12,
            "dm_count_reset_at": now - timedelta(hours=2),
            "created_at": now - timedelta(days=2),
        })
    )

    modified = asyncio.run(reset_monthly_dm_counts(mock_db))
    assert modified >= 1

    u1 = asyncio.run(mock_db.users.find_one({"_id": res1.inserted_id}))
    assert u1["dm_count_this_month"] == 0

    u2 = asyncio.run(mock_db.users.find_one({"_id": res2.inserted_id}))
    assert u2["dm_count_this_month"] == 12


def test_ensure_monthly_dm_count_current_inline_reset():
    """_ensure_monthly_dm_count_current resets count inline when event arrives in a new month."""
    mock_db = MockDB()
    now = datetime.now(timezone.utc)
    last_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1)

    user = {
        "_id": ObjectId(),
        "email": "inline_test@example.com",
        "dm_count_this_month": 100,
        "dm_count_reset_at": last_month,
    }
    asyncio.run(mock_db.users.insert_one(user))

    updated_user = asyncio.run(_ensure_monthly_dm_count_current(mock_db, user))
    assert updated_user["dm_count_this_month"] == 0
    assert updated_user["dm_count_reset_at"].year == now.year
    assert updated_user["dm_count_reset_at"].month == now.month


def test_indexes_created_for_ttl_and_deletion():
    """Verify that unverified_expires_at TTL and dm_logs sent_at TTL indexes are defined in _create_indexes."""
    mock_db = MockDB()
    asyncio.run(_create_indexes(mock_db))

    # Check unverified_expires_at on users
    user_ttls = [kw for k, kw in mock_db.users.indexes if k == "unverified_expires_at"]
    assert len(user_ttls) == 1
    assert user_ttls[0].get("expireAfterSeconds") == 0

    # Check sent_at TTL on dm_logs (90 days = 7776000 seconds, distinct ASCENDING index)
    dm_ttls = [
        kw for k, kw in mock_db.dm_logs.indexes
        if (k == [("sent_at", ASCENDING)] or k == "sent_at") and kw.get("expireAfterSeconds") == 7776000
    ]
    assert len(dm_ttls) == 1
    assert dm_ttls[0].get("expireAfterSeconds") == 7776000
    assert "sent_at_1" in getattr(mock_db.dm_logs, "dropped_indexes", [])

    # Check confirmation_code index on data_deletion_requests
    dd_unique = [kw for k, kw in mock_db.data_deletion_requests.indexes if k == "confirmation_code"]
    assert len(dd_unique) == 1
    assert dd_unique[0].get("unique") is True


# ─────────────────────────────────────────────────────────────────────────────
# 5. Prompt S1: Verify email already-verified no cookie, startup validation, no reset token
# ─────────────────────────────────────────────────────────────────────────────

def test_already_verified_email_with_any_otp_returns_no_set_cookie(test_setup):
    """Verify that an already-verified email with ANY OTP never receives a cookie, and OTP validation only happens for unverified."""
    client, mock_db = test_setup
    verified_email = f"already_verified_{secrets.token_hex(4)}@example.com"
    user_id = ObjectId()

    # Pre-seed an already verified user
    asyncio.run(
        mock_db.users.insert_one({
            "_id": user_id,
            "email": verified_email,
            "email_verified": True,
            "is_active": True,
            "plan": PlanType.Free.value,
            "created_at": datetime.now(timezone.utc),
        })
    )

    test_otps = ["123456", "000000", "999999", "invalid", "12", ""]
    for test_otp in test_otps:
        client.cookies.clear()
        resp = client.post("/auth/verify-email", json={"email": verified_email, "otp": test_otp})
        assert resp.status_code == 200, f"Expected 200 for OTP {test_otp}, got {resp.status_code}: {resp.text}"

        # CRITICAL SECURITY CHECK: No Set-Cookie header must EVER be sent
        assert "set-cookie" not in resp.headers, f"Set-Cookie header found in response for OTP {test_otp}!"
        assert not client.cookies.get("pg_token"), f"pg_token cookie was set on client for OTP {test_otp}!"
        assert resp.json() == {"message": "Already verified"}

    # Also verify unverified user flow:
    # 1. Bad OTP -> 400 and NO cookie
    unverified_email = f"unverified_{secrets.token_hex(4)}@example.com"
    from app.routes.auth import hash_otp
    correct_otp = "842195"
    asyncio.run(
        mock_db.users.insert_one({
            "email": unverified_email,
            "email_verified": False,
            "otp_hash": hash_otp(correct_otp),
            "otp_expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
            "otp_attempts": 0,
            "is_active": True,
            "plan": PlanType.Free.value,
            "created_at": datetime.now(timezone.utc),
        })
    )

    client.cookies.clear()
    bad_resp = client.post("/auth/verify-email", json={"email": unverified_email, "otp": "000000"})
    assert bad_resp.status_code == 400
    assert "set-cookie" not in bad_resp.headers
    assert not client.cookies.get("pg_token")

    # 2. Correct OTP -> 200 and DOES set pg_token
    good_resp = client.post("/auth/verify-email", json={"email": unverified_email, "otp": correct_otp})
    assert good_resp.status_code == 200
    assert "set-cookie" in good_resp.headers
    assert client.cookies.get("pg_token") is not None
    assert good_resp.json().get("message") == "Email verified"

    # 3. Now that it is verified, calling again with any OTP returns {"message": "Already verified"} and NO cookie
    client.cookies.clear()
    again_resp = client.post("/auth/verify-email", json={"email": unverified_email, "otp": "999999"})
    assert again_resp.status_code == 200
    assert again_resp.json() == {"message": "Already verified"}
    assert "set-cookie" not in again_resp.headers
    assert not client.cookies.get("pg_token")


def test_default_environment_is_production():
    """Verify that the default ENVIRONMENT in Settings is 'production'."""
    from app.config import Settings
    assert Settings.model_fields["ENVIRONMENT"].default == "production"


def test_startup_validation_rules():
    """Verify startup validation raises RuntimeError in production on invalid/missing critical secrets."""
    from app.config import validate_startup_config, Settings

    valid_fernet = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
    long_jwt = "a" * 32

    # 1. Valid settings passes in production
    valid_cfg = SimpleNamespace(
        ENVIRONMENT="production",
        JWT_SECRET=long_jwt,
        ENCRYPTION_KEY=valid_fernet,
        META_APP_SECRET="valid_meta_secret",
        RAZORPAY_WEBHOOK_SECRET="valid_rp_secret",
    )
    validate_startup_config(valid_cfg)  # Should not raise

    # 2. JWT_SECRET < 32 chars in production
    short_jwt_cfg = SimpleNamespace(
        ENVIRONMENT="production",
        JWT_SECRET="short_jwt_secret_under_32",
        ENCRYPTION_KEY=valid_fernet,
        META_APP_SECRET="valid_meta_secret",
        RAZORPAY_WEBHOOK_SECRET="valid_rp_secret",
    )
    with pytest.raises(RuntimeError, match="JWT_SECRET must be at least 32 characters"):
        validate_startup_config(short_jwt_cfg)

    # 3. Invalid Fernet key in production
    invalid_fernet_cfg = SimpleNamespace(
        ENVIRONMENT="production",
        JWT_SECRET=long_jwt,
        ENCRYPTION_KEY="not-a-valid-fernet-key!!!",
        META_APP_SECRET="valid_meta_secret",
        RAZORPAY_WEBHOOK_SECRET="valid_rp_secret",
    )
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY must be a valid Fernet key"):
        validate_startup_config(invalid_fernet_cfg)

    # 4. Missing / empty META_APP_SECRET in production
    empty_meta_cfg = SimpleNamespace(
        ENVIRONMENT="production",
        JWT_SECRET=long_jwt,
        ENCRYPTION_KEY=valid_fernet,
        META_APP_SECRET="",
        RAZORPAY_WEBHOOK_SECRET="valid_rp_secret",
    )
    with pytest.raises(RuntimeError, match="META_APP_SECRET must not be empty"):
        validate_startup_config(empty_meta_cfg)

    # 5. Missing / empty RAZORPAY_WEBHOOK_SECRET in production
    empty_rp_cfg = SimpleNamespace(
        ENVIRONMENT="production",
        JWT_SECRET=long_jwt,
        ENCRYPTION_KEY=valid_fernet,
        META_APP_SECRET="valid_meta_secret",
        RAZORPAY_WEBHOOK_SECRET="   ",
    )
    with pytest.raises(RuntimeError, match="RAZORPAY_WEBHOOK_SECRET must not be empty"):
        validate_startup_config(empty_rp_cfg)

    # 6. In development, does not raise on empty secrets
    dev_cfg = SimpleNamespace(
        ENVIRONMENT="development",
        JWT_SECRET="dev",
        ENCRYPTION_KEY="dev",
        META_APP_SECRET="",
        RAZORPAY_WEBHOOK_SECRET="",
    )
    validate_startup_config(dev_cfg)  # Should not raise


def test_forgot_password_never_returns_reset_token_or_url(test_setup, monkeypatch, capsys):
    """Never return reset_token or reset_url in any user or admin forgot password response; log to console only in development."""
    client, mock_db = test_setup
    test_user_email = "user_forgot@example.com"
    admin_email = "admin_forgot@example.com"

    # Pre-seed user in mock_db
    asyncio.run(
        mock_db.users.insert_one({
            "email": test_user_email,
            "email_verified": True,
            "created_at": datetime.now(timezone.utc),
        })
    )

    monkeypatch.setattr(settings, "ADMIN_EMAIL", admin_email)
    monkeypatch.setattr("app.routes.auth.send_password_reset_email", AsyncMock(return_value=True))

    # Test in development: tokens logged to console, but NEVER in response
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    _ = capsys.readouterr()  # clear buffer

    resp_user = client.post("/auth/forgot-password/request", json={"email": test_user_email})
    assert resp_user.status_code == 200
    data_user = resp_user.json()
    assert "reset_token" not in data_user
    assert "reset_url" not in data_user

    resp_admin = client.post("/admin/auth/forgot-password/request", json={"email": admin_email})
    assert resp_admin.status_code == 200
    data_admin = resp_admin.json()
    assert "reset_token" not in data_admin
    assert "reset_url" not in data_admin

    dev_captured = capsys.readouterr().out
    assert "[DEVELOPMENT ONLY]" in dev_captured
    assert "Password reset" in dev_captured

    # Test in production: tokens NEVER in response AND NEVER logged to console
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    _ = capsys.readouterr()  # clear buffer

    resp_user_prod = client.post("/auth/forgot-password/request", json={"email": test_user_email})
    assert resp_user_prod.status_code == 200
    data_user_prod = resp_user_prod.json()
    assert "reset_token" not in data_user_prod
    assert "reset_url" not in data_user_prod

    resp_admin_prod = client.post("/admin/auth/forgot-password/request", json={"email": admin_email})
    assert resp_admin_prod.status_code == 200
    data_admin_prod = resp_admin_prod.json()
    assert "reset_token" not in data_admin_prod
    assert "reset_url" not in data_admin_prod

    prod_captured = capsys.readouterr().out
    assert "[DEVELOPMENT ONLY]" not in prod_captured
    assert "Password reset" not in prod_captured


def test_reregister_per_email_limit_3_per_hour(test_setup, monkeypatch):
    """Enforce per-email limit of 3 re-registrations per hour on unverified accounts."""
    client, mock_db = test_setup
    monkeypatch.setattr("app.routes.auth.send_otp_email", AsyncMock(return_value=True))

    email = f"reregister_{secrets.token_hex(4)}@example.com"
    reg_payload = {
        "email": email,
        "password": "Password123!",
        "first_name": "Test",
    }

    # 1. Initial registration (attempt 0 for re-register)
    r0 = client.post("/auth/register", json=reg_payload)
    assert r0.status_code == 200, r0.text

    # 2. Re-register #1: allowed (valid_attempts count becomes 1)
    r1 = client.post("/auth/register", json=reg_payload)
    assert r1.status_code == 200, r1.text
    assert "resent" in r1.json().get("message", "").lower()

    # 3. Re-register #2: allowed (valid_attempts count becomes 2)
    r2 = client.post("/auth/register", json=reg_payload)
    assert r2.status_code == 200, r2.text

    # 4. Re-register #3: allowed (valid_attempts count becomes 3)
    r3 = client.post("/auth/register", json=reg_payload)
    assert r3.status_code == 200, r3.text

    # 5. Re-register #4 within 1 hour: rejected with 429!
    r4 = client.post("/auth/register", json=reg_payload)
    assert r4.status_code == 429, r4.text
    assert "too many registration attempts" in r4.json().get("detail", "").lower()


def test_meta_callbacks_warn_but_confirm_when_no_user_matches(test_setup, caplog):
    """Unknown signed_request user_id: log a warning, still return Meta's confirmation."""
    client, _mock_db = test_setup
    payload = {
        "algorithm": "HMAC-SHA256",
        "user_id": f"unknown_{secrets.token_hex(4)}",
        "issued_at": int(datetime.now(timezone.utc).timestamp()),
    }
    signed_req = _generate_meta_signed_request(payload)

    with caplog.at_level(logging.WARNING, logger="app.routes.auth"):
        deletion = client.post("/auth/data-deletion-callback", data={"signed_request": signed_req})
        deauth = client.post("/auth/deauthorize", data={"signed_request": signed_req})

    assert deletion.status_code == 200, deletion.text
    assert deletion.json()["confirmation_code"]
    assert deletion.json()["url"]
    assert deauth.status_code == 200
    assert deauth.json() == {"success": True}

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("data-deletion callback: no user matched" in m and payload["user_id"] in m for m in warnings)
    assert any("deauthorize callback: no user matched" in m and payload["user_id"] in m for m in warnings)


def test_data_deletion_callback_looks_up_by_app_scoped_id(test_setup):
    """In /auth/data-deletion-callback, user is looked up by Meta's app-scoped ID stored in meta_app_scoped_id or instagram_account_ids."""
    client, mock_db = test_setup
    now = datetime.now(timezone.utc)

    app_scoped_id = f"asid_{secrets.token_hex(6)}"
    ig_biz_id = f"ig_{secrets.token_hex(4)}"

    # Seed user with app_scoped_id in meta_app_scoped_id and instagram_account_ids
    res = asyncio.run(
        mock_db.users.insert_one({
            "email": f"asid_user_{secrets.token_hex(4)}@example.com",
            "is_active": True,
            "email_verified": True,
            "instagram_user_id": ig_biz_id,
            "instagram_account_ids": [ig_biz_id, app_scoped_id],
            "meta_app_scoped_id": app_scoped_id,
            "instagram_access_token": "token_asid_123",
            "created_at": now,
        })
    )
    user_id = res.inserted_id
    user_id_str = str(user_id)

    # Seed contacts and webhook events for this user
    asyncio.run(mock_db.contacts.insert_one({"user_id": user_id_str, "ig_user_id": "fan_123"}))
    asyncio.run(mock_db.webhook_events.insert_one({"_id": f"msg:{ig_biz_id}:fan_123:mid_1", "source": "messaging"}))

    # Build signed_request using Meta's actual App Secret containing the app-scoped ID
    payload = {
        "algorithm": "HMAC-SHA256",
        "user_id": app_scoped_id,
        "issued_at": int(now.timestamp()),
    }
    signed_req = _generate_meta_signed_request(payload, secret=settings.META_APP_SECRET)

    resp = client.post("/auth/data-deletion-callback", data={"signed_request": signed_req})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "confirmation_code" in data
    assert "url" in data

    # Verify user was matched by the app-scoped ID and deactivated
    updated_user = asyncio.run(mock_db.users.find_one({"_id": user_id}))
    assert updated_user["is_active"] is False
    assert updated_user["deleted_at"] is not None
    assert asyncio.run(mock_db.contacts.count_documents({"user_id": user_id_str})) == 0


def test_parse_meta_signed_request_validation():
    """Verify parse_meta_signed_request uses hmac.compare_digest, pads base64url, and rejects algorithm != HMAC-SHA256."""
    secret = settings.META_APP_SECRET
    now = datetime.now(timezone.utc)

    # 1. Valid signed request with stripped padding (base64url)
    valid_payload = {
        "algorithm": "HMAC-SHA256",
        "user_id": "test_user_456",
        "issued_at": int(now.timestamp()),
    }
    valid_sr = _generate_meta_signed_request(valid_payload, secret=secret)
    parsed = parse_meta_signed_request(valid_sr)
    assert parsed["user_id"] == "test_user_456"
    assert parsed["algorithm"] == "HMAC-SHA256"

    # 2. Rejects algorithm != HMAC-SHA256
    bad_algo_payload = {
        "algorithm": "HMAC-SHA1",
        "user_id": "test_user_456",
        "issued_at": int(now.timestamp()),
    }
    bad_algo_sr = _generate_meta_signed_request(bad_algo_payload, secret=secret)
    with pytest.raises(HTTPException) as exc_info:
        parse_meta_signed_request(bad_algo_sr)
    assert exc_info.value.status_code == 400
    assert "Unsupported signature algorithm" in exc_info.value.detail

    # 3. Rejects tampered signature (hmac.compare_digest verification)
    parts = valid_sr.split(".")
    tampered_sig_sr = f"tamperedSig_{parts[0][12:]}.{parts[1]}"
    with pytest.raises(HTTPException) as exc_info:
        parse_meta_signed_request(tampered_sig_sr)
    assert exc_info.value.status_code == 400
    assert "Invalid signed_request signature" in exc_info.value.detail


def test_jwt_claims_and_type_enforcement(test_setup):
    """create_jwt must include typ='session', iat, jti, and get_current_user must reject tokens without typ=='session'."""
    client, mock_db = test_setup
    user_id = ObjectId()
    email = f"jwt_claims_{secrets.token_hex(4)}@example.com"
    asyncio.run(
        mock_db.users.insert_one({
            "_id": user_id,
            "email": email,
            "email_verified": True,
            "is_active": True,
            "plan": PlanType.Free.value,
            "session_version": 1,
            "created_at": datetime.now(timezone.utc),
        })
    )

    # 1. Inspect raw token generated by create_jwt
    token = create_jwt(str(user_id), session_version=1)
    import jwt as pyjwt
    decoded = pyjwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    assert decoded.get("typ") == "session"
    assert "iat" in decoded
    assert "jti" in decoded
    assert decoded.get("sv") == 1

    # 2. Token with typ='session' succeeds on /auth/me
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["email"] == email

    # 3. Token missing typ is rejected by get_current_user
    bad_payload = {"sub": str(user_id), "sv": 1, "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    bad_token = pyjwt.encode(bad_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    bad_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {bad_token}"})
    assert bad_resp.status_code == 401
    assert "Invalid token type" in bad_resp.json()["detail"]

    # 4. Token with wrong typ (e.g. 'access') is rejected
    wrong_type_payload = {"sub": str(user_id), "typ": "access", "sv": 1, "exp": datetime.now(timezone.utc) + timedelta(hours=1)}
    wrong_type_token = pyjwt.encode(wrong_type_payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    wrong_type_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {wrong_type_token}"})
    assert wrong_type_resp.status_code == 401
    assert "Invalid token type" in wrong_type_resp.json()["detail"]


def test_user_and_admin_logout_revokes_session(test_setup, monkeypatch):
    """User and admin logout increments session version, invalidating previously issued tokens."""
    client, mock_db = test_setup

    # 1. User logout
    user_id = ObjectId()
    email = f"logout_user_{secrets.token_hex(4)}@example.com"
    asyncio.run(
        mock_db.users.insert_one({
            "_id": user_id,
            "email": email,
            "email_verified": True,
            "is_active": True,
            "plan": PlanType.Free.value,
            "session_version": 0,
            "created_at": datetime.now(timezone.utc),
        })
    )
    user_token = create_jwt(str(user_id), session_version=0)
    # Auth works
    me_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"})
    assert me_resp.status_code == 200

    # Perform logout
    logout_resp = client.post("/auth/logout", headers={"Authorization": f"Bearer {user_token}"})
    assert logout_resp.status_code == 200

    # User's session_version in DB is now incremented to 1
    updated_user = asyncio.run(mock_db.users.find_one({"_id": user_id}))
    assert updated_user.get("session_version") == 1

    # Old token (with sv=0) is now expired/revoked
    old_token_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"})
    assert old_token_resp.status_code == 401
    assert "Session expired" in old_token_resp.json()["detail"]

    # 2. Admin logout
    admin_email = "admin_logout_test@example.com"
    monkeypatch.setattr(settings, "ADMIN_EMAIL", admin_email)
    import app.routes.admin as admin_module
    monkeypatch.setattr(admin_module.settings, "ADMIN_EMAIL", admin_email)
    monkeypatch.setattr(admin_module.settings, "ADMIN_PASSWORD_HASH", admin_module.pwd_ctx.hash("AdminPass123!"))

    # Admin login
    admin_login_resp = client.post("/admin/login", json={"email": admin_email, "password": "AdminPass123!"})
    assert admin_login_resp.status_code == 200
    assert "token" not in admin_login_resp.json()  # Verifying "token" field removed

    # Access /admin/me works via cookie
    admin_me_resp = client.get("/admin/me")
    assert admin_me_resp.status_code == 200

    # Perform admin logout
    admin_logout_resp = client.post("/admin/auth/logout")
    assert admin_logout_resp.status_code == 200

    # admin_config has admin_session_version incremented
    admin_doc = asyncio.run(mock_db.admin_config.find_one({"_id": "admin_session"}))
    assert admin_doc is not None
    assert admin_doc.get("admin_session_version") >= 1

    # Using the previous cookie or bearer token is now rejected
    admin_cookie = admin_login_resp.cookies.get("pg_admin_token")
    client.cookies.clear()
    denied_resp = client.get("/admin/me", headers={"Authorization": f"Bearer {admin_cookie}"})
    assert denied_resp.status_code == 401
    assert "Session expired" in denied_resp.json()["detail"]


def test_password_reset_token_single_use(test_setup, monkeypatch):
    """Password reset token must be single-use; second attempt must be rejected."""
    client, mock_db = test_setup
    email = f"singleuse_{secrets.token_hex(4)}@example.com"
    user_id = ObjectId()
    initial_password = "InitialPassword123!"

    asyncio.run(
        mock_db.users.insert_one({
            "_id": user_id,
            "email": email,
            "hashed_password": hash_password(initial_password),
            "email_verified": True,
            "is_active": True,
            "session_version": 0,
            "created_at": datetime.now(timezone.utc),
        })
    )

    # Request password reset link
    monkeypatch.setattr("app.routes.auth.send_password_reset_email", AsyncMock(return_value=True))
    req_resp = client.post("/auth/forgot-password/request", json={"email": email})
    assert req_resp.status_code == 200

    # Check user doc has reset token hash and used=False
    user_doc = asyncio.run(mock_db.users.find_one({"_id": user_id}))
    assert user_doc.get("password_reset_token_hash") is not None
    assert user_doc.get("password_reset_token_used") is False

    # Generate a matching token using the auth route's helper
    from app.routes.auth import _create_password_reset_token
    token = _create_password_reset_token(email)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    asyncio.run(mock_db.users.update_one({"_id": user_id}, {"$set": {"password_reset_token_hash": token_hash}}))

    # First reset attempt: Succeeds
    reset_payload = {
        "email": email,
        "reset_token": token,
        "new_password": "NewSecretPassword123!",
    }
    r1 = client.post("/auth/forgot-password/reset", json=reset_payload)
    assert r1.status_code == 200, r1.text
    assert "Password updated" in r1.json()["message"]

    # Verify user doc now has password_reset_token_used = True
    updated_user = asyncio.run(mock_db.users.find_one({"_id": user_id}))
    assert updated_user.get("password_reset_token_used") is True
    assert updated_user.get("password_reset_token_hash") is None

    # Second reset attempt with same token: Rejected with 400
    r2 = client.post("/auth/forgot-password/reset", json=reset_payload)
    assert r2.status_code == 400
    assert "already been used" in r2.json()["detail"].lower()


def test_generic_404_and_dummy_bcrypt_verify(test_setup):
    """resend-otp, verify-email, forgot-password/reset return generic 404s for unknown emails, running dummy bcrypt verify."""
    client, mock_db = test_setup
    unknown_email = f"ghost_{secrets.token_hex(4)}@example.com"

    # 1. /auth/resend-otp returns 404 with generic detail
    r_resend = client.post("/auth/resend-otp", json={"email": unknown_email})
    assert r_resend.status_code == 404
    assert r_resend.json()["detail"] == "Invalid request"

    # 2. /auth/verify-email returns 404 with generic detail
    r_verify = client.post("/auth/verify-email", json={"email": unknown_email, "otp": "123456"})
    assert r_verify.status_code == 404
    assert r_verify.json()["detail"] == "Invalid request"

    # 3. /auth/forgot-password/reset returns 404 with generic detail
    from app.routes.auth import _create_password_reset_token
    dummy_token = _create_password_reset_token(unknown_email)
    r_reset = client.post(
        "/auth/forgot-password/reset",
        json={"email": unknown_email, "reset_token": dummy_token, "new_password": "NewPassword123!"},
    )
    assert r_reset.status_code == 404
    assert r_reset.json()["detail"] == "Invalid request"

    # 4. Verify dummy bcrypt verify runs during unknown login
    with patch("app.routes.auth.pwd_ctx.verify", side_effect=lambda p, h: False) as mock_verify:
        client.post("/auth/login", json={"email": unknown_email, "password": "WrongPassword123!"})
        assert mock_verify.called, "Dummy bcrypt verify was not invoked for unknown email on /auth/login!"


def test_lockout_keyed_on_email_and_ip(test_setup, monkeypatch):
    """Lockout must be keyed on (email + IP) so an attacker cannot DoS a user on another IP."""
    client, mock_db = test_setup
    # Simulates deployment behind a trusted proxy that sets X-Forwarded-For.
    monkeypatch.setattr(settings, "CLIENT_IP_HEADER", "x-forwarded-for")
    email = f"lockout_{secrets.token_hex(4)}@example.com"
    correct_pass = "GoodPassword123!"
    attacker_ip = "198.51.100.22"
    victim_ip = "203.0.113.88"

    asyncio.run(
        mock_db.users.insert_one({
            "email": email,
            "hashed_password": hash_password(correct_pass),
            "email_verified": True,
            "is_active": True,
            "plan": PlanType.Free.value,
            "session_version": 0,
            "created_at": datetime.now(timezone.utc),
        })
    )

    # Attacker fails 5 times from attacker_ip
    for _ in range(5):
        resp = client.post(
            "/auth/login",
            json={"email": email, "password": "WrongPassword!"},
            headers={"X-Forwarded-For": attacker_ip},
        )
        assert resp.status_code == 401

    # 6th attempt from attacker_ip is locked out (429)
    blocked_resp = client.post(
        "/auth/login",
        json={"email": email, "password": "WrongPassword!"},
        headers={"X-Forwarded-For": attacker_ip},
    )
    assert blocked_resp.status_code == 429
    assert "Too many login attempts" in blocked_resp.json()["detail"]

    # Legitimate user from victim_ip is NOT locked out and can log in successfully
    client.cookies.clear()
    ok_resp = client.post(
        "/auth/login",
        json={"email": email, "password": correct_pass},
        headers={"X-Forwarded-For": victim_ip},
    )
    assert ok_resp.status_code == 200, ok_resp.text


