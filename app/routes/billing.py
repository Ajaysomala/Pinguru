import asyncio
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel

from app.config import settings
from app.database import get_db
from app.models.models import PlanType, get_plan_limits, get_plan_type
from app.routes.auth import get_current_user
from app.security import limiter, summarize_api_error
from app.services.email import send_admin_alert_email, send_subscription_expired_email

router = APIRouter()
logger = logging.getLogger(__name__)


class CheckoutRequest(BaseModel):
    plan: str
    billing_cycle: str = "monthly"

class RefundRequest(BaseModel):
    reason: str
    payment_id: str | None = None


def _frontend_base_url() -> str:
    return settings.FRONTEND_URL or settings.BASE_URL


def _plan_rank(plan: PlanType) -> int:
    return {PlanType.Free: 0, PlanType.Starter: 1, PlanType.Pro: 2}[plan]


def _normalize_requested_plan(plan_value: str) -> PlanType:
    value = (plan_value or "").strip().lower()
    if value in {"starter", "starter_monthly", "starter_quarterly", "starter_annually"}:
        return PlanType.Starter
    if value in {"pro", "pro_monthly", "pro_quarterly", "pro_annually"}:
        return PlanType.Pro
    if value in {"free", "free_monthly", "free_forever"}:
        return PlanType.Free
    return get_plan_type(value)


def _normalize_billing_cycle(value: str | None) -> str:
    normalized = (value or "monthly").strip().lower()
    aliases = {
        "month": "monthly",
        "monthly": "monthly",
        "quarter": "quarterly",
        "quarterly": "quarterly",
        "qtr": "quarterly",
        "year": "yearly",
        "yearly": "yearly",
        "annual": "yearly",
        "annually": "yearly",
    }
    return aliases.get(normalized, "monthly")


def _resolve_razorpay_plan_id(plan: PlanType, billing_cycle: str) -> str:
    cycle = _normalize_billing_cycle(billing_cycle)
    if plan == PlanType.Starter:
        if cycle == "quarterly":
            return (settings.RAZORPAY_PLAN_STARTER_QUARTERLY or "").strip()
        if cycle == "yearly":
            return (settings.RAZORPAY_PLAN_STARTER_YEARLY or "").strip()
        return ((settings.RAZORPAY_PLAN_STARTER_MONTHLY or "").strip() or (settings.RAZORPAY_PLAN_STARTER or "").strip())
    if plan == PlanType.Pro:
        if cycle == "quarterly":
            return (settings.RAZORPAY_PLAN_PRO_QUARTERLY or "").strip()
        if cycle == "yearly":
            return (settings.RAZORPAY_PLAN_PRO_YEARLY or "").strip()
        return ((settings.RAZORPAY_PLAN_PRO_MONTHLY or "").strip() or (settings.RAZORPAY_PLAN_PRO or "").strip())
    return ""


def _is_razorpay_configured() -> bool:
    return bool((settings.RAZORPAY_KEY_ID or "").strip()) and bool(
        (settings.RAZORPAY_KEY_SECRET or "").strip()
    )


def _ensure_razorpay_checkout_ready(target_plan: PlanType, billing_cycle: str) -> None:
    if not _is_razorpay_configured():
        raise HTTPException(status_code=503, detail="Payments are temporarily unavailable")

    resolved_plan_id = _resolve_razorpay_plan_id(target_plan, billing_cycle)
    if not resolved_plan_id:
        cycle = _normalize_billing_cycle(billing_cycle)
        raise HTTPException(status_code=503, detail=f"{target_plan.value.capitalize()} {cycle} plan is not configured")


def _ensure_razorpay_webhook_ready() -> None:
    if not (settings.RAZORPAY_WEBHOOK_SECRET or "").strip():
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")


def _normalize_user_plan(user_doc: dict[str, Any]) -> PlanType:
    return get_plan_type(user_doc.get("plan", PlanType.Free))


def _pending_subscription_id(user: dict[str, Any]) -> str | None:
    """Subscription id of an in-flight checkout.

    New checkouts store it in pending_razorpay_subscription_id. Checkouts started
    before that field existed stored it in razorpay_subscription_id on a Free user.
    """
    pending = str(user.get("pending_razorpay_subscription_id") or "").strip()
    if pending:
        return pending
    if user.get("pending_plan") and _normalize_user_plan(user) == PlanType.Free:
        return str(user.get("razorpay_subscription_id") or "").strip() or None
    return None


