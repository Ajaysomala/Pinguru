import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from bson import ObjectId
import httpx
from starlette.requests import Request

from app.routes.auth import (
    me,
    instagram_media,
    instagram_callback,
    create_oauth_state,
)
from app.routes.webhook import _process_webhook_payload
from app.models.models import TriggerType
from app.services.instagram import (
    InstagramService,
)
from app.services.token_refresh import refresh_expiring_tokens


class _MockCursor:
    def __init__(self, items):
        self._items = items

    async def to_list(self, length=1000):
        return list(self._items[:length])


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
                    if op == "$nin" and field_val in op_val:
                        return False
                    if op == "$ne" and field_val == op_val:
                        return False
                    if op == "$exists":
                        exists = k in doc and doc[k] is not None
                        if exists != op_val:
                            return False
                    if op == "$lte":
                        if field_val is None:
                            return False
                        if isinstance(field_val, datetime) and isinstance(op_val, str):
                            try:
                                op_dt = datetime.fromisoformat(op_val.replace("Z", "+00:00"))
                                f_dt = field_val if field_val.tzinfo else field_val.replace(tzinfo=timezone.utc)
                                o_dt = op_dt if op_dt.tzinfo else op_dt.replace(tzinfo=timezone.utc)
                                if f_dt > o_dt:
                                    return False
                            except Exception:
                                return False
                        elif isinstance(field_val, str) and isinstance(op_val, datetime):
                            try:
                                f_dt = datetime.fromisoformat(field_val.replace("Z", "+00:00"))
                                f_dt = f_dt if f_dt.tzinfo else f_dt.replace(tzinfo=timezone.utc)
                                o_dt = op_val if op_val.tzinfo else op_val.replace(tzinfo=timezone.utc)
                                if f_dt > o_dt:
                                    return False
                            except Exception:
                                return False
                        else:
                            try:
                                if field_val > op_val:
                                    return False
                            except TypeError:
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
                        self.data[i].update(update_query["$set"])
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
                new_doc.update(update_query["$set"])
            if "$setOnInsert" in update_query:
                new_doc.update(update_query["$setOnInsert"])
            new_doc["_id"] = ObjectId()
            self.data.append(new_doc)
            return SimpleNamespace(matched_count=0, upserted_id=new_doc["_id"])
        return SimpleNamespace(matched_count=0)

    def find(self, query):
        matching = [dict(item) for item in self.data if self._matches_filter(item, query)]
        return _MockCursor(matching)

    async def count_documents(self, query):
        return len([item for item in self.data if self._matches_filter(item, query)])


def _make_dummy_request():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "app": SimpleNamespace(state=SimpleNamespace()),
    }
    return Request(scope)


# ── Test 1: Long-lived token exchange failure fails connect ────────────────────

def test_exchange_code_for_token_long_lived_failure_does_not_save_1h_token(monkeypatch):
    """If long-lived exchange fails, fail the connect, don't save the 1h token."""
    monkeypatch.setattr(InstagramService, "_resolve_instagram_oauth_credentials", lambda: ("app_id", "app_secret", "test"))

    async def mock_post(client, url, data=None):
        # Short-lived succeeds
        return httpx.Response(200, json={"access_token": "short_1h_token", "user_id": "12345"})

    async def mock_get(client, url, params=None):
        # Long-lived fails with 400
        return httpx.Response(400, json={"error": {"message": "Invalid OAuth credentials", "code": 100}})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    res = asyncio.run(InstagramService.exchange_code_for_token("auth_code_123", "https://app.test/callback"))
    assert res["success"] is False
    assert "Invalid OAuth credentials" in res["error"]
    assert "token_data" not in res


# ── Test 2: Webhook subscription with mentions and fallback ───────────────────

def test_subscribe_app_to_webhooks_fallback_without_mentions(monkeypatch):
    """POST graph.instagram.com/{ig_id}/subscribed_apps falls back if mentions fails."""
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    called_urls = []

    async def mock_post(client, url, params=None):
        called_urls.append(params.get("subscribed_fields"))
        if "mentions" in (params.get("subscribed_fields") or ""):
            # Fails with 400 because mentions is not supported
            return httpx.Response(400, json={"error": {"message": "Unsupported field: mentions"}})
        # Fallback succeeds
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    success = asyncio.run(InstagramService.subscribe_app_to_webhooks("test_tok", "ig_user_789"))
    assert success is True
    assert called_urls == [
        "messages,comments,messaging_postbacks,mentions",
        "messages,comments,messaging_postbacks",
    ]


