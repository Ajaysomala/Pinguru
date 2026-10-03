import asyncio
import ipaddress
import logging
import socket
import urllib.parse
import httpx
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings

logger = logging.getLogger(__name__)

BASE_GRAPH_FB = f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_API_VERSION}"  # for FB Login / admin
BASE_GRAPH_IG = "https://graph.instagram.com"  # for IG Business Login — NO version in URL
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

METADATA_IPS = {
    "169.254.169.254",
    "169.254.170.2",
    "100.100.100.200",
    "fd00:ec2::254",
}
CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

class InstagramTokenExpiredError(Exception):
    """Raised when Instagram Graph API returns error code 190 or 102 indicating expired or revoked token."""
    def __init__(self, error_message: str = "Instagram token expired or invalid", error_code: int = 190):
        super().__init__(error_message)
        self.error_message = error_message
        self.error_code = error_code


class InstagramService:

    @staticmethod
    def _resolve_instagram_oauth_credentials() -> tuple[str, str, str]:
        """Return (client_id, client_secret, source) for OAuth token exchange.

        Uses IG app credentials when both are set; otherwise falls back to Meta app credentials.
        Prevents mixed credential pairs (e.g., IG app id + Meta app secret), which Meta rejects.
        """
        ig_client_id = (settings.IG_APP_ID or "").strip()
        ig_client_secret = (settings.IG_APP_SECRET or "").strip()
        meta_client_id = (settings.META_APP_ID or "").strip()
        meta_client_secret = (settings.META_APP_SECRET or "").strip()

        if bool(ig_client_id) != bool(ig_client_secret):
            raise ValueError("IG_APP_ID and IG_APP_SECRET must both be set together")

        if ig_client_id and ig_client_secret:
            return ig_client_id, ig_client_secret, "instagram-app"

        if meta_client_id and meta_client_secret:
            return meta_client_id, meta_client_secret, "meta-app"

        raise ValueError("Missing OAuth app credentials: set IG_APP_ID/IG_APP_SECRET or META_APP_ID/META_APP_SECRET")

    @staticmethod
    def _normalize_media_kind(item: dict) -> str:
        media_type = str(item.get("media_type") or "").strip().upper()
        media_product_type = str(item.get("media_product_type") or "").strip().upper()

        if media_product_type == "REELS" or media_type == "REEL":
            return "reel"
        if media_type in {"IMAGE", "CAROUSEL_ALBUM"}:
            return "post"
        if media_type == "VIDEO":
            return "reel" if media_product_type == "REELS" else "post"
        return "all"

    @staticmethod
    def _fernet() -> Fernet:
        return Fernet(settings.ENCRYPTION_KEY.encode("utf-8"))

    @staticmethod
    def encrypt_access_token(access_token: str) -> str:
        if not access_token:
            return ""
        return InstagramService._fernet().encrypt(access_token.encode("utf-8")).decode("utf-8")

    @staticmethod
    def decrypt_access_token(encrypted_access_token: str) -> str:
        if not encrypted_access_token:
            return ""
        try:
            return InstagramService._fernet().decrypt(encrypted_access_token.encode("utf-8")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            if settings.ENVIRONMENT.lower() == "production":
                logger.error("Failed to decrypt Instagram access token in production: %s", exc)
                raise
            # In development/test environments, allow plain-text mock tokens
            logger.warning("Decryption failed; returning raw token (development fallback): %s", exc)
            return encrypted_access_token

    @staticmethod
    async def send_dm(
        access_token: str,
        recipient_ig_id: str,
        message: str,
        ig_user_id: str,
        attachment_url: str | None = None,
        attachment_type: str = "image",
        comment_id: str | None = None,
        buttons: list[dict] | None = None,
    ) -> dict:
        """Send a DM to an Instagram user via Graph API."""
        try:
            access_token = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in send_dm: %s", exc)
            return {"success": False, "error": "Invalid or corrupted access token", "status_code": 401, "error_code": 190}
        endpoint_id = ig_user_id or "me"
        url = f"{BASE_GRAPH_IG}/{endpoint_id}/messages"
        recipient_payload = {"comment_id": comment_id} if comment_id else {"id": recipient_ig_id}
        payload: dict = {
            "recipient": recipient_payload,
            "access_token": access_token,
        }
        message_payload: dict = {}
        if buttons:
            message_payload["attachment"] = {
                "type": "template",
                "payload": {
                    "template_type": "button",
                    "text": message or "Please follow our profile to continue.",
                    "buttons": buttons,
                },
            }
        elif attachment_url:
            message_payload["attachment"] = {
                "type": attachment_type,
                "payload": {"url": attachment_url},
            }
            if message:
                message_payload["text"] = message
        elif message:
            message_payload["text"] = message
        else:
            message_payload["text"] = ""
        payload["message"] = message_payload
        logger.info("Sending Instagram DM request (has_buttons=%s, comment_id=%s)", bool(buttons), comment_id)
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(url, json=payload)
        except httpx.RequestError:
            logger.exception("Instagram DM request failed")
            return {"success": False, "error": "Instagram API request failed"}

        try:
            data = resp.json()
        except ValueError:
            data = {}

        if resp.status_code != 200:
            logger.error("DM failed: status=%s body=%s", resp.status_code, data)
            # If a specific user ID was used and failed with 400/404, retry via /me/messages
            if endpoint_id != "me" and resp.status_code in {400, 404}:
                logger.info("Retrying DM request via /me/messages fallback")
                fallback_url = f"{BASE_GRAPH_IG}/me/messages"
                try:
                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                        resp_fallback = await client.post(fallback_url, json=payload)
                    if resp_fallback.status_code == 200:
                        try:
                            fb_data = resp_fallback.json()
                        except ValueError:
                            fb_data = {}
                        return {"success": True, "data": fb_data}
                except httpx.RequestError:
                    pass

            if buttons:
                logger.warning("Button template DM failed, retrying with standard text message fallback")
                return await InstagramService.send_dm(
                    access_token=access_token,
                    recipient_ig_id=recipient_ig_id,
                    message=message,
                    ig_user_id=ig_user_id,
                    attachment_url=attachment_url,
                    attachment_type=attachment_type,
                    comment_id=comment_id,
                    buttons=None,
                )
            error_obj = data.get("error") or {}
            return {
                "success": False,
                "error": error_obj.get("message", "Instagram API request failed"),
                "status_code": resp.status_code,
                "error_code": error_obj.get("code"),
            }
        return {"success": True, "data": data}

    @staticmethod
    async def verify_account_ownership(access_token: str, ig_account_id: str) -> dict | None:
        """Check if the provided access token owns or can access the given ig_account_id.
        Returns dict with 'id' and 'username' if verified, None otherwise.
        """
        try:
            token = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in verify_account_ownership: %s", exc)
            return None

        url = f"{BASE_GRAPH_IG}/{ig_account_id}"
        params = {
            "fields": "id,username,name",
            "access_token": token,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
                resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json() or {}
                if data.get("id") or data.get("username"):
                    return data
            return None
        except httpx.RequestError:
            logger.warning("Network error while verifying ig_account_id=%s via Graph API", ig_account_id)
            return None

    @staticmethod
    async def get_user_profile(access_token: str) -> dict:
        """Get Instagram business account info."""
        try:
            access_token = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in get_user_profile: %s", exc)
            return {}
        url = f"{BASE_GRAPH_IG}/me"
        params = {
            "fields": "id,name,username,user_id,profile_picture_url,account_type,followers_count",
            "access_token": access_token,
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    return resp.json() or {}
                # Fallback to basic fields if extended fields are unsupported on this node
                logger.info("Extended profile fetch returned %s, falling back to basic fields", resp.status_code)
                fallback_resp = await client.get(url, params={
                    "fields": "id,name,username,user_id",
                    "access_token": access_token,
                })
                if fallback_resp.status_code == 200:
                    return fallback_resp.json() or {}
                return {}
        except httpx.RequestError:
            logger.exception("Instagram profile fetch failed")
            return {}

    @staticmethod
    async def get_messaging_user_profile(access_token: str, instagram_scoped_user_id: str) -> dict:
        """Get the display name, username, and follow status for a user who sent a message."""
        try:
            decrypted = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in get_messaging_user_profile: %s", exc)
            return {}
        url = f"{BASE_GRAPH_IG}/{instagram_scoped_user_id}"
        params = {
            "fields": "name,username,is_user_follow_business",
            "access_token": decrypted,
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 400:
                    # Fallback to name,username if is_user_follow_business is not supported on this node
                    retry_resp = await client.get(url, params={"fields": "name,username", "access_token": decrypted})
                    if retry_resp.status_code == 200:
                        return retry_resp.json() or {}

            if resp.status_code != 200:
                logger.warning(
                    "Instagram messaging user profile lookup returned %s: %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return {}
            return resp.json() or {}
        except httpx.RequestError:
            logger.exception("Instagram messaging user profile lookup failed")
            return {}

    @staticmethod
    async def get_business_account_id(access_token: str, preferred_username: str | None = None) -> str | None:
        """Resolve Instagram Business Account ID that matches webhook entry.id/recipient.id."""
        try:
            decrypted = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in get_business_account_id: %s", exc)
            return None
        url = f"{BASE_GRAPH_FB}/me/accounts"
        params = {
            "fields": "instagram_business_account{id,username}",
            "access_token": decrypted,
        }
        preferred = (preferred_username or "").strip().lower()

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning("Instagram business account lookup returned %s", resp.status_code)
                return None

            payload = resp.json() or {}
            candidates: list[dict] = []
            for account in payload.get("data", []):
                ig_business = account.get("instagram_business_account") or {}
                ig_id = str(ig_business.get("id") or "").strip()
                if not ig_id:
                    continue
                candidates.append(
                    {
                        "id": ig_id,
                        "username": str(ig_business.get("username") or "").strip().lower(),
                    }
                )

            if not candidates:
                return None

            if preferred:
                for candidate in candidates:
                    if candidate["username"] == preferred:
                        return str(candidate["id"])

            return str(candidates[0]["id"])
        except httpx.RequestError:
            logger.exception("Instagram business account lookup failed")
            return None

    @staticmethod
    async def get_user_media(access_token: str, limit: int = 25, media_type: str = "all") -> list[dict]:
        """Fetch recent Instagram media for the connected business account."""
        try:
            decrypted = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in get_user_media: %s", exc)
            raise InstagramTokenExpiredError(
                error_message="Invalid or corrupted Instagram access token",
                error_code=190,
            ) from exc
        url = f"{BASE_GRAPH_IG}/me/media"
        params = {
            "fields": "id,caption,media_type,media_product_type,media_url,thumbnail_url,permalink,timestamp",
            "limit": limit,
            "access_token": decrypted,
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
            if resp.status_code != 200:
                body_sample = resp.text[:300]
                logger.warning("Instagram media fetch returned %s: %s", resp.status_code, body_sample)
                try:
                    payload = resp.json() or {}
                except ValueError:
                    payload = {}
                error_obj = payload.get("error") or {}
                error_code = error_obj.get("code")
                if error_code in (190, 102) or (resp.status_code in (400, 401) and ("token" in body_sample.lower() or error_code in (190, 102))):
                    raise InstagramTokenExpiredError(
                        error_message=str(error_obj.get("message") or "Instagram token expired or invalid"),
                        error_code=error_code or 190,
                    )
                return []

            payload = resp.json() or {}
            items: list[dict] = []
            wanted = (media_type or "all").strip().lower()
            for item in payload.get("data", []):
                kind = InstagramService._normalize_media_kind(item)
                if wanted in {"post", "reel"} and kind != wanted:
                    continue
                items.append(
                    {
                        "id": str(item.get("id") or ""),
                        "caption": item.get("caption") or "",
                        "media_type": kind,
                        "media_product_type": str(item.get("media_product_type") or "").lower() or None,
                        "media_url": item.get("media_url") or "",
                        "thumbnail_url": item.get("thumbnail_url") or "",
                        "permalink": item.get("permalink") or "",
                        "timestamp": item.get("timestamp") or "",
                    }
                )
            return items
        except InstagramTokenExpiredError:
            raise
        except httpx.RequestError:
            logger.exception("Instagram media fetch failed")
            return []

    @staticmethod
    async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
        """Exchange OAuth code for short-lived token (Instagram Business Login flow),
        then exchange for long-lived token (60 days)."""
        try:
            ig_client_id, ig_client_secret, credential_source = InstagramService._resolve_instagram_oauth_credentials()
        except ValueError as exc:
            logger.error("Instagram OAuth config error: %s", exc)
            return {"success": False, "error": "Instagram OAuth configuration is incomplete"}

        # Step 1: short-lived token via Instagram API endpoint (not Facebook Graph)
        token_url = "https://api.instagram.com/oauth/access_token"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(token_url, data={
                    "client_id": ig_client_id,
                    "client_secret": ig_client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                    "code": code,
                })
            try:
                short = resp.json()
            except ValueError:
                short = {}
            logger.info("Short-lived token response status: %s (credentials=%s)", resp.status_code, credential_source)
            if "access_token" not in short:
                error_message = str(
                    short.get("error_message")
                    or short.get("error_description")
                    or short.get("error")
                    or "Instagram token exchange failed"
                )
                logger.warning("Instagram short-lived token exchange failed: status=%s body=%s", resp.status_code, short)
                return {"success": False, "error": error_message}
        except httpx.RequestError:
            logger.exception("Instagram short-lived token exchange failed")
            return {"success": False, "error": "Instagram token exchange failed"}

        # Capture user_id from short token — available here, not in long-lived response
        short_user_id = str(short.get("user_id") or "")

        # Step 2: exchange for long-lived token (60 days) via Graph API
        ll_url = f"{BASE_GRAPH_IG}/access_token"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(ll_url, params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": ig_client_secret,
                    "access_token": short["access_token"],
                })
            try:
                ll_data = resp.json()
            except ValueError:
                ll_data = {}
            logger.info("Long-lived token response status: %s", resp.status_code)
            if "access_token" not in ll_data:
                error_message = str(
                    ll_data.get("error", {}).get("message")
                    or ll_data.get("error_message")
                    or "Failed to exchange for long-lived Instagram access token"
                )
                logger.warning("Long-lived token exchange failed: status=%s body=%s", resp.status_code, ll_data)
                return {"success": False, "error": error_message}
            return {"success": True, "token_data": {**ll_data, "user_id": short_user_id}}
        except httpx.RequestError:
            logger.exception("Instagram long-lived token exchange failed")
            return {"success": False, "error": "Instagram long-lived token exchange request failed"}

    @staticmethod
    async def reply_to_comment(access_token: str, comment_id: str, message: str) -> dict:
        """Reply to an Instagram comment (not DM — comment reply)."""
        url = f"{BASE_GRAPH_IG}/{comment_id}/replies"
        try:
            decrypted = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in reply_to_comment: %s", exc)
            return {"success": False, "error": "Invalid or corrupted access token"}
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(url, data={
                    "message": message,
                    "access_token": decrypted,
                })
            return resp.json()
        except httpx.RequestError:
            logger.exception("Instagram comment reply failed")
            return {"success": False, "error": "Instagram API request failed"}
    @staticmethod
    async def subscribe_app_to_webhooks(access_token: str, ig_id: str) -> bool:
        """Call POST graph.instagram.com/{ig_id}/subscribed_apps?subscribed_fields=messages,comments,messaging_postbacks (mentions if supported).
        Returns True if successful, False otherwise.
        """
        try:
            token = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in subscribe_app_to_webhooks: %s", exc)
            return False

        if not ig_id:
            logger.warning("Missing ig_id for subscribe_app_to_webhooks")
            return False

        url = f"{BASE_GRAPH_IG}/{ig_id}/subscribed_apps"
        fields_with_mentions = "messages,comments,messaging_postbacks,mentions"
        fields_fallback = "messages,comments,messaging_postbacks"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(url, params={
                    "subscribed_fields": fields_with_mentions,
                    "access_token": token,
                })
                if resp.status_code == 200:
                    data = resp.json() or {}
                    return bool(data.get("success") is True or data.get("data") is not None or resp.status_code == 200)

                # Retry without mentions if first call fails (e.g. mentions unsupported on this node)
                logger.info("Subscribed apps with mentions returned %s: %s; retrying with fallback fields", resp.status_code, resp.text[:200])
                resp_fallback = await client.post(url, params={
                    "subscribed_fields": fields_fallback,
                    "access_token": token,
                })
                if resp_fallback.status_code == 200:
                    data = resp_fallback.json() or {}
                    return bool(data.get("success") is True or data.get("data") is not None or resp_fallback.status_code == 200)

                logger.warning("Subscribed apps failed with fallback fields: %s %s", resp_fallback.status_code, resp_fallback.text[:200])
                return False
        except httpx.RequestError:
            logger.exception("Network error while subscribing app to Instagram webhooks")
            return False

    @staticmethod
    async def get_granted_permissions(access_token: str) -> list[str]:
        """Fetch list of granted scopes/permissions for the Instagram access token."""
        try:
            token = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in get_granted_permissions: %s", exc)
            return []

        url = f"{BASE_GRAPH_IG}/me/permissions"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, params={"access_token": token})
            if resp.status_code == 200:
                payload = resp.json() or {}
                granted = [
                    str(item.get("permission"))
                    for item in payload.get("data", [])
                    if item.get("status") == "granted" and item.get("permission")
                ]
                return granted
            logger.warning("Fetching permissions returned %s: %s", resp.status_code, resp.text[:200])
            return []
        except httpx.RequestError:
            logger.exception("Network error while fetching granted permissions")
            return []

    @staticmethod
    async def refresh_long_lived_token(access_token: str) -> dict:
        """Refresh a long-lived token. Call every 30-45 days."""
        try:
            decrypted = InstagramService.decrypt_access_token(access_token)
        except (InvalidToken, ValueError) as exc:
            logger.error("Failed to decrypt access token in refresh_long_lived_token: %s", exc)
            return {"error": "Invalid or corrupted access token"}
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(f"{BASE_GRAPH_IG}/refresh_access_token", params={
                    "grant_type": "ig_refresh_token",
                    "access_token": decrypted,
                })
                try:
                    payload = resp.json() or {}
                except ValueError:
                    payload = {}
                if resp.status_code != 200:
                    logger.warning("Instagram token refresh returned %s: %s", resp.status_code, resp.text[:200])
                return payload
        except httpx.RequestError:
            logger.exception("Network error while refreshing Instagram token")
            return {"error": "Network error while refreshing Instagram token"}

    @staticmethod
    def is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        """Reject loopback, private, link-local, reserved, multicast, unspecified, and cloud metadata IPs."""
        if isinstance(ip_obj, ipaddress.IPv6Address) and getattr(ip_obj, "ipv4_mapped", None):
            ip_obj = ip_obj.ipv4_mapped

        if (
            ip_obj.is_loopback
            or ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        ):
            return True

        if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in CGNAT_NETWORK:
            return True

        if str(ip_obj).lower() in METADATA_IPS:
            return True

        return False

    @staticmethod
    async def _resolve_and_validate_host(hostname: str) -> tuple[bool, str]:
        """Resolve hostname and reject loopback, private, link-local, reserved, and metadata IPs (IPv4 and IPv6)."""
        clean_host = hostname.strip().strip("[]")
        if not clean_host:
            return False, "Missing hostname in attachment URL"

        # 1. Direct IP literal check
        try:
            direct_ip = ipaddress.ip_address(clean_host)
            if InstagramService.is_forbidden_ip(direct_ip):
                return False, f"Attachment URL references forbidden IP: {direct_ip}"
            return True, ""
        except ValueError:
            pass

        # 2. Reject obvious local hostnames before DNS resolution
        lower_host = clean_host.lower()
        if lower_host in {"localhost", "localhost.localdomain", "broadcasthost"} or lower_host.endswith(".local"):
            return False, f"Attachment URL references forbidden host: {clean_host}"

        # 3. DNS resolution via getaddrinfo
        loop = asyncio.get_running_loop()
        try:
            addr_infos = await loop.run_in_executor(
                None,
                socket.getaddrinfo,
                clean_host,
                443,
                socket.AF_UNSPEC,
                socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            return False, f"Could not resolve host '{clean_host}': {exc}"
        except Exception as exc:
            return False, f"DNS resolution failed for host '{clean_host}': {exc}"

        if not addr_infos:
            return False, f"No IP addresses resolved for host '{clean_host}'"

        for item in addr_infos:
            sockaddr = item[4]
            ip_str = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(ip_str)
                if InstagramService.is_forbidden_ip(ip_obj):
                    return False, f"Attachment URL host '{clean_host}' resolves to forbidden IP: {ip_str}"
            except ValueError:
                return False, f"Invalid resolved IP '{ip_str}' for host '{clean_host}'"

        return True, ""

    @staticmethod
    async def validate_attachment_url(url: str, max_size_bytes: int = 8 * 1024 * 1024) -> tuple[bool, str]:
        """Validate an image attachment URL:
        - Must start with https://
        - Require port 443
        - Resolve hostname and reject loopback, private, link-local, reserved, and metadata IPs (IPv4 and IPv6)
        - Use follow_redirects=False, re-validating every redirect hop with max 3 hops
        - 3.0s timeout
        - Content-Type must be an image (starts with image/)
        - Content-Length must not exceed max_size_bytes (8MB)
        Returns (is_valid, error_message).
        """
        clean_url = (url or "").strip()
        if not clean_url:
            return False, "Attachment URL is required"
        if len(clean_url) > 2048:
            return False, "Attachment URL must be 2048 characters or fewer"

        current_url = clean_url
        max_redirects = 3
        redirect_count = 0
        timeout = httpx.Timeout(3.0, connect=3.0)

        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                while True:
                    parsed = urllib.parse.urlsplit(current_url)
                    if parsed.scheme.lower() != "https":
                        return False, f"Attachment URL must use https (got '{parsed.scheme}')"

                    port = parsed.port or 443
                    if port != 443:
                        return False, f"Attachment URL must use port 443 (got port {port})"

                    hostname = parsed.hostname
                    if not hostname:
                        return False, "Attachment URL missing valid hostname"

                    valid_host, host_err = await InstagramService._resolve_and_validate_host(hostname)
                    if not valid_host:
                        return False, host_err

                    try:
                        resp = await client.head(current_url)
                    except httpx.RequestError as exc:
                        return False, f"Could not reach attachment URL: {exc}"

                    # Re-validate every redirect hop with a max of 3
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= max_redirects:
                            return False, f"Too many redirects (max {max_redirects} allowed)"
                        loc = resp.headers.get("location")
                        if not loc:
                            return False, f"Redirect status {resp.status_code} missing Location header"
                        current_url = urllib.parse.urljoin(current_url, loc)
                        redirect_count += 1
                        continue

                    # Fallback if server doesn't allow HEAD
                    if resp.status_code == 405:
                        try:
                            resp = await client.get(current_url, headers={"Range": "bytes=0-1024"})
                        except httpx.RequestError as exc:
                            return False, f"Could not reach attachment URL: {exc}"

                        if resp.status_code in {301, 302, 303, 307, 308}:
                            if redirect_count >= max_redirects:
                                return False, f"Too many redirects (max {max_redirects} allowed)"
                            loc = resp.headers.get("location")
                            if not loc:
                                return False, f"Redirect status {resp.status_code} missing Location header"
                            current_url = urllib.parse.urljoin(current_url, loc)
                            redirect_count += 1
                            continue

                    if resp.status_code not in {200, 206}:
                        return False, f"Attachment URL returned HTTP status {resp.status_code}"

                    content_type = (resp.headers.get("content-type") or "").strip().lower()
                    if not content_type.startswith("image/"):
                        return False, f"Attachment URL must point to an image, got Content-Type: '{content_type or 'unknown'}'"

                    content_length_str = resp.headers.get("content-length")
                    if content_length_str and content_length_str.isdigit():
                        size = int(content_length_str)
                        if size > max_size_bytes:
                            return False, "Attachment image size exceeds the 8MB limit"

                    return True, ""
        except Exception as exc:
            return False, f"Attachment URL validation failed: {str(exc)}"
