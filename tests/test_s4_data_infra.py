"""S4: CSV injection, Razorpay lifecycle events, refund ownership, webhook verify, log redaction, Docker."""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from bson import ObjectId
from fastapi import HTTPException

import app.main  # noqa: F401  (configures logging)
from app.config import settings
from app.routes import billing as billing_module
from app.routes.billing import RefundRequest, razorpay_webhook, request_refund
from app.routes.contacts import _csv_safe, export_contacts_csv
from app.routes.webhook import verify_webhook
from app.security import summarize_api_error

ROOT = Path(__file__).resolve().parents[1]


class _Users:
    def __init__(self, docs):
        self.docs = list(docs)

    def _match(self, doc, query):
        return all(doc.get(k) == v for k, v in query.items())

    async def find_one(self, query):
        return next((d for d in self.docs if self._match(d, query)), None)

    async def update_one(self, query, update, upsert=False):
        doc = await self.find_one(query)
        if doc:
            doc.update(update.get("$set", {}))
        return SimpleNamespace(matched_count=1 if doc else 0)


class _Inserts:
    def __init__(self):
        self.docs = []

    async def insert_one(self, doc):
        self.docs.append(doc)


# ── CSV formula injection ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        ('=HYPERLINK("http://evil","x")', '\'=HYPERLINK("http://evil","x")'),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
        ("-2+3+cmd", "'-2+3+cmd"),
        ("\t=1", "'\t=1"),
        ("\r=1", "'\r=1"),
        ("+919876543210", "+919876543210"),  # plain phone number stays usable
        ("-5", "-5"),
        ("normal name", "normal name"),
        (3, 3),
        ("", ""),
    ],
)
def test_csv_safe(value, expected):
    assert _csv_safe(value) == expected


def test_contacts_export_escapes_formula_cells():
    class _Cursor:
        def __init__(self, docs):
            self.docs = docs

        def sort(self, *_args):
            return self

        def __aiter__(self):
            self._it = iter(self.docs)
            return self

        async def __anext__(self):
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    contact = {
        "ig_username": "=cmd|' /C calc'!A0",
        "display_name": '=HYPERLINK("http://evil","click")',
        "captured_phone": "+919876543210",
        "tags": ["@vip"],
        "ig_user_id": "123",
    }
    db = SimpleNamespace(contacts=SimpleNamespace(find=lambda _q: _Cursor([contact])))

    async def _run():
        resp = await export_contacts_csv(user={"_id": ObjectId()}, db=db)
        chunks = [c async for c in resp.body_iterator]
        return "".join(c if isinstance(c, str) else c.decode() for c in chunks)

    rows = list(csv.reader(io.StringIO(asyncio.run(_run()))))
    assert rows[1][0] == "'=cmd|' /C calc'!A0"
    assert rows[1][1] == '\'=HYPERLINK("http://evil","click")'
    assert rows[1][3] == "+919876543210"
    assert rows[1][4] == "'@vip"


# ── Razorpay webhook lifecycle ────────────────────────────────────────────────

SECRET = "s4_webhook_secret"
PLAN_ID = "plan_starter_monthly_s4"