def _cleared_pending_fields(user: dict[str, Any], pending_sub_id: str | None) -> dict[str, Any]:
    """Fields that drop an in-flight checkout without touching the current plan."""
    fields: dict[str, Any] = {
        "pending_plan": None,
        "pending_plan_billing_cycle": None,
        "pending_razorpay_subscription_id": None,
        "checkout_initiated_at": None,
    }
    # Legacy checkout kept its id in razorpay_subscription_id; clear only that case.
    if pending_sub_id and str(user.get("razorpay_subscription_id") or "") == pending_sub_id:
        fields["razorpay_subscription_id"] = None
    return fields


PENDING_CHECKOUT_TTL_MINUTES = 60
# Shown to paid users before upgrading (F6 cancels the old subscription immediately,
# with no proration, credit or refund for unused days).
UPGRADE_PRORATION_NOTICE = (
    "Upgrading starts your new plan immediately and charges its full price today. "
    "Your current plan is cancelled at the same time, and unused days on it are not "
    "prorated, credited or refunded."
)


def upgrade_notice_for(billing_status: dict[str, Any]) -> str | None:
    """Proration notice when an upgrade would replace an active paid (Starter) subscription."""
    if billing_status.get("current_plan") == PlanType.Starter.value and billing_status.get("is_active_paid"):
        return UPGRADE_PRORATION_NOTICE
    return None
_TERMINAL_SUBSCRIPTION_STATES = {"cancelled", "completed", "expired"}
# Razorpay states meaning the customer completed payment/mandate for the checkout.
_PAID_SUBSCRIPTION_STATES = {"active", "authenticated", "charged"}
# States meaning the checkout link was never paid and can safely be cancelled.
_UNPAID_SUBSCRIPTION_STATES = {"created", "pending", "unpaid"}
_CANCEL_ATTEMPTS = 3
_CANCEL_BACKOFF_SECONDS = 0.5
_backoff_sleep = asyncio.sleep  # patched in tests


