"""Redact credentials from every log record before any handler formats it."""
import logging
import re

REDACTED = "[REDACTED]"

_SECRET_PARAMS = r"access_token|client_secret|fb_exchange_token|refresh_token|hub\.verify_token"

_PATTERNS = (
    # Query strings / form bodies, also URL-encoded: access_token=..., access_token%3D...
    re.compile(rf"(?i)((?:{_SECRET_PARAMS})(?:=|%3D))[^&\s\"',;)\]}}]+"),
    # JSON / dict reprs: "access_token": "...", 'client_secret': '...'
    re.compile(rf"(?i)([\"']?(?:{_SECRET_PARAMS})[\"']?\s*:\s*[\"'])[^\"']+"),
    # Authorization headers
    re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+"),
)


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(rf"\g<1>{REDACTED}", text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrites a record's message (msg % args) and exception text with secrets redacted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_pg_redacted", False):
            return True
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        record._pg_redacted = True
        return True


_installed = False


def install_log_redaction() -> None:
    """Attach RedactingFilter to the root logger and to every record at creation.

    A filter on the root logger only runs for records logged on the root logger
    itself: records from child loggers (app.*, httpx, uvicorn) reach root's
    handlers without passing root's filters. Running the same filter from the
    record factory covers every logger and every handler, including ones added
    later (uvicorn access logs, pytest's capture handler).
    """
    global _installed
    if _installed:
        return
    redacting_filter = RedactingFilter()
    logging.getLogger().addFilter(redacting_filter)

    previous_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        redacting_filter.filter(record)
        return record

    logging.setLogRecordFactory(_factory)
    _installed = True
