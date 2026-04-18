from fastapi import APIRouter, Depends, Request

from app.database import get_db
from app.models.models import PLAN_LIMITS, PlanType
from app.routes.auth import get_current_user
from app.routes.billing import CheckoutRequest, create_checkout_session, get_billing_status, razorpay_webhook

router = APIRouter()

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

# ── Create Razorpay Checkout Session (via billing flow) ─────────────────────

@router.post("/checkout/{plan}")
async def create_checkout(
    plan: PlanType,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    return await create_checkout_session(
        payload=CheckoutRequest(plan=plan.value),
        user=user,
        db=db,
    )

# ── Razorpay Webhook Alias ───────────────────────────────────────────────────

@router.post("/razorpay-webhook")
async def plans_razorpay_webhook(request: Request, db=Depends(get_db)):
    return await razorpay_webhook(request=request, db=db)


@router.get("/status")
async def plans_billing_status(user=Depends(get_current_user)):
    return await get_billing_status(user=user)
