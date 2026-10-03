"""F6: cancel the replaced subscription after an upgrade activates; expire stale pending upgrades."""
import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from bson import ObjectId

from app.config import settings
from app.routes import billing as billing_module
from app.routes.billing import (
    CheckoutRequest,
    cancel_pending_checkout,
    create_checkout_session,
    expire_stale_pending_checkouts,
    get_billing_status,
    razorpay_webhook,
)

SECRET = "f6_webhook_secret"
STARTER_PLAN = "plan_starter_f6"
PRO_PLAN = "plan_pro_f6"
OLD_SUB = "sub_old_starter"
NEW_SUB = "sub_new_pro"


def _matches(doc, query):
    for key, cond in query.items():
        value = doc.get(key)
        if isinstance(cond, dict):
            if "$in" in cond and value not in cond["$in"]:
                return False
            if "$lte" in cond and not (value is not None and value <= cond["$lte"]):
                return False
        elif value != cond:
            return False
    return True


class _Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, query):
        return next((d for d in self.docs if _matches(d, query)), None)

    def find(self, query):
        docs = [d for d in self.docs if _matches(d, query)]
        return SimpleNamespace(to_list=lambda _n: _async(docs))

    async def update_one(self, query, update, upsert=False):
        doc = await self.find_one(query)
        if doc:
            doc.update(update.get("$set", {}))
        return SimpleNamespace(matched_count=1 if doc else 0)

    async def insert_one(self, doc):
        self.docs.append(doc)


async def _async(value):
    return value


class _Razorpay:
    """Scripted stand-in for the Razorpay HTTP API."""

    def __init__(self, cancel_responses=None, status="active"):
        self.cancel_responses = list(cancel_responses or [])
        self.status = status
        self.cancel_calls = []
        self.on_cancel = None

    def response_for_cancel(self):
        if self.cancel_responses:
            nxt = self.cancel_responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return httpx.Response(nxt, json={"error": {"code": "SERVER_ERROR"}} if nxt != 200 else {"status": "cancelled"})
        return httpx.Response(200, json={"status": "cancelled"})


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "rzp_secret")
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_STARTER_MONTHLY", STARTER_PLAN)
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_PRO_MONTHLY", PRO_PLAN)
    monkeypatch.setattr(billing_module.limiter, "enabled", False)

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(billing_module, "_backoff_sleep", fake_sleep)

    async def no_email(*_args):
        return None

    monkeypatch.setattr(billing_module, "send_subscription_expired_email", no_email)

    razorpay = _Razorpay()

    async def fake_post(self, url, json=None, auth=None, **kwargs):
        if url.endswith("/v1/subscriptions"):
            return httpx.Response(200, json={"id": NEW_SUB, "short_url": "https://rzp.io/x"})
        if url.endswith("/cancel"):
            sub_id = url.rsplit("/", 2)[-2]
            razorpay.cancel_calls.append((sub_id, json))
            if razorpay.on_cancel:
                razorpay.on_cancel(sub_id)
            return razorpay.response_for_cancel()
        raise AssertionError(f"unexpected POST {url}")

    async def fake_get(self, url, params=None, auth=None, **kwargs):
        return httpx.Response(200, json={"id": url.rsplit("/", 1)[-1], "status": razorpay.status})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return SimpleNamespace(razorpay=razorpay, sleeps=sleeps)


def _user(**fields):
    return {
        "_id": ObjectId(),
        "email": "u@example.com",
        "plan": "starter",
        "billing_cycle": "monthly",
        "razorpay_subscription_id": OLD_SUB,
        **fields,
    }


def _db(user):
    return SimpleNamespace(users=_Collection([user]), admin_alerts=_Collection())


def _event(db, event, sub_id, plan, plan_id, user):
    entity = {
        "id": sub_id,
        "plan_id": plan_id,
        "status": "active",
        "notes": {"user_id": str(user["_id"]), "plan": plan, "billing_cycle": "monthly"},
    }
    raw = json.dumps({"event": event, "payload": {"subscription": {"entity": entity}}}).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()

    async def _body():
        return raw

    return asyncio.run(razorpay_webhook(request=SimpleNamespace(body=_body, headers={"X-Razorpay-Signature": sig}), db=db))