async def _fetch_razorpay_subscription(sub_id: str) -> dict[str, Any] | None:
    """GET a subscription from Razorpay; None if it could not be fetched."""
    if not _is_razorpay_configured():
        return None
    auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = await client.get(f"https://api.razorpay.com/v1/subscriptions/{sub_id}", auth=auth)
    except httpx.RequestError as exc:
        logger.warning("Razorpay fetch of subscription %s failed: %s", sub_id, type(exc).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("Razorpay fetch of subscription %s returned %s: %s", sub_id, resp.status_code, summarize_api_error(resp))
        return None
    try:
        entity = resp.json() or {}
    except ValueError:
        return None
    return entity if isinstance(entity, dict) else None


async def _razorpay_subscription_status(client: httpx.AsyncClient, auth, sub_id: str) -> str | None:
    try:
        resp = await client.get(f"https://api.razorpay.com/v1/subscriptions/{sub_id}", auth=auth)
    except httpx.RequestError:
        return None
    if resp.status_code != 200:
        return None
    return str((resp.json() or {}).get("status") or "") or None


async def _cancel_razorpay_subscription(sub_id: str, attempts: int = _CANCEL_ATTEMPTS) -> tuple[bool, str]:
    """Cancel a Razorpay subscription immediately. Returns (done, detail).

    Already-terminal subscriptions count as done. Network errors, 429 and 5xx are
    retried with exponential backoff; other 4xx responses are not retried.
    """
    if not _is_razorpay_configured():
        return False, "razorpay_not_configured"
    auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    cancel_url = f"https://api.razorpay.com/v1/subscriptions/{sub_id}/cancel"
    detail = "unknown_error"
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
        for attempt in range(1, attempts + 1):
            retryable = True
            try:
                resp = await client.post(cancel_url, json={"cancel_at_cycle_end": 0}, auth=auth)
                if resp.status_code == 200:
                    return True, "cancelled"
                detail = f"status={resp.status_code} {summarize_api_error(resp)}"
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    status = await _razorpay_subscription_status(client, auth, sub_id)
                    if status in _TERMINAL_SUBSCRIPTION_STATES:
                        return True, f"already_{status}"
                    retryable = False
            except httpx.RequestError as exc:
                detail = f"request_error={type(exc).__name__}"
            if not retryable or attempt == attempts:
                break
            await _backoff_sleep(_CANCEL_BACKOFF_SECONDS * (2 ** (attempt - 1)))
    return False, detail


EMAILED_ALERT_TYPES = {"razorpay_cancel_failed", "unmatched_subscription_activation"}


async def _write_admin_alert(db, alert_type: str, **fields: Any) -> None:
    doc = {"type": alert_type, "resolved": False, "created_at": datetime.now(timezone.utc), **fields}
    collection = getattr(db, "admin_alerts", None)
    if collection is None:
        logger.error("admin_alerts collection unavailable; alert not stored: %s", doc)
    else:
        await collection.insert_one(dict(doc))
    if alert_type in EMAILED_ALERT_TYPES:
        recipient = (settings.ADMIN_ALERT_EMAIL or settings.ADMIN_EMAIL or "").strip()
        if not recipient:
            logger.error("Admin alert %s not emailed: ADMIN_ALERT_EMAIL/ADMIN_EMAIL not set", alert_type)
            return
        try:
            sent = await send_admin_alert_email(recipient, doc)
        except Exception:
            logger.exception("Admin alert email failed for %s", alert_type)
            return
        if not sent:
            logger.error("Admin alert email for %s was not sent (Resend unavailable)", alert_type)


async def _cancel_previous_subscription(db, user_id: Any, previous_sub_id: str, new_sub_id: str) -> None:
    """After an upgrade activates, cancel the subscription it replaced (idempotent)."""
    user_doc = await db.users.find_one({"_id": user_id}) or {}
    if (
        str(user_doc.get("previous_razorpay_subscription_id") or "") == previous_sub_id
        and user_doc.get("previous_subscription_cancelled_at")
    ):
        return

    done, detail = await _cancel_razorpay_subscription(previous_sub_id)
    now = datetime.now(timezone.utc)
    if done:
        await db.users.update_one(
            {"_id": user_id},
            {"$set": {
                "previous_razorpay_subscription_id": previous_sub_id,
                "previous_subscription_cancelled_at": now,
                "previous_subscription_cancel_status": detail,
            }},
        )
        logger.info("Cancelled replaced Razorpay subscription %s for user %s (%s)", previous_sub_id, user_id, detail)
        return

    logger.error(
        "Failed to cancel replaced Razorpay subscription %s for user %s after upgrade to %s: %s",
        previous_sub_id,
        user_id,
        new_sub_id,
        detail,
    )
    await db.users.update_one(
        {"_id": user_id},
        {"$set": {"previous_subscription_cancel_status": "failed"}},
    )
    await _write_admin_alert(
        db,
        "razorpay_cancel_failed",
        user_id=str(user_id),
        subscription_id=previous_sub_id,
        new_subscription_id=new_sub_id,
        error=detail,
    )


async def _clear_stale_pending_checkout(db, user: dict[str, Any]) -> bool:
    pending_plan = user.get("pending_plan")
    if pending_plan not in {PlanType.Starter.value, PlanType.Pro.value}:
        return False

    initiated_at = user.get("checkout_initiated_at")
    if not initiated_at:
        return False

    if initiated_at.tzinfo is None:
        initiated_at = initiated_at.replace(tzinfo=timezone.utc)

    age_minutes = (datetime.now(timezone.utc) - initiated_at).total_seconds() / 60
    if age_minutes <= PENDING_CHECKOUT_TTL_MINUTES:
        return False

    pending_sub_id = _pending_subscription_id(user)
    if not pending_sub_id:
        return await _drop_pending_checkout(db, user, pending_plan, None, "no subscription id")

    # The customer may have paid after the webhook was missed: ask Razorpay before cancelling.
    entity = await _fetch_razorpay_subscription(pending_sub_id)
    if entity is None:
        logger.warning("Keeping expired pending checkout %s for user=%s: Razorpay fetch failed; retry next sweep",
                       pending_sub_id, user.get("_id"))
        return False

    status = str(entity.get("status") or "").lower()
    if status in _PAID_SUBSCRIPTION_STATES:
        return await _promote_paid_pending_subscription(db, user, pending_plan, pending_sub_id, entity)

    if status in _UNPAID_SUBSCRIPTION_STATES:
        done, detail = await _cancel_razorpay_subscription(pending_sub_id, attempts=1)
        if not done:
            logger.warning("Keeping expired pending checkout %s: cancel failed (%s); retry next sweep", pending_sub_id, detail)
            return False
        return await _drop_pending_checkout(db, user, pending_plan, pending_sub_id, f"unpaid ({status}), {detail}")

    if status in _TERMINAL_SUBSCRIPTION_STATES:
        return await _drop_pending_checkout(db, user, pending_plan, pending_sub_id, f"already {status}")

    # halted/paused or an unknown state on a checkout that never activated here: needs a human.
    await _write_admin_alert(
        db,
        "pending_subscription_unexpected_status",
        user_id=str(user["_id"]),
        subscription_id=pending_sub_id,
        status=status or None,
    )
    return await _drop_pending_checkout(db, user, pending_plan, pending_sub_id, f"unexpected status {status!r}")


async def _drop_pending_checkout(db, user: dict[str, Any], pending_plan: str, pending_sub_id: str | None, reason: str) -> bool:
    result = await db.users.update_one(
        {"_id": user["_id"], "pending_plan": pending_plan},
        {"$set": _cleared_pending_fields(user, pending_sub_id)},
    )
    if getattr(result, "matched_count", 1) == 0:
        return False
    logger.info("Expired pending checkout %s for user=%s: %s", pending_sub_id, user.get("_id"), reason)
    return True


async def _promote_paid_pending_subscription(
    db, user: dict[str, Any], pending_plan: str, sub_id: str, entity: dict[str, Any]
) -> bool:
    """Apply a paid pending subscription found by the sweep, exactly like its activation event."""
    notes = entity.get("notes") or {}
    resolved = _plan_from_subscription({**entity, "id": entity.get("id") or sub_id})
    if str(notes.get("user_id") or "") != str(user["_id"]) or not resolved:
        logger.error("Paid pending subscription %s does not match user=%s or a configured plan", sub_id, user.get("_id"))
        await _write_admin_alert(
            db,
            "unmatched_subscription_activation",
            user_id=str(user["_id"]),
            subscription_id=sub_id,
            status=str(entity.get("status") or "") or None,
            reason="found paid by expiry sweep but notes/plan_id do not match",
        )
        return await _drop_pending_checkout(db, user, pending_plan, None, "paid but unmatched; admin alerted")

    plan_enum, billing_cycle = resolved
    logger.warning("Pending subscription %s for user=%s was paid without a processed webhook; promoting",
                   sub_id, user.get("_id"))
    await _activate_subscription(db, user, plan_enum, billing_cycle, sub_id)
    return True


async def expire_stale_pending_checkouts(db) -> int:
    """Background sweep: clear pending upgrades older than PENDING_CHECKOUT_TTL_MINUTES."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=PENDING_CHECKOUT_TTL_MINUTES)
    users = await db.users.find({
        "pending_plan": {"$in": [PlanType.Starter.value, PlanType.Pro.value]},
        "checkout_initiated_at": {"$lte": cutoff},
    }).to_list(500)
    cleared = 0
    for user in users:
        if await _clear_stale_pending_checkout(db, user):
            cleared += 1
    if cleared:
        logger.info("Expired %d stale pending checkout(s)", cleared)
    return cleared


@router.post("/create-checkout")
@limiter.limit("10/minute")
async def create_checkout_session(
    request: Request,
    payload: CheckoutRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    current_plan = _normalize_user_plan(user)
    target_plan = _normalize_requested_plan(payload.plan)
    billing_cycle = _normalize_billing_cycle(payload.billing_cycle)

    if target_plan == PlanType.Free:
        raise HTTPException(status_code=400, detail="Cannot checkout free plan")

    if _plan_rank(target_plan) <= _plan_rank(current_plan):
        raise HTTPException(status_code=400, detail="Only upgrades are allowed")

    if await _clear_stale_pending_checkout(db, user):
        user = await db.users.find_one({"_id": user["_id"]}) or user
        # The expired checkout may have turned out to be paid and was just applied.
        if _plan_rank(target_plan) <= _plan_rank(_normalize_user_plan(user)):
            raise HTTPException(status_code=400, detail="Only upgrades are allowed")

    pending_plan = user.get("pending_plan")
    if pending_plan in {PlanType.Starter.value, PlanType.Pro.value}:
        raise HTTPException(status_code=409, detail="A checkout is already pending confirmation")

    _ensure_razorpay_checkout_ready(target_plan, billing_cycle)

    plan_id = _resolve_razorpay_plan_id(target_plan, billing_cycle)

    auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    total_count = max(1, int(settings.RAZORPAY_SUBSCRIPTION_TOTAL_COUNT or 1))
    sub_payload = {
        "plan_id": plan_id,
        "total_count": total_count,
        "quantity": 1,
        "customer_notify": 1,
        "notes": {"user_id": str(user["_id"]), "plan": target_plan.value, "email": user["email"]},
    }
    sub_payload["notes"]["billing_cycle"] = billing_cycle

    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.razorpay.com/v1/subscriptions", json=sub_payload, auth=auth)

    if resp.status_code != 200:
        logger.error("Razorpay subscription creation failed: status=%s %s", resp.status_code, summarize_api_error(resp))
        raise HTTPException(status_code=502, detail="Failed to create payment session")

    sub = resp.json()
    sub_id = sub.get("id")
    if not sub_id:
        logger.error("Razorpay response missing subscription id")
        raise HTTPException(status_code=502, detail="Invalid response from payment provider")

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                # Current subscription stays in razorpay_subscription_id until this one activates.
                "pending_razorpay_subscription_id": sub_id,
                "pending_plan": target_plan.value,
                "pending_plan_billing_cycle": billing_cycle,
                "checkout_initiated_at": datetime.now(timezone.utc),
            }
        },
    )

    short_url = str(sub.get("short_url") or "").strip()
    return {
        "subscription_id": sub_id,
        "checkout_url": short_url or f"https://rzp.io/l/{sub_id}",
        "key_id": settings.RAZORPAY_KEY_ID,
        "prefill_email": user["email"],
        "plan": target_plan.value,
        "billing_cycle": billing_cycle,
    }


@router.post("/portal")
async def get_customer_portal_url(user=Depends(get_current_user)):
    frontend_base = _frontend_base_url()
    sub_id = user.get("razorpay_subscription_id")
    if not sub_id or not _is_razorpay_configured():
        return {"portal_url": f"{frontend_base}/billing?portal=unavailable&message={quote('No active subscription found')}"}
    return {"portal_url": f"https://razorpay.com/subscription/{sub_id}"}


@router.post("/cancel-pending")
@limiter.limit("10/minute")
async def cancel_pending_checkout(
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    pending_plan = user.get("pending_plan")
    if not pending_plan:
        return {"cancelled": False, "message": "No pending checkout found"}

    # Only the pending checkout is cancelled; the current plan and subscription stay as they are.
    sub_id = _pending_subscription_id(user)

    # Best effort cancellation on provider side if subscription was already created.
    if sub_id and _is_razorpay_configured():
        auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        cancel_url = f"https://api.razorpay.com/v1/subscriptions/{sub_id}/cancel"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(cancel_url, auth=auth)
            if resp.status_code >= 400:
                logger.warning("Razorpay pending cancellation returned %s: %s", resp.status_code, summarize_api_error(resp))
        except httpx.RequestError as exc:
            logger.warning("Failed to cancel pending Razorpay subscription %s: %s", sub_id, exc)

    result = await db.users.update_one(
        {
            "_id": user["_id"],
            "pending_plan": pending_plan,
            "pending_razorpay_subscription_id": user.get("pending_razorpay_subscription_id"),
        },
        {"$set": _cleared_pending_fields(user, sub_id)},
    )

    if result.matched_count == 0:
        logger.info("cancel-pending skipped because checkout already changed for user=%s", str(user.get("_id")))
        return {"cancelled": False, "message": "Subscription already changed or activated"}

    return {"cancelled": True, "message": "Pending checkout cancelled"}


@router.post("/razorpay-webhook")
async def razorpay_webhook(request: Request, db=Depends(get_db)):
    _ensure_razorpay_webhook_ready()

    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not signature:
        raise HTTPException(status_code=400, detail="Missing signature")

    expected = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = json.loads(raw_body)
    event_type = str(event.get("event") or "")
    logger.info("Razorpay webhook: %s", event_type)
    sub_entity = event.get("payload", {}).get("subscription", {}).get("entity", {}) or {}

    if event_type == "subscription.activated":
        return await _handle_subscription_activated(db, sub_entity)

    if event_type in ("subscription.charged", "subscription.resumed"):
        return await _handle_subscription_renewed(db, sub_entity, event_type)

    if event_type == "subscription.pending":
        # Renewal payment failed; Razorpay keeps retrying. Keep access during retries.
        await _set_subscription_status(db, sub_entity.get("id"), "past_due")
        return {"status": "ok"}

    if event_type in ("subscription.halted", "subscription.paused"):
        # Retries exhausted (halted) or paused: remove paid access, keep the
        # subscription id so a later charged/resumed event can restore it.
        status = "halted" if event_type == "subscription.halted" else "paused"
        await _downgrade_subscription(db, sub_entity.get("id"), status, keep_subscription_id=True)
        return {"status": "ok"}

    if event_type in ("subscription.cancelled", "subscription.expired", "subscription.completed"):
        await _downgrade_subscription(db, sub_entity.get("id"), event_type.split(".", 1)[1], keep_subscription_id=False)
        return {"status": "ok"}

    if event_type == "payment.failed":
        payment = event.get("payload", {}).get("payment", {}).get("entity", {}) or {}
        sub_id = str(payment.get("subscription_id") or "").strip()
        logger.warning(
            "Razorpay payment failed: payment_id=%s subscription_id=%s error_code=%s",
            payment.get("id"),
            sub_id or None,
            payment.get("error_code"),
        )
        if sub_id:
            await db.users.update_one(
                {"razorpay_subscription_id": sub_id},
                {"$set": {"last_payment_failed_at": datetime.now(timezone.utc)}},
            )
        return {"status": "ok"}

    return {"status": "ok"}


def _plan_from_subscription(sub_entity: dict[str, Any]) -> tuple[PlanType, str] | None:
    """Return (plan, billing_cycle) from notes, only if plan_id matches our configured plan."""
    notes = sub_entity.get("notes") or {}
    plan_enum = get_plan_type(notes.get("plan") or "")
    if plan_enum not in {PlanType.Starter, PlanType.Pro}:
        logger.warning("Ignoring unsupported plan in webhook notes: %s", notes.get("plan"))
        return None
    billing_cycle = _normalize_billing_cycle(notes.get("billing_cycle"))
    expected_plan_id = _resolve_razorpay_plan_id(plan_enum, billing_cycle)
    incoming_plan_id = str(sub_entity.get("plan_id") or "").strip()
    if not expected_plan_id or not hmac.compare_digest(incoming_plan_id, expected_plan_id):
        logger.error(
            "Ignoring Razorpay event: plan_id missing/unknown for subscription %s (notes plan=%s cycle=%s incoming plan_id=%s, configured=%s)",
            sub_entity.get("id"),
            plan_enum.value,
            billing_cycle,
            incoming_plan_id or None,
            bool(expected_plan_id),
        )
        return None
    return plan_enum, billing_cycle


def _paid_plan_fields(plan_enum: PlanType, billing_cycle: str, sub_id: str) -> dict[str, Any]:
    return {
        "plan": plan_enum.value,
        "dm_limit": get_plan_limits(plan_enum).get("dm_limit"),
        "razorpay_subscription_id": sub_id,
        "subscription_status": "active",
        "pending_plan": None,
        "billing_cycle": billing_cycle,
        "pending_plan_billing_cycle": None,
        "checkout_initiated_at": None,
    }


async def _handle_subscription_activated(db, sub_entity: dict[str, Any]) -> dict[str, str]:
    notes = sub_entity.get("notes") or {}
    user_id = notes.get("user_id")
    incoming_sub_id = str(sub_entity.get("id") or "")
    if not user_id or not notes.get("plan"):
        return {"status": "ok"}

    resolved = _plan_from_subscription(sub_entity)
    if not resolved:
        return {"status": "ignored"}
    plan_enum, billing_cycle = resolved

    try:
        user_object_id = ObjectId(user_id)
    except InvalidId:
        logger.warning("Ignoring webhook with invalid user id: %s", user_id)
        return {"status": "ignored"}

    user_doc = await db.users.find_one({"_id": user_object_id})
    if not user_doc:
        logger.warning("Webhook for unknown user id: %s", user_id)
        return {"status": "ignored"}

    stored_sub_id = str(user_doc.get("razorpay_subscription_id") or "")
    pending_sub_id = str(user_doc.get("pending_razorpay_subscription_id") or "")
    if incoming_sub_id not in {stored_sub_id, pending_sub_id} and (stored_sub_id or pending_sub_id):
        logger.error(
            "Subscription id mismatch user=%s stored=%s pending=%s incoming=%s",
            user_id,
            stored_sub_id,
            pending_sub_id,
            incoming_sub_id,
        )
        # A customer may have paid an expired or replaced checkout link: needs a human.
        await _write_admin_alert(
            db,
            "unmatched_subscription_activation",
            user_id=str(user_id),
            subscription_id=incoming_sub_id,
            stored_subscription_id=stored_sub_id or None,
            pending_subscription_id=pending_sub_id or None,
        )
        return {"status": "ignored"}

    await _activate_subscription(db, user_doc, plan_enum, billing_cycle, incoming_sub_id)
    return {"status": "ok"}


async def _activate_subscription(
    db,
    user_doc: dict[str, Any],
    plan_enum: PlanType,
    billing_cycle: str,
    sub_id: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Make sub_id the user's current subscription (promoting a pending one if needed)."""
    fields = _paid_plan_fields(plan_enum, billing_cycle, sub_id)
    fields["pending_razorpay_subscription_id"] = None
    fields.update(extra or {})
    previous_sub_id = str(user_doc.get("razorpay_subscription_id") or "")
    replaced = bool(previous_sub_id and previous_sub_id != sub_id)
    if replaced:
        fields["previous_razorpay_subscription_id"] = previous_sub_id
        fields["previous_subscription_cancelled_at"] = None
        fields["previous_subscription_cancel_status"] = "pending"
    # Promote first: once the old subscription is no longer current, its
    # subscription.cancelled webhook cannot downgrade the user.
    await db.users.update_one({"_id": user_doc["_id"]}, {"$set": fields})
    if replaced:
        await _cancel_previous_subscription(db, user_doc["_id"], previous_sub_id, sub_id)


async def _handle_subscription_renewed(db, sub_entity: dict[str, Any], event_type: str) -> dict[str, str]:
    """A successful charge (or resume) restores paid access for the subscription's owner."""
    sub_id = str(sub_entity.get("id") or "").strip()
    if not sub_id:
        return {"status": "ok"}
    if event_type == "subscription.resumed" and str(sub_entity.get("status") or "active") != "active":
        return {"status": "ok"}

    user_doc = await db.users.find_one({"razorpay_subscription_id": sub_id})
    if not user_doc:
        user_doc = await db.users.find_one({"pending_razorpay_subscription_id": sub_id})
    if not user_doc:
        logger.warning("Razorpay %s for unknown subscription %s", event_type, sub_id)
        return {"status": "ignored"}

    notes = sub_entity.get("notes") or {}
    if str(notes.get("user_id") or "") != str(user_doc["_id"]):
        logger.warning("Razorpay %s notes.user_id does not match owner of %s", event_type, sub_id)
        return {"status": "ignored"}

    resolved = _plan_from_subscription(sub_entity)
    if not resolved:
        return {"status": "ignored"}
    plan_enum, billing_cycle = resolved

    extra = {"last_charged_at": datetime.now(timezone.utc)} if event_type == "subscription.charged" else None
    await _activate_subscription(db, user_doc, plan_enum, billing_cycle, sub_id, extra)
    return {"status": "ok"}


async def _set_subscription_status(db, sub_id: Any, status: str) -> None:
    sub_id = str(sub_id or "").strip()
    if not sub_id:
        return
    await db.users.update_one(
        {"razorpay_subscription_id": sub_id},
        {"$set": {"subscription_status": status, "subscription_status_at": datetime.now(timezone.utc)}},
    )


async def _downgrade_subscription(db, sub_id: Any, status: str, keep_subscription_id: bool) -> None:
    sub_id = str(sub_id or "").strip()
    if not sub_id:
        return
    # Fetch user before downgrading to get email + current plan
    user_doc = await db.users.find_one({"razorpay_subscription_id": sub_id})
    if not user_doc:
        pending_owner = await db.users.find_one({"pending_razorpay_subscription_id": sub_id})
        if pending_owner:
            # The pending upgrade ended before activating: current plan is untouched.
            await db.users.update_one(
                {"_id": pending_owner["_id"], "pending_razorpay_subscription_id": sub_id},
                {"$set": _cleared_pending_fields(pending_owner, sub_id)},
            )
            logger.info("Pending subscription %s ended (%s); current plan kept", sub_id, status)
        return
    previous_plan = str(user_doc.get("plan") or "paid")
    was_paid = get_plan_type(user_doc.get("plan", PlanType.Free)) in {PlanType.Starter, PlanType.Pro}

    await db.users.update_one(
        {"_id": user_doc["_id"]},
        {
            "$set": {
                "plan": PlanType.Free.value,
                "dm_limit": get_plan_limits(PlanType.Free).get("dm_limit"),
                "razorpay_subscription_id": sub_id if keep_subscription_id else None,
                "subscription_status": status,
                "subscription_status_at": datetime.now(timezone.utc),
                "pending_plan": None,
                "billing_cycle": None,
                "pending_plan_billing_cycle": None,
                "checkout_initiated_at": None,
            }
        },
    )

    # Notify user their plan has ended
    if was_paid and user_doc.get("email"):
        try:
            await send_subscription_expired_email(user_doc["email"], previous_plan)
        except Exception:
            logger.warning("Failed to send subscription expired email to user %s", user_doc.get("_id"))


@router.get("/status")
async def get_billing_status(user=Depends(get_current_user), db=Depends(get_db)):
    if await _clear_stale_pending_checkout(db, user):
        user = await db.users.find_one({"_id": user["_id"]}) or user
    current_plan = _normalize_user_plan(user)
    pending_value = user.get("pending_plan")
    pending_plan = pending_value if pending_value in {PlanType.Starter.value, PlanType.Pro.value} else None
    current_billing_cycle = _normalize_billing_cycle(user.get("billing_cycle")) if user.get("billing_cycle") else None
    pending_billing_cycle = _normalize_billing_cycle(user.get("pending_plan_billing_cycle")) if user.get("pending_plan_billing_cycle") else None
    sub_id = user.get("razorpay_subscription_id")
    is_active_paid = current_plan in {PlanType.Starter, PlanType.Pro} and bool(sub_id)

    return {
        "current_plan": current_plan.value,
        "pending_plan": pending_plan,
        "subscription_id": sub_id,
        "payment_provider": "razorpay",
        "is_active_paid": is_active_paid,
        "is_checkout_pending": pending_plan is not None,
        "current_billing_cycle": current_billing_cycle,
        "pending_billing_cycle": pending_billing_cycle,
    }


_PAYMENT_ID_RE = re.compile(r"^pay_[A-Za-z0-9]{6,40}$")


async def _payment_belongs_to_subscription(payment_id: str, sub_id: str) -> bool:
    """True if payment_id paid one of this subscription's invoices (checked with Razorpay)."""
    if not _is_razorpay_configured():
        raise HTTPException(status_code=503, detail="Payments are temporarily unavailable")
    auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    skip = 0
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                resp = await client.get(
                    "https://api.razorpay.com/v1/invoices",
                    params={"subscription_id": sub_id, "count": 100, "skip": skip},
                    auth=auth,
                )
                if resp.status_code != 200:
                    logger.warning("Razorpay invoice lookup for %s returned %s", sub_id, resp.status_code)
                    raise HTTPException(status_code=502, detail="Could not verify payment. Please try again later.")
                items = (resp.json() or {}).get("items") or []
                if any(str(item.get("payment_id") or "") == payment_id for item in items):
                    return True
                if len(items) < 100 or skip >= 1000:
                    return False
                skip += 100
    except httpx.RequestError as exc:
        logger.warning("Razorpay invoice lookup for %s failed: %s", sub_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not verify payment. Please try again later.")


@router.post("/refund")
@limiter.limit("5/minute")
async def request_refund(
    request: Request,
    data: RefundRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    current_plan = _normalize_user_plan(user)

    # Free plan users have nothing to refund
    if current_plan == PlanType.Free:
        raise HTTPException(status_code=400, detail="No active paid subscription found to refund.")

    # Require a reason
    reason = data.reason.strip()[:500]
    if not reason:
        raise HTTPException(status_code=400, detail="Please provide a reason for your refund request.")

    payment_id = (data.payment_id or "").strip() or None
    if payment_id:
        sub_id = str(user.get("razorpay_subscription_id") or "").strip()
        if not _PAYMENT_ID_RE.match(payment_id) or not sub_id:
            raise HTTPException(status_code=400, detail="Payment not found for your subscription.")
        if not await _payment_belongs_to_subscription(payment_id, sub_id):
            raise HTTPException(status_code=400, detail="Payment not found for your subscription.")

    await db.refund_requests.insert_one({
        "user_id": str(user["_id"]),
        "email": user.get("email"),
        "plan": user.get("plan"),
        "reason": reason,
        "payment_id": payment_id,
        "subscription_id": user.get("razorpay_subscription_id"),
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    return {"message": "Refund request submitted. We will review and respond within 5 business days."}