# ── Test 3: Verify granted scopes helper ──────────────────────────────────────

def test_get_granted_permissions(monkeypatch):
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    async def mock_get(client, url, params=None):
        return httpx.Response(
            200,
            json={
                "data": [
                    {"permission": "instagram_business_basic", "status": "granted"},
                    {"permission": "instagram_business_manage_messages", "status": "granted"},
                    {"permission": "instagram_business_manage_comments", "status": "declined"},
                ]
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    perms = asyncio.run(InstagramService.get_granted_permissions("tok"))
    assert perms == ["instagram_business_basic", "instagram_business_manage_messages"]


# ── Test 4: OAuth callback saves new user fields and verifies scopes ──────────

def test_oauth_callback_saves_fields_and_verifies_scopes(monkeypatch):
    user_id = ObjectId()
    user_doc = {"_id": user_id, "email": "test@pinguru.io"}
    users_col = _MockCollection([user_doc])
    db = SimpleNamespace(users=users_col)

    state = create_oauth_state(str(user_id))

    monkeypatch.setattr(
        InstagramService,
        "exchange_code_for_token",
        AsyncMock(return_value={
            "success": True,
            "token_data": {"access_token": "ll_token_999", "expires_in": 5184000, "user_id": "meta_user_1"},
        }),
    )
    monkeypatch.setattr(
        InstagramService,
        "get_user_profile",
        AsyncMock(return_value={
            "id": "ig_biz_999",
            "username": "awesome_creator",
            "user_id": "meta_user_1",
            "profile_picture_url": "https://cdn.instagram.com/pic.jpg",
            "account_type": "BUSINESS",
            "followers_count": 12500,
        }),
    )
    monkeypatch.setattr(InstagramService, "get_business_account_id", AsyncMock(return_value="ig_biz_999"))
    monkeypatch.setattr(InstagramService, "encrypt_access_token", lambda tok: f"enc_{tok}")
    monkeypatch.setattr(
        InstagramService,
        "get_granted_permissions",
        AsyncMock(return_value=["instagram_business_basic", "instagram_business_manage_messages"]),
    )
    monkeypatch.setattr(InstagramService, "subscribe_app_to_webhooks", AsyncMock(return_value=True))

    req = _make_dummy_request()
    resp = asyncio.run(instagram_callback(
        request=req,
        code="valid_code",
        state=state,
        db=db,
    ))

    assert resp.status_code in {200, 302, 307}

    saved = asyncio.run(users_col.find_one({"_id": user_id}))
    assert saved["instagram_user_id"] == "ig_biz_999"
    assert saved["ig_connection_status"] == "active"
    assert saved["webhook_subscribed"] is True
    assert saved["ig_profile_picture_url"] == "https://cdn.instagram.com/pic.jpg"
    assert saved["ig_account_type"] == "BUSINESS"
    assert saved["ig_followers_count"] == 12500
    assert saved["ig_connected_at"] is not None
    assert saved["ig_last_refreshed_at"] is not None


# ── Test 5: OAuth callback rejects when required scope is missing ─────────────

def test_oauth_callback_rejects_missing_scope(monkeypatch):
    user_id = ObjectId()
    user_doc = {"_id": user_id, "email": "test@pinguru.io"}
    users_col = _MockCollection([user_doc])
    db = SimpleNamespace(users=users_col)
    state = create_oauth_state(str(user_id))

    monkeypatch.setattr(
        InstagramService,
        "exchange_code_for_token",
        AsyncMock(return_value={
            "success": True,
            "token_data": {"access_token": "ll_token_999", "expires_in": 5184000, "user_id": "meta_user_1"},
        }),
    )
    monkeypatch.setattr(
        InstagramService,
        "get_user_profile",
        AsyncMock(return_value={"id": "ig_biz_999", "username": "creator"}),
    )
    monkeypatch.setattr(InstagramService, "get_business_account_id", AsyncMock(return_value="ig_biz_999"))
    # Only granted basic, missing manage_messages
    monkeypatch.setattr(
        InstagramService,
        "get_granted_permissions",
        AsyncMock(return_value=["instagram_business_basic"]),
    )

    req = _make_dummy_request()
    resp = asyncio.run(instagram_callback(request=req, code="valid_code", state=state, db=db))
    # Callback catches HTTPException and redirects with error detail in URL
    assert "error=" in resp.headers.get("location", "")
    assert "instagram_business_manage_messages" in resp.headers.get("location", "")


# ── Test 6: Webhook preserves token and sets needs_reauth on 401 & 400 (190/102)

def test_webhook_dm_token_error_sets_needs_reauth_and_preserves_token(monkeypatch):
    """Replace token wipe in webhook.py (401 / code 190/102 on status 400) with ig_connection_status=needs_reauth."""
    user_id = ObjectId()
    user_doc = {
        "_id": user_id,
        "email": "user@pinguru.io",
        "instagram_user_id": "biz_100",
        "instagram_account_ids": ["biz_100"],
        "instagram_access_token": "existing_valid_enc_token",
        "ig_token_expires_at": datetime.now(timezone.utc) + timedelta(days=20),
        "plan": "free",
        "ig_connection_status": "active",
    }
    rule_id = ObjectId()
    rule_doc = {
        "_id": rule_id,
        "user_id": str(user_id),
        "name": "Auto reply",
        "trigger_type": TriggerType.KEYWORD.value,
        "keywords": ["hello"],
        "match_mode": "contains",
        "reply_message": "Hey there!",
        "is_active": True,
    }

    users_col = _MockCollection([user_doc])
    rules_col = _MockCollection([rule_doc])
    dm_logs_col = _MockCollection([])
    contacts_col = _MockCollection([])
    events_col = _MockCollection([])

    db = SimpleNamespace(
        users=users_col,
        automation_rules=rules_col,
        dm_logs=dm_logs_col,
        contacts=contacts_col,
        webhook_events=events_col,
    )

    # Simulate Meta returning HTTP 400 with error code 190
    monkeypatch.setattr(
        InstagramService,
        "send_dm",
        AsyncMock(return_value={
            "success": False,
            "error": "Error validating access token: Session expired.",
            "status_code": 400,
            "error_code": 190,
        }),
    )
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"username": "fan"}))

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": "biz_100",
                "messaging": [
                    {
                        "sender": {"id": "fan_123"},
                        "recipient": {"id": "biz_100"},
                        "message": {"mid": "m_001", "text": "hello"},
                    }
                ],
            }
        ],
    }

    asyncio.run(_process_webhook_payload(db, payload, raw_body=b'{"mock": true}'))

    updated = asyncio.run(users_col.find_one({"_id": user_id}))
    # MUST NOT be wiped!
    assert updated["instagram_access_token"] == "existing_valid_enc_token"
    assert updated["instagram_user_id"] == "biz_100"
    assert updated["instagram_account_ids"] == ["biz_100"]
    assert updated["ig_token_expires_at"] is not None
    # MUST be set to needs_reauth!
    assert updated["ig_connection_status"] == "needs_reauth"


