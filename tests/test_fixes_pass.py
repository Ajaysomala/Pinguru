import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
import httpx
from starlette.requests import Request
from starlette.testclient import TestClient

from app.config import settings
from app.database import get_db
import app.main as main_module
from app.models.models import TriggerType
from app.routes.webhook import (
    _render_template,
    _render_comment_template,
    handle_comment_event,
    handle_messaging_event,
    _process_webhook_payload,
)
from app.routes.auth import instagram_media
from app.services.instagram import (
    InstagramService,
    InstagramTokenExpiredError,
    BASE_GRAPH_IG,
)


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


def _make_dummy_request():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/auth/instagram/media",
        "headers": [],
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_fix_1_get_messaging_user_profile_uses_base_graph_ig(monkeypatch):
    """Fix 1: verify get_messaging_user_profile uses graph.instagram.com, not facebook."""
    requested_urls = []

    async def mock_get(self, url, params=None):
        requested_urls.append((url, params))
        return httpx.Response(
            status_code=200,
            json={"name": "Alice Wonderland", "username": "alice_ig"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    result = asyncio.run(
        InstagramService.get_messaging_user_profile("token_abc", "178414000123")
    )

    assert len(requested_urls) == 1
    assert requested_urls[0][0] == f"{BASE_GRAPH_IG}/178414000123"
    assert "facebook.com" not in requested_urls[0][0]
    assert result == {"name": "Alice Wonderland", "username": "alice_ig"}


def test_fix_2_and_4_dm_trigger_renders_and_enriches_contact(monkeypatch):
    """Fix 2: DM trigger resolves name/username from profile, saves to Contact, renders template."""
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
        "name": "DM Rule",
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["promo"],
        "reply_message": "Hello {{name}} (@{{username}})! Use keyword {{keyword}} for 20% off!",
        "ask_follow_before_dm": False,
        "is_active": True,
    }

    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()
    rules_col = _MockCollection([rule])
    users_col = _MockCollection([user])
    dedup_col = _MockCollection()

    db = SimpleNamespace(
        contacts=contacts_col,
        dm_logs=dm_logs_col,
        automation_rules=rules_col,
        users=users_col,
        webhook_events=dedup_col,
    )

    sent_dms = []

    async def mock_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", mock_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(
        InstagramService,
        "get_messaging_user_profile",
        AsyncMock(return_value={"name": "Alice Wonderland", "username": "alice_w"}),
    )

    dm_payload = {
        "entry": [
            {
                "id": "biz_123",
                "messaging": [
                    {
                        "sender": {"id": "fan_dm_999"},
                        "recipient": {"id": "biz_123"},
                        "message": {"mid": "mid_001", "text": "promo please"},
                        "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
                    }
                ],
            }
        ]
    }

    raw_body = b"{}"
    res = asyncio.run(_process_webhook_payload(db, dm_payload, raw_body))
    assert res["processed_events"] == 1

    # Verify sent DM has resolved variables (not blank!)
    assert len(sent_dms) == 1
    assert "Hello Alice Wonderland (@alice_w)!" in sent_dms[0]["message"]
    assert "keyword promo for 20% off!" in sent_dms[0]["message"]

    # Verify contact record was saved with real ig_username and display_name
    contact = asyncio.run(
        contacts_col.find_one({"user_id": str(user_id), "ig_user_id": "fan_dm_999"})
    )
    assert contact is not None
    assert contact["display_name"] == "Alice Wonderland"
    assert contact["ig_username"] == "alice_w"


