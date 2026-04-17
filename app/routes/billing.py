from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import stripe

from app.config import settings
from app.database import get_db
from app.models.models import PlanType, get_plan_limits, get_plan_type
from app.routes.auth import get_current_user

router = APIRouter()


class CheckoutRequest(BaseModel):
    plan: str


def _frontend_base_url() -> str:
    return settings.FRONTEND_URL or settings.BASE_URL


def _plan_rank(plan: PlanType) -> int:
    order = {
        PlanType.Free: 0,
        PlanType.Starter: 1,
        PlanType.Pro: 2,
    }
    return order[plan]


def _normalize_requested_plan(plan_value: str) -> PlanType:
    value = (plan_value or "").strip().lower()

    if value in {"starter", "starter_monthly", "starter_quarterly", "starter_annually"}:
        return PlanType.Starter
    if value in {"pro", "pro_monthly", "pro_quarterly", "pro_annually"}:
        return PlanType.Pro
    if value in {"free", "free_monthly", "free_forever"}:
        return PlanType.Free

    return get_plan_type(value)


def _is_stripe_configured() -> bool:
    return bool((settings.STRIPE_SECRET_KEY or "").strip()) and bool((settings.STRIPE_WEBHOOK_SECRET or "").strip())


def _price_id_for_plan(plan: PlanType) -> str:
    if plan == PlanType.Starter:
        return (settings.STRIPE_PRICE_STARTER_199 or "").strip()
    if plan == PlanType.Pro:
        return (settings.STRIPE_PRICE_PRO_399 or "").strip()
    return ""


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

    if not _is_stripe_configured():
        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "plan": target_plan,
                    "dm_limit": get_plan_limits(target_plan).get("dm_limit"),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        checkout_url = f"{_frontend_base_url()}/billing?upgraded=true&simulated=true&plan={target_plan.value}"
        return {"checkout_url": checkout_url}

    price_id = _price_id_for_plan(target_plan)
    if not price_id:
        raise HTTPException(status_code=400, detail="Price ID not configured for selected plan")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=user["email"],
        metadata={"user_id": str(user["_id"]), "plan": target_plan.value},
        success_url=f"{_frontend_base_url()}/billing?upgraded=true",
        cancel_url=f"{_frontend_base_url()}/billing",
    )
    return {"checkout_url": session.url}


@router.post("/portal")
async def get_customer_portal_url(user=Depends(get_current_user)):
    frontend_base = _frontend_base_url()

    if not _is_stripe_configured() or not user.get("stripe_customer_id"):
        message = quote("Stripe billing portal is not configured yet")
        return {"portal_url": f"{frontend_base}/billing?portal=unavailable&message={message}"}

    stripe.api_key = settings.STRIPE_SECRET_KEY
    session = stripe.billing_portal.Session.create(
        customer=user["stripe_customer_id"],
        return_url=f"{frontend_base}/billing",
    )
    return {"portal_url": session.url}
