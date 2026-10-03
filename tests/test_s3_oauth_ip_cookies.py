"""S3: OAuth state bound to a browser nonce, trusted client IP, host-scoped cookies."""
import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import unquote

import jwt
import pytest
from bson import ObjectId
from fastapi import Response
from fastapi.testclient import TestClient
from starlette.requests import Request

import app.main as main_module
from app.config import settings
from app.database import get_db
from app.routes.auth import (
    OAUTH_NONCE_COOKIE,
    _auth_response,
    create_oauth_state,
    instagram_callback,
    instagram_initiate,
)
from app.routes.admin import _set_admin_cookie
from app.security import client_ip, cookie_name, limiter
from app.services.instagram import InstagramService


def _request(headers=None, cookies=None, peer="10.0.0.5"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": (peer, 1234),
        "app": SimpleNamespace(state=SimpleNamespace()),
    })


class _Users:
    def __init__(self, docs):
        self.docs = {d["_id"]: d for d in docs}
        self.updates = []

    async def find_one(self, query):
        if "_id" in query:
            return self.docs.get(query["_id"])
        return None

    async def update_one(self, query, update):
        self.updates.append((query, update))


def _set_cookie_headers(response):
    return [v.decode() for k, v in response.raw_headers if k == b"set-cookie"]


# ── OAuth nonce binding ───────────────────────────────────────────────────────