@pytest.fixture
def rp(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_STARTER_MONTHLY", PLAN_ID)
    emails = []

    async def fake_email(email, plan):
        emails.append((email, plan))

    monkeypatch.setattr(billing_module, "send_subscription_expired_email", fake_email)
    return emails


def _send(db, event, entity=None, payment=None):
    body = {"event": event, "payload": {}}
    if entity is not None:
        body["payload"]["subscription"] = {"entity": entity}
    if payment is not None:
        body["payload"]["payment"] = {"entity": payment}
    raw = json.dumps(body).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    async def _body():
        return raw

    request = SimpleNamespace(body=_body, headers={"X-Razorpay-Signature": sig})
    return asyncio.run(razorpay_webhook(request=request, db=db))


def _sub(user, plan_id=PLAN_ID, plan="starter", sub_id="sub_1", **extra):
    return {
        "id": sub_id,
        "plan_id": plan_id,
        "notes": {"user_id": str(user["_id"]), "plan": plan, "billing_cycle": "monthly"},
        **extra,
    }


def _user(**fields):
    return {"_id": ObjectId(), "email": "u@example.com", "plan": "free", "razorpay_subscription_id": "sub_1", **fields}


def test_activated_with_mismatched_plan_id_is_ignored(rp):
    user = _user()
    db = SimpleNamespace(users=_Users([user]))
    # notes claim Pro but plan_id is the Starter plan
    assert _send(db, "subscription.activated", _sub(user, plan="pro"))["status"] == "ignored"
    assert _send(db, "subscription.activated", _sub(user, plan_id="plan_other"))["status"] == "ignored"
    assert user["plan"] == "free"

    assert _send(db, "subscription.activated", _sub(user))["status"] == "ok"
    assert user["plan"] == "starter"
    assert user["subscription_status"] == "active"


def test_pending_keeps_access_and_marks_past_due(rp):
    user = _user(plan="starter")
    db = SimpleNamespace(users=_Users([user]))
    _send(db, "subscription.pending", _sub(user))
    assert user["plan"] == "starter"
    assert user["subscription_status"] == "past_due"


@pytest.mark.parametrize("event,status", [("subscription.halted", "halted"), ("subscription.paused", "paused")])
def test_halted_and_paused_remove_access_but_keep_subscription(rp, event, status):
    user = _user(plan="starter")
    db = SimpleNamespace(users=_Users([user]))
    _send(db, event, _sub(user))
    assert user["plan"] == "free"
    assert user["razorpay_subscription_id"] == "sub_1"
    assert user["subscription_status"] == status
    assert rp == [("u@example.com", "starter")]


def test_charged_restores_access_after_halt(rp):
    user = _user(plan="starter")
    db = SimpleNamespace(users=_Users([user]))
    _send(db, "subscription.halted", _sub(user))
    assert user["plan"] == "free"

    assert _send(db, "subscription.charged", _sub(user))["status"] == "ok"
    assert user["plan"] == "starter"
    assert user["subscription_status"] == "active"
    assert user["last_charged_at"] is not None


def test_charged_ignored_when_notes_user_does_not_own_subscription(rp):
    owner = _user(plan="free")
    other = _user(razorpay_subscription_id="sub_other")
    db = SimpleNamespace(users=_Users([owner, other]))
    assert _send(db, "subscription.charged", _sub(other, sub_id="sub_1"))["status"] == "ignored"
    assert owner["plan"] == "free"


def test_cancelled_clears_subscription(rp):
    user = _user(plan="starter")
    db = SimpleNamespace(users=_Users([user]))
    _send(db, "subscription.cancelled", _sub(user))
    assert user["plan"] == "free"
    assert user["razorpay_subscription_id"] is None


def test_payment_failed_recorded_without_downgrade(rp):
    user = _user(plan="starter")
    db = SimpleNamespace(users=_Users([user]))
    _send(db, "payment.failed", payment={"id": "pay_abc123", "subscription_id": "sub_1", "error_code": "BAD_REQUEST_ERROR"})
    assert user["plan"] == "starter"
    assert user["last_payment_failed_at"] is not None


# ── Refund ownership ──────────────────────────────────────────────────────────

def _refund(monkeypatch, payment_id, invoices):
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")
    calls = []

    async def fake_get(self, url, params=None, auth=None):
        calls.append(params)
        return httpx.Response(200, json={"items": invoices}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    refunds = _Inserts()
    user = {"_id": ObjectId(), "plan": "starter", "razorpay_subscription_id": "sub_1", "email": "u@example.com"}

    async def _run():
        return await request_refund(
            request=SimpleNamespace(),
            data=RefundRequest(reason="not useful", payment_id=payment_id),
            user=user,
            db=SimpleNamespace(refund_requests=refunds),
        )

    return _run, refunds, calls


def _call_unlimited(run):
    # The unlimited_refund fixture disables slowapi, which would otherwise need a real Request.
    return asyncio.run(run())


@pytest.fixture
def unlimited_refund(monkeypatch):
    monkeypatch.setattr(billing_module.limiter, "enabled", False)


def test_refund_rejects_payment_not_on_users_subscription(monkeypatch, unlimited_refund):
    run, refunds, calls = _refund(monkeypatch, "pay_someoneelse1", [{"payment_id": "pay_mine00001"}])
    with pytest.raises(HTTPException) as exc:
        _call_unlimited(run)
    assert exc.value.status_code == 400
    assert refunds.docs == []
    assert calls and calls[0]["subscription_id"] == "sub_1"


def test_refund_rejects_malformed_payment_id_without_api_call(monkeypatch, unlimited_refund):
    run, refunds, calls = _refund(monkeypatch, "../../v1/payments", [])
    with pytest.raises(HTTPException):
        _call_unlimited(run)
    assert calls == []
    assert refunds.docs == []


def test_refund_accepts_users_own_payment(monkeypatch, unlimited_refund):
    run, refunds, _calls = _refund(monkeypatch, "pay_mine00001", [{"payment_id": "pay_mine00001"}])
    _call_unlimited(run)
    assert refunds.docs[0]["payment_id"] == "pay_mine00001"
    assert refunds.docs[0]["subscription_id"] == "sub_1"


# ── Webhook verify token ──────────────────────────────────────────────────────

def test_webhook_verify_token_constant_time_and_rejects_empty(monkeypatch):
    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "right-token")
    ok = asyncio.run(verify_webhook(hub_mode="subscribe", hub_verify_token="right-token", hub_challenge="42"))
    assert ok.body == b"42"
    with pytest.raises(HTTPException):
        asyncio.run(verify_webhook(hub_mode="subscribe", hub_verify_token="wrong", hub_challenge="42"))

    monkeypatch.setattr(settings, "META_WEBHOOK_VERIFY_TOKEN", "")
    with pytest.raises(HTTPException):
        asyncio.run(verify_webhook(hub_mode="subscribe", hub_verify_token="", hub_challenge="42"))


# ── Log redaction ─────────────────────────────────────────────────────────────

def test_summarize_api_error_drops_body_and_tokens():
    token = "IGAA" + "x" * 120
    resp = httpx.Response(
        400,
        json={
            "error": {"message": f"Invalid token {token}", "type": "OAuthException", "code": 190, "fbtrace_id": "abc"},
            "echo": {"access_token": token, "email": "fan@example.com"},
        },
    )
    summary = summarize_api_error(resp)
    assert token not in summary
    assert "fan@example.com" not in summary
    assert "code=190" in summary and "type=OAuthException" in summary and "[REDACTED]" in summary


def test_httpx_request_urls_not_logged_at_info():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# ── Docker ────────────────────────────────────────────────────────────────────

def test_dockerfile_non_root_and_pinned():
    dockerfile = (ROOT / "Dockerfile").read_text()
    from_line = next(line for line in dockerfile.splitlines() if line.startswith("FROM "))
    assert "@sha256:" in from_line
    user_lines = [line for line in dockerfile.splitlines() if line.startswith("USER ")]
    assert user_lines and user_lines[-1].split()[1] not in {"root", "0"}


def test_dockerignore_excludes_secrets_and_repo_metadata():
    entries = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()}
    assert {".env", ".env.*", ".git", "tests", "docs"} <= entries


def test_update_frontend_media_script_removed():
    assert not (ROOT / "update_frontend_media.py").exists()


# ── Admin token refresh query ─────────────────────────────────────────────────

def test_admin_refresh_query_excludes_null_and_empty_tokens():
    from app.routes.admin import refresh_instagram_tokens

    captured = {}

    class _Cursor:
        async def to_list(self, _n):
            return []

    def find(query, projection=None):
        captured["query"] = query
        return _Cursor()

    db = SimpleNamespace(users=SimpleNamespace(find=find))
    asyncio.run(refresh_instagram_tokens(admin={"email": "a@example.com"}, db=db))

    for field in ("instagram_user_id", "instagram_access_token"):
        assert captured["query"][field] == {"$exists": True, "$nin": [None, ""]}
