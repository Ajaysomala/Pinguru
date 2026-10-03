import asyncio
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.models.models import (
    AutomationRuleCreate,
    PlanType,
    TriggerType,
)
from app.routes.automation import (
    ATTACHMENT_ALLOWED_TRIGGERS,
    create_rule,
    toggle_rule,
    update_rule,
)
from app.routes.webhook import (
    _evaluate_keyword_match,
    _send_rule_reply,
    handle_comment_event,
)
from app.services.instagram import InstagramService


class MockRulesCollection:
    def __init__(self, initial_rules=None):
        self.rules = list(initial_rules or [])

    async def count_documents(self, query):
        count = 0
        for r in self.rules:
            match = True
            if "user_id" in query and str(r.get("user_id")) != str(query["user_id"]):
                match = False
            if "is_active" in query and r.get("is_active") != query["is_active"]:
                match = False
            if match:
                count += 1
        return count

    async def insert_one(self, doc):
        doc = dict(doc)
        if "_id" not in doc:
            doc["_id"] = ObjectId()
        self.rules.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def find_one(self, query):
        for r in self.rules:
            match = True
            for k, v in query.items():
                if k == "_id":
                    if str(r.get("_id")) != str(v):
                        match = False
                        break
                elif str(r.get(k)) != str(v):
                    match = False
                    break
            if match:
                return dict(r)
        return None

    async def update_one(self, query, update):
        for r in self.rules:
            match = True
            for k, v in query.items():
                if k == "_id":
                    if str(r.get("_id")) != str(v):
                        match = False
                        break
                elif str(r.get(k)) != str(v):
                    match = False
                    break
            if match:
                if "$set" in update:
                    r.update(update["$set"])
                if "$inc" in update:
                    for ik, iv in update["$inc"].items():
                        r[ik] = r.get(ik, 0) + iv
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)


class _MockCursor:
    def __init__(self, items):
        self.items = items

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, _length):
        return [dict(i) for i in self.items]


class MockSimpleCollection:
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
                if str(doc.get(k)) != str(v):
                    return False
        return True

    async def find_one(self, query):
        for item in self.data:
            if self._matches_filter(item, query):
                return dict(item)
        return None

    def find(self, query):
        matching = [dict(item) for item in self.data if self._matches_filter(item, query)]
        return _MockCursor(matching)

    async def count_documents(self, query):
        return len([item for item in self.data if self._matches_filter(item, query)])

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
                if str(d.get("_id")) == str(existing.get("_id")):
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
            return SimpleNamespace(matched_count=1, modified_count=1)
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
            if "_id" not in new_doc:
                new_doc["_id"] = ObjectId()
            self.data.append(new_doc)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=new_doc["_id"])
        return SimpleNamespace(matched_count=0, modified_count=0)


# ── 1. Inactive Rules Do Not Count Toward Rule Limit ──────────────────────────

def test_inactive_rules_do_not_count_toward_rule_limit(monkeypatch):
    """5 inactive rules should not block creating a new rule on Free plan (limit=5)."""
    user_id = str(ObjectId())
    # Create 5 inactive rules
    rules = [
        {"_id": ObjectId(), "user_id": user_id, "is_active": False, "name": f"Inactive {i}"}
        for i in range(5)
    ]
    db = SimpleNamespace(automation_rules=MockRulesCollection(rules))
    free_user = {"_id": ObjectId(user_id), "plan": "free"}

    payload = AutomationRuleCreate(
        name="Active Rule 1",
        trigger_type=TriggerType.KEYWORD,
        keywords=["promo"],
        reply_message="Here is your promo code!",
    )

    # Creating should succeed because active_count == 0 < 5
    res = asyncio.run(create_rule(data=payload, db=db, user=free_user))
    assert res["rule"]["name"] == "Active Rule 1"
    assert res["rule"]["is_active"] is True


