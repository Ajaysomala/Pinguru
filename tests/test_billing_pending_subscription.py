"""Upgrades keep the current subscription until the new one activates or charges."""
import asyncio
import hashlib
import hmac
import json
import logging
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
    razorpay_webhook,
)

SECRET = "pending_sub_webhook_secret"
STARTER_PLAN = "plan_starter_m"
PRO_PLAN = "plan_pro_m"


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


@pytest.fixture
def billing(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "rzp_secret")
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_STARTER_MONTHLY", STARTER_PLAN)
    monkeypatch.setattr(settings, "RAZORPAY_PLAN_PRO_MONTHLY", PRO_PLAN)
    monkeypatch.setattr(billing_module.limiter, "enabled", False)

    async def _no_email(*_args):
        return None

    monkeypatch.setattr(billing_module, "send_subscription_expired_email", _no_email)

    razorpay_posts = []

    async def fake_post(self, url, json=None, auth=None, **kwargs):
        razorpay_posts.append(url)
        if url.endswith("/v1/subscriptions"):
            return httpx.Response(200, json={"id": "sub_new_pro", "short_url": "https://rzp.io/x"})
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    return razorpay_posts


def _starter_user():
    return {
        "_id": ObjectId(),
        "email": "paid@example.com",
        "plan": "starter",
        "billing_cycle": "monthly",
        "razorpay_subscription_id": "sub_current_starter",
    }


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


def test_upgrade_stores_pending_id_and_keeps_current_subscription(billing):
    user = _starter_user()
    db = SimpleNamespace(users=_Users([user]))

    result = _checkout(db, user)

    assert result["subscription_id"] == "sub_new_pro"
    assert user["plan"] == "starter"
    assert user["razorpay_subscription_id"] == "sub_current_starter"
    assert user["pending_razorpay_subscription_id"] == "sub_new_pro"
    assert user["pending_plan"] == "pro"


@pytest.mark.parametrize("event", ["subscription.activated", "subscription.charged"])
def test_pending_subscription_promoted_on_activated_or_charged(billing, event):
    user = _starter_user()
    db = SimpleNamespace(users=_Users([user]))
    _checkout(db, user)

    assert _event(db, event, "sub_new_pro", "pro", PRO_PLAN, user)["status"] == "ok"

    assert user["plan"] == "pro"
    assert user["razorpay_subscription_id"] == "sub_new_pro"
    assert user["pending_razorpay_subscription_id"] is None
    assert user["pending_plan"] is None
    assert user["previous_razorpay_subscription_id"] == "sub_current_starter"


def test_cancel_pending_upgrade_keeps_current_plan(billing):
    user = _starter_user()
    db = SimpleNamespace(users=_Users([user]))
    _checkout(db, user)
    billing.clear()

    result = asyncio.run(cancel_pending_checkout(request=SimpleNamespace(), user=user, db=db))

    assert result["cancelled"] is True
    assert user["plan"] == "starter"
    assert user["razorpay_subscription_id"] == "sub_current_starter"
    assert user["pending_razorpay_subscription_id"] is None
    assert user["pending_plan"] is None
    # Only the pending subscription is cancelled at Razorpay.
    assert billing == ["https://api.razorpay.com/v1/subscriptions/sub_new_pro/cancel"]


def test_pending_subscription_cancelled_webhook_keeps_current_plan(billing):
    user = _starter_user()
    db = SimpleNamespace(users=_Users([user]))
    _checkout(db, user)

    _event(db, "subscription.cancelled", "sub_new_pro", "pro", PRO_PLAN, user)

    assert user["plan"] == "starter"
    assert user["razorpay_subscription_id"] == "sub_current_starter"
    assert user["pending_razorpay_subscription_id"] is None


def test_events_for_replaced_subscription_are_ignored(billing):
    user = _starter_user()
    db = SimpleNamespace(users=_Users([user]))
    _checkout(db, user)
    _event(db, "subscription.activated", "sub_new_pro", "pro", PRO_PLAN, user)

    assert _event(db, "subscription.charged", "sub_current_starter", "starter", STARTER_PLAN, user)["status"] == "ignored"
    _event(db, "subscription.cancelled", "sub_current_starter", "starter", STARTER_PLAN, user)
    assert user["plan"] == "pro"
    assert user["razorpay_subscription_id"] == "sub_new_pro"


def test_activation_for_unrelated_subscription_is_ignored(billing):
    user = _starter_user()
    db = SimpleNamespace(users=_Users([user]))
    _checkout(db, user)
    assert _event(db, "subscription.activated", "sub_random", "pro", PRO_PLAN, user)["status"] == "ignored"
    assert user["plan"] == "starter"


def test_legacy_free_checkout_cancel_clears_old_style_id(billing):
    # Checkouts started before this change kept the pending id in razorpay_subscription_id.
    user = {"_id": ObjectId(), "email": "f@example.com", "plan": "free",
            "pending_plan": "starter", "razorpay_subscription_id": "sub_legacy_pending"}
    db = SimpleNamespace(users=_Users([user]))

    asyncio.run(cancel_pending_checkout(request=SimpleNamespace(), user=user, db=db))

    assert user["razorpay_subscription_id"] is None
    assert user["pending_plan"] is None
    assert billing == ["https://api.razorpay.com/v1/subscriptions/sub_legacy_pending/cancel"]


def test_legacy_free_checkout_still_activates(billing):
    user = {"_id": ObjectId(), "email": "f@example.com", "plan": "free",
            "pending_plan": "starter", "razorpay_subscription_id": "sub_legacy_pending"}
    db = SimpleNamespace(users=_Users([user]))
    _event(db, "subscription.activated", "sub_legacy_pending", "starter", STARTER_PLAN, user)
    assert user["plan"] == "starter"
    assert "previous_razorpay_subscription_id" not in user


@pytest.mark.parametrize("plan_id", ["", "plan_unknown"])
def test_unknown_or_missing_plan_id_logged_at_error(billing, caplog, plan_id):
    user = _starter_user()
    db = SimpleNamespace(users=_Users([user]))
    with caplog.at_level(logging.ERROR, logger="app.routes.billing"):
        assert _event(db, "subscription.activated", "sub_current_starter", "pro", plan_id, user)["status"] == "ignored"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "plan_id missing/unknown" in r.getMessage()]
    assert errors
    assert user["plan"] == "starter"
