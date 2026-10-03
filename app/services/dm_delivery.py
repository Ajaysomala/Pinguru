"""Meta messaging rules for every automated Instagram DM.

All automated sends go through send_dm_with_policy():
- Opted-out contacts never receive automation.
- A private reply (comment_id set) is sent at most once per comment, only
  within 7 days of the comment, and at most PRIVATE_REPLY_HOURLY_LIMIT per
  account per rolling hour.
- Any other DM needs an inbound message from the recipient in the last 24h.
- HTTP 429 / error 613 responses are queued in dm_retry_queue and retried
  with exponential backoff (MAX_SEND_ATTEMPTS tries in total).

The inbound event currently being handled is passed through a contextvar
(event_context) so deeply nested senders know the inbound/comment time
without threading extra arguments through every call.
"""
import asyncio
import contextvars
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.services.instagram import InstagramService

logger = logging.getLogger(__name__)

MESSAGING_WINDOW = timedelta(hours=24)
PRIVATE_REPLY_MAX_AGE = timedelta(days=7)
PRIVATE_REPLY_HOURLY_LIMIT = 700
MAX_SEND_ATTEMPTS = 5
RETRY_BASE_SECONDS = 60
RATE_LIMIT_DEFER_SECONDS = 300
RATE_LIMIT_ERROR_CODES = {613}
OPT_OUT_PHRASES = {"stop", "unsubscribe", "stop messaging"}
STALE_LOCK = timedelta(minutes=10)