def test_activating_inactive_rule_past_limit_is_rejected():
    """Activating an inactive rule when already at max active rules raises 403."""
    user_id = str(ObjectId())
    # 5 active rules (Free plan limit is 5)
    rules = [
        {"_id": ObjectId(), "user_id": user_id, "is_active": True, "name": f"Active {i}"}
        for i in range(5)
    ]
    # Plus 1 inactive rule
    inactive_id = ObjectId()
    rules.append({"_id": inactive_id, "user_id": user_id, "is_active": False, "name": "Inactive 1"})

    db = SimpleNamespace(automation_rules=MockRulesCollection(rules))
    free_user = {"_id": ObjectId(user_id), "plan": "free"}

    with pytest.raises(HTTPException) as exc:
        asyncio.run(toggle_rule(rule_id=str(inactive_id), db=db, user=free_user))
    assert exc.value.status_code == 403
    assert "Rule limit reached" in exc.value.detail


def test_deactivating_rule_allows_activating_another():
    """Deactivating an active rule frees up a slot allowing another rule to be activated."""
    user_id = str(ObjectId())
    active_id = ObjectId()
    inactive_id = ObjectId()
    rules = [
        {"_id": active_id, "user_id": user_id, "is_active": True, "name": "Active rule"},
        {"_id": ObjectId(), "user_id": user_id, "is_active": True, "name": "Active 2"},
        {"_id": ObjectId(), "user_id": user_id, "is_active": True, "name": "Active 3"},
        {"_id": ObjectId(), "user_id": user_id, "is_active": True, "name": "Active 4"},
        {"_id": ObjectId(), "user_id": user_id, "is_active": True, "name": "Active 5"},
        {"_id": inactive_id, "user_id": user_id, "is_active": False, "name": "Inactive rule"},
    ]
    db = SimpleNamespace(automation_rules=MockRulesCollection(rules))
    free_user = {"_id": ObjectId(user_id), "plan": "free"}

    # Deactivate one active rule
    res1 = asyncio.run(toggle_rule(rule_id=str(active_id), db=db, user=free_user))
    assert res1["is_active"] is False

    # Now activate the inactive rule — should succeed!
    res2 = asyncio.run(toggle_rule(rule_id=str(inactive_id), db=db, user=free_user))
    assert res2["is_active"] is True


# ── 2. Attachments Allowed on keyword/new_dm/story for Pro ─────────────────────

def test_attachment_allowed_triggers_constant():
    assert TriggerType.KEYWORD.value in ATTACHMENT_ALLOWED_TRIGGERS
    assert TriggerType.NEW_DM.value in ATTACHMENT_ALLOWED_TRIGGERS
    assert TriggerType.STORY_REPLY.value in ATTACHMENT_ALLOWED_TRIGGERS
    assert TriggerType.STORY_MENTION.value in ATTACHMENT_ALLOWED_TRIGGERS
    assert TriggerType.COMMENT.value in ATTACHMENT_ALLOWED_TRIGGERS
    assert TriggerType.POST_COMMENT.value in ATTACHMENT_ALLOWED_TRIGGERS
    assert TriggerType.REEL_COMMENT.value in ATTACHMENT_ALLOWED_TRIGGERS


@pytest.mark.parametrize("trigger", [
    TriggerType.KEYWORD,
    TriggerType.NEW_DM,
    TriggerType.STORY_REPLY,
    TriggerType.STORY_MENTION,
])
def test_create_rule_attachment_allowed_for_pro(monkeypatch, trigger):
    """Pro plan user can attach image URL to keyword, new_dm, and story rules."""
    user_id = str(ObjectId())
    db = SimpleNamespace(automation_rules=MockRulesCollection([]))
    pro_user = {"_id": ObjectId(user_id), "plan": "pro"}

    async def mock_valid(url, max_size=8*1024*1024):
        return True, ""

    monkeypatch.setattr(InstagramService, "validate_attachment_url", mock_valid)

    payload = AutomationRuleCreate(
        name=f"Pro Attachment Test {trigger.value}",
        trigger_type=trigger,
        keywords=["look"] if trigger != TriggerType.NEW_DM else [],
        reply_message="Check out this photo!",
        dm_attachment_url="https://cdn.pinguru.ai/images/promo.jpg",
        dm_attachment_type="image",
    )

    res = asyncio.run(create_rule(data=payload, db=db, user=pro_user))
    assert res["rule"]["dm_attachment_url"] == "https://cdn.pinguru.ai/images/promo.jpg"
    assert res["rule"]["dm_attachment_type"] == "image"