def _checkout(db, user):
    return asyncio.run(create_checkout_session(
        request=SimpleNamespace(), payload=CheckoutRequest(plan="pro", billing_cycle="monthly"), user=user, db=db,
    ))


# ── Cancellation of the replaced subscription ─────────────────────────────────

@pytest.mark.parametrize("event", ["subscription.activated", "subscription.charged"])
def test_previous_subscription_cancelled_only_after_promotion(env, event):
    user = _user()
    db = _db(user)
    _checkout(db, user)
    assert env.razorpay.cancel_calls == []  # checkout never cancels anything

    state_at_cancel = {}
    env.razorpay.on_cancel = lambda _sub: state_at_cancel.update(
        current=user["razorpay_subscription_id"], plan=user["plan"],
    )

    _event(db, event, NEW_SUB, "pro", PRO_PLAN, user)

    assert env.razorpay.cancel_calls == [(OLD_SUB, {"cancel_at_cycle_end": 0})]
    # The user was already on the new subscription when the old one was cancelled.
    assert state_at_cancel == {"current": NEW_SUB, "plan": "pro"}
    assert user["previous_razorpay_subscription_id"] == OLD_SUB
    assert user["previous_subscription_cancelled_at"] is not None
    assert user["previous_subscription_cancel_status"] == "cancelled"


def test_no_cancellation_before_activation(env):
    user = _user()
    db = _db(user)
    _checkout(db, user)
    # Events that are not the pending subscription's activation/charge never cancel the current one.
    _event(db, "subscription.pending", NEW_SUB, "pro", PRO_PLAN, user)
    _event(db, "subscription.charged", OLD_SUB, "starter", STARTER_PLAN, user)
    _event(db, "subscription.activated", "sub_unrelated", "pro", PRO_PLAN, user)
    _event(db, "subscription.activated", NEW_SUB, "pro", "plan_wrong", user)  # bad plan_id: ignored
    asyncio.run(cancel_pending_checkout(request=SimpleNamespace(), user=user, db=db))

    assert all(sub != OLD_SUB for sub, _ in env.razorpay.cancel_calls)
    assert user["plan"] == "starter"
    assert user["razorpay_subscription_id"] == OLD_SUB


def test_cancellation_is_idempotent(env):
    user = _user()
    db = _db(user)
    _checkout(db, user)
    _event(db, "subscription.activated", NEW_SUB, "pro", PRO_PLAN, user)
    _event(db, "subscription.activated", NEW_SUB, "pro", PRO_PLAN, user)  # Razorpay redelivery
    _event(db, "subscription.charged", NEW_SUB, "pro", PRO_PLAN, user)
    asyncio.run(billing_module._cancel_previous_subscription(db, user["_id"], OLD_SUB, NEW_SUB))

    assert env.razorpay.cancel_calls == [(OLD_SUB, {"cancel_at_cycle_end": 0})]


def test_retry_with_backoff_then_success(env):
    env.razorpay.cancel_responses = [503, httpx.ConnectError("boom"), 200]
    user = _user()
    db = _db(user)
    _checkout(db, user)
    _event(db, "subscription.activated", NEW_SUB, "pro", PRO_PLAN, user)

    assert len(env.razorpay.cancel_calls) == 3
    assert env.sleeps == [0.5, 1.0]
    assert user["previous_subscription_cancel_status"] == "cancelled"
    assert db.admin_alerts.docs == []


