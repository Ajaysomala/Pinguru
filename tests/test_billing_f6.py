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
        self.get_calls = []
        self.on_cancel = None
        self.entities = {}  # sub_id -> subscription entity returned by GET
        self.get_failures = []  # queued failures for GET: int status code or Exception

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
        sub_id = url.rsplit("/", 1)[-1]
        razorpay.get_calls.append(sub_id)
        if razorpay.get_failures:
            failure = razorpay.get_failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, json={"error": {"code": "SERVER_ERROR"}})
        return httpx.Response(200, json=razorpay.entities.get(sub_id) or {"id": sub_id, "status": razorpay.status})

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


def test_background_loop_calls_sweep(monkeypatch):
    from app.services import token_refresh

    calls = []

    async def fake_sweep(db):
        calls.append(db)
        return 0

    monkeypatch.setattr(billing_module, "expire_stale_pending_checkouts", fake_sweep)
    asyncio.run(token_refresh._expire_stale_pending_checkouts("db"))
    assert calls == ["db"]


# ── Pending-upgrade expiry (60 minutes, Razorpay is checked before cancelling) ──

def _pending_user(minutes_ago, **extra):
    return _user(
        pending_plan="pro",
        pending_plan_billing_cycle="monthly",
        pending_razorpay_subscription_id=NEW_SUB,
        checkout_initiated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        **extra,
    )


def _pending_entity(user, status, plan_id=PRO_PLAN, plan="pro"):
    return {
        "id": NEW_SUB,
        "status": status,
        "plan_id": plan_id,
        "notes": {"user_id": str(user["_id"]), "plan": plan, "billing_cycle": "monthly"},
    }


def test_expiry_window_is_60_minutes():
    assert billing_module.PENDING_CHECKOUT_TTL_MINUTES == 60


def test_paid_at_minute_31_webhook_still_activates(env):
    """Under the old 30-minute expiry this payment was ignored; now the pending checkout is still there."""
    user = _pending_user(31)
    db = _db(user)

    status = asyncio.run(get_billing_status(user=dict(user), db=db))
    assert status["pending_plan"] == "pro"  # not expired
    assert env.razorpay.get_calls == [] and env.razorpay.cancel_calls == []

    assert _event(db, "subscription.activated", NEW_SUB, "pro", PRO_PLAN, user)["status"] == "ok"
    assert user["plan"] == "pro" and user["razorpay_subscription_id"] == NEW_SUB
    assert env.razorpay.cancel_calls == [(OLD_SUB, {"cancel_at_cycle_end": 0})]
    assert db.admin_alerts.docs == []


@pytest.mark.parametrize("paid_status", ["active", "authenticated", "charged"])
def test_sweep_promotes_paid_subscription_whose_webhook_was_missed(env, paid_status):
    """Paid at minute 31, webhook never processed: the sweep finds it paid and promotes instead of cancelling."""
    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, paid_status)
    db = _db(user)

    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1

    assert user["plan"] == "pro"
    assert user["razorpay_subscription_id"] == NEW_SUB
    assert user["pending_razorpay_subscription_id"] is None and user["pending_plan"] is None
    # The paid subscription is kept; only the replaced one is cancelled.
    assert env.razorpay.cancel_calls == [(OLD_SUB, {"cancel_at_cycle_end": 0})]
    assert user["previous_subscription_cancel_status"] == "cancelled"


@pytest.mark.parametrize("unpaid_status", ["created", "pending", "unpaid"])
def test_sweep_cancels_still_unpaid_checkout(env, unpaid_status):
    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, unpaid_status)
    db = _db(user)

    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1

    assert env.razorpay.cancel_calls == [(NEW_SUB, {"cancel_at_cycle_end": 0})]
    assert user["plan"] == "starter" and user["razorpay_subscription_id"] == OLD_SUB
    assert user["pending_razorpay_subscription_id"] is None and user["pending_plan"] is None


@pytest.mark.parametrize("failure", [500, 404, httpx.ConnectError("down")])
def test_sweep_fetch_failure_keeps_pending_and_retries_next_sweep(env, failure):
    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, "created")
    env.razorpay.get_failures = [failure]
    db = _db(user)

    assert asyncio.run(expire_stale_pending_checkouts(db)) == 0
    assert user["pending_razorpay_subscription_id"] == NEW_SUB and user["pending_plan"] == "pro"
    assert env.razorpay.cancel_calls == []

    # Next sweep: Razorpay answers, the unpaid checkout is cancelled and cleared.
    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1
    assert env.razorpay.cancel_calls == [(NEW_SUB, {"cancel_at_cycle_end": 0})]
    assert user["pending_plan"] is None


def test_sweep_keeps_pending_when_cancel_of_unpaid_checkout_fails(env):
    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, "created")
    env.razorpay.cancel_responses = [500]
    db = _db(user)

    assert asyncio.run(expire_stale_pending_checkouts(db)) == 0
    assert user["pending_razorpay_subscription_id"] == NEW_SUB


