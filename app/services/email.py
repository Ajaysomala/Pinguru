import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _otp_html(otp: str) -> str:
    return f"""
    <div style='font-family: Inter, Arial, sans-serif; max-width: 520px; margin: 0 auto; padding: 28px;'>
      <h2 style='margin: 0 0 10px; color: #6d28d9;'>PinGuru Email Verification</h2>
      <p style='margin: 0 0 14px; color: #111827;'>Your one-time verification code is:</p>
      <div style='font-size: 36px; letter-spacing: 8px; font-weight: 700; color: #4f46e5; margin: 8px 0 16px;'>
        {otp}
      </div>
      <p style='margin: 0; color: #6b7280; font-size: 14px;'>
        This code expires in 5 minutes. Do not share this code with anyone.
      </p>
    </div>
    """


async def _send_via_resend(to_email: str, otp: str) -> bool:
    if not settings.RESEND_API_KEY:
        return False

    from_email = settings.OTP_FROM_EMAIL or settings.SMTP_EMAIL or "noreply@pinguru.me"
    payload = {
        "from": f"PinGuru <{from_email}>",
        "to": [to_email],
        "subject": "Verify your PinGuru account",
        "html": _otp_html(otp),
    }
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            resp = await client.post("https://api.resend.com/emails", headers=headers, json=payload)
        if resp.status_code in (200, 201, 202):
            return True
        logger.error("Resend failed with status code %s", resp.status_code)
        return False
    except Exception:
        logger.exception("Resend email send failed")
        return False


def _send_via_smtp_sync(to_email: str, otp: str) -> bool:
    smtp_email = settings.SMTP_EMAIL.strip()
    smtp_password = settings.SMTP_APP_PASSWORD.strip()
    if not smtp_email or not smtp_password:
        return False

    message = MIMEMultipart("alternative")
    message["Subject"] = "Verify your PinGuru account"
    message["From"] = settings.OTP_FROM_EMAIL.strip() or smtp_email
    message["To"] = to_email
    message.attach(MIMEText(_otp_html(otp), "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(message["From"], [to_email], message.as_string())
        return True
    except Exception:
        logger.exception("SMTP OTP send failed")
        return False


async def send_otp_email(to_email: str, otp: str) -> bool:
    """Try Resend first, SMTP fallback."""
    resend_ok = await _send_via_resend(to_email, otp)
    if resend_ok:
        return True
    return await asyncio.to_thread(_send_via_smtp_sync, to_email, otp)