# ── Test 7: /instagram/media Meta code 190 on status 400 sets needs_reauth ────

def test_instagram_media_code_190_on_status_400_preserves_token(monkeypatch):
    """Handle Meta code 190/102 even when HTTP status is 400 in /instagram/media."""
    user_id = ObjectId()
    user_doc = {
        "_id": user_id,
        "instagram_user_id": "biz_999",
        "instagram_access_token": "my_secret_token",
        "instagram_account_ids": ["biz_999"],
        "ig_token_expires_at": datetime.now(timezone.utc) + timedelta(days=15),
    }
    users_col = _MockCollection([user_doc])
    db = SimpleNamespace(users=users_col)

    async def mock_get(client, url, params=None):
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Error validating access token: Session has expired.",
                    "type": "OAuthException",
                    "code": 190,
                }
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    req = _make_dummy_request()
    res = asyncio.run(instagram_media(
        request=req,
        media_type="all",
        limit=25,
        user=user_doc,
        db=db,
    ))

    assert res["source"] == "token_expired"
    updated = asyncio.run(users_col.find_one({"_id": user_id}))
    assert updated["ig_connection_status"] == "needs_reauth"
    # Token not wiped
    assert updated["instagram_access_token"] == "my_secret_token"
    assert updated["instagram_user_id"] == "biz_999"