def test_failure_after_three_attempts_logs_error_and_alerts(env, caplog):
    env.razorpay.cancel_responses = [500, 502, 503]
    user = _user()
    db = _db(user)
    _checkout(db, user)
    with caplog.at_level(logging.ERROR, logger="app.routes.billing"):
        assert _event(db, "subscription.activated", NEW_SUB, "pro", PRO_PLAN, user)["status"] == "ok"

    assert len(env.razorpay.cancel_calls) == 3
    assert env.sleeps == [0.5, 1.0]
    # The upgrade itself still applies.
    assert user["plan"] == "pro" and user["razorpay_subscription_id"] == NEW_SUB
    assert user["previous_subscription_cancel_status"] == "failed"
    assert user.get("previous_subscription_cancelled_at") is None
    assert any("Failed to cancel replaced Razorpay subscription" in r.getMessage() for r in caplog.records)
    [alert] = db.admin_alerts.docs
    assert alert["type"] == "razorpay_cancel_failed"
    assert alert["subscription_id"] == OLD_SUB and alert["new_subscription_id"] == NEW_SUB
    assert alert["user_id"] == str(user["_id"]) and alert["resolved"] is False


def test_already_cancelled_subscription_counts_as_done(env):
    env.razorpay.cancel_responses = [400]
    env.razorpay.status = "cancelled"
    user = _user()
    db = _db(user)
    _checkout(db, user)
    _event(db, "subscription.activated", NEW_SUB, "pro", PRO_PLAN, user)

    assert len(env.razorpay.cancel_calls) == 1
    assert env.sleeps == []
    assert user["previous_subscription_cancel_status"] == "already_cancelled"
    assert db.admin_alerts.docs == []


def test_non_retryable_client_error_alerts_without_retry(env):
    env.razorpay.cancel_responses = [400]
    env.razorpay.status = "active"
    user = _user()
    db = _db(user)
    _checkout(db, user)
    _event(db, "subscription.activated", NEW_SUB, "pro", PRO_PLAN, user)

    assert len(env.razorpay.cancel_calls) == 1
    assert env.sleeps == []
    assert db.admin_alerts.docs[0]["type"] == "razorpay_cancel_failed"


def test_unmatched_activation_writes_admin_alert(env):
    user = _user()
    db = _db(user)
    _checkout(db, user)
    assert _event(db, "subscription.activated", "sub_paid_after_expiry", "pro", PRO_PLAN, user)["status"] == "ignored"
    [alert] = db.admin_alerts.docs
    assert alert["type"] == "unmatched_subscription_activation"
    assert alert["subscription_id"] == "sub_paid_after_expiry"


# ── Stale pending upgrades ────────────────────────────────────────────────────

def _pending_user(minutes_ago):
    return _user(
        pending_plan="pro",
        pending_plan_billing_cycle="monthly",
        pending_razorpay_subscription_id=NEW_SUB,
        checkout_initiated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


def test_status_route_expires_pending_upgrade_older_than_30_minutes(env):
    user = _pending_user(31)
    db = _db(user)
    status = asyncio.run(get_billing_status(user=dict(user), db=db))

    assert status["pending_plan"] is None
    assert status["is_checkout_pending"] is False
    assert status["current_plan"] == "starter"
    assert user["pending_razorpay_subscription_id"] is None
    assert user["razorpay_subscription_id"] == OLD_SUB
    # The abandoned checkout link is cancelled so it cannot be paid later; the current one is not.
    assert env.razorpay.cancel_calls == [(NEW_SUB, {"cancel_at_cycle_end": 0})]


def test_status_route_keeps_recent_pending_upgrade(env):
    user = _pending_user(10)
    db = _db(user)
    status = asyncio.run(get_billing_status(user=dict(user), db=db))
    assert status["pending_plan"] == "pro"
    assert status["is_checkout_pending"] is True
    assert env.razorpay.cancel_calls == []


def test_background_sweep_expires_only_stale_pending_upgrades(env):
    stale, fresh = _pending_user(45), _pending_user(5)
    fresh["_id"] = ObjectId()
    db = SimpleNamespace(users=_Collection([stale, fresh]), admin_alerts=_Collection())

    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1
    assert stale["pending_plan"] is None and stale["plan"] == "starter"
    assert fresh["pending_plan"] == "pro"


def test_background_loop_calls_sweep(monkeypatch):
    from app.services import token_refresh

    calls = []

    async def fake_sweep(db):
        calls.append(db)
        return 0

    monkeypatch.setattr(billing_module, "expire_stale_pending_checkouts", fake_sweep)
    asyncio.run(token_refresh._expire_stale_pending_checkouts("db"))
    assert calls == ["db"]
