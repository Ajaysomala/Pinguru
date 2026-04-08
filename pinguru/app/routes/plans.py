from fastapi import APIRouter, Header, HTTPException, Request, Depends
from app.database import get_db
from app.routes.auth import get_current_user
from app.models.models import PLAN_LIMITS, PlanType
from app.config import settings
import stripe
import logging

router = APIRouter()
logger = logging.getLogger(__name__)

stripe.api_key = settings.STRIPE_SECRET_KEY

STRIPE_PRICE_IDS = {
    # Replace with your actual Stripe price IDs after creating products
    PlanType.STARTER: "price_STARTER_ID_HERE",
    PlanType.PRO:     "price_PRO_ID_HERE",
    PlanType.AGENCY:  "price_AGENCY_ID_HERE",
}

# ── Get All Plans ─────────────────────────────────────────────────────────────

@router.get("")
async def get_plans():
    return {
        "plans": [
            {
                "name": plan.value,
                "price_usd": limits["price"],
                "dm_limit": limits["dm_limit"],
                "rule_limit": limits["rules"],
            }
            for plan, limits in PLAN_LIMITS.items()
        ]
    }

# ── Create Stripe Checkout Session ────────────────────────────────────────────

@router.post("/checkout/{plan}")
async def create_checkout(
    plan: PlanType,
    authorization: str = Header(...),
    db=Depends(get_db)
):
    if plan == PlanType.FREE:
        raise HTTPException(status_code=400, detail="Cannot checkout free plan")

    token = authorization.replace("Bearer ", "")
    user = await get_current_user(token, db)

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
        event = stripe.Webhook.construct_event(
            payload, sig, settings.STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session["metadata"]["user_id"]
        plan = session["metadata"]["plan"]
        plan_enum = PlanType(plan)

        await db.users.update_one(
            {"_id": user_id},
            {"$set": {
                "plan": plan_enum,
                "dm_limit": PLAN_LIMITS[plan_enum]["dm_limit"],
                "stripe_customer_id": session.get("customer"),
                "stripe_subscription_id": session.get("subscription"),
            }}
        )
        logger.info(f"User {user_id} upgraded to {plan}")

    elif event["type"] == "customer.subscription.deleted":
        # Downgrade to free on cancel
        customer_id = event["data"]["object"]["customer"]
        await db.users.update_one(
            {"stripe_customer_id": customer_id},
            {"$set": {"plan": PlanType.FREE, "dm_limit": PLAN_LIMITS[PlanType.FREE]["dm_limit"]}}
        )

    return {"status": "ok"}
