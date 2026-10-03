"""Tokens and secrets must never reach log output."""
import asyncio
import json
import logging

import httpx
import pytest

import app.main  # noqa: F401  (installs log redaction)
from app.log_redaction import redact
from app.services.instagram import BASE_GRAPH_IG, InstagramService

TOKEN = "IGAAsecretTOKENvalue1234567890abcdefXYZ"
SECRET = "app_secret_value_987654321"


@pytest.mark.parametrize(
    "text",
    [
        f"{BASE_GRAPH_IG}/me?fields=id&access_token={TOKEN}",
        f"https://graph.instagram.com/me?access_token%3D{TOKEN}",
        f"grant_type=ig_exchange_token&client_secret={SECRET}&access_token={TOKEN}",
        f"fb_exchange_token={TOKEN}&x=1",
        f"refresh_token={TOKEN}",
        f"hub.mode=subscribe&hub.verify_token={TOKEN}&hub.challenge=1",
        f'{{"access_token": "{TOKEN}", "client_secret": "{SECRET}"}}',
        f"{{'access_token': '{TOKEN}'}}",
        f"Authorization: Bearer {TOKEN}",
        f"headers={{'authorization': 'bearer {TOKEN}'}}",
    ],
)
def test_redact_patterns(text):
    out = redact(text)
    assert TOKEN not in out
    assert SECRET not in out
    assert "[REDACTED]" in out


def test_graph_url_with_access_token_never_in_captured_logs(caplog):
    url = f"{BASE_GRAPH_IG}/me/media?fields=id&access_token={TOKEN}"
    app_logger = logging.getLogger("app.services.instagram")

    def handler(request):
        return httpx.Response(200, json={"ok": True})

    async def _call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await client.get(url)

    # Even if someone lowers httpx back to INFO/DEBUG, its "HTTP Request: GET <url>" line is redacted.
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG)
    asyncio.run(_call())
    app_logger.info("Calling %s", url)
    app_logger.warning("Body: %s", {"access_token": TOKEN, "client_secret": SECRET})
    logging.getLogger().error(f"root logger {url}")
    try:
        raise RuntimeError(f"request failed for {url}")
    except RuntimeError:
        app_logger.exception("Graph call failed")

    assert any("HTTP Request" in r.getMessage() for r in caplog.records), "httpx line not captured"
    assert TOKEN not in caplog.text
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in (record.exc_text or "")


def test_httpx_and_httpcore_default_to_warning():
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# ── Bearer header instead of query string ─────────────────────────────────────

@pytest.fixture
def captured_requests(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "1", "username": "u", "data": [], "success": True})

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(InstagramService, "decrypt_access_token", lambda tok: tok)
    return requests


def _assert_bearer_only(request):
    assert request.headers.get("authorization") == f"Bearer {TOKEN}"
    assert "access_token" not in str(request.url)
    assert TOKEN not in str(request.url)
    assert TOKEN not in request.content.decode("utf-8", "ignore")


@pytest.mark.parametrize(
    "call",
    [
        lambda: InstagramService.send_dm(TOKEN, "user_1", "hi", "biz_1"),
        lambda: InstagramService.get_messaging_user_profile(TOKEN, "user_1"),
        lambda: InstagramService.get_user_profile(TOKEN),
        lambda: InstagramService.get_user_media(TOKEN),
        lambda: InstagramService.verify_account_ownership(TOKEN, "biz_1"),
        lambda: InstagramService.reply_to_comment(TOKEN, "comment_1", "thanks"),
        lambda: InstagramService.subscribe_app_to_webhooks(TOKEN, "biz_1"),
        lambda: InstagramService.get_granted_permissions(TOKEN),
    ],
)
def test_graph_instagram_calls_send_token_in_header(captured_requests, call):
    asyncio.run(call())
    assert captured_requests
    for request in captured_requests:
        assert request.url.host == "graph.instagram.com"
        _assert_bearer_only(request)


def test_send_dm_body_has_no_token(captured_requests):
    asyncio.run(InstagramService.send_dm(TOKEN, "user_1", "hi", "biz_1"))
    body = json.loads(captured_requests[0].content)
    assert "access_token" not in body
    assert body["recipient"] == {"id": "user_1"}


@pytest.mark.parametrize(
    "call",
    [
        lambda: InstagramService.send_dm(TOKEN, "user_1", "hi", "biz_1"),
        lambda: InstagramService.get_messaging_user_profile(TOKEN, "user_1"),
        lambda: InstagramService.get_user_profile(TOKEN),
        lambda: InstagramService.get_user_media(TOKEN),
        lambda: InstagramService.verify_account_ownership(TOKEN, "biz_1"),
        lambda: InstagramService.reply_to_comment(TOKEN, "comment_1", "thanks"),
        lambda: InstagramService.subscribe_app_to_webhooks(TOKEN, "biz_1"),
        lambda: InstagramService.get_granted_permissions(TOKEN),
    ],
)
def test_ig_token_in_header_false_uses_access_token_parameter(captured_requests, monkeypatch, call):
    from app.config import settings

    monkeypatch.setattr(settings, "IG_TOKEN_IN_HEADER", False)
    asyncio.run(call())
    assert captured_requests
    for request in captured_requests:
        assert "authorization" not in request.headers
        body = request.content.decode("utf-8", "ignore")
        in_query = request.url.params.get("access_token") == TOKEN
        in_body = f'"access_token": "{TOKEN}"' in body or f'"access_token":"{TOKEN}"' in body or f"access_token={TOKEN}" in body
        assert in_query or in_body, f"no access_token parameter on {request.method} {request.url.path}"


def test_ig_token_in_header_defaults_to_true():
    from app.config import Settings

    assert Settings.model_fields["IG_TOKEN_IN_HEADER"].default is True