def test_fix_2_and_4_comment_trigger_extracts_username_and_enriches_contact(monkeypatch):
    """Fix 4 & 2: Comment trigger reads value.from.username directly, enriches contact, renders template."""
    user_id = ObjectId()
    rule_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Pro",
        "instagram_username": "my_brand",
    }
    rule = {
        "_id": rule_id,
        "user_id": str(user_id),
        "name": "Comment Rule",
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["price"],
        "reply_message": "Hey {{name}} (@{{username}}), the {{keyword}} is $49!",
        "any_comment_keyword": False,
        "ask_follow_before_dm": False,
        "public_comment_reply_enabled": True,
        "public_comment_reply_template": "Check your DMs @{{username}}!",
        "is_active": True,
    }

    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()
    rules_col = _MockCollection([rule])
    users_col = _MockCollection([user])
    dedup_col = _MockCollection()

    db = SimpleNamespace(
        contacts=contacts_col,
        dm_logs=dm_logs_col,
        automation_rules=rules_col,
        users=users_col,
        webhook_events=dedup_col,
    )

    sent_dms = []
    sent_replies = []

    async def mock_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    async def mock_reply_to_comment(access_token, comment_id, message):
        sent_replies.append({"comment_id": comment_id, "message": message})
        return {"id": "reply_123"}

    profile_lookup_mock = AsyncMock()

    monkeypatch.setattr(InstagramService, "send_dm", mock_send_dm)
    monkeypatch.setattr(InstagramService, "reply_to_comment", mock_reply_to_comment)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", profile_lookup_mock)

    comment_payload = {
        "entry": [
            {
                "id": "biz_123",
                "changes": [
                    {
                        "field": "comments",
                        "value": {
                            "from": {"id": "commenter_555", "username": "bob_instagram"},
                            "text": "what is the price?",
                            "comment_id": "comm_777",
                        },
                    }
                ],
            }
        ]
    }

    raw_body = b"{}"
    res = asyncio.run(_process_webhook_payload(db, comment_payload, raw_body))
    assert res["processed_events"] == 1

    # Public comment reply should have replaced username with bob_instagram
    assert len(sent_replies) == 1
    assert "Check your DMs @bob_instagram!" in sent_replies[0]["message"]

    # DM sent should have replaced {{name}} and {{username}} with bob_instagram (not blank!)
    assert len(sent_dms) == 1
    assert "Hey bob_instagram (@bob_instagram), the price is $49!" in sent_dms[0]["message"]

    # Verify contact record has real ig_username and display_name
    contact = asyncio.run(
        contacts_col.find_one({"user_id": str(user_id), "ig_user_id": "commenter_555"})
    )
    assert contact is not None
    assert contact["ig_username"] == "bob_instagram"
    assert contact["display_name"] == "bob_instagram"

    # Profile lookup should not have been called because comment payload provided username directly!
    profile_lookup_mock.assert_not_called()


def test_fix_3_instagram_media_token_expired_checks():
    """Fix 3: Verify token expiry check before Graph API call and error 190 reauth handling."""
    user_expired = {
        "_id": ObjectId(),
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "ig_token_expires_at": datetime.now(timezone.utc) - timedelta(days=2),
    }

    req = _make_dummy_request()
    db = SimpleNamespace()
    res = asyncio.run(
        instagram_media(
            request=req,
            media_type="all",
            limit=25,
            user=user_expired,
            db=db,
        )
    )
    assert res == {"media": [], "source": "token_expired", "connected": True}