def test_free_user_cannot_create_rule_with_attachment(monkeypatch):
    """Free user cannot create a rule with dm_attachment_url."""
    user_id = str(ObjectId())
    db = SimpleNamespace(automation_rules=MockRulesCollection([]))
    free_user = {"_id": ObjectId(user_id), "plan": "free"}

    payload = AutomationRuleCreate(
        name="Free Attachment Test",
        trigger_type=TriggerType.KEYWORD,
        keywords=["free"],
        reply_message="Hi",
        dm_attachment_url="https://cdn.pinguru.ai/images/promo.jpg",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(create_rule(data=payload, db=db, user=free_user))
    assert exc.value.status_code == 403
    assert "DM image attachments is available on the Pro plan only" in exc.value.detail


# ── 3. Validate Attachment URLs ───────────────────────────────────────────────

@pytest.mark.anyio
async def test_validate_attachment_url_rejects_non_https():
    is_valid, err = await InstagramService.validate_attachment_url("http://insecure.com/image.jpg")
    assert is_valid is False
    assert "https" in err.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_rejects_non_image(monkeypatch):
    class MockResp:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8", "content-length": "1024"}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def head(self, url): return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: MockClient())

    is_valid, err = await InstagramService.validate_attachment_url("https://example.com/not-an-image")
    assert is_valid is False
    assert "image" in err.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_rejects_oversized_image(monkeypatch):
    class MockResp:
        status_code = 200
        headers = {"content-type": "image/jpeg", "content-length": str(10 * 1024 * 1024)}  # 10MB > 8MB

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def head(self, url): return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: MockClient())

    is_valid, err = await InstagramService.validate_attachment_url("https://example.com/huge.jpg")
    assert is_valid is False
    assert "8MB" in err or "size" in err.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_accepts_valid_image(monkeypatch):
    class MockResp:
        status_code = 200
        headers = {"content-type": "image/png", "content-length": str(2 * 1024 * 1024)}

    class MockClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def head(self, url): return MockResp()

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: MockClient())

    is_valid, err = await InstagramService.validate_attachment_url("https://example.com/good.png")
    assert is_valid is True
    assert err == ""


@pytest.mark.anyio
async def test_validate_attachment_url_rejects_127_0_0_1():
    is_valid, err = await InstagramService.validate_attachment_url("https://127.0.0.1/image.png")
    assert is_valid is False
    assert "127.0.0.1" in err or "forbidden" in err.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_rejects_10_x():
    is_valid1, err1 = await InstagramService.validate_attachment_url("https://10.0.0.1/image.png")
    assert is_valid1 is False
    assert "10.0.0.1" in err1 or "forbidden" in err1.lower()

    is_valid2, err2 = await InstagramService.validate_attachment_url("https://10.254.1.2/image.png")
    assert is_valid2 is False
    assert "10.254.1.2" in err2 or "forbidden" in err2.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_rejects_169_254_169_254():
    is_valid, err = await InstagramService.validate_attachment_url("https://169.254.169.254/latest/meta-data/image.png")
    assert is_valid is False
    assert "169.254.169.254" in err or "forbidden" in err.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_rejects_redirect_to_private_ip(monkeypatch):
    class MockResp:
        def __init__(self, status_code, location=None):
            self.status_code = status_code
            self.headers = {"location": location} if location else {}

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def head(self, url):
            if "redirect" in url:
                return MockResp(302, "https://10.0.0.1/secret-internal.png")
            return MockResp(200)

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: MockClient())

    is_valid, err = await InstagramService.validate_attachment_url("https://example.com/redirect")
    assert is_valid is False
    assert "10.0.0.1" in err or "forbidden" in err.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_requires_port_443():
    is_valid, err = await InstagramService.validate_attachment_url("https://example.com:8443/image.png")
    assert is_valid is False
    assert "443" in err


