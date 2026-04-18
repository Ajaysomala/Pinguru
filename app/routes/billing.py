import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import settings
from app.database import get_db
from app.models.models import PlanType, get_plan_limits, get_plan_type
from app.routes.auth import get_current_user

router = APIRouter()
logger = logging.getLogger(__name__)


class CheckoutRequest(BaseModel):
    plan: str

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


def _is_razorpay_configured() -> bool:
    return bool((settings.RAZORPAY_KEY_ID or "").strip()) and bool(
        (settings.RAZORPAY_KEY_SECRET or "").strip()
    )


@router.post("/create-checkout")
async def create_checkout_session(
    payload: CheckoutRequest,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    current_plan = get_plan_type(user.get("plan", PlanType.Free))
    target_plan = _normalize_requested_plan(payload.plan)

    if target_plan == PlanType.Free:
        raise HTTPException(status_code=400, detail="Cannot checkout free plan")

    if _plan_rank(target_plan) <= _plan_rank(current_plan):
        raise HTTPException(status_code=400, detail="Only upgrades are allowed")

    if not _is_razorpay_configured():
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"plan": target_plan, "dm_limit": get_plan_limits(target_plan).get("dm_limit"), "updated_at": datetime.now(timezone.utc)}},
        )
        return {"checkout_url": f"{_frontend_base_url()}/billing?upgraded=true&simulated=true&plan={target_plan.value}"}

    plan_id = settings.RAZORPAY_PLAN_STARTER if target_plan == PlanType.Starter else settings.RAZORPAY_PLAN_PRO
    if not plan_id:
        raise HTTPException(status_code=400, detail="Razorpay plan ID not configured")

    auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    sub_payload = {
        "plan_id": plan_id,
        "total_count": 12,
        "quantity": 1,
        "customer_notify": 1,
        "notes": {"user_id": str(user["_id"]), "plan": target_plan.value, "email": user["email"]},
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post("https://api.razorpay.com/v1/subscriptions", json=sub_payload, auth=auth)

    if resp.status_code != 200:
        logger.error(f"Razorpay subscription creation failed: {resp.text}")
        raise HTTPException(status_code=502, detail="Failed to create payment session")

    sub = resp.json()
    sub_id = sub.get("id")
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"razorpay_subscription_id": sub_id, "pending_plan": target_plan.value}},
    )

    frontend_base = _frontend_base_url()
    checkout_url = f"https://rzp.io/l/{sub_id}?prefill[email]={user['email']}&callback_url={frontend_base}/billing?upgraded=true&cancel_url={frontend_base}/billing"
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
        from bson import ObjectId
        payload = event.get("payload", {}).get("subscription", {}).get("entity", {})
        notes = payload.get("notes", {})
        user_id = notes.get("user_id")
        plan_str = notes.get("plan")
        if user_id and plan_str:
            plan_enum = get_plan_type(plan_str)
            await db.users.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"plan": plan_enum, "dm_limit": get_plan_limits(plan_enum).get("dm_limit"), "razorpay_subscription_id": payload.get("id"), "pending_plan": None}},
            )

    elif event_type in ("subscription.cancelled", "subscription.expired"):
        payload = event.get("payload", {}).get("subscription", {}).get("entity", {})
        sub_id = payload.get("id")
        if sub_id:
            await db.users.update_one(
                {"razorpay_subscription_id": sub_id},
                {"$set": {"plan": PlanType.Free, "dm_limit": get_plan_limits(PlanType.Free).get("dm_limit")}},
            )

    return {"status": "ok"}


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