def test_fix_3_instagram_media_error_190_flags_user(monkeypatch):
    """Fix 3: Verify error code 190 from Graph API flags user token and returns token_expired."""
    user_id = ObjectId()
    user_active = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "ig_token_expires_at": datetime.now(timezone.utc) + timedelta(days=30),
    }

    users_col = _MockCollection([user_active])
    db = SimpleNamespace(users=users_col)

    async def mock_get(self, url, params=None):
        return httpx.Response(
            status_code=400,
            json={
                "error": {
                    "message": "Error validating access token: Session has expired.",
                    "type": "OAuthException",
                    "code": 190,
                    "error_subcode": 463,
                }
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    req = _make_dummy_request()
    res = asyncio.run(
        instagram_media(
            request=req,
            media_type="all",
            limit=25,
            user=user_active,
            db=db,
        )
    )
    assert res == {"media": [], "source": "token_expired", "connected": True}

    # Verify user's connection status was updated to needs_reauth and other fields kept
    updated_user = asyncio.run(users_col.find_one({"_id": user_id}))
    assert updated_user.get("ig_connection_status") == "needs_reauth"
    assert updated_user["instagram_access_token"] == "token_abc"
    assert updated_user["instagram_user_id"] == "biz_123"


def test_dev_simulator_endpoints_with_sample_payloads(monkeypatch):
    """End-to-end test with TestClient hitting:
    1. GET /webhook/dev/sample-payload
    2. POST /webhook/dev/simulate for DM trigger
    3. POST /webhook/dev/simulate for comment trigger
    Verifies contact record has real ig_username/display_name and rendered DM text has substituted variables.
    """
    user_id = ObjectId()
    biz_id = "biz_account_123"

    user = {
        "_id": user_id,
        "instagram_user_id": biz_id,
        "instagram_access_token": "token_sim_abc",
        "plan": "Starter",
        "instagram_username": "simulator_biz",
    }
    dm_rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "name": "Sim DM Rule",
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["link bhejo"],
        "reply_message": "Hello {{name}} (@{{username}}), here is your {{keyword}}!",
        "is_active": True,
    }
    comment_rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "name": "Sim Comment Rule",
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["price"],
        "reply_message": "Hey {{name}} (@{{username}}), checking {{keyword}}!",
        "any_comment_keyword": True,
        "is_active": True,
    }

    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()
    rules_col = _MockCollection([dm_rule, comment_rule])
    users_col = _MockCollection([user])
    dedup_col = _MockCollection()

    mock_db = SimpleNamespace(
        contacts=contacts_col,
        dm_logs=dm_logs_col,
        automation_rules=rules_col,
        users=users_col,
        webhook_events=dedup_col,
    )

    async def _noop():
        return None

    monkeypatch.setattr(main_module, "connect_db", _noop)
    monkeypatch.setattr(main_module, "disconnect_db", _noop)
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")

    main_module.app.dependency_overrides[get_db] = lambda: mock_db

    sent_dms = []

    async def mock_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", mock_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(
        InstagramService,
        "get_messaging_user_profile",
        AsyncMock(return_value={"name": "Priya Sharma", "username": "priya_s"}),
    )

    with TestClient(main_module.app) as client:
        # 1. Fetch sample payloads
        sample_resp = client.get("/webhook/dev/sample-payload")
        assert sample_resp.status_code == 200
        samples = sample_resp.json()
        assert "dm_keyword" in samples
        assert "comment" in samples

        # 2. Simulate DM keyword trigger
        dm_fixture = samples["dm_keyword"]
        dm_fixture["entry"][0]["id"] = biz_id
        dm_fixture["entry"][0]["messaging"][0]["sender"]["id"] = "customer_dm_1"

        sim_dm_resp = client.post(
            "/webhook/dev/simulate",
            json={"payload": dm_fixture, "process_payload": True, "sign_with_app_secret": False},
        )
        assert sim_dm_resp.status_code == 200
        assert sim_dm_resp.json()["status"] == "simulated"
        assert sim_dm_resp.json()["processed_events"] == 1

        # Check DM sent text
        assert any(
            "Hello Priya Sharma (@priya_s), here is your link bhejo!" in m["message"]
            for m in sent_dms
        )

        # Check contact record
        dm_contact = asyncio.run(
            contacts_col.find_one({"user_id": str(user_id), "ig_user_id": "customer_dm_1"})
        )
        assert dm_contact is not None
        assert dm_contact["display_name"] == "Priya Sharma"
        assert dm_contact["ig_username"] == "priya_s"
        assert dm_contact.get("dm_count") == 1

        # 2b. Simulate DM keyword trigger for the SAME contact a second time ($set vs $setOnInsert test)
        dm_fixture_2 = json.loads(json.dumps(samples["dm_keyword"]))
        dm_fixture_2["entry"][0]["id"] = biz_id
        dm_fixture_2["entry"][0]["messaging"][0]["sender"]["id"] = "customer_dm_1"
        dm_fixture_2["entry"][0]["messaging"][0]["message"]["mid"] = "m_second_time"
        dm_fixture_2["entry"][0]["messaging"][0]["message"]["text"] = "link bhejo again"

        sim_dm_resp_2 = client.post(
            "/webhook/dev/simulate",
            json={"payload": dm_fixture_2, "process_payload": True, "sign_with_app_secret": False},
        )
        assert sim_dm_resp_2.status_code == 200
        assert sim_dm_resp_2.json()["status"] == "simulated"
        assert sim_dm_resp_2.json()["processed_events"] == 1

        # Confirm the contact doc STILL has ig_username + display_name (not overwritten or cleared)
        dm_contact_second = asyncio.run(
            contacts_col.find_one({"user_id": str(user_id), "ig_user_id": "customer_dm_1"})
        )
        assert dm_contact_second is not None
        assert dm_contact_second["display_name"] == "Priya Sharma"
        assert dm_contact_second["ig_username"] == "priya_s"
        assert dm_contact_second.get("dm_count") == 2

        # 3. Simulate Comment trigger
        comment_fixture = json.loads(json.dumps(samples["comment"]))
        comment_fixture["entry"][0]["id"] = biz_id
        comment_fixture["entry"][0]["changes"][0]["value"]["from"] = {
            "id": "customer_comment_2",
            "username": "rahul_v",
        }

        sim_comm_resp = client.post(
            "/webhook/dev/simulate",
            json={"payload": comment_fixture, "process_payload": True, "sign_with_app_secret": False},
        )
        assert sim_comm_resp.status_code == 200
        assert sim_comm_resp.json()["status"] == "simulated"
        assert sim_comm_resp.json()["processed_events"] == 1

        # Check Comment DM sent text has {{name}} properly substituted and NOT blank
        comment_dms = [m["message"] for m in sent_dms if "checking" in m["message"]]
        assert len(comment_dms) >= 1
        assert "Hey rahul_v (@rahul_v), checking price!" in comment_dms[0]
        assert "{{name}}" not in comment_dms[0]
        assert "Hey  (" not in comment_dms[0]  # confirm name wasn't rendered as empty/blank string!

        # Check contact record
        comm_contact = asyncio.run(
            contacts_col.find_one({"user_id": str(user_id), "ig_user_id": "customer_comment_2"})
        )
        assert comm_contact is not None
        assert comm_contact["display_name"] == "rahul_v"
        assert comm_contact["ig_username"] == "rahul_v"

    main_module.app.dependency_overrides.clear()


