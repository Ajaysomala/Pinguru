from fastapi import APIRouter, Depends, Request, Query

from app.database import get_db
from app.models.models import PLAN_LIMITS, PlanType
from app.routes.auth import get_current_user
from app.routes.billing import CheckoutRequest, create_checkout_session, get_billing_status, razorpay_webhook

router = APIRouter()

PLAN_FEATURES = {
    PlanType.Free: [
        "5 automation flows",
        "Unlimited DMs",
        "500 contacts / month",
        "Basic analytics",
        "Email support",
        "DM footer: © PinGuru",
    ],
    PlanType.Starter: [
        "15 automation flows",
        "Unlimited DMs",
        "Unlimited contacts",
        "No footer branding",
        "Premium analytics",
        "Ask-to-follow before DM delivery",
        "Priority email support",
    ],
    PlanType.Pro: [
        "Unlimited automation flows",
        "Unlimited DMs",
        "Unlimited contacts",
        "Premium analytics",
        "24/7 faster support",
        "No footer branding",
        "Ask-to-follow before DM delivery",
    ],
}

# ── Get All Plans ─────────────────────────────────────────────────────────────

@router.get("")
async def get_plans():
    billing_cycles = {
        "monthly": 1,
        "quarterly": 3,
        "yearly": 12,
    }

    return {
        "plans": [
            {
                "id": plan.value,
                "name": plan.name,
                "price_inr": limits["price_inr"],
                "dm_limit": limits["dm_limit"],
                "rule_limit": "Unlimited" if limits["rules"] is None else limits["rules"],
                "contacts_limit": limits.get("contacts_limit"),
                "analytics_tier": limits.get("analytics_tier", "basic"),
                "support_tier": limits.get("support_tier", "email"),
                "branding": limits.get("branding", "footer_copyright"),
                "ask_follow_before_dm": bool(limits.get("ask_follow_before_dm", False)),
                "features": PLAN_FEATURES.get(plan, []),
                "pricing": {
                    cycle: (limits["price_inr"] * multiplier)
                    for cycle, multiplier in billing_cycles.items()
                },
            }
            for plan, limits in PLAN_LIMITS.items()
        ]
    }

# ── Create Razorpay Checkout Session (via billing flow) ─────────────────────

@router.post("/checkout/{plan}")
async def create_checkout(
    plan: PlanType,
    billing_cycle: str = Query(default="monthly"),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    return await create_checkout_session(
        payload=CheckoutRequest(plan=plan.value, billing_cycle=billing_cycle),
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
