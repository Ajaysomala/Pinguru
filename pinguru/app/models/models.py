from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime, timezone
from enum import Enum

# ── Enums ─────────────────────────────────────────────────────────────────────

class PlanType(str, Enum):
    Free = "free"
    Starter = "starter"
    Pro = "pro"

class TriggerType(str, Enum):
    KEYWORD  = "keyword"
    STORY_REPLY = "story_reply"
    POST_COMMENT = "post_comment"
    REEL_COMMENT = "reel_comment"
    NEW_FOLLOWER = "new_follower"

# ── User ──────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    instagram_username: Optional[str] = None

class UserInDB(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    email: EmailStr
    hashed_password: str
    plan: PlanType = PlanType.Free
    instagram_user_id: Optional[str] = None
    instagram_access_token: Optional[str] = None
    ig_token_expires_at: Optional[datetime] = None
    dm_count_this_month: int = 0
    dm_limit: int = 200         # updated based on plan
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# ── Automation Rule ────────────────────────────────────────────────────────────

class AutomationRule(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    user_id: str
    name: str
    trigger_type: TriggerType
    keywords: List[str] = []        # for keyword / comment triggers
    match_mode: str = "exact"      # options: "exact" or "hinglish"
    reply_message: str              # DM template, supports {{username}}
    is_active: bool = True
    sent_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class AutomationRuleCreate(BaseModel):
    name: str
    trigger_type: TriggerType
    keywords: List[str] = []
    match_mode: str = "exact"
    reply_message: str

# ── DM Log ────────────────────────────────────────────────────────────────────

class DMLog(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    user_id: str
    rule_id: str
    recipient_ig_id: str
    message_sent: str
    trigger_type: TriggerType
    status: str = "sent"       # sent | failed
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

# ── Plan Info ─────────────────────────────────────────────────────────────────

PLAN_LIMITS = {
    PlanType.Free:    {"dm_limit": 200,   "price_inr": 0,   "rules": 1},
    PlanType.Starter: {"dm_limit": 3000,  "price_inr": 199, "rules": 5},
    PlanType.Pro:     {"dm_limit": 15000, "price_inr": 399, "rules": None},
}


def get_plan_type(plan: str | PlanType) -> PlanType:
    if isinstance(plan, PlanType):
        return plan
    try:
        return PlanType(plan)
    except ValueError:
        return PlanType.Free


def get_plan_limits(plan: str | PlanType) -> dict:
    return PLAN_LIMITS[get_plan_type(plan)]
