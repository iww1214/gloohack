"""Shared Twilio client configuration for API-key and legacy auth."""

import os

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()


def create_twilio_client() -> Client:
    """Prefer a restricted API key; use the account Auth Token only as fallback."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    key_sid = (
        os.getenv("TWILIO_API_KEY_SID")
        or os.getenv("TWILIO_API_KEY")
        or ""
    ).strip()
    key_secret = (
        os.getenv("TWILIO_API_KEY_SECRET")
        or os.getenv("TWILIO_API_SECRET")
        or ""
    ).strip()

    if account_sid.startswith("SK"):
        raise RuntimeError(
            "TWILIO_ACCOUNT_SID must be your Account SID (starts with AC...), "
            "not your API Key SID (SK...). Copy the AC... value from the Twilio "
            "Console dashboard."
        )
    if key_sid and not key_sid.startswith("SK"):
        raise RuntimeError("Twilio API Key SID must start with SK...")
    if key_secret.startswith("SK"):
        raise RuntimeError(
            "Twilio API key secret must be the 32-character secret shown when the "
            "key was created, not an SK... SID. Check TWILIO_API_KEY_SECRET/"
            "TWILIO_API_SECRET."
        )

    if bool(key_sid) != bool(key_secret):
        raise RuntimeError(
            "Set both Twilio API key ID and secret using TWILIO_API_KEY/"
            "TWILIO_API_SECRET or TWILIO_API_KEY_SID/TWILIO_API_KEY_SECRET"
        )
    if key_sid and key_secret:
        if not account_sid:
            raise RuntimeError("TWILIO_ACCOUNT_SID is required with an API key")
        return Client(key_sid, key_secret, account_sid=account_sid)

    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    if account_sid and auth_token:
        return Client(account_sid, auth_token)
    raise RuntimeError("Configure Twilio API key credentials or account SID/Auth Token")


def get_twilio_from_phone() -> str:
    """Return configured sender number, supporting both project naming variants."""
    phone = (os.getenv("TWILIO_FROM_PHONE") or os.getenv("TWILIO_PHONE_NUMBER") or "").strip()
    if not phone:
        raise RuntimeError("Configure TWILIO_FROM_PHONE or TWILIO_PHONE_NUMBER")
    return phone