_event: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("pg_dm_event", default=None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def parse_event_time(value: Any) -> datetime | None:
    """Webhook timestamps: unix seconds or milliseconds, or ISO 8601 strings."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        if number > 1e12:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        if re.match(r".*[+-]\d{4}$", text):
            text = f"{text[:-2]}:{text[-2:]}"
        try:
            return _as_utc(datetime.fromisoformat(text))
        except ValueError:
            return None
    return None


@contextmanager
def event_context(**fields: Any):
    """Describe the inbound event being handled (user_id, sender_id, inbound_at, comment_id, comment_created_at)."""
    token = _event.set(fields)
    try:
        yield
    finally:
        _event.reset(token)


def is_opt_out_message(text: str | None) -> bool:
    normalized = re.sub(r"[^\w\s]", " ", str(text or "").lower())
    normalized = " ".join(normalized.split())
    return normalized in OPT_OUT_PHRASES


async def record_inbound(db, user_id: str, ig_user_id: str, at: datetime) -> None:
    """Store the latest inbound message time on an existing contact (opens the 24h window)."""
    await db.contacts.update_one(
        {"user_id": user_id, "ig_user_id": ig_user_id},
        {"$max": {"last_inbound_at": at}},
    )


async def mark_opted_out(db, user_id: str, ig_user_id: str, at: datetime) -> None:
    await db.contacts.update_one(
        {"user_id": user_id, "ig_user_id": ig_user_id},
        {
            "$set": {"opted_out": True, "opted_out_at": at, "last_inbound_at": at},
            "$setOnInsert": {"user_id": user_id, "ig_user_id": ig_user_id, "first_seen_at": at},
        },
        upsert=True,
    )


def dm_log_fields(result: dict[str, Any]) -> dict[str, Any]:
    """dm_logs status fields for a send result: sent, queued, skipped or failed."""
    if result.get("skipped"):
        return {"status": "skipped", "skip_reason": result["skipped"]}
    if result.get("queued"):
        return {"status": "queued", "retry_job_id": result.get("retry_job_id")}
    return {"status": "sent" if result.get("success") else "failed"}


def _skipped(reason: str, **log: Any) -> dict[str, Any]:
    logger.info("DM skipped: reason=%s %s", reason, " ".join(f"{k}={v}" for k, v in log.items()))
    return {"success": False, "skipped": reason, "error": reason}


def _is_rate_limited(result: dict[str, Any]) -> bool:
    return result.get("status_code") == 429 or result.get("error_code") in RATE_LIMIT_ERROR_CODES


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)))


def _ctx_for(user_id: str, recipient_id: str) -> dict[str, Any]:
    ctx = _event.get() or {}
    if str(ctx.get("user_id") or "") == user_id and str(ctx.get("sender_id") or "") == recipient_id:
        return ctx
    return {}


def _latest_inbound(contact: dict[str, Any] | None, ctx: dict[str, Any]) -> datetime | None:
    times = [t for t in (_as_utc((contact or {}).get("last_inbound_at")), _as_utc(ctx.get("inbound_at"))) if t]
    return max(times) if times else None


async def _claim_comment(db, user_id: str, comment_id: str, recipient_id: str, created_at: datetime) -> dict[str, Any] | None:
    """Reserve the single private reply for this comment. None if it was already used."""
    history = getattr(db, "comment_dm_history", None)
    doc = {
        "user_id": user_id,
        "comment_id": comment_id,
        "recipient_id": recipient_id,
        "comment_created_at": created_at,
        "claimed_at": _utcnow(),
        "status": "claimed",
    }
    if history is None:
        return doc
    try:
        await history.insert_one(dict(doc))
    except DuplicateKeyError:
        return None
    return doc


async def _set_claim(db, user_id: str, comment_id: str, **fields: Any) -> None:
    history = getattr(db, "comment_dm_history", None)
    if history is not None:
        await history.update_one({"user_id": user_id, "comment_id": comment_id}, {"$set": fields})


async def _release_claim(db, user_id: str, comment_id: str) -> None:
    """Meta did not accept the private reply, so it can still be used (e.g. by a fallback send)."""
    history = getattr(db, "comment_dm_history", None)
    if history is not None:
        await history.delete_one({"user_id": user_id, "comment_id": comment_id, "status": "claimed"})


async def _private_replies_last_hour(db, user_id: str) -> int:
    history = getattr(db, "comment_dm_history", None)
    if history is None:
        return 0
    return await history.count_documents({"user_id": user_id, "sent_at": {"$gte": _utcnow() - timedelta(hours=1)}})


async def _enqueue(db, user_id: str, recipient_id: str, send: dict[str, Any], *, attempts: int, run_at: datetime,
                   reason: str, inbound_at: datetime | None, comment_created_at: datetime | None,
                   rule_id: Any, trigger_type: Any) -> dict[str, Any]:
    queue = getattr(db, "dm_retry_queue", None)
    if queue is None:
        logger.error("dm_retry_queue unavailable; rate-limited DM to %s dropped", recipient_id)
        return {"success": False, "error": "rate_limited"}
    job = {
        "user_id": user_id,
        "recipient_id": recipient_id,
        "send": send,
        "attempts": attempts,
        "status": "pending",
        "next_run_at": run_at,
        "last_error": reason,
        "inbound_at": inbound_at,
        "comment_created_at": comment_created_at,
        "rule_id": str(rule_id) if rule_id is not None else None,
        "trigger_type": getattr(trigger_type, "value", trigger_type),
        "created_at": _utcnow(),
    }
    inserted = await queue.insert_one(job)
    job_id = str(getattr(inserted, "inserted_id", "") or job.get("_id") or "")
    if send.get("comment_id"):
        await _set_claim(db, user_id, send["comment_id"], status="queued")
    logger.warning("DM to %s queued for retry (%s), attempt %s, next at %s", recipient_id, reason, attempts, run_at.isoformat())
    # Accepted for delivery: callers treat it like a send; dm_logs records it as "queued".
    return {"success": True, "queued": True, "retry_job_id": job_id}


async def send_dm_with_policy(
    db,
    user: dict[str, Any],
    recipient_ig_id: str,
    message: str,
    *,
    attachment_url: str | None = None,
    attachment_type: str = "image",
    comment_id: str | None = None,
    buttons: list[dict] | None = None,
    rule_id: Any = None,
    trigger_type: Any = None,
) -> dict[str, Any]:
    user_id = str(user["_id"])
    recipient_ig_id = str(recipient_ig_id)
    now = _utcnow()
    contact = await db.contacts.find_one({"user_id": user_id, "ig_user_id": recipient_ig_id})
    if contact and contact.get("opted_out"):
        return _skipped("opted_out", recipient=recipient_ig_id)

    ctx = _ctx_for(user_id, recipient_ig_id)
    inbound_at = _latest_inbound(contact, ctx)
    comment_created_at = None
    send = {
        "message": message,
        "attachment_url": attachment_url,
        "attachment_type": attachment_type,
        "comment_id": comment_id,
        "buttons": buttons,
    }

    if comment_id:
        event = _event.get() or {}
        created_hint = _as_utc(event.get("comment_created_at")) if str(event.get("comment_id") or "") == comment_id else None
        claim = await _claim_comment(db, user_id, comment_id, recipient_ig_id, created_hint or now)
        if claim is None:
            return _skipped("comment_already_replied", comment_id=comment_id)
        comment_created_at = claim["comment_created_at"]
        if now - comment_created_at > PRIVATE_REPLY_MAX_AGE:
            await _set_claim(db, user_id, comment_id, status="too_old")
            return _skipped("comment_too_old", comment_id=comment_id)
        if await _private_replies_last_hour(db, user_id) >= PRIVATE_REPLY_HOURLY_LIMIT:
            return await _enqueue(
                db, user_id, recipient_ig_id, send, attempts=0, run_at=now + timedelta(seconds=RATE_LIMIT_DEFER_SECONDS),
                reason="local_hourly_limit", inbound_at=inbound_at, comment_created_at=comment_created_at,
                rule_id=rule_id, trigger_type=trigger_type,
            )
    elif not inbound_at or now - inbound_at > MESSAGING_WINDOW:
        return _skipped("outside_window", recipient=recipient_ig_id)

    result = await InstagramService.send_dm(
        access_token=user["instagram_access_token"],
        recipient_ig_id=recipient_ig_id,
        ig_user_id=user["instagram_user_id"],
        **send,
    )
    if result.get("success"):
        if comment_id:
            await _set_claim(db, user_id, comment_id, status="sent", sent_at=_utcnow())
        return result
    if _is_rate_limited(result):
        return await _enqueue(
            db, user_id, recipient_ig_id, send, attempts=1, run_at=_utcnow() + _retry_delay(1),
            reason=f"rate_limited status={result.get('status_code')} code={result.get('error_code')}",
            inbound_at=inbound_at, comment_created_at=comment_created_at, rule_id=rule_id, trigger_type=trigger_type,
        )
    if comment_id:
        await _release_claim(db, user_id, comment_id)
    return result


# ── Retry worker ──────────────────────────────────────────────────────────────

async def _finish_job(db, job: dict[str, Any], status: str, reason: str) -> None:
    now = _utcnow()
    await db.dm_retry_queue.update_one(
        {"_id": job["_id"]},
        {"$set": {"status": status, "finished_at": now, "last_error": reason}},
    )
    log_status = "sent" if status == "done" else "failed"
    log_update = {"status": log_status, "retry_attempts": job.get("attempts", 0), "updated_at": now}
    if log_status == "failed":
        log_update["failure_reason"] = reason
    result = await db.dm_logs.update_many({"retry_job_id": str(job["_id"])}, {"$set": log_update})
    if getattr(result, "matched_count", 0) == 0:
        await db.dm_logs.insert_one({
            "user_id": job["user_id"],
            "rule_id": job.get("rule_id"),
            "recipient_ig_id": job["recipient_id"],
            "message_sent": (job.get("send") or {}).get("message", ""),
            "trigger_type": job.get("trigger_type"),
            "retry_job_id": str(job["_id"]),
            "sent_at": now,
            **log_update,
        })


async def _run_job(db, job: dict[str, Any]) -> str:
    send = dict(job.get("send") or {})
    user_id, recipient_id = job["user_id"], job["recipient_id"]
    comment_id = send.get("comment_id")
    now = _utcnow()

    try:
        user = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        user = None
    if not user or not user.get("instagram_access_token") or not user.get("instagram_user_id"):
        await _finish_job(db, job, "failed", "user_unavailable")
        return "failed"

    contact = await db.contacts.find_one({"user_id": user_id, "ig_user_id": recipient_id})
    if contact and contact.get("opted_out"):
        await _finish_job(db, job, "failed", "opted_out")
        return "failed"

    if comment_id:
        created_at = _as_utc(job.get("comment_created_at"))
        if created_at and now - created_at > PRIVATE_REPLY_MAX_AGE:
            await _set_claim(db, user_id, comment_id, status="too_old")
            await _finish_job(db, job, "failed", "comment_too_old")
            return "failed"
        if await _private_replies_last_hour(db, user_id) >= PRIVATE_REPLY_HOURLY_LIMIT:
            await db.dm_retry_queue.update_one(
                {"_id": job["_id"]},
                {"$set": {"status": "pending", "next_run_at": now + timedelta(seconds=RATE_LIMIT_DEFER_SECONDS)}},
            )
            return "deferred"
    else:
        inbound_at = _latest_inbound(contact, {"inbound_at": job.get("inbound_at")})
        if not inbound_at or now - inbound_at > MESSAGING_WINDOW:
            await _finish_job(db, job, "failed", "outside_window")
            return "failed"

    attempts = int(job.get("attempts") or 0) + 1
    job["attempts"] = attempts
    result = await InstagramService.send_dm(
        access_token=user["instagram_access_token"],
        recipient_ig_id=recipient_id,
        ig_user_id=user["instagram_user_id"],
        **send,
    )
    if result.get("success"):
        if comment_id:
            await _set_claim(db, user_id, comment_id, status="sent", sent_at=_utcnow())
        await _finish_job(db, job, "done", "")
        return "done"

    reason = f"status={result.get('status_code')} code={result.get('error_code')}"
    if _is_rate_limited(result) and attempts < MAX_SEND_ATTEMPTS:
        await db.dm_retry_queue.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "pending", "attempts": attempts, "last_error": reason,
                      "next_run_at": _utcnow() + _retry_delay(attempts)}},
        )
        return "retry"

    if comment_id:
        await _set_claim(db, user_id, comment_id, status="failed")
    final = f"max_attempts_reached ({reason})" if _is_rate_limited(result) else reason
    await _finish_job(db, job, "failed", final)
    return "failed"


async def process_due_dm_retries(db, limit: int = 50) -> int:
    """Run due retry jobs. Each job is claimed atomically, so several workers can run safely."""
    queue = getattr(db, "dm_retry_queue", None)
    if queue is None:
        return 0
    processed = 0
    for _ in range(limit):
        now = _utcnow()
        job = await queue.find_one_and_update(
            {"$or": [
                {"status": "pending", "next_run_at": {"$lte": now}},
                # A worker died mid-job: make it claimable again.
                {"status": "processing", "locked_at": {"$lte": now - STALE_LOCK}},
            ]},
            {"$set": {"status": "processing", "locked_at": now}},
            sort=[("next_run_at", 1)],
            return_document=ReturnDocument.AFTER,
        )
        if not job:
            break
        try:
            await _run_job(db, job)
        except Exception:
            logger.exception("DM retry job %s failed unexpectedly", job.get("_id"))
            await queue.update_one(
                {"_id": job["_id"]},
                {"$set": {"status": "pending", "next_run_at": _utcnow() + _retry_delay(int(job.get("attempts") or 1))}},
            )
        processed += 1
    return processed


async def dm_retry_background_loop(interval_seconds: int = 30) -> None:
    from app.database import get_db

    while True:
        try:
            db = get_db()
            if db is not None:
                await process_due_dm_retries(db)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Error in DM retry loop")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            break
