import httpx
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings
import logging

logger = logging.getLogger(__name__)

BASE_GRAPH_FB = f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_API_VERSION}"  # for FB Login / admin
BASE_GRAPH_IG = "https://graph.instagram.com"  # for IG Business Login — NO version in URL
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

class InstagramService:

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
        except InvalidToken:
            # Backward compatibility: allow pre-hardening plain-text tokens.
            return encrypted_access_token

    @staticmethod
    async def send_dm(access_token: str, recipient_ig_id: str, message: str, ig_user_id: str) -> dict:
        """Send a DM to an Instagram user via Graph API."""
        access_token = InstagramService.decrypt_access_token(access_token)
        url = f"{BASE_GRAPH_IG}/{ig_user_id}/messages"
        payload = {
            "recipient": {"id": recipient_ig_id},
            "message": {"text": message},
            "access_token": access_token,
        }
        logger.info("Sending Instagram DM request")
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
            logger.error("DM failed with non-200 response from Instagram API: %s", resp.status_code)
            return {"success": False, "error": "Instagram API request failed", "status_code": resp.status_code}
        return {"success": True, "data": data}

    @staticmethod
    async def get_user_profile(access_token: str) -> dict:
        """Get Instagram business account info."""
        url = f"{BASE_GRAPH_IG}/me"
        params = {
            "fields": "id,name,username",
            "access_token": access_token,
        }
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, params=params)
            return resp.json()
        except httpx.RequestError:
            logger.exception("Instagram profile fetch failed")
            return {}

    @staticmethod
    async def get_user_media(access_token: str, limit: int = 25, media_type: str = "all") -> list[dict]:
        """Fetch recent Instagram media for the connected business account."""
        decrypted = InstagramService.decrypt_access_token(access_token)
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
                logger.warning("Instagram media fetch returned %s", resp.status_code)
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
        except httpx.RequestError:
            logger.exception("Instagram media fetch failed")
            return []

    @staticmethod
    async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
        """Exchange OAuth code for short-lived token (Instagram Business Login flow),
        then exchange for long-lived token (60 days)."""
        # Use IG_APP_ID/IG_APP_SECRET for token exchange (Instagram sub-app credentials)
        ig_client_id = settings.IG_APP_ID or settings.META_APP_ID
        ig_client_secret = settings.IG_APP_SECRET or settings.META_APP_SECRET
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
            short = resp.json()
            logger.info("Short-lived token response status: %s", resp.status_code)
            if "access_token" not in short:
                return {"success": False, "error": "Instagram token exchange failed"}
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
            ll_data = resp.json()
            logger.info("Long-lived token response status: %s", resp.status_code)
            if "access_token" not in ll_data:
                # Fall back to short-lived token if long-lived exchange fails
                logger.warning("Long-lived token exchange failed, using short-lived token")
                return {"success": True, "token_data": {
                    "access_token": short["access_token"],
                    "expires_in": 3600,
                    "user_id": short_user_id,
                }}
            return {"success": True, "token_data": {**ll_data, "user_id": short_user_id}}
        except httpx.RequestError:
            logger.exception("Instagram long-lived token exchange failed")
            return {"success": True, "token_data": {
                "access_token": short["access_token"],
                "expires_in": 3600,
                "user_id": short_user_id,
            }}

    @staticmethod
    async def reply_to_comment(access_token: str, comment_id: str, message: str) -> dict:
        """Reply to an Instagram comment (not DM — comment reply)."""
        url = f"{BASE_GRAPH_IG}/{comment_id}/replies"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.post(url, data={
                    "message": message,
                    "access_token": access_token,
                })
            return resp.json()
        except httpx.RequestError:
            logger.exception("Instagram comment reply failed")
            return {"success": False, "error": "Instagram API request failed"}
    # Add to instagram.py
    @staticmethod
    async def refresh_long_lived_token(access_token: str) -> dict:
        """Refresh a long-lived token. Call every 30-45 days."""
        decrypted = InstagramService.decrypt_access_token(access_token)
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{BASE_GRAPH_IG}/refresh_access_token", params={
                "grant_type": "ig_refresh_token",
                "access_token": decrypted,
            })
            return resp.json()  # returns {access_token, token_type, expires_in}
