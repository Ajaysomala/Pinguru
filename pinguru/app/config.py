from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8"
    )

    MONGODB_URI: str
    DB_NAME: str = "pinguru"
    META_APP_ID: str
    META_APP_SECRET: str
    META_WEBHOOK_VERIFY_TOKEN: str
    INSTAGRAM_GRAPH_API_VERSION: str = "v19.0"
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080
    BASE_URL: str = "http://localhost:8000"
    ENVIRONMENT: str = "development"


settings = Settings()