# ── Test 8: /auth/me stops calling Graph and exposes status ────────────────────

def test_auth_me_no_graph_calls_and_exposes_status(monkeypatch):
    """Stop calling Graph on every /auth/me and expose ig_connection_status."""
    graph_called = False

    async def fake_get_user_profile(token):
        nonlocal graph_called
        graph_called = True
        return {}

    monkeypatch.setattr(InstagramService, "get_user_profile", fake_get_user_profile)

    now = datetime.now(timezone.utc)
    user_doc = {
        "_id": ObjectId(),
        "email": "creator@pinguru.io",
        "first_name": "Pin",
        "last_name": "Guru",
        "instagram_user_id": "biz_123",
        "instagram_username": "pinguru_official",
        "instagram_access_token": "enc_tok",
        "ig_connection_status": "active",
        "ig_connected_at": now - timedelta(days=2),
        "ig_last_refreshed_at": now - timedelta(hours=1),
        "ig_profile_picture_url": "https://img.test/pic.png",
        "ig_account_type": "BUSINESS",
        "ig_followers_count": 5400,
        "webhook_subscribed": True,
    }

    req = _make_dummy_request()
    db = SimpleNamespace()
    res = asyncio.run(me(request=req, user=user_doc, db=db))

    # Graph API must NOT be called
    assert graph_called is False
    # Status and fields are exposed
    assert res["ig_connection_status"] == "active"
    assert res["ig_connected_at"] == user_doc["ig_connected_at"]
    assert res["ig_last_refreshed_at"] == user_doc["ig_last_refreshed_at"]
    assert res["ig_profile_picture_url"] == "https://img.test/pic.png"
    assert res["ig_account_type"] == "BUSINESS"
    assert res["ig_followers_count"] == 5400
    assert res["webhook_subscribed"] is True


# ── Test 9: Startup background refresh loop refreshes expiring <= 10 days ──────

def test_refresh_expiring_tokens_loop(monkeypatch):
    """Add a startup background loop that refreshes tokens expiring within 10 days."""
    now = datetime.now(timezone.utc)
    user_expiring_soon = {
        "_id": ObjectId(),
        "email": "soon@pinguru.io",
        "instagram_access_token": "enc_tok_soon",
        "ig_token_expires_at": now + timedelta(days=5),
        "ig_connection_status": "active",
    }
    user_expiring_far = {
        "_id": ObjectId(),
        "email": "far@pinguru.io",
        "instagram_access_token": "enc_tok_far",
        "ig_token_expires_at": now + timedelta(days=45),
        "ig_connection_status": "active",
    }
    user_needs_reauth = {
        "_id": ObjectId(),
        "email": "reauth@pinguru.io",
        "instagram_access_token": "enc_tok_reauth",
        "ig_token_expires_at": now + timedelta(days=2),
        "ig_connection_status": "needs_reauth",
    }

    users_col = _MockCollection([user_expiring_soon, user_expiring_far, user_needs_reauth])
    db = SimpleNamespace(users=users_col)

    refreshed_tokens = []

    async def fake_refresh(access_token):
        refreshed_tokens.append(access_token)
        return {"access_token": f"refreshed_{access_token}", "expires_in": 5184000}

    monkeypatch.setattr(InstagramService, "refresh_long_lived_token", fake_refresh)
    monkeypatch.setattr(InstagramService, "encrypt_access_token", lambda tok: f"enc_{tok}")

    stats = asyncio.run(refresh_expiring_tokens(db, days_ahead=10))

    # Only user_expiring_soon should have been refreshed
    assert stats["refreshed"] == 1
    assert refreshed_tokens == ["enc_tok_soon"]

    updated = asyncio.run(users_col.find_one({"_id": user_expiring_soon["_id"]}))
    assert updated["instagram_access_token"] == "enc_refreshed_enc_tok_soon"
    assert updated["ig_connection_status"] == "active"
    assert updated["ig_last_refreshed_at"] is not None
    assert updated["ig_token_expires_at"] > now + timedelta(days=50)

    # far user was not touched
    far_doc = asyncio.run(users_col.find_one({"_id": user_expiring_far["_id"]}))
    assert far_doc["instagram_access_token"] == "enc_tok_far"
