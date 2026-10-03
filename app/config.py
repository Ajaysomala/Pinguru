from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    MONGODB_URI: str
    DB_NAME: str = "pinguru"
    META_APP_ID: str
    META_APP_SECRET: str
    IG_APP_ID: str = ""
    IG_APP_SECRET: str = ""
    META_WEBHOOK_VERIFY_TOKEN: str
    INSTAGRAM_GRAPH_API_VERSION: str = "v22.0"

    # Razorpay
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_PLAN_STARTER: str = ""   # plan_xxx from Razorpay dashboard
    RAZORPAY_PLAN_PRO: str = ""       # plan_xxx from Razorpay dashboard
    RAZORPAY_PLAN_STARTER_MONTHLY: str = ""
    RAZORPAY_PLAN_STARTER_QUARTERLY: str = ""
    RAZORPAY_PLAN_STARTER_YEARLY: str = ""
    RAZORPAY_PLAN_PRO_MONTHLY: str = ""
    RAZORPAY_PLAN_PRO_QUARTERLY: str = ""
    RAZORPAY_PLAN_PRO_YEARLY: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    RAZORPAY_SUBSCRIPTION_TOTAL_COUNT: int = 120

    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080
    BASE_URL: str = "https://api.pinguru.me"
    FRONTEND_URL: str = ""
    INSTAGRAM_REDIRECT_URI: str = ""
    ADMIN_FRONTEND_URLS: str = ""
    ENCRYPTION_KEY: str
    admin_api_key: str = ""
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD_HASH: str = ""
    GOOGLE_CLIENT_ID: str = ""
    DEFAULT_OAUTH_PASSWORD: str = ""
    RESEND_API_KEY: str = ""
    SMTP_EMAIL: str = ""
    SMTP_APP_PASSWORD: str = ""
    OTP_FROM_EMAIL: str = ""
    ENVIRONMENT: str = "production"
    DISABLE_WEBHOOK_SIGNATURE: bool = False

    # Real client IP behind a proxy. Leave CLIENT_IP_HEADER empty to use the
    # direct peer. Set to "cf-connecting-ip", "x-real-ip" or "x-forwarded-for"
    # only when that header is set by a proxy you control.
    CLIENT_IP_HEADER: str = ""
    # Comma-separated CIDRs of proxies allowed to set CLIENT_IP_HEADER (empty = any peer).
    TRUSTED_PROXY_IPS: str = ""
    # For x-forwarded-for: number of trusted proxies that append to the header.
    TRUSTED_PROXY_COUNT: int = 1
    # Rate-limit store. "memory://" is per-process; use a mongodb:// or redis://
    # URI when running more than one worker/instance.
    RATE_LIMIT_STORAGE_URI: str = "memory://"
    # Rollback switch: True restores the old .parent-domain cookies without __Host- prefix.
    LEGACY_SHARED_COOKIES: bool = False


settings = Settings()  # pyright: ignore[reportCallIssue]


def validate_startup_config(target_settings: Settings | None = None) -> None:
    """Validate critical environment secrets and keys at startup.

    Refuses to boot in production if:
    - JWT_SECRET must be at least 32 characters long
    - ENCRYPTION_KEY must be a valid 32-byte urlsafe base64-encoded Fernet key
    - META_APP_SECRET must be non-empty
    - RAZORPAY_WEBHOOK_SECRET must be non-empty
    """
    cfg = target_settings or settings
    env = str(getattr(cfg, "ENVIRONMENT", "production") or "production").strip().lower()

    if env == "production":
        from cryptography.fernet import Fernet

        jwt_sec = str(getattr(cfg, "JWT_SECRET", "") or "").strip()
        if len(jwt_sec) < 32:
            raise RuntimeError("Startup validation failed: JWT_SECRET must be at least 32 characters long in production")

        enc_key = getattr(cfg, "ENCRYPTION_KEY", "") or ""
        if isinstance(enc_key, str):
            enc_key = enc_key.strip().encode("utf-8")
        try:
            Fernet(enc_key)
        except Exception as e:
            raise RuntimeError(f"Startup validation failed: ENCRYPTION_KEY must be a valid Fernet key in production: {e}")

        meta_sec = str(getattr(cfg, "META_APP_SECRET", "") or "").strip()
        if not meta_sec:
            raise RuntimeError("Startup validation failed: META_APP_SECRET must not be empty in production")

        rp_webhook_sec = str(getattr(cfg, "RAZORPAY_WEBHOOK_SECRET", "") or "").strip()
        if not rp_webhook_sec:
            raise RuntimeError("Startup validation failed: RAZORPAY_WEBHOOK_SECRET must not be empty in production")