def test_sweep_clears_already_terminal_checkout_without_cancel(env):
    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, "expired")
    db = _db(user)
    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1
    assert env.razorpay.cancel_calls == []
    assert user["pending_plan"] is None and user["plan"] == "starter"


def test_sweep_paid_but_unmatched_plan_alerts_and_does_not_promote(env):
    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, "active", plan_id="plan_unknown")
    db = _db(user)
    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1
    assert user["plan"] == "starter" and user["razorpay_subscription_id"] == OLD_SUB
    assert env.razorpay.cancel_calls == []
    assert [a["type"] for a in db.admin_alerts.docs] == ["unmatched_subscription_activation"]


def test_sweep_unexpected_status_alerts(env):
    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, "halted")
    db = _db(user)
    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1
    assert env.razorpay.cancel_calls == []
    assert [a["type"] for a in db.admin_alerts.docs] == ["pending_subscription_unexpected_status"]


def test_status_route_applies_expiry_after_60_minutes_only(env):
    fresh = _pending_user(59)
    status = asyncio.run(get_billing_status(user=dict(fresh), db=_db(fresh)))
    assert status["pending_plan"] == "pro" and env.razorpay.get_calls == []

    stale = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(stale, "created")
    status = asyncio.run(get_billing_status(user=dict(stale), db=_db(stale)))
    assert status["pending_plan"] is None and status["is_checkout_pending"] is False
    assert status["current_plan"] == "starter"


def test_background_sweep_expires_only_stale_pending_upgrades(env):
    stale, fresh = _pending_user(75), _pending_user(5)
    fresh["_id"] = ObjectId()
    env.razorpay.entities[NEW_SUB] = _pending_entity(stale, "created")
    db = SimpleNamespace(users=_Collection([stale, fresh]), admin_alerts=_Collection())

    assert asyncio.run(expire_stale_pending_checkouts(db)) == 1
    assert stale["pending_plan"] is None and stale["plan"] == "starter"
    assert fresh["pending_plan"] == "pro"


def test_checkout_after_sweep_promotes_rejects_duplicate_upgrade(env):
    from fastapi import HTTPException

    user = _pending_user(61)
    env.razorpay.entities[NEW_SUB] = _pending_entity(user, "active")
    db = _db(user)
    with pytest.raises(HTTPException) as exc:
        _checkout(db, dict(user))
    assert exc.value.status_code == 400
    assert user["plan"] == "pro"


# ── Admin alert email ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "alert_type,emailed",
    [
        ("razorpay_cancel_failed", True),
        ("unmatched_subscription_activation", True),
        ("pending_subscription_unexpected_status", False),
    ],
)
def test_admin_alert_email_sent_for_money_alerts(monkeypatch, alert_type, emailed):
    monkeypatch.setattr(settings, "ADMIN_ALERT_EMAIL", "")
    monkeypatch.setattr(settings, "ADMIN_EMAIL", "owner@example.com")
    sent = []

    async def fake_send(to, alert):
        sent.append((to, alert))
        return True

    monkeypatch.setattr(billing_module, "send_admin_alert_email", fake_send)
    db = SimpleNamespace(admin_alerts=_Collection())
    asyncio.run(billing_module._write_admin_alert(db, alert_type, user_id="u1", subscription_id="sub_x"))

    assert len(db.admin_alerts.docs) == 1
    if emailed:
        [(to, alert)] = sent
        assert to == "owner@example.com"
        assert alert["type"] == alert_type and alert["subscription_id"] == "sub_x"
    else:
        assert sent == []


def test_admin_alert_email_uses_resend_and_escapes_html(monkeypatch):
    from app.services import email as email_module

    monkeypatch.setattr(settings, "RESEND_API_KEY", "re_test")
    posted = []

    async def fake_post(self, url, headers=None, json=None, **kwargs):
        posted.append((url, json))
        return httpx.Response(200, json={"id": "email_1"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    ok = asyncio.run(email_module.send_admin_alert_email(
        "owner@example.com", {"type": "razorpay_cancel_failed", "error": "<script>x</script>"},
    ))
    assert ok is True
    [(url, payload)] = posted
    assert url == "https://api.resend.com/emails"
    assert payload["to"] == ["owner@example.com"]
    assert "razorpay_cancel_failed" in payload["subject"]
    assert "<script>" not in payload["html"] and "&lt;script&gt;" in payload["html"]


# ── Proration notice ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"plan": "starter", "razorpay_subscription_id": OLD_SUB}, True),
        ({"plan": "free", "razorpay_subscription_id": None}, False),
        ({"plan": "pro", "razorpay_subscription_id": "sub_pro"}, False),
    ],
)
def test_plans_status_includes_upgrade_notice(env, fields, expected):
    from app.routes.plans import plans_billing_status

    user = _user(**fields)
    status = asyncio.run(plans_billing_status(request=SimpleNamespace(), user=user, db=_db(user)))
    if expected:
        assert status["upgrade_notice"] == billing_module.UPGRADE_PRORATION_NOTICE
        assert "not prorated, credited or refunded" in status["upgrade_notice"]
    else:
        assert status["upgrade_notice"] is None
