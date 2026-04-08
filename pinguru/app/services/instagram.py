import httpx
from app.config import settings
import logging

logger = logging.getLogger(__name__)

BASE = f"https://graph.facebook.com/{settings.INSTAGRAM_GRAPH_API_VERSION}"

class InstagramService:

    @staticmethod
    async def send_dm(access_token: str, recipient_ig_id: str, message: str) -> dict:
        """Send a DM to an Instagram user via Graph API."""
        url = f"{BASE}/me/messages"
        payload = {
            "recipient": {"id": recipient_ig_id},
            "message": {"text": message},
            "access_token": access_token,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload)
            data = resp.json()
            if resp.status_code != 200:
                logger.error(f"DM failed: {data}")
                return {"success": False, "error": data}
            return {"success": True, "data": data}

    @staticmethod
    async def get_user_profile(access_token: str) -> dict:
        """Get Instagram business account info."""
        url = f"{BASE}/me"
        params = {
            "fields": "id,name,username,profile_picture_url,followers_count",
            "access_token": access_token,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params)
            return resp.json()

    @staticmethod
    async def exchange_code_for_token(code: str, redirect_uri: str) -> dict:
        """Exchange OAuth code for short-lived token, then get long-lived token."""
        # Step 1: short-lived token
        token_url = f"{BASE}/oauth/access_token"
        async with httpx.AsyncClient() as client:
            resp = await client.post(token_url, data={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            })
            short = resp.json()
            if "access_token" not in short:
                return {"success": False, "error": short}

        # Step 2: exchange for long-lived token (60 days)
        ll_url = f"{BASE}/oauth/access_token"
        async with httpx.AsyncClient() as client:
            resp = await client.get(ll_url, params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "fb_exchange_token": short["access_token"],
            })
            return {"success": True, "token_data": resp.json()}

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
