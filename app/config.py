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
    META_APP_ID: str          # Main Meta/Facebook App ID (933347079475444) — used for webhooks
    META_APP_SECRET: str      # Secret for the main Meta app
    IG_APP_ID: str = ""       # Instagram App ID (2430244137406063) — used for OAuth login
    IG_APP_SECRET: str = ""   # Instagram App Secret — used for OAuth token exchange
    META_WEBHOOK_VERIFY_TOKEN: str
    INSTAGRAM_GRAPH_API_VERSION: str = "v22.0"
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080
    BASE_URL: str = "https://api.pinguru.me"
    FRONTEND_URL: str = ""
    STRIPE_PRICE_FREE: str = "price_FREE"
    STRIPE_PRICE_STARTER_199: str = "price_STARTER_199"
    STRIPE_PRICE_PRO_399: str = "price_PRO_399"
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


settings = Settings()
