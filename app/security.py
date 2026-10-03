import ipaddress
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from fastapi import Request, Response
from slowapi import Limiter

from app.config import settings

logger = logging.getLogger(__name__)


# ── Client IP ─────────────────────────────────────────────────────────────────

def _trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks = []
    for raw in (settings.TRUSTED_PROXY_IPS or "").split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXY_IPS entry: %s", value)
    return networks


def _peer_is_trusted_proxy(peer: str) -> bool:
    networks = _trusted_proxy_networks()
    if not networks:
        # No allowlist configured: trust the header only because the operator
        # explicitly opted in via CLIENT_IP_HEADER.
        return True
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(peer_ip in network for network in networks)


def _valid_ip(value: str) -> str | None:
    value = (value or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """Return the real client IP.

    Proxy headers are only honoured when CLIENT_IP_HEADER is configured and the
    direct peer is a trusted proxy. For X-Forwarded-For we count from the right
    (entries appended by our own proxies), never the client-controlled left side.
    """
    peer = request.client.host if request.client else "127.0.0.1"
    header = (settings.CLIENT_IP_HEADER or "").strip().lower()
    if not header or not _peer_is_trusted_proxy(peer):
        return peer

    raw = request.headers.get(header)
    if not raw:
        return peer

    if header == "x-forwarded-for":
        hops = [part.strip() for part in raw.split(",") if part.strip()]
        count = max(1, int(settings.TRUSTED_PROXY_COUNT or 1))
        if len(hops) < count:
            return peer
        return _valid_ip(hops[-count]) or peer

    return _valid_ip(raw.split(",")[0]) or peer


limiter = Limiter(
    key_func=client_ip,
    default_limits=[],
    storage_uri=(settings.RATE_LIMIT_STORAGE_URI or "memory://").strip(),
    in_memory_fallback_enabled=True,
)


# ── Cookies ───────────────────────────────────────────────────────────────────

def is_production() -> bool:
    return settings.ENVIRONMENT.lower() == "production"


def _use_host_cookies() -> bool:
    # __Host- cookies require Secure, so they are only used in production.
    return is_production() and not settings.LEGACY_SHARED_COOKIES


def cookie_name(base: str) -> str:
    """Map a logical cookie name (pg_token, pg_csrf, ...) to its on-the-wire name."""
    return f"__Host-{base}" if _use_host_cookies() else base


def legacy_shared_cookie_domain() -> str | None:
    """Parent domain (e.g. .pinguru.me) used by the old shared-cookie scheme."""
    if not is_production():
        return None
    host = (urlparse((settings.FRONTEND_URL or "").strip()).hostname or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1"}:
        return None
    host_parts = host.split(".")
    if len(host_parts) < 2:
        return None
    return f".{'.'.join(host_parts[-2:])}"


def set_session_cookie(response: Response, base: str, value: str, max_age: int, httponly: bool) -> None:
    host_only = _use_host_cookies()
    response.set_cookie(
        key=cookie_name(base),
        value=value,
        httponly=httponly,
        secure=is_production(),
        samesite="lax",
        max_age=max_age,
        path="/",
        domain=None if host_only else legacy_shared_cookie_domain(),
    )


def _cleanup_domains() -> list[str | None]:
    domains: set[str] = set()
    shared = legacy_shared_cookie_domain()
    if shared:
        domains.add(shared)
        domains.add(shared.lstrip("."))
    for source in (settings.FRONTEND_URL, settings.BASE_URL):
        host = (urlparse(source or "").hostname or "").strip().lower()
        if not host or host in {"localhost", "127.0.0.1"}:
            continue
        domains.add(host)
        if host.startswith("www."):
            domains.add(host[4:])
    return [None, *sorted(domains)]


def clear_session_cookies(response: Response, *bases: str) -> None:
    """Delete current and legacy variants of the given cookies."""
    for base in bases:
        if is_production():
            response.delete_cookie(key=f"__Host-{base}", path="/", secure=True, samesite="lax")
        for domain in _cleanup_domains():
            response.delete_cookie(key=base, path="/", domain=domain, secure=is_production(), samesite="lax")


def get_cookie(request: Request, base: str) -> str | None:
    return request.cookies.get(cookie_name(base))


# ── Log redaction ─────────────────────────────────────────────────────────────

_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-|.]{40,}")
_ERROR_FIELDS = ("code", "error_subcode", "type", "fbtrace_id", "error_type", "source", "step", "reason")


def summarize_api_error(source: Any) -> str:
    """Safe one-line summary of a third-party API error for logs.

    Logs error codes/types and a short message with token-like strings removed,
    never the raw response body (which can echo tokens or user data).
    """
    payload: Any = source
    if isinstance(source, httpx.Response):
        try:
            payload = source.json()
        except ValueError:
            return f"non-JSON body ({len(source.content)} bytes)"
    if not isinstance(payload, dict):
        return "unparsed error body"

    error = payload.get("error")
    parts: list[str] = []
    if isinstance(error, dict):
        for key in _ERROR_FIELDS:
            if error.get(key) not in (None, ""):
                parts.append(f"{key}={error[key]}")
        message = error.get("message") or error.get("description")
    else:
        if error:
            parts.append(f"error={str(error)[:60]}")
        message = payload.get("error_message") or payload.get("error_description")
    if payload.get("error_type"):
        parts.append(f"error_type={payload['error_type']}")
    if message:
        parts.append(f"message={_TOKEN_LIKE.sub('[REDACTED]', str(message))[:160]!r}")
    return " ".join(parts) or "no error details"
