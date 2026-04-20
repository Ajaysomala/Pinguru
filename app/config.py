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
    ENVIRONMENT: str = "development"
    DISABLE_WEBHOOK_SIGNATURE: bool = False


settings = Settings()  # pyright: ignore[reportCallIssue]
