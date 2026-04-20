from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from app.config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None

db_instance = Database()

async def _create_indexes(db) -> None:
    # ── users ──────────────────────────────────────────────────────────────────
    await db.users.create_index("email", unique=True)
    await db.users.create_index("instagram_user_id", sparse=True)
    await db.users.create_index("instagram_account_ids", sparse=True)
    await db.users.create_index("razorpay_subscription_id", sparse=True)
    await db.users.create_index([("created_at", DESCENDING)])

    # ── automation_rules ───────────────────────────────────────────────────────
    await db.automation_rules.create_index("user_id")
    await db.automation_rules.create_index([("user_id", ASCENDING), ("is_active", ASCENDING)])
    await db.automation_rules.create_index([("user_id", ASCENDING), ("trigger_type", ASCENDING)])

    # ── dm_logs ────────────────────────────────────────────────────────────────
    await db.dm_logs.create_index("user_id")
    await db.dm_logs.create_index([("user_id", ASCENDING), ("sent_at", DESCENDING)])
    await db.dm_logs.create_index([("sent_at", DESCENDING)])
    await db.dm_logs.create_index("status")

    # ── contacts ───────────────────────────────────────────────────────────────
    await db.contacts.create_index(
        [("user_id", ASCENDING), ("ig_user_id", ASCENDING)],
        unique=True,
    )
    await db.contacts.create_index([("user_id", ASCENDING), ("last_seen_at", DESCENDING)])

    # ── webhook_events (dedup store) ───────────────────────────────────────────
    # TTL: auto-delete dedup records after 48 hours — keeps collection lean
    await db.webhook_events.create_index(
        "received_at",
        expireAfterSeconds=172800,  # 48 hours
    )

    # ── refund_requests ────────────────────────────────────────────────────────
    await db.refund_requests.create_index("user_id")
    await db.refund_requests.create_index([("created_at", DESCENDING)])

    # ── admin_audit ────────────────────────────────────────────────────────────
    await db.admin_audit.create_index([("createdAt", DESCENDING)])

    logger.info("✅ MongoDB indexes created")


async def connect_db():
    db_instance.client = AsyncIOMotorClient(settings.MONGODB_URI)
    db_instance.db = db_instance.client[settings.DB_NAME]
    await _create_indexes(db_instance.db)
    logger.info(f"Connected to MongoDB: {settings.DB_NAME}")

async def disconnect_db():
    if db_instance.client:
        db_instance.client.close()

def get_db():
    return db_instance.db