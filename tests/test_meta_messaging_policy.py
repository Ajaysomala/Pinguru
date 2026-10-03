"""G1: Meta messaging rules (24h window, private replies, rate limits/retries, opt-out)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.models.models import TriggerType
from app.routes import webhook as webhook_module
from app.routes.webhook import _send_rule_reply, handle_change_event, handle_messaging_event
from app.services import dm_delivery
from app.services.dm_delivery import (
    PRIVATE_REPLY_HOURLY_LIMIT,
    event_context,
    is_opt_out_message,
    process_due_dm_retries,
    send_dm_with_policy,
)
from app.services.instagram import InstagramService

NOW = datetime.now(timezone.utc)


# ── In-memory Mongo stand-in ──────────────────────────────────────────────────

def _cmp(value, cond):
    if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$in" and value not in arg:
                return False
            if op == "$lte" and not (value is not None and value <= arg):
                return False
            if op == "$gte" and not (value is not None and value >= arg):
                return False
            if op == "$exists" and (value is not None) != arg:
                return False
        return True
    return value == cond


def _match(doc, query):
    for key, cond in query.items():
        if key == "$or":
            if not any(_match(doc, sub) for sub in cond):
                return False
        elif not _cmp(doc.get(key), cond):
            return False
    return True


def _apply(doc, update, inserting=False):
    doc.update(update.get("$set", {}))
    if inserting:
        doc.update(update.get("$setOnInsert", {}))
    for key, value in update.get("$max", {}).items():
        if doc.get(key) is None or value > doc[key]:
            doc[key] = value
    for key, value in update.get("$inc", {}).items():
        doc[key] = doc.get(key, 0) + value
    for key, value in update.get("$addToSet", {}).items():
        doc.setdefault(key, [])
        if value not in doc[key]:
            doc[key].append(value)


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *_a, **_k):
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    async def to_list(self, _n):
        return list(self.docs)


class Collection:
    def __init__(self, docs=None, unique=None):
        self.docs = list(docs or [])
        self.unique = unique

    async def find_one(self, query, *_args, **_kwargs):
        return next((d for d in self.docs if _match(d, query)), None)

    def find(self, query, *_args, **_kwargs):
        return _Cursor([d for d in self.docs if _match(d, query)])

    async def count_documents(self, query):
        return sum(1 for d in self.docs if _match(d, query))

    async def insert_one(self, doc):
        if self.unique and any(all(d.get(k) == doc.get(k) for k in self.unique) for d in self.docs):
            raise DuplicateKeyError("duplicate key")
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, query, update, upsert=False):
        doc = await self.find_one(query)
        if doc:
            _apply(doc, update)
            return SimpleNamespace(matched_count=1)
        if upsert:
            new = {k: v for k, v in query.items() if not k.startswith("$")}
            _apply(new, update, inserting=True)
            new.setdefault("_id", ObjectId())
            self.docs.append(new)
        return SimpleNamespace(matched_count=0)

    async def update_many(self, query, update):
        matched = [d for d in self.docs if _match(d, query)]
        for doc in matched:
            _apply(doc, update)
        return SimpleNamespace(matched_count=len(matched))

    async def delete_one(self, query):
        doc = await self.find_one(query)
        if doc:
            self.docs.remove(doc)

    async def find_one_and_update(self, query, update, sort=None, return_document=None):
        candidates = [d for d in self.docs if _match(d, query)]
        if sort:
            key, _direction = sort[0]
            candidates.sort(key=lambda d: d.get(key) or NOW)
        if not candidates:
            return None
        _apply(candidates[0], update)
        return candidates[0]


def make_db(user, contacts=None, rules=None):
    return SimpleNamespace(
        users=Collection([user]),
        contacts=Collection(contacts or []),
        dm_logs=Collection(),
        automation_rules=Collection(rules or []),
        comment_dm_history=Collection(unique=("user_id", "comment_id")),
        dm_retry_queue=Collection(),
        webhook_events=Collection(),
    )


def make_user(plan="pro"):
    return {
        "_id": ObjectId(),
        "email": "biz@example.com",
        "plan": plan,
        "instagram_user_id": "biz_1",
        "instagram_account_ids": ["biz_1"],
        "instagram_access_token": "enc_token",
        "instagram_username": "brand",
        "dm_count_this_month": 0,
    }


@pytest.fixture
def sends(monkeypatch):
    """Records Graph sends; responses can be scripted per call."""
    calls = []
    responses = []

    async def fake_send_dm(access_token, recipient_ig_id, message, ig_user_id, attachment_url=None,
                           attachment_type="image", comment_id=None, buttons=None):
        calls.append({"recipient": recipient_ig_id, "message": message, "comment_id": comment_id,
                      "attachment_url": attachment_url})
        return responses.pop(0) if responses else {"success": True}

    monkeypatch.setattr(InstagramService, "send_dm", fake_send_dm)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    monkeypatch.setattr(InstagramService, "get_messaging_user_profile",
                        lambda *a, **k: asyncio.sleep(0, result={}))
    return SimpleNamespace(calls=calls, responses=responses)


def run(coro):
    return asyncio.run(coro)


# ── (1) 24h messaging window ──────────────────────────────────────────────────

def test_dm_without_inbound_is_skipped_outside_window(sends):
    user = make_user()
    db = make_db(user)
    result = run(send_dm_with_policy(db, user, "fan_1", "hello"))
    assert result == {"success": False, "skipped": "outside_window", "error": "outside_window"}
    assert sends.calls == []


@pytest.mark.parametrize("hours_ago,expected", [(23, True), (25, False)])
def test_dm_uses_last_inbound_at(sends, hours_ago, expected):
    user = make_user()
    contact = {"user_id": str(user["_id"]), "ig_user_id": "fan_1", "last_inbound_at": NOW - timedelta(hours=hours_ago)}
    db = make_db(user, contacts=[contact])
    result = run(send_dm_with_policy(db, user, "fan_1", "hello"))
    assert result.get("success") is expected
    assert len(sends.calls) == (1 if expected else 0)
    if not expected:
        assert result["skipped"] == "outside_window"


def test_inbound_message_records_last_inbound_at_and_opens_window(sends):
    user = make_user()
    contact = {"user_id": str(user["_id"]), "ig_user_id": "fan_1"}
    db = make_db(user, contacts=[contact])
    ts_ms = int(NOW.timestamp() * 1000)
    run(handle_messaging_event(db, "biz_1", {
        "sender": {"id": "fan_1"}, "recipient": {"id": "biz_1"}, "timestamp": ts_ms,
        "message": {"mid": "m1", "text": "hi there"},
    }))
    assert abs((contact["last_inbound_at"] - NOW).total_seconds()) < 1
    # Later, outside the event, a follow-up DM is allowed because of the stored time.
    assert run(send_dm_with_policy(db, user, "fan_1", "follow-up"))["success"] is True


def test_echo_and_own_messages_do_not_open_window(sends):
    user = make_user()
    contact = {"user_id": str(user["_id"]), "ig_user_id": "fan_1"}
    db = make_db(user, contacts=[contact])
    run(handle_messaging_event(db, "biz_1", {
        "sender": {"id": "fan_1"}, "message": {"mid": "m1", "text": "x", "is_echo": True},
    }))
    assert "last_inbound_at" not in contact


def test_comment_trigger_text_after_image_is_skipped_outside_window(sends):
    """Private reply = the image; the follow-up text would be a second message the commenter never opened."""
    user = make_user("pro")
    rule = {
        "_id": ObjectId(), "user_id": str(user["_id"]), "name": "img", "trigger_type": TriggerType.COMMENT.value,
        "keywords": ["price"], "reply_message": "Here you go", "dm_attachment_url": "https://cdn.example.com/a.png",
        "attachment_url_hash": __import__("hashlib").sha256(b"https://cdn.example.com/a.png").hexdigest(),
        "attachment_validated_at": NOW, "is_active": True,
    }
    db = make_db(user, rules=[rule])
    with event_context(comment_id="c1", comment_created_at=NOW):
        run(_send_rule_reply(db, user, "fan_1", rule, TriggerType.COMMENT, matched_keyword="price", comment_id="c1"))

    assert [c["comment_id"] for c in sends.calls] == ["c1"]  # only the image private reply was sent
    assert sends.calls[0]["attachment_url"]
    statuses = sorted((log["status"], log.get("skip_reason")) for log in db.dm_logs.docs)
    assert statuses == [("sent", None), ("skipped", "outside_window")]


# ── (2) One private reply per comment, 7-day limit ────────────────────────────

def test_one_private_reply_per_comment(sends):
    user = make_user()
    db = make_db(user)
    first = run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="c1"))
    second = run(send_dm_with_policy(db, user, "fan_1", "again", comment_id="c1"))
    assert first["success"] is True
    assert second["skipped"] == "comment_already_replied"
    assert len(sends.calls) == 1
    [claim] = db.comment_dm_history.docs
    assert claim["status"] == "sent" and claim["sent_at"]


def test_comment_redelivered_after_dedup_expiry_sends_once(sends):
    user = make_user("starter")
    rule = {"_id": ObjectId(), "user_id": str(user["_id"]), "name": "c", "trigger_type": TriggerType.COMMENT.value,
            "keywords": ["info"], "reply_message": "Details", "is_active": True}
    db = make_db(user, rules=[rule])
    change = {"field": "comments", "value": {"id": "c42", "text": "info please", "from": {"id": "fan_9"},
                                              "media": {"id": "m1", "media_product_type": "FEED"}},
              "_entry_time": int(NOW.timestamp())}
    run(handle_change_event(db, "biz_1", change))
    db.webhook_events.docs.clear()  # Meta redelivers after the 48h dedup window
    run(handle_change_event(db, "biz_1", change))
    private_replies = [c for c in sends.calls if c["comment_id"] == "c42"]
    assert len(private_replies) == 1


def test_failed_private_reply_releases_claim(sends):
    user = make_user()
    db = make_db(user)
    sends.responses.append({"success": False, "status_code": 400, "error_code": 100})
    assert run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="c1"))["success"] is False
    assert db.comment_dm_history.docs == []
    assert run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="c1"))["success"] is True


@pytest.mark.parametrize("days_old,allowed", [(6, True), (8, False)])
def test_private_reply_refused_for_comments_older_than_7_days(sends, days_old, allowed):
    user = make_user()
    db = make_db(user)
    with event_context(comment_id="c1", comment_created_at=NOW - timedelta(days=days_old)):
        result = run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="c1"))
    assert result.get("success") is allowed
    if not allowed:
        assert result["skipped"] == "comment_too_old"
        assert sends.calls == []
        # Never retried later either.
        assert run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="c1"))["skipped"] == "comment_already_replied"


def test_comment_age_taken_from_webhook_entry_time(sends):
    user = make_user("starter")
    rule = {"_id": ObjectId(), "user_id": str(user["_id"]), "name": "c", "trigger_type": TriggerType.COMMENT.value,
            "keywords": ["info"], "reply_message": "Details", "is_active": True}
    db = make_db(user, rules=[rule])
    old = int((NOW - timedelta(days=9)).timestamp())
    run(handle_change_event(db, "biz_1", {
        "field": "comments", "_entry_time": old,
        "value": {"id": "c7", "text": "info", "from": {"id": "fan_7"}, "media": {"id": "m1", "media_product_type": "FEED"}},
    }))
    assert [c for c in sends.calls if c["comment_id"] == "c7"] == []


# ── (3) Rate limit and retry queue ────────────────────────────────────────────

def _fill_sent_replies(db, user, count):
    for i in range(count):
        db.comment_dm_history.docs.append({"user_id": str(user["_id"]), "comment_id": f"old{i}",
                                           "status": "sent", "sent_at": NOW - timedelta(minutes=30)})


@pytest.mark.parametrize("already_sent,queued", [(PRIVATE_REPLY_HOURLY_LIMIT - 1, False), (PRIVATE_REPLY_HOURLY_LIMIT, True)])
def test_private_reply_hourly_limit(sends, already_sent, queued):
    user = make_user()
    db = make_db(user)
    _fill_sent_replies(db, user, already_sent)
    result = run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="new"))
    assert result["success"] is True
    assert bool(result.get("queued")) is queued
    assert len(sends.calls) == (0 if queued else 1)
    if queued:
        [job] = db.dm_retry_queue.docs
        assert job["attempts"] == 0 and job["last_error"] == "local_hourly_limit"


def test_old_replies_do_not_count_toward_hourly_limit(sends):
    user = make_user()
    db = make_db(user)
    for i in range(PRIVATE_REPLY_HOURLY_LIMIT):
        db.comment_dm_history.docs.append({"user_id": str(user["_id"]), "comment_id": f"o{i}",
                                           "status": "sent", "sent_at": NOW - timedelta(minutes=61)})
    assert run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="new")).get("queued") is None


@pytest.mark.parametrize("response", [
    {"success": False, "status_code": 429, "error_code": None},
    {"success": False, "status_code": 400, "error_code": 613},
])
def test_rate_limited_send_is_queued(sends, response):
    user = make_user()
    contact = {"user_id": str(user["_id"]), "ig_user_id": "fan_1", "last_inbound_at": NOW}
    db = make_db(user, contacts=[contact])
    sends.responses.append(response)
    result = run(send_dm_with_policy(db, user, "fan_1", "hi"))
    assert result["success"] is True and result["queued"] is True
    [job] = db.dm_retry_queue.docs
    assert job["attempts"] == 1 and job["status"] == "pending"
    delay = (job["next_run_at"] - NOW).total_seconds()
    assert 55 <= delay <= 75
    assert dm_delivery.dm_log_fields(result) == {"status": "queued", "retry_job_id": str(job["_id"])}


def _due(db):
    for job in db.dm_retry_queue.docs:
        if job["status"] == "pending":
            job["next_run_at"] = NOW - timedelta(seconds=1)


def test_retry_backoff_then_failed_after_5_tries(sends):
    user = make_user()
    contact = {"user_id": str(user["_id"]), "ig_user_id": "fan_1", "last_inbound_at": NOW}
    db = make_db(user, contacts=[contact])
    rate_limited = {"success": False, "status_code": 429, "error_code": None}
    sends.responses.extend([dict(rate_limited) for _ in range(5)])

    result = run(send_dm_with_policy(db, user, "fan_1", "hi", rule_id="r1"))
    db.dm_logs.docs.append({"retry_job_id": result["retry_job_id"], **dm_delivery.dm_log_fields(result)})
    [job] = db.dm_retry_queue.docs

    delays = []
    for _ in range(4):
        _due(db)
        before = dm_delivery._utcnow()
        run(process_due_dm_retries(db))
        if job["status"] == "pending":
            delays.append(round((job["next_run_at"] - before).total_seconds() / 60))

    assert len(sends.calls) == 5  # 1 initial + 4 retries
    assert delays == [2, 4, 8]  # minutes after tries 2, 3, 4
    assert job["status"] == "failed" and job["attempts"] == 5
    [log] = db.dm_logs.docs
    assert log["status"] == "failed"
    assert log["failure_reason"].startswith("max_attempts_reached")
    assert log["retry_attempts"] == 5


def test_retry_success_marks_log_sent(sends):
    user = make_user()
    db = make_db(user)
    sends.responses.append({"success": False, "status_code": 429})
    result = run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="c1"))
    db.dm_logs.docs.append({"retry_job_id": result["retry_job_id"], **dm_delivery.dm_log_fields(result)})
    assert db.comment_dm_history.docs[0]["status"] == "queued"

    _due(db)
    assert run(process_due_dm_retries(db)) == 1
    assert db.dm_logs.docs[0]["status"] == "sent"
    assert db.comment_dm_history.docs[0]["status"] == "sent"
    assert db.dm_retry_queue.docs[0]["status"] == "done"
    assert [c["comment_id"] for c in sends.calls] == ["c1", "c1"]


def test_worker_rechecks_window_and_comment_age(sends):
    user = make_user()
    db = make_db(user)
    db.dm_retry_queue.docs.extend([
        {"_id": ObjectId(), "user_id": str(user["_id"]), "recipient_id": "fan_1", "status": "pending",
         "attempts": 1, "next_run_at": NOW - timedelta(seconds=1), "inbound_at": NOW - timedelta(hours=30),
         "send": {"message": "late", "comment_id": None}},
        {"_id": ObjectId(), "user_id": str(user["_id"]), "recipient_id": "fan_2", "status": "pending",
         "attempts": 1, "next_run_at": NOW - timedelta(seconds=1), "comment_created_at": NOW - timedelta(days=8),
         "send": {"message": "old", "comment_id": "c9"}},
    ])
    run(process_due_dm_retries(db))
    assert sends.calls == []
    assert {j["last_error"] for j in db.dm_retry_queue.docs} == {"outside_window", "comment_too_old"}
    assert all(log["status"] == "failed" for log in db.dm_logs.docs)


def test_stale_processing_job_is_reclaimed(sends):
    user = make_user()
    db = make_db(user)
    db.contacts.docs.append({"user_id": str(user["_id"]), "ig_user_id": "fan_1", "last_inbound_at": NOW})
    db.dm_retry_queue.docs.append({
        "_id": ObjectId(), "user_id": str(user["_id"]), "recipient_id": "fan_1", "status": "processing",
        "locked_at": NOW - timedelta(minutes=15), "attempts": 1, "next_run_at": NOW - timedelta(minutes=16),
        "send": {"message": "hi", "comment_id": None},
    })
    assert run(process_due_dm_retries(db)) == 1
    assert db.dm_retry_queue.docs[0]["status"] == "done"


# ── (4) Opt-out ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("STOP", True), ("stop", True), (" Stop! ", True), ("UNSUBSCRIBE", True), ("unsubscribe.", True),
    ("stop messaging", True), ("Stop   Messaging", True), ("STOP MESSAGING!!", True),
    ("please don't stop", False), ("stop it now", False), ("stopped", False), ("", False), (None, False),
])
def test_opt_out_phrases(text, expected):
    assert is_opt_out_message(text) is expected


def _inbound(text, sender="fan_1"):
    return {"sender": {"id": sender}, "recipient": {"id": "biz_1"},
            "timestamp": int(NOW.timestamp() * 1000), "message": {"mid": f"m_{text}", "text": text}}


def test_opt_out_creates_contact_and_suppresses_all_automation(sends):
    user = make_user("pro")
    dm_rule = {"_id": ObjectId(), "user_id": str(user["_id"]), "name": "kw", "trigger_type": TriggerType.KEYWORD.value,
               "keywords": ["price"], "reply_message": "Prices", "is_active": True}
    comment_rule = {"_id": ObjectId(), "user_id": str(user["_id"]), "name": "c", "trigger_type": TriggerType.COMMENT.value,
                    "keywords": ["price"], "reply_message": "Prices", "is_active": True,
                    "public_comment_reply_enabled": True, "public_comment_reply_templates": ["Check your DMs!"]}
    db = make_db(user, rules=[dm_rule, comment_rule])
    public_replies = []

    async def fake_public_reply(access_token, comment_id, message):
        public_replies.append(comment_id)
        return {"id": "r1"}

    InstagramService.reply_to_comment, original = fake_public_reply, InstagramService.reply_to_comment
    try:
        run(handle_messaging_event(db, "biz_1", _inbound("STOP")))
        [contact] = db.contacts.docs
        assert contact["opted_out"] is True and contact["opted_out_at"]
        assert sends.calls == []  # no reply to STOP itself

        run(handle_messaging_event(db, "biz_1", _inbound("price")))
        run(handle_change_event(db, "biz_1", {"field": "comments", "_entry_time": int(NOW.timestamp()), "value": {
            "id": "c1", "text": "price", "from": {"id": "fan_1"}, "media": {"id": "m1", "media_product_type": "FEED"}}}))
        assert sends.calls == []
        assert public_replies == []
        assert run(send_dm_with_policy(db, user, "fan_1", "hi"))["skipped"] == "opted_out"
        assert run(send_dm_with_policy(db, user, "fan_1", "hi", comment_id="c2"))["skipped"] == "opted_out"

        # Control: the same comment from someone who did not opt out gets both replies.
        run(handle_change_event(db, "biz_1", {"field": "comments", "_entry_time": int(NOW.timestamp()), "value": {
            "id": "c3", "text": "price", "from": {"id": "fan_2"}, "media": {"id": "m1", "media_product_type": "FEED"}}}))
        assert public_replies == ["c3"]
        assert [c["comment_id"] for c in sends.calls] == ["c3"]
    finally:
        InstagramService.reply_to_comment = original


def test_opt_out_stops_queued_retries(sends):
    user = make_user()
    contact = {"user_id": str(user["_id"]), "ig_user_id": "fan_1", "last_inbound_at": NOW}
    db = make_db(user, contacts=[contact])
    sends.responses.append({"success": False, "status_code": 429})
    run(send_dm_with_policy(db, user, "fan_1", "hi"))
    contact["opted_out"] = True
    _due(db)
    run(process_due_dm_retries(db))
    assert len(sends.calls) == 1
    assert db.dm_retry_queue.docs[0]["status"] == "failed"
    assert db.dm_retry_queue.docs[0]["last_error"] == "opted_out"


def test_non_opt_out_message_still_triggers_automation(sends):
    user = make_user("starter")
    rule = {"_id": ObjectId(), "user_id": str(user["_id"]), "name": "kw", "trigger_type": TriggerType.KEYWORD.value,
            "keywords": ["price"], "reply_message": "Prices here", "is_active": True}
    db = make_db(user, rules=[rule])
    run(handle_messaging_event(db, "biz_1", _inbound("price please, don't stop")))
    assert len(sends.calls) == 1 and "Prices here" in sends.calls[0]["message"]
    assert not any(c.get("opted_out") for c in db.contacts.docs)
