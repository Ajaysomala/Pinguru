from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum

# ── Enums ─────────────────────────────────────────────────────────────────────

class PlanType(str, Enum):
    FREE    = "free"
    STARTER = "starter"   # $9/mo — 500 DMs/mo
    PRO     = "pro"       # $29/mo — 5000 DMs/mo
    AGENCY  = "agency"    # $79/mo — unlimited

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
    plan: PlanType = PlanType.FREE
    instagram_user_id: Optional[str] = None
    instagram_access_token: Optional[str] = None
    ig_token_expires_at: Optional[datetime] = None
    dm_count_this_month: int = 0
    dm_limit: int = 50          # updated based on plan
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)

# ── Automation Rule ────────────────────────────────────────────────────────────

class AutomationRule(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    user_id: str
    name: str
    trigger_type: TriggerType
    keywords: List[str] = []        # for keyword / comment triggers
    reply_message: str              # DM template, supports {{username}}
    is_active: bool = True
    sent_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)

class AutomationRuleCreate(BaseModel):
    name: str
    trigger_type: TriggerType
    keywords: List[str] = []
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
    sent_at: datetime = Field(default_factory=datetime.utcnow)

# ── Plan Info ─────────────────────────────────────────────────────────────────

PLAN_LIMITS = {
    PlanType.FREE:    {"dm_limit": 50,    "price": 0,  "rules": 1},
    PlanType.STARTER: {"dm_limit": 500,   "price": 9,  "rules": 5},
    PlanType.PRO:     {"dm_limit": 5000,  "price": 29, "rules": 20},
    PlanType.AGENCY:  {"dm_limit": 99999, "price": 79, "rules": 100},
}