def test_decrypt_access_token_production_vs_development(monkeypatch):
    """Verify decrypt_access_token re-raises on invalid tokens in production, but allows dev fallback."""
    from cryptography.fernet import InvalidToken

    # 1. In development, plain text or invalid tokens fall back to raw input
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    plain = InstagramService.decrypt_access_token("raw_plain_text_token")
    assert plain == "raw_plain_text_token"

    # 2. In production, properly encrypted token decrypts cleanly
    real_token = "EAAB123456realtoken"
    encrypted = InstagramService.encrypt_access_token(real_token)
    assert encrypted != real_token

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    decrypted = InstagramService.decrypt_access_token(encrypted)
    assert decrypted == real_token

    # 3. In production, corrupted or invalid token re-raises (does NOT swallow)
    with pytest.raises((InvalidToken, ValueError)):
        InstagramService.decrypt_access_token("invalid_corrupted_token")


def test_corrupted_access_token_in_production_returns_clean_response_and_flags_reauth(monkeypatch):
    """Verify corrupted instagram_access_token on a user document in production returns
    a clean response (not 500) and flags the account for reauth in the DB."""
    from app.routes.auth import get_current_user

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    user_id = ObjectId()
    corrupted_token = "gAAAAABcorrupted_invalid_token_1234567890"
    user_corrupted = {
        "_id": user_id,
        "email": "creator@pinguru.io",
        "instagram_user_id": "biz_12345",
        "instagram_access_token": corrupted_token,
        "instagram_account_ids": ["biz_12345"],
        "ig_token_expires_at": datetime.now(timezone.utc) + timedelta(days=30),
        "plan": "pro",
    }

    users_col = _MockCollection([dict(user_corrupted)])
    db = SimpleNamespace(users=users_col)

    # 1. Direct call to instagram_media route handler
    req = _make_dummy_request()
    res = asyncio.run(
        instagram_media(
            request=req,
            media_type="all",
            limit=25,
            user=user_corrupted,
            db=db,
        )
    )
    # Confirm clean response (not 500) and token_expired source
    assert res["connected"] is True
    assert res["source"] == "token_expired"
    assert res["media"] == []

    # Confirm user account in DB was flagged for reauth (needs_reauth, other fields kept)
    updated_user = asyncio.run(users_col.find_one({"_id": user_id}))
    assert updated_user.get("ig_connection_status") == "needs_reauth"
    assert updated_user["instagram_access_token"] == corrupted_token
    assert updated_user["instagram_user_id"] == "biz_12345"

    # 2. TestClient HTTP GET /auth/instagram/media test
    # Reset DB with corrupted user
    users_col = _MockCollection([dict(user_corrupted)])
    mock_db = SimpleNamespace(users=users_col)

    main_module.app.dependency_overrides[get_current_user] = lambda: dict(user_corrupted)
    main_module.app.dependency_overrides[get_db] = lambda: mock_db

    try:
        client = TestClient(main_module.app)
        http_res = client.get("/auth/instagram/media")
        assert http_res.status_code == 200, f"Expected 200 but got {http_res.status_code}: {http_res.text}"
        body = http_res.json()
        assert body["source"] == "token_expired"
        assert body["media"] == []
        assert body["connected"] is True

        # Confirm DB was updated to flag reauth
        client_updated_user = asyncio.run(users_col.find_one({"_id": user_id}))
        assert client_updated_user.get("ig_connection_status") == "needs_reauth"
        assert client_updated_user["instagram_access_token"] == corrupted_token
    finally:
        main_module.app.dependency_overrides.clear()