@pytest.mark.anyio
async def test_validate_attachment_url_rejects_ipv6_forbidden():
    is_valid, err = await InstagramService.validate_attachment_url("https://[::1]/image.png")
    assert is_valid is False
    assert "forbidden" in err.lower()

    is_valid_meta, err_meta = await InstagramService.validate_attachment_url("https://[fd00:ec2::254]/image.png")
    assert is_valid_meta is False
    assert "forbidden" in err_meta.lower()


@pytest.mark.anyio
async def test_validate_attachment_url_max_redirects_exceeded(monkeypatch):
    class MockResp:
        def __init__(self, status_code, location=None):
            self.status_code = status_code
            self.headers = {"location": location} if location else {}

    class MockClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def head(self, url):
            return MockResp(302, "https://example.com/next-hop")

    monkeypatch.setattr("httpx.AsyncClient", lambda *args, **kwargs: MockClient())

    is_valid, err = await InstagramService.validate_attachment_url("https://example.com/infinite-redirect")
    assert is_valid is False
    assert "Too many redirects" in err


@pytest.mark.anyio
async def test_rule_attachment_caching_and_no_refetch_if_hash_unchanged(monkeypatch):
    """Verify that _send_rule_reply does NOT re-fetch/validate attachment when URL hash matches cached hash,
    and only revalidates when URL hash changed."""
    validate_calls = []

    async def mock_validate(url, **kwargs):
        validate_calls.append(url)
        return True, ""

    monkeypatch.setattr(InstagramService, "validate_attachment_url", mock_validate)

    send_dm_calls = []

    async def mock_send_dm(**kwargs):
        send_dm_calls.append(kwargs)
        return {"success": True, "status_code": 200}

    monkeypatch.setattr(InstagramService, "send_dm", mock_send_dm)

    user = {
        "_id": "user_cache_test",
        "plan": PlanType.Pro.value,
        "instagram_access_token": "valid_token",
        "instagram_user_id": "ig_biz_1",
    }
    url = "https://cdn.pinguru.ai/images/flyer.png"
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()

    # Rule with matching cached hash and validated_at timestamp
    rule = {
        "_id": ObjectId(),
        "user_id": "user_cache_test",
        "reply_message": "Hello!",
        "dm_attachment_url": url,
        "dm_attachment_type": "image",
        "attachment_url_hash": url_hash,
        "attachment_validated_at": datetime.now(timezone.utc),
    }

    mock_db = SimpleNamespace(
        users=MockSimpleCollection([user]),
        contacts=MockSimpleCollection(),
        dm_logs=MockSimpleCollection(),
        automation_rules=MockSimpleCollection([rule]),
    )

    # 1. Send with cached rule: validate_attachment_url should NOT be called!
    await _send_rule_reply(
        user=user,
        recipient_id="contact_1",
        rule=rule,
        trigger_type=TriggerType.KEYWORD.value,
        db=mock_db,
    )

    assert len(validate_calls) == 0, "validate_attachment_url should not be called on cache hit"
    assert len(send_dm_calls) == 2  # image DM + text DM

    # 2. Change the attachment URL on rule: now hash differs -> validate_attachment_url MUST be called!
    new_url = "https://cdn.pinguru.ai/images/new_flyer.png"
    rule["dm_attachment_url"] = new_url

    await _send_rule_reply(
        user=user,
        recipient_id="contact_1",
        rule=rule,
        trigger_type=TriggerType.KEYWORD.value,
        db=mock_db,
    )

    assert len(validate_calls) == 1
    assert validate_calls[0] == new_url
    assert rule["attachment_url_hash"] == hashlib.sha256(new_url.encode("utf-8")).hexdigest()
    assert rule["attachment_validated_at"] is not None


@pytest.mark.anyio
async def test_database_safe_create_index_catches_and_logs_error():
    from app.database import _safe_create_index

    class FailingCollection:
        name = "test_failing_col"
        async def create_index(self, keys, **kwargs):
            raise RuntimeError("Simulated Mongo index conflict")

    result = await _safe_create_index(FailingCollection(), [("sent_at", 1)])
    assert result is None