def test_initiate_sets_nonce_cookie_bound_to_state():
    response = Response()
    result = asyncio.run(instagram_initiate(response=response, user={"_id": ObjectId()}))

    cookies = _set_cookie_headers(response)
    nonce_cookie = next(c for c in cookies if c.startswith(f"{OAUTH_NONCE_COOKIE}="))
    nonce = nonce_cookie.split(";", 1)[0].split("=", 1)[1]
    assert "httponly" in nonce_cookie.lower()
    assert "samesite=lax" in nonce_cookie.lower()
    assert "Domain=" not in nonce_cookie

    state = unquote(result["auth_url"].split("state=", 1)[1].split("&", 1)[0])
    payload = jwt.decode(state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    assert payload["nh"] == hashlib.sha256(nonce.encode()).hexdigest()


@pytest.mark.parametrize(
    "state_nonce,cookie_nonce",
    [
        ("browser-nonce", None),               # victim's browser has no nonce (attacker's link)
        ("browser-nonce", "different-nonce"),  # victim started their own, different flow
        (None, "browser-nonce"),               # legacy state without nonce hash
    ],
)
def test_callback_rejects_state_not_bound_to_browser(monkeypatch, state_nonce, cookie_nonce):
    user_id = ObjectId()
    users = _Users([{"_id": user_id, "email": "attacker@example.com"}])
    exchange = AsyncMock()
    monkeypatch.setattr(InstagramService, "exchange_code_for_token", exchange)

    state = create_oauth_state(str(user_id), state_nonce)
    cookies = {OAUTH_NONCE_COOKIE: cookie_nonce} if cookie_nonce else None
    resp = asyncio.run(instagram_callback(
        request=_request(cookies=cookies), code="code", state=state, db=SimpleNamespace(users=users),
    ))

    assert "ig_error=" in resp.headers["location"]
    exchange.assert_not_called()
    assert users.updates == []
    # Nonce cookie is always cleared after a callback.
    assert any(c.startswith(f"{OAUTH_NONCE_COOKIE}=") for c in _set_cookie_headers(resp))


def test_session_token_cannot_be_used_as_oauth_state_and_vice_versa():
    # State JWT has no typ=session, so it is not a bearer token (checked in S2);
    # here: a bound state must still carry the instagram_oauth_state type.
    state = create_oauth_state(str(ObjectId()), "n")
    payload = jwt.decode(state, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    assert payload["type"] == "instagram_oauth_state"
    assert "typ" not in payload


# ── Client IP ─────────────────────────────────────────────────────────────────

def test_limiter_uses_client_ip_key():
    assert limiter._key_func is client_ip


def test_forwarded_headers_ignored_by_default(monkeypatch):
    monkeypatch.setattr(settings, "CLIENT_IP_HEADER", "")
    req = _request(headers={"X-Forwarded-For": "1.2.3.4", "CF-Connecting-IP": "5.6.7.8"})
    assert client_ip(req) == "10.0.0.5"


def test_xff_uses_rightmost_trusted_hop_not_spoofed_left(monkeypatch):
    monkeypatch.setattr(settings, "CLIENT_IP_HEADER", "x-forwarded-for")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 1)
    req = _request(headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert client_ip(req) == "203.0.113.9"

    monkeypatch.setattr(settings, "TRUSTED_PROXY_COUNT", 2)
    req = _request(headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9, 10.1.1.1"})
    assert client_ip(req) == "203.0.113.9"


def test_cf_connecting_ip_and_invalid_values(monkeypatch):
    monkeypatch.setattr(settings, "CLIENT_IP_HEADER", "cf-connecting-ip")
    assert client_ip(_request(headers={"CF-Connecting-IP": "198.51.100.7"})) == "198.51.100.7"
    assert client_ip(_request(headers={"CF-Connecting-IP": "not-an-ip"})) == "10.0.0.5"


def test_header_only_trusted_from_allowlisted_proxy(monkeypatch):
    monkeypatch.setattr(settings, "CLIENT_IP_HEADER", "cf-connecting-ip")
    monkeypatch.setattr(settings, "TRUSTED_PROXY_IPS", "10.0.0.0/8")
    assert client_ip(_request(headers={"CF-Connecting-IP": "198.51.100.7"}, peer="10.2.3.4")) == "198.51.100.7"
    assert client_ip(_request(headers={"CF-Connecting-IP": "198.51.100.7"}, peer="8.8.8.8")) == "8.8.8.8"


# ── Cookies ───────────────────────────────────────────────────────────────────

def test_production_cookies_are_host_prefixed_and_host_only(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "LEGACY_SHARED_COOKIES", False)

    resp = _auth_response({"plan": "Free"}, "jwt-value")
    cookies = _set_cookie_headers(resp)
    admin_resp = Response()
    _set_admin_cookie(admin_resp, "admin-jwt")
    cookies += _set_cookie_headers(admin_resp)

    names = {c.split("=", 1)[0] for c in cookies}
    assert names == {"__Host-pg_token", "__Host-pg_csrf", "__Host-pg_admin_token", "__Host-pg_admin_csrf"}
    for c in cookies:
        assert "Domain=" not in c
        assert "Path=/" in c
        assert "secure" in c.lower()

    import json
    body = json.loads(resp.body)
    csrf_cookie = next(c for c in cookies if c.startswith("__Host-pg_csrf="))
    assert csrf_cookie.split(";", 1)[0].split("=", 1)[1] == body["csrf_token"]


def test_legacy_shared_cookie_switch(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "LEGACY_SHARED_COOKIES", True)
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://pinguru.me")
    assert cookie_name("pg_token") == "pg_token"
    cookies = _set_cookie_headers(_auth_response({}, "jwt"))
    assert all("Domain=.pinguru.me" in c for c in cookies)


def test_development_cookie_names_unprefixed(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert cookie_name("pg_token") == "pg_token"


@pytest.fixture
def prod_client(monkeypatch):
    async def _noop():
        return None

    async def _fake_db():
        yield SimpleNamespace()

    monkeypatch.setattr(main_module, "connect_db", _noop)
    monkeypatch.setattr(main_module, "disconnect_db", _noop)
    monkeypatch.setattr(main_module, "validate_startup_config", lambda: None)
    main_module.app.dependency_overrides[get_db] = _fake_db
    with TestClient(main_module.app) as c:
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        monkeypatch.setattr(settings, "LEGACY_SHARED_COOKIES", False)
        yield c
    main_module.app.dependency_overrides.clear()


def test_csrf_middleware_reads_host_prefixed_cookie(prod_client):
    prod_client.cookies.set("__Host-pg_token", "x")
    prod_client.cookies.set("__Host-pg_csrf", "good")
    resp = prod_client.post("/billing/cancel-pending", headers={"X-CSRF-Token": "bad"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid CSRF token"


def test_legacy_parent_domain_csrf_cookie_cannot_satisfy_check(prod_client):
    # A sibling subdomain can plant pg_csrf on .pinguru.me; it must not count.
    prod_client.cookies.set("__Host-pg_token", "x")
    prod_client.cookies.set("pg_csrf", "attacker")
    resp = prod_client.post("/billing/cancel-pending", headers={"X-CSRF-Token": "attacker"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Invalid CSRF token"
