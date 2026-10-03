import asyncio
from datetime import datetime, timezone, timedelta
import logging
from typing import Any
from app.services.instagram import InstagramService
from app.database import get_db

logger = logging.getLogger(__name__)


def _parse_datetime(dt_val: Any) -> datetime | None:
    if isinstance(dt_val, datetime):
        if dt_val.tzinfo is None:
            return dt_val.replace(tzinfo=timezone.utc)
        return dt_val
    if isinstance(dt_val, str):
        try:
            parsed = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except (ValueError, TypeError):
            return None
    return None


async def refresh_expiring_tokens(db: Any, days_ahead: int = 10) -> dict[str, int]:
    """Find and refresh Instagram tokens that expire within `days_ahead` days.
    
    Tokens with status 'needs_reauth' are skipped because user re-authorization is required.
    Tokens successfully refreshed are updated with the new token, new expiration, active status,
    and refreshed timestamp.
    Tokens that fail refresh with auth/expiry errors are marked 'needs_reauth' or 'expired'.
    """
    now = datetime.now(timezone.utc)
    threshold = now + timedelta(days=days_ahead)

    # Query tokens expiring before threshold (supporting both datetime and ISO string fields)
    query = {
        "instagram_access_token": {"$exists": True, "$nin": [None, ""]},
        "ig_connection_status": {"$ne": "needs_reauth"},
        "$or": [
            {"ig_token_expires_at": {"$lte": threshold}},
            {"ig_token_expires_at": {"$lte": threshold.isoformat()}},
        ],
    }

    refreshed_count = 0
    failed_count = 0
    skipped_count = 0

    try:
        cursor = db.users.find(query)
        users = await cursor.to_list(length=1000)
    except Exception:
        logger.exception("Failed to query users for expiring Instagram tokens")
        return {"refreshed": 0, "failed": 0, "skipped": 0}

    logger.info("Found %d Instagram connection(s) expiring within %d days", len(users), days_ahead)

    for user in users:
        user_id = user.get("_id")
        encrypted_token = str(user.get("instagram_access_token") or "").strip()
        if not encrypted_token:
            skipped_count += 1
            continue

        raw_expires = user.get("ig_token_expires_at")
        user_expires_at = _parse_datetime(raw_expires)
        is_already_expired = user_expires_at is not None and user_expires_at <= now

        try:
            res = await InstagramService.refresh_long_lived_token(encrypted_token)
            new_token = res.get("access_token")
            if new_token:
                expires_in = int(res.get("expires_in") or 5183944)
                new_expires_at = now + timedelta(seconds=expires_in)
                encrypted_new = InstagramService.encrypt_access_token(new_token)

                await db.users.update_one(
                    {"_id": user_id},
                    {
                        "$set": {
                            "instagram_access_token": encrypted_new,
                            "ig_token_expires_at": new_expires_at,
                            "ig_last_refreshed_at": now,
                            "ig_connection_status": "active",
                        }
                    },
                )
                refreshed_count += 1
                logger.info("Refreshed Instagram token for user %s (valid until %s)", user_id, new_expires_at)
            else:
                error_obj = res.get("error") if isinstance(res.get("error"), dict) else {}
                error_code = error_obj.get("code")
                error_msg = error_obj.get("message") or str(res)
                logger.warning(
                    "Instagram token refresh failed for user %s (code=%s): %s",
                    user_id,
                    error_code,
                    error_msg,
                )

                # Set status to expired if past expiration date, or needs_reauth on OAuth error
                new_status = "expired" if is_already_expired else "needs_reauth"
                await db.users.update_one(
                    {"_id": user_id},
                    {"$set": {"ig_connection_status": new_status}},
                )
                failed_count += 1
        except Exception:
            logger.exception("Unexpected error while refreshing Instagram token for user %s", user_id)
            failed_count += 1

    return {
        "refreshed": refreshed_count,
        "failed": failed_count,
        "skipped": skipped_count,
    }


async def reset_monthly_dm_counts(db: Any) -> int:
    """Monthly reset for dm_count_this_month on users when a new calendar month begins."""
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    query = {
        "$or": [
            {"dm_count_reset_at": {"$lt": month_start}},
            {
                "dm_count_reset_at": None,
                "created_at": {"$lt": month_start},
                "dm_count_this_month": {"$gt": 0},
            },
        ]
    }
    try:
        res = await db.users.update_many(
            query,
            {
                "$set": {
                    "dm_count_this_month": 0,
                    "dm_count_reset_at": now,
                }
            },
        )
        if res.modified_count > 0:
            logger.info("Reset monthly DM counts for %d user(s)", res.modified_count)
        return res.modified_count
    except Exception:
        logger.exception("Failed to reset monthly DM counts")
        return 0


async def cleanup_unverified_accounts(db: Any) -> int:
    """Clean up expired unverified user accounts."""
    now = datetime.now(timezone.utc)
    query = {
        "email_verified": False,
        "$or": [
            {"unverified_expires_at": {"$lte": now}},
            {
                "unverified_expires_at": None,
                "created_at": {"$lte": now - timedelta(hours=24)},
            },
        ],
    }
    try:
        res = await db.users.delete_many(query)
        if res.deleted_count > 0:
            logger.info("Cleaned up %d expired unverified account(s)", res.deleted_count)
        return res.deleted_count
    except Exception:
        logger.exception("Failed to cleanup unverified accounts")
        return 0


async def _expire_stale_pending_checkouts(db: Any) -> int:
    # Imported lazily: app.routes.billing imports route modules that import this service.
    from app.routes.billing import expire_stale_pending_checkouts

    try:
        return await expire_stale_pending_checkouts(db)
    except Exception:
        logger.exception("Failed to expire stale pending checkouts")
        return 0


async def token_refresh_background_loop(interval_seconds: int = 43200, days_ahead: int = 10) -> None:
    """Startup background loop that periodically refreshes Instagram tokens expiring within 10 days,
    resets monthly DM counts on month rollover, and cleans up unverified accounts.
    """
    logger.info(
        "Starting Instagram token refresh loop (interval=%ds, expiring_within=%ddays)",
        interval_seconds,
        days_ahead,
    )
    while True:
        try:
            db = get_db()
            if db is not None:
                await refresh_expiring_tokens(db, days_ahead=days_ahead)
                await reset_monthly_dm_counts(db)
                await cleanup_unverified_accounts(db)
                await _expire_stale_pending_checkouts(db)
            else:
                logger.warning("Database not initialized yet; skipping token refresh cycle")
        except asyncio.CancelledError:
            logger.info("Instagram token refresh background loop cancelled")
            break
        except Exception:
            logger.exception("Error in Instagram token refresh loop cycle")

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Instagram token refresh background loop cancelled during sleep")
            break

