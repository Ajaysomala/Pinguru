import json
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, HTTPException, Request
from app.database import get_db
from app.routes.auth import get_current_user
from app.models.models import PLAN_LIMITS, PlanType, get_plan_limits, get_plan_type
from app.config import settings
import stripe
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

STRIPE_PRICE_IDS = {
    PlanType.Free: settings.STRIPE_PRICE_FREE,
    PlanType.Starter: settings.STRIPE_PRICE_STARTER_199,
    PlanType.Pro: settings.STRIPE_PRICE_PRO_399,
}

# ── Get All Plans ─────────────────────────────────────────────────────────────

@router.get("")
async def get_plans():
    return {
        "plans": [
            {
                "name": plan.name,
                "price_inr": limits["price_inr"],
                "dm_limit": limits["dm_limit"],
                "rule_limit": "Unlimited" if limits["rules"] is None else limits["rules"],
            }
            for plan, limits in PLAN_LIMITS.items()
        ]
    }

# ── Create Stripe Checkout Session ────────────────────────────────────────────

@router.post("/checkout/{plan}")
async def create_checkout(
    plan: PlanType,
    user=Depends(get_current_user),
):
    if plan == PlanType.Free:
        raise HTTPException(status_code=400, detail="Cannot checkout free plan")

    price_id = STRIPE_PRICE_IDS.get(plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=user["email"],
        metadata={"user_id": str(user["_id"]), "plan": plan.value},
        success_url=f"{settings.BASE_URL}/dashboard?upgraded=true",
        cancel_url=f"{settings.BASE_URL}/plans",
    )
    return {"checkout_url": session.url}

# ── Stripe Webhook ────────────────────────────────────────────────────────────

@router.post("/stripe-webhook")
async def stripe_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature")

    try:
        stripe.WebhookSignature.verify_header(
            payload.decode("utf-8"), sig, settings.STRIPE_WEBHOOK_SECRET
        )
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")

    event = json.loads(payload)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id")

        if session_id:
            already_processed = await db.stripe_webhook_events.find_one({"session_id": session_id})
            if already_processed:
                return {"status": "ok"}

        user_id = session["metadata"]["user_id"]
        plan = session["metadata"]["plan"]
        plan_enum = get_plan_type(plan)

        try:
            user_object_id = ObjectId(user_id)
        except InvalidId:
            raise HTTPException(status_code=400, detail="Invalid user ID in Stripe metadata")

        await db.users.update_one(
            {"_id": user_object_id},
            {"$set": {
                "plan": plan_enum,
                "dm_limit": get_plan_limits(plan_enum)["dm_limit"],
                "stripe_customer_id": session.get("customer"),
                "stripe_subscription_id": session.get("subscription"),
            }}
        )

        if session_id:
            await db.stripe_webhook_events.insert_one(
                {
                    "session_id": session_id,
                    "event_type": event["type"],
                    "processed_at": datetime.now(timezone.utc),
                }
            )

    elif event["type"] == "customer.subscription.deleted":
        # Downgrade to free on cancel
        customer_id = event["data"]["object"]["customer"]
        await db.users.update_one(
            {"stripe_customer_id": customer_id},
            {"$set": {"plan": PlanType.Free, "dm_limit": get_plan_limits(PlanType.Free)["dm_limit"]}}
        )

    return {"status": "ok"}
