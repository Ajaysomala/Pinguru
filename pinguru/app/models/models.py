from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List
from datetime import datetime, timezone
from enum import Enum
import re

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

# ── User ──────────────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    instagram_username: Optional[str] = None

    @field_validator('email')
    @classmethod
    def validate_email_domain(cls, v: EmailStr) -> EmailStr:
        """Block disposable emails while allowing mainstream providers and business domains."""
        domain = str(v).split('@')[-1].lower()
        disposable_domains = {
            'mailinator.com', '10minutemail.com', 'guerrillamail.com',
            'temp-mail.org', 'yopmail.com', 'sharklasers.com', 'dispostable.com'
        }
        if domain in disposable_domains:
            raise ValueError('Please use a real email address (disposable emails are not allowed)')
        return v
    
    @field_validator('password')
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Enforce strong password policy: 8+ chars with uppercase, lowercase, number, special char."""
        if len(v) < 12:
            raise ValueError('Password must be at least 8 characters long')
        if not re.search(r'[A-Z]', v):
            raise ValueError('Password must contain at least one uppercase letter (A-Z)')
        if not re.search(r'[a-z]', v):
            raise ValueError('Password must contain at least one lowercase letter (a-z)')
        if not re.search(r'\d', v):
            raise ValueError('Password must contain at least one number (0-9)')
        if not re.search(r'[!@#$%^&*()-_=+\[\]{}|;:,.<>?]', v):
            raise ValueError('Password must contain at least one special character (!@#$%^&*...)')
        return v

class UserInDB(BaseModel):
    id: Optional[str] = Field(None, alias="_id")
    email: EmailStr
    hashed_password: str
    email_verified: bool = False
    otp_hash: Optional[str] = None
    otp_expires_at: Optional[datetime] = None
    otp_attempts: int = 0
    otp_resend_count: int = 0
    otp_resend_window_started_at: Optional[datetime] = None
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