# ── 4. On Downgrade, Strip Rather Than Blocking PUT Edits ─────────────────────

def test_put_edit_on_downgrade_strips_pro_features_without_403():
    """When a user downgrades from Pro to Free, PUT /rules/{id} must NOT raise 403.
    It should strip dm_attachment_url, public comment reply, and hinglish."""
    user_id = str(ObjectId())
    rule_id = ObjectId()
    initial_rule = {
        "_id": rule_id,
        "user_id": user_id,
        "name": "Original Pro Rule",
        "trigger_type": TriggerType.COMMENT.value,
        "keywords": ["deal"],
        "match_mode": "hinglish",
        "reply_message": "Old reply message",
        "dm_attachment_url": "https://cdn.pinguru.ai/images/flyer.png",
        "dm_attachment_type": "image",
        "public_comment_reply_enabled": True,
        "public_comment_reply_template": "Check your DMs!",
        "public_comment_reply_templates": ["Check your DMs!"],
        "is_active": True,
    }
    db = SimpleNamespace(automation_rules=MockRulesCollection([initial_rule]))
    downgraded_user = {"_id": ObjectId(user_id), "plan": "free"}

    # User attempts to update the rule (still passing Pro fields from frontend cache or keeping old values)
    payload = AutomationRuleCreate(
        name="Updated Rule on Free",
        trigger_type=TriggerType.COMMENT,
        keywords=["deal"],
        match_mode="hinglish",
        reply_message="New updated reply message",
        dm_attachment_url="https://cdn.pinguru.ai/images/flyer.png",
        public_comment_reply_enabled=True,
        public_comment_reply_template="Check your DMs!",
    )

    res = asyncio.run(update_rule(rule_id=str(rule_id), data=payload, db=db, user=downgraded_user))
    assert res["updated"] is True
    rule = res["rule"]

    # Pro features must be stripped
    assert rule["dm_attachment_url"] is None
    assert rule["dm_attachment_type"] is None
    assert rule["public_comment_reply_enabled"] is False
    assert rule["public_comment_reply_template"] is None
    assert rule["public_comment_reply_templates"] == []
    assert rule["match_mode"] == "contains"

    # Explicit warning must be returned for hinglish downgrade
    assert "warning" in res
    assert "Hinglish keyword matching is available on the Pro plan only" in res["warning"]
    assert "warning" in rule


def test_create_rule_returns_warning_on_hinglish_downgrade():
    """When a non-Pro user creates a rule requesting match_mode='hinglish', a warning is returned."""
    user_id = str(ObjectId())
    db = SimpleNamespace(automation_rules=MockRulesCollection([]))
    free_user = {"_id": ObjectId(user_id), "plan": "free"}

    payload = AutomationRuleCreate(
        name="Hinglish Test",
        trigger_type=TriggerType.KEYWORD,
        keywords=["bhai"],
        match_mode="hinglish",
        reply_message="Haan bhai!",
    )

    res = asyncio.run(create_rule(data=payload, db=db, user=free_user))
    assert res["rule"]["match_mode"] == "contains"
    assert "warning" in res
    assert "Hinglish keyword matching is available on the Pro plan only" in res["warning"]


# ── 5. Re-check Plan at Send Time in _send_rule_reply (Sequential Messages) ───