def test_corrupted_token_service_callers_fail_gracefully_in_production(monkeypatch):
    """Verify that send_dm and get_messaging_user_profile catch decrypt failures in production
    and return safe fallback values without raising uncaught exceptions."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    # send_dm should catch InvalidToken/ValueError and return failure dict with 401
    dm_res = asyncio.run(
        InstagramService.send_dm(
            access_token="corrupted_token_123",
            recipient_ig_id="recipient_123",
            message="Hello",
            ig_user_id="biz_123",
        )
    )
    assert dm_res["success"] is False
    assert dm_res["status_code"] == 401
    assert "Invalid or corrupted access token" in dm_res["error"]

    # get_messaging_user_profile should catch InvalidToken/ValueError and return empty dict
    profile_res = asyncio.run(
        InstagramService.get_messaging_user_profile(
            access_token="corrupted_token_123",
            instagram_scoped_user_id="scoped_123",
        )
    )
    assert profile_res == {}


def test_find_user_for_ig_account_verifies_via_graph_api(monkeypatch):
    """Verify that when multiple candidates exist, _find_user_for_ig_account calls
    verify_account_ownership and matches the correct user."""
    from app.routes.webhook import _find_user_for_ig_account

    user1_id = ObjectId()
    user2_id = ObjectId()
    user1 = {
        "_id": user1_id,
        "email": "user1@example.com",
        "instagram_user_id": "scoped_user_1",
        "instagram_account_ids": ["scoped_user_1"],
        "instagram_username": "account_one",
        "instagram_access_token": "token_1",
    }
    user2 = {
        "_id": user2_id,
        "email": "user2@example.com",
        "instagram_user_id": "scoped_user_2",
        "instagram_account_ids": ["scoped_user_2"],
        "instagram_username": "account_two",
        "instagram_access_token": "token_2",
    }

    users_col = _MockCollection([user1, user2])
    db = SimpleNamespace(users=users_col)

    async def mock_verify(token, ig_account_id):
        if token == "token_1" and ig_account_id == "178414999999":
            return {"id": "scoped_user_1", "username": "account_one"}
        return None

    monkeypatch.setattr(InstagramService, "verify_account_ownership", mock_verify)

    matched_user = asyncio.run(_find_user_for_ig_account(db, "178414999999"))

    assert matched_user is not None
    assert matched_user["_id"] == user1_id
    assert "178414999999" in matched_user["instagram_account_ids"]


def test_find_user_for_ig_account_skips_expired_candidates(monkeypatch):
    """Verify that if Graph API is inconclusive, candidates with expired tokens are excluded."""
    from app.routes.webhook import _find_user_for_ig_account

    user_active_id = ObjectId()
    user_expired_id = ObjectId()
    now = datetime.now(timezone.utc)
    user_active = {
        "_id": user_active_id,
        "email": "active@example.com",
        "instagram_user_id": "scoped_active",
        "instagram_account_ids": ["scoped_active"],
        "instagram_username": "active_user",
        "instagram_access_token": "token_active",
        "ig_token_expires_at": now + timedelta(days=30),
    }
    user_expired = {
        "_id": user_expired_id,
        "email": "expired@example.com",
        "instagram_user_id": "scoped_expired",
        "instagram_account_ids": ["scoped_expired"],
        "instagram_username": "expired_user",
        "instagram_access_token": "token_expired",
        "ig_token_expires_at": now - timedelta(days=2),
    }

    users_col = _MockCollection([user_active, user_expired])
    db = SimpleNamespace(users=users_col)

    monkeypatch.setattr(InstagramService, "verify_account_ownership", AsyncMock(return_value=None))

    matched_user = asyncio.run(_find_user_for_ig_account(db, "178414888888"))

    assert matched_user is not None
    assert matched_user["_id"] == user_active_id
    assert matched_user["instagram_user_id"] == "178414888888"


def test_end_to_end_new_user_workflow_triggers_automations(monkeypatch):
    """End-to-end test for a completely new user:
    1. New user signs up and connects Instagram (stored with app-scoped ID).
    2. New user creates automation rule.
    3. External follower sends a DM matching the rule to a new 17-digit Business ID.
    4. _find_user_for_ig_account dynamically verifies ownership via Graph API and updates DB.
    5. Rule executes and reply DM is successfully dispatched."""
    new_user_id = ObjectId()
    new_rule_id = ObjectId()
    new_business_id = "17841400088888888"

    new_user = {
        "_id": new_user_id,
        "email": "new_creator@example.com",
        "plan": "Starter",
        "instagram_user_id": "scoped_new_777",
        "instagram_account_ids": ["scoped_new_777"],
        "instagram_username": "new_creator_official",
        "instagram_access_token": "token_new_creator_abc",
        "ig_token_expires_at": datetime.now(timezone.utc) + timedelta(days=60),
    }

    new_rule = {
        "_id": new_rule_id,
        "user_id": str(new_user_id),
        "name": "Sale Promo",
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["deal", "discount"],
        "reply_message": "Hello {{name}}! Here is your deal: 50% OFF!",
        "ask_follow_before_dm": False,
        "is_active": True,
        "triggers_count": 0,
        "sent_count": 0,
    }

    users_col = _MockCollection([new_user])
    rules_col = _MockCollection([new_rule])
    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()
    webhook_events_col = _MockCollection()

    db = SimpleNamespace(
        users=users_col,
        automation_rules=rules_col,
        contacts=contacts_col,
        dm_logs=dm_logs_col,
        webhook_events=webhook_events_col,
    )

    # Mock Graph API verification
    async def mock_verify(token, ig_account_id):
        if token == "token_new_creator_abc" and ig_account_id == new_business_id:
            return {"id": "scoped_new_777", "username": "new_creator_official"}
        return None

    sent_dms = []
    async def mock_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_dms.append({
            "access_token": access_token,
            "recipient_ig_id": recipient_ig_id,
            "message": message,
            "ig_user_id": ig_user_id,
        })
        return {"success": True, "data": {"message_id": "mid_new_sent_1"}}

    monkeypatch.setattr(InstagramService, "verify_account_ownership", mock_verify)
    monkeypatch.setattr(InstagramService, "send_dm", mock_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(
        InstagramService,
        "get_messaging_user_profile",
        AsyncMock(return_value={"name": "Happy Customer", "username": "customer_1"}),
    )

    incoming_messaging = {
        "sender": {"id": "customer_ig_456"},
        "recipient": {"id": new_business_id},
        "message": {"mid": "mid_incoming_123", "text": "Can I get a discount?"},
    }

    # Execute incoming message
    asyncio.run(handle_messaging_event(db, new_business_id, incoming_messaging))

    # Assert 1: User document was updated with the 17-digit Business ID
    updated_user = asyncio.run(users_col.find_one({"_id": new_user_id}))
    assert new_business_id in updated_user["instagram_account_ids"]
    assert updated_user["instagram_user_id"] == new_business_id

    # Assert 2: DM reply was sent to the customer
    assert len(sent_dms) == 1
    assert sent_dms[0]["recipient_ig_id"] == "customer_ig_456"
    assert "Hello Happy Customer! Here is your deal: 50% OFF!" in sent_dms[0]["message"]

    # Assert 3: Subsequent webhook resolves instantly from DB without calling verify_account_ownership
    verify_call_count = 0
    async def mock_verify_counted(token, ig_account_id):
        nonlocal verify_call_count
        verify_call_count += 1
        return None
    monkeypatch.setattr(InstagramService, "verify_account_ownership", mock_verify_counted)

    second_messaging = {
        "sender": {"id": "customer_ig_789"},
        "recipient": {"id": new_business_id},
        "message": {"mid": "mid_incoming_999", "text": "give me the deal"},
    }
    asyncio.run(handle_messaging_event(db, new_business_id, second_messaging))

    assert len(sent_dms) == 2
    assert verify_call_count == 0  # Instant DB match without extra API call!


def test_comment_reply_resolves_keyword_variable(monkeypatch):
    """Verify comment reply properly populates {{keyword}} in the rendered DM template."""
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
        "keywords": ["deal", "promo"],
        "any_comment_keyword": False,
        "reply_message": "Hey {{name}}! Here is your link for {{keyword}}: https://pinguru.com/deal",
        "ask_follow_before_dm": False,
        "is_active": True,
    }

    users_col = _MockCollection([user])
    rules_col = _MockCollection([rule])
    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()

    db = SimpleNamespace(
        users=users_col,
        automation_rules=rules_col,
        contacts=contacts_col,
        dm_logs=dm_logs_col,
    )

    sent_dms = []
    async def fake_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    from app.routes.webhook import handle_comment_event
    comment_payload = {
        "from": {"id": "commenter_999", "username": "superfan", "name": "Super Fan"},
        "text": "Please send me the deal!",
        "comment_id": "comm_111",
        "id": "comm_111",
    }

    asyncio.run(handle_comment_event(db, "biz_123", comment_payload))

    assert len(sent_dms) == 1
    assert "Hey Super Fan! Here is your link for deal: https://pinguru.com/deal" in sent_dms[0]["message"]
    assert "{{keyword}}" not in sent_dms[0]["message"]


def test_comment_reply_follow_gate_scoped_per_rule(monkeypatch):
    """Verify that a contact who completed follow gate on Rule A is NOT bypassed for Rule B."""
    user_id = ObjectId()
    rule_a_id = ObjectId()
    rule_b_id = ObjectId()

    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
        "instagram_username": "my_brand",
    }

    rule_b = {
        "_id": rule_b_id,
        "user_id": str(user_id),
        "name": "Post B Rule",
        "trigger_type": TriggerType.COMMENT,
        "keywords": ["access"],
        "any_comment_keyword": True,
        "reply_message": "Here is VIP access: https://pinguru.com/access",
        "ask_follow_before_dm": True,
        "is_active": True,
    }

    # Contact previously completed follow gate on Rule A
    contact = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "ig_user_id": "tester_777",
        "display_name": "Tester",
        "ig_username": "tester_777",
        "follow_gate_status": "completed",
        "follow_gate_rule_id": str(rule_a_id),
        "completed_follow_gate_rule_ids": [str(rule_a_id)],
    }

    users_col = _MockCollection([user])
    rules_col = _MockCollection([rule_b])
    contacts_col = _MockCollection([contact])
    dm_logs_col = _MockCollection()

    db = SimpleNamespace(
        users=users_col,
        automation_rules=rules_col,
        contacts=contacts_col,
        dm_logs=dm_logs_col,
    )

    sent_dms = []
    async def fake_send_dm(access_token, recipient_ig_id, message, ig_user_id, buttons=None, **kwargs):
        sent_dms.append({"recipient": recipient_ig_id, "message": message, "buttons": buttons})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    from app.routes.webhook import handle_comment_event
    comment_payload = {
        "from": {"id": "tester_777", "username": "tester_777", "name": "Tester"},
        "text": "Give me access please",
        "comment_id": "comm_222",
        "id": "comm_222",
    }

    asyncio.run(handle_comment_event(db, "biz_123", comment_payload))

    # Assert: Even though tester completed Rule A in the past, Rule B still prompts Follow Gate!
    assert len(sent_dms) == 1
    assert "Oh no! It seems you're not following me" in sent_dms[0]["message"]
    assert sent_dms[0]["buttons"] is not None
    assert any(b.get("payload") == "FOLLOWED" for b in sent_dms[0]["buttons"])


def test_story_reply_supports_keyword_and_renders_template(monkeypatch):
    """Verify story reply replaces {{keyword}} and {{name}}."""
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
        "name": "Story Rule",
        "trigger_type": TriggerType.STORY_REPLY,
        "keywords": ["coupon"],
        "reply_message": "Thanks for replying with {{keyword}}, @{{username}}!",
        "ask_follow_before_dm": False,
        "is_active": True,
    }

    users_col = _MockCollection([user])
    rules_col = _MockCollection([rule])
    contacts_col = _MockCollection([{
        "user_id": str(user_id),
        "ig_user_id": "fan_story_1",
        "display_name": "Story Fan",
        "ig_username": "story_fan",
    }])
    dm_logs_col = _MockCollection()

    db = SimpleNamespace(
        users=users_col,
        automation_rules=rules_col,
        contacts=contacts_col,
        dm_logs=dm_logs_col,
    )

    sent_dms = []
    async def fake_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)

    from app.routes.webhook import handle_story_reply_event
    story_msg = {
        "sender": {"id": "fan_story_1"},
        "message": {"text": "I want the coupon please!", "is_story_reply": True},
    }

    asyncio.run(handle_story_reply_event(db, "biz_123", story_msg))

    assert len(sent_dms) == 1
    assert "Thanks for replying with coupon, @story_fan!" in sent_dms[0]["message"]


def test_dm_keyword_takes_precedence_over_new_dm_rule(monkeypatch):
    """Verify that keyword rules take precedence over NEW_DM, and NEW_DM fires when no keyword matches."""
    user_id = ObjectId()
    kw_rule_id = ObjectId()
    new_dm_rule_id = ObjectId()

    user = {
        "_id": user_id,
        "instagram_user_id": "biz_123",
        "instagram_access_token": "token_abc",
        "plan": "Starter",
        "instagram_username": "my_brand",
    }

    kw_rule = {
        "_id": kw_rule_id,
        "user_id": str(user_id),
        "name": "Keyword Rule",
        "trigger_type": TriggerType.KEYWORD,
        "keywords": ["pricing"],
        "reply_message": "Our pricing is 199/month!",
        "is_active": True,
    }

    new_dm_rule = {
        "_id": new_dm_rule_id,
        "user_id": str(user_id),
        "name": "Welcome Rule",
        "trigger_type": TriggerType.NEW_DM,
        "keywords": [],
        "reply_message": "Welcome to PinGuru! How can we assist you today?",
        "is_active": True,
    }

    users_col = _MockCollection([user])
    rules_col = _MockCollection([new_dm_rule, kw_rule])  # Note: new_dm_rule is FIRST in DB!
    contacts_col = _MockCollection()
    dm_logs_col = _MockCollection()

    db = SimpleNamespace(
        users=users_col,
        automation_rules=rules_col,
        contacts=contacts_col,
        dm_logs=dm_logs_col,
    )

    sent_dms = []
    async def fake_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile", AsyncMock(return_value={"name": "Alice", "username": "alice"}))

    from app.routes.webhook import handle_dm_event

    # 1. Message containing keyword 'pricing' -> must trigger Keyword Rule, NOT New DM!
    pricing_msg = {
        "sender": {"id": "user_p1"},
        "message": {"text": "what is your pricing?"},
    }
    asyncio.run(handle_dm_event(db, "biz_123", pricing_msg))
    assert len(sent_dms) == 1
    assert "Our pricing is 199/month!" in sent_dms[0]["message"]

    # 2. General greeting without keywords -> triggers New DM rule
    hello_msg = {
        "sender": {"id": "user_p2"},
        "message": {"text": "hello there"},
    }
    asyncio.run(handle_dm_event(db, "biz_123", hello_msg))
    assert len(sent_dms) == 2
    assert "Welcome to PinGuru! How can we assist you today?" in sent_dms[1]["message"]





