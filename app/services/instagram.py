import httpx
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings
import logging

logger = logging.getLogger(__name__)

BASE = f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_API_VERSION}"

class InstagramService:

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
        url = f"{BASE}/{ig_user_id}/messages"
        payload = {
            "recipient": {"id": recipient_ig_id},
            "message": {"text": message},
            "access_token": access_token,
        }
        logger.info(f"DM request payload: {payload}")
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if resp.status_code != 200:
                logger.error("DM failed with non-200 response from Instagram API")
                logger.error(f"DM error response: {resp.text}")
                return {"success": False, "error": data}
            return {"success": True, "data": data}

    @staticmethod
    async def get_user_profile(access_token: str) -> dict:
        """Get Instagram business account info."""
        url = f"{BASE}/me"
        params = {
            "fields": "id,name",
            "access_token": access_token,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params)
            return resp.json()

    @staticmethod
    async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
        # CHANGE THIS: Use Graph API instead of api.instagram.com
        token_url = f"{BASE}/oauth/access_token" 
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(token_url, params={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "redirect_uri": redirect_uri,
                "code": code,
            })
            token_data = resp.json()
            
            if "access_token" not in token_data:
                return {"success": False, "error": token_data}
                
            return {"success": True, "token_data": token_data}

        # Step 2: exchange for long-lived token (60 days) via Graph API
        ll_url = f"{BASE}/access_token"
        async with httpx.AsyncClient() as client:
            resp = await client.get(ll_url, params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.META_APP_SECRET,
                "access_token": short["access_token"],
            })
            ll_data = resp.json()
            logger.info(f"Long-lived token response status: {resp.status_code}")
            if "access_token" not in ll_data:
                # Fall back to short-lived token if long-lived exchange fails
                logger.warning("Long-lived token exchange failed, using short-lived token")
                return {"success": True, "token_data": {
                    "access_token": short["access_token"],
                    "expires_in": 3600,
                }}
            return {"success": True, "token_data": ll_data}

    @staticmethod
    async def reply_to_comment(access_token: str, comment_id: str, message: str) -> dict:
        """Reply to an Instagram comment (not DM — comment reply)."""
        url = f"{BASE}/{comment_id}/replies"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data={
                "message": message,
                "access_token": access_token,
            })
            return resp.json()