def test_send_rule_reply_pro_sends_image_and_text_sequentially(monkeypatch):
    """On Pro plan, _send_rule_reply sends two sequential DMs: image first, then text."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_user_1",
        "instagram_access_token": "token_123",
        "plan": "pro",
    }
    rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "reply_message": "Here is the special offer!",
        "dm_attachment_url": "https://cdn.pinguru.ai/images/special.jpg",
        "dm_attachment_type": "image",
    }

    db = SimpleNamespace(
        contacts=MockSimpleCollection(),
        dm_logs=MockSimpleCollection(),
        automation_rules=MockSimpleCollection([rule]),
        users=MockSimpleCollection([user]),
    )

    sent_calls = []

    async def mock_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_calls.append({"message": message, "kwargs": kwargs})
        return {"success": True}

    async def mock_valid_url(url, max_size=8*1024*1024):
        return True, ""

    monkeypatch.setattr(InstagramService, "send_dm", mock_send_dm)
    monkeypatch.setattr(InstagramService, "validate_attachment_url", mock_valid_url)

    success = asyncio.run(
        _send_rule_reply(
            db=db,
            user=user,
            recipient_id="recipient_456",
            rule=rule,
            trigger_type=TriggerType.KEYWORD,
        )
    )

    assert success is True
    assert len(sent_calls) == 2

    # Message 1: image attachment with empty text
    assert sent_calls[0]["kwargs"].get("attachment_url") == "https://cdn.pinguru.ai/images/special.jpg"
    assert sent_calls[0]["message"] == ""

    # Message 2: text reply with no attachment
    assert sent_calls[1]["kwargs"].get("attachment_url") is None
    assert "Here is the special offer!" in sent_calls[1]["message"]


def test_send_rule_reply_downgraded_user_strips_attachment(monkeypatch):
    """On Free/Starter plan, _send_rule_reply strips dm_attachment_url at send time and sends 1 text DM."""
    user_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_user_1",
        "instagram_access_token": "token_123",
        "plan": "free",  # Downgraded/Free user
    }
    rule = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "reply_message": "Hello from Free user!",
        "dm_attachment_url": "https://cdn.pinguru.ai/images/special.jpg",
        "dm_attachment_type": "image",
    }

    db = SimpleNamespace(
        contacts=MockSimpleCollection(),
        dm_logs=MockSimpleCollection(),
        automation_rules=MockSimpleCollection([rule]),
        users=MockSimpleCollection([user]),
    )

    sent_calls = []

    async def mock_send_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        sent_calls.append({"message": message, "kwargs": kwargs})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", mock_send_dm)

    success = asyncio.run(
        _send_rule_reply(
            db=db,
            user=user,
            recipient_id="recipient_456",
            rule=rule,
            trigger_type=TriggerType.KEYWORD,
        )
    )

    assert success is True
    # Only 1 text message should have been sent (attachment stripped!)
    assert len(sent_calls) == 1
    assert sent_calls[0]["kwargs"].get("attachment_url") is None
    assert "Hello from Free user!" in sent_calls[0]["message"]


# ── 6. Re-check Plan at Send Time for Public Comment Reply ────────────────────

def test_comment_event_public_reply_sent_for_pro_user(monkeypatch):
    """On Pro plan, public comment reply is executed."""
    user_id = ObjectId()
    rule_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_100",
        "instagram_access_token": "tok_100",
        "plan": "pro",
    }
    rule = {
        "_id": rule_id,
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT.value,
        "keywords": ["price"],
        "reply_message": "DM reply text",
        "public_comment_reply_enabled": True,
        "public_comment_reply_template": "Check your DMs @{{username}}!",
        "is_active": True,
    }

    db = SimpleNamespace(
        contacts=MockSimpleCollection(),
        dm_logs=MockSimpleCollection(),
        automation_rules=MockSimpleCollection([rule]),
        users=MockSimpleCollection([user]),
    )

    replies = []
    dms = []

    async def mock_reply(access_token, comment_id, message):
        replies.append({"comment_id": comment_id, "message": message})
        return {"id": "rep_1"}

    async def mock_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "reply_to_comment", mock_reply)
    monkeypatch.setattr(InstagramService, "send_dm", mock_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda t: t)

    comment_value = {
        "from": {"id": "commenter_999", "username": "buyer_bob"},
        "text": "what is the price?",
        "comment_id": "comm_123",
    }

    asyncio.run(handle_comment_event(db, "biz_100", comment_value))
    assert len(replies) == 1
    assert "Check your DMs @buyer_bob!" in replies[0]["message"]
    assert len(dms) == 1


def test_comment_event_public_reply_stripped_for_starter_or_free_user(monkeypatch):
    """On Free or Starter plan, public comment reply is stripped at send time."""
    user_id = ObjectId()
    rule_id = ObjectId()
    user = {
        "_id": user_id,
        "instagram_user_id": "biz_100",
        "instagram_access_token": "tok_100",
        "plan": "starter",  # Non-Pro!
    }
    rule = {
        "_id": rule_id,
        "user_id": str(user_id),
        "trigger_type": TriggerType.COMMENT.value,
        "keywords": ["price"],
        "reply_message": "DM reply text",
        "public_comment_reply_enabled": True,
        "public_comment_reply_template": "Check your DMs @{{username}}!",
        "is_active": True,
    }

    db = SimpleNamespace(
        contacts=MockSimpleCollection(),
        dm_logs=MockSimpleCollection(),
        automation_rules=MockSimpleCollection([rule]),
        users=MockSimpleCollection([user]),
    )

    replies = []
    dms = []

    async def mock_reply(access_token, comment_id, message):
        replies.append({"comment_id": comment_id, "message": message})
        return {"id": "rep_1"}

    async def mock_dm(access_token, recipient_ig_id, message, ig_user_id, **kwargs):
        dms.append({"recipient": recipient_ig_id, "message": message})
        return {"success": True}

    monkeypatch.setattr(InstagramService, "reply_to_comment", mock_reply)
    monkeypatch.setattr(InstagramService, "send_dm", mock_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda t: t)

    comment_value = {
        "from": {"id": "commenter_999", "username": "buyer_bob"},
        "text": "what is the price?",
        "comment_id": "comm_123",
    }

    asyncio.run(handle_comment_event(db, "biz_100", comment_value))
    # Public reply must NOT be sent
    assert len(replies) == 0
    # But DM reply still sends
    assert len(dms) == 1


# ── 7. Re-check Plan at Send Time for Hinglish ────────────────────────────────

def test_evaluate_keyword_match_hinglish_requires_pro():
    """Hinglish slang matching only works for Pro; downgraded to contains on non-Pro."""
    # "bhai kitne ka hai" -> keyword is "price"
    # In hinglish_keyword_match, "kitne" maps to price
    message = "bhai kitne ka hai"
    keywords = ["price"]

    # Pro plan: Hinglish matches
    is_match_pro, _ = _evaluate_keyword_match(
        message_text=message,
        keywords=keywords,
        match_mode="hinglish",
        user_plan=PlanType.Pro,
    )
    assert is_match_pro is True

    # Free plan: Hinglish downgraded to contains (does not match "price" in "bhai kitne ka hai")
    is_match_free, _ = _evaluate_keyword_match(
        message_text=message,
        keywords=keywords,
        match_mode="hinglish",
        user_plan=PlanType.Free,
    )
    assert is_match_free is False

    # Starter plan: Hinglish also downgraded
    is_match_starter, _ = _evaluate_keyword_match(
        message_text=message,
        keywords=keywords,
        match_mode="hinglish",
        user_plan=PlanType.Starter,
    )
    assert is_match_starter is False

    # But if the word is explicitly in the message, contains matching finds it on Free
    is_match_contains, matched_kw = _evaluate_keyword_match(
        message_text="bhai what is the price",
        keywords=keywords,
        match_mode="hinglish",
        user_plan=PlanType.Free,
    )
    assert is_match_contains is True
    assert matched_kw == "price"


def test_cap_total_rules_at_50_regardless_of_is_active():
    """Cap total rules per user at 50 regardless of is_active."""
    user_id = str(ObjectId())
    user = {"_id": ObjectId(user_id), "plan": PlanType.Pro.value}

    class MockRulesWithCount:
        def __init__(self, count):
            self._count = count

        async def count_documents(self, query):
            if "is_active" in query:
                return 10
            return self._count

        async def insert_one(self, doc):
            return SimpleNamespace(inserted_id=ObjectId())

    # User already has 50 total rules (even though plan is Pro)
    db = SimpleNamespace(automation_rules=MockRulesWithCount(50))
    payload = AutomationRuleCreate(
        name="Rule 51",
        trigger_type=TriggerType.KEYWORD,
        keywords=["hello"],
        reply_message="Hi there!",
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(create_rule(data=payload, db=db, user=user))

    assert exc_info.value.status_code == 403
    assert "50 total rules" in exc_info.value.detail.lower()
