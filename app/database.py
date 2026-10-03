from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING
from app.config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None

db_instance = Database()

async def _safe_create_index(collection, keys, **kwargs):
    """Safely create an index on a MongoDB collection, wrapping in try/except and logging any error."""
    try:
        return await collection.create_index(keys, **kwargs)
    except Exception as exc:
        col_name = getattr(collection, "name", str(collection))
        logger.error("Failed to create index %s on %s: %s", keys, col_name, exc)
        return None


async def _create_indexes(db) -> None:
    # ── users ──────────────────────────────────────────────────────────────────
    await _safe_create_index(db.users, "email", unique=True)
    await _safe_create_index(db.users, "instagram_user_id", sparse=True)
    await _safe_create_index(db.users, "instagram_account_ids", sparse=True)
    await _safe_create_index(db.users, "razorpay_subscription_id", sparse=True)
    await _safe_create_index(db.users, [("created_at", DESCENDING)])
    # TTL: auto-delete unverified user records when unverified_expires_at is reached
    await _safe_create_index(db.users, "unverified_expires_at", expireAfterSeconds=0)

    # ── automation_rules ───────────────────────────────────────────────────────
    await _safe_create_index(db.automation_rules, "user_id")
    await _safe_create_index(db.automation_rules, [("user_id", ASCENDING), ("is_active", ASCENDING)])
    await _safe_create_index(db.automation_rules, [("user_id", ASCENDING), ("trigger_type", ASCENDING)])

    # ── dm_logs ────────────────────────────────────────────────────────────────
    await _safe_create_index(db.dm_logs, "user_id")
    await _safe_create_index(db.dm_logs, [("user_id", ASCENDING), ("sent_at", DESCENDING)])
    await _safe_create_index(db.dm_logs, [("sent_at", DESCENDING)])
    await _safe_create_index(db.dm_logs, "status")
    # Drop legacy/conflicting sent_at index before creating distinct ASCENDING TTL index
    if hasattr(db.dm_logs, "drop_index"):
        try:
            await db.dm_logs.drop_index("sent_at_1")
        except Exception as exc:
            logger.debug("Old sent_at_1 index drop skipped or not present: %s", exc)
        try:
            await db.dm_logs.drop_index("sent_at_ttl")
        except Exception as exc:
            logger.debug("Old sent_at_ttl index drop skipped or not present: %s", exc)
    # Distinct ASCENDING TTL index: auto-delete DM log records after 90 days (7,776,000 seconds)
    await _safe_create_index(
        db.dm_logs,
        [("sent_at", ASCENDING)],
        expireAfterSeconds=7776000,
        name="sent_at_ttl_asc",
    )

    # ── contacts ───────────────────────────────────────────────────────────────
    await _safe_create_index(
        db.contacts,
        [("user_id", ASCENDING), ("ig_user_id", ASCENDING)],
        unique=True,
    )
    await _safe_create_index(db.contacts, [("user_id", ASCENDING), ("last_seen_at", DESCENDING)])

    # ── webhook_events (dedup store) ───────────────────────────────────────────
    # TTL: auto-delete dedup records after 48 hours — keeps collection lean
    await _safe_create_index(
        db.webhook_events,
        "received_at",
        expireAfterSeconds=172800,  # 48 hours
    )

    # ── data_deletion_requests ─────────────────────────────────────────────────
    await _safe_create_index(db.data_deletion_requests, "confirmation_code", unique=True)
    await _safe_create_index(db.data_deletion_requests, [("requested_at", DESCENDING)])

    # ── refund_requests ────────────────────────────────────────────────────────
    await _safe_create_index(db.refund_requests, "user_id")
    await _safe_create_index(db.refund_requests, [("created_at", DESCENDING)])

    # ── admin_audit ────────────────────────────────────────────────────────────
    await _safe_create_index(db.admin_audit, [("createdAt", DESCENDING)])

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