import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel

from app.config import settings
from app.database import get_db
from app.models.models import PlanType, get_plan_limits, get_plan_type
from app.routes.auth import get_current_user

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
    if settings.ENVIRONMENT.lower() == "production" and not (settings.RAZORPAY_WEBHOOK_SECRET or "").strip():
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")


def _normalize_user_plan(user_doc: dict[str, Any]) -> PlanType:
    return get_plan_type(user_doc.get("plan", PlanType.Free))


@router.post("/create-checkout")
async def create_checkout_session(
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

    pending_plan = user.get("pending_plan")
    if pending_plan in {PlanType.Starter.value, PlanType.Pro.value}:
        raise HTTPException(status_code=409, detail="A checkout is already pending confirmation")

    _ensure_razorpay_checkout_ready(target_plan, billing_cycle)

    plan_id = _resolve_razorpay_plan_id(target_plan, billing_cycle)

    auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    sub_payload = {
        "plan_id": plan_id,
        "total_count": 0,
        "quantity": 1,
        "customer_notify": 1,
        "notes": {"user_id": str(user["_id"]), "plan": target_plan.value, "email": user["email"]},
    }
    sub_payload["notes"]["billing_cycle"] = billing_cycle

    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.razorpay.com/v1/subscriptions", json=sub_payload, auth=auth)

    if resp.status_code != 200:
        logger.error(f"Razorpay subscription creation failed: {resp.text}")
        raise HTTPException(status_code=502, detail="Failed to create payment session")

    sub = resp.json()
    sub_id = sub.get("id")
    if not sub_id:
        logger.error("Razorpay response missing subscription id: %s", resp.text)
        raise HTTPException(status_code=502, detail="Invalid response from payment provider")

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "razorpay_subscription_id": sub_id,
                "pending_plan": target_plan.value,
                "pending_plan_billing_cycle": billing_cycle,
            }
        },
    )

    frontend_base = _frontend_base_url()
    checkout_url = (
        f"https://rzp.io/l/{sub_id}?prefill[email]={user['email']}"
        f"&callback_url={frontend_base}/billing?payment=processing&provider=razorpay"
        f"&cancel_url={frontend_base}/billing"
    )
    return {"checkout_url": checkout_url}


@router.post("/portal")
async def get_customer_portal_url(user=Depends(get_current_user)):
    frontend_base = _frontend_base_url()
    sub_id = user.get("razorpay_subscription_id")
    if not sub_id or not _is_razorpay_configured():
        return {"portal_url": f"{frontend_base}/billing?portal=unavailable&message={quote('No active subscription found')}"}
    return {"portal_url": f"https://razorpay.com/subscription/{sub_id}"}


@router.post("/razorpay-webhook")
async def razorpay_webhook(request: Request, db=Depends(get_db)):
    _ensure_razorpay_webhook_ready()

    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if settings.RAZORPAY_WEBHOOK_SECRET:
        expected = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = json.loads(raw_body)
    event_type = event.get("event")
    logger.info(f"Razorpay webhook: {event_type}")

    if event_type == "subscription.activated":
        payload = event.get("payload", {}).get("subscription", {}).get("entity", {})
        notes = payload.get("notes", {})
        user_id = notes.get("user_id")
        plan_str = notes.get("plan")
        billing_cycle = _normalize_billing_cycle(notes.get("billing_cycle"))
        if user_id and plan_str:
            if plan_str not in {PlanType.Starter.value, PlanType.Pro.value}:
                logger.warning("Ignoring unsupported plan in webhook notes: %s", plan_str)
                return {"status": "ignored"}

            plan_enum = get_plan_type(plan_str)
            try:
                user_object_id = ObjectId(user_id)
            except InvalidId:
                logger.warning("Ignoring webhook with invalid user id: %s", user_id)
                return {"status": "ignored"}

            await db.users.update_one(
                {"_id": user_object_id},
                {
                    "$set": {
                        "plan": plan_enum,
                        "dm_limit": get_plan_limits(plan_enum).get("dm_limit"),
                        "razorpay_subscription_id": payload.get("id"),
                        "pending_plan": None,
                        "billing_cycle": billing_cycle,
                        "pending_plan_billing_cycle": None,
                    }
                },
            )

    elif event_type in ("subscription.cancelled", "subscription.expired"):
        payload = event.get("payload", {}).get("subscription", {}).get("entity", {})
        sub_id = payload.get("id")
        if sub_id:
            await db.users.update_one(
                {"razorpay_subscription_id": sub_id},
                {
                    "$set": {
                        "plan": PlanType.Free,
                        "dm_limit": get_plan_limits(PlanType.Free).get("dm_limit"),
                        "razorpay_subscription_id": None,
                        "pending_plan": None,
                        "billing_cycle": None,
                        "pending_plan_billing_cycle": None,
                    }
                },
            )

    return {"status": "ok"}


@router.get("/status")
async def get_billing_status(user=Depends(get_current_user)):
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
        "is_checkout_pending": pending_plan is not None and not is_active_paid,
        "current_billing_cycle": current_billing_cycle,
        "pending_billing_cycle": pending_billing_cycle,
    }


@router.post("/refund")
async def request_refund(data: RefundRequest, user=Depends(get_current_user), db=Depends(get_db)):
    await db.refund_requests.insert_one({
        "user_id": str(user["_id"]),
        "email": user.get("email"),
        "plan": user.get("plan"),
        "reason": data.reason.strip()[:500],
        "payment_id": data.payment_id,
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
    })
    return {"message": "Refund request submitted. We will review and respond within 5 business days."}
