"""
facebook_connection.py — Facebook Messenger (Send API).
"""

import logging
import os
from typing import Dict, Optional, Tuple

import httpx

from client.base_connection import (
    GRAPH_API_URL,
    carry_over_context,
    get_session_id as _get_session_id,
    get_yesterday_session_id as _get_yesterday_session_id,
    truncate,
    verify_webhook as _verify_webhook,
)

logger = logging.getLogger(__name__)

FACEBOOK_PAGE_ACCESS_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
FACEBOOK_PAGE_ID = os.getenv("FACEBOOK_PAGE_ID")
FACEBOOK_VERIFY_TOKEN = os.getenv("FACEBOOK_VERIFY_TOKEN")

MAX_LENGTH = 2000

_http_client: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def close():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def is_configured() -> bool:
    return bool(FACEBOOK_PAGE_ACCESS_TOKEN)


# ==================== WEBHOOK ====================

def verify_webhook(mode: Optional[str], token: Optional[str], challenge: Optional[str]) -> Tuple[bool, str]:
    return _verify_webhook(mode, token, FACEBOOK_VERIFY_TOKEN, challenge, "Facebook")


def parse_webhook_payload(body: dict) -> Optional[Dict]:
    """Text messages and postbacks. Echoes, reads and attachments return None."""
    try:
        entry = body.get("entry", [{}])[0]
        messaging = entry.get("messaging", [])
        if not messaging:
            return None

        event = messaging[0]
        sender_id = event.get("sender", {}).get("id")
        recipient_id = event.get("recipient", {}).get("id")
        timestamp = event.get("timestamp")

        if "message" in event:
            message_data = event["message"]
            message_text = message_data.get("text")
            if not message_text:
                logger.info(f"📎 Ignoring non-text Facebook message from {sender_id}")
                return None
            return {
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message": message_text,
                "message_id": message_data.get("mid"),
                "timestamp": timestamp,
                "message_type": "text",
                "is_echo": message_data.get("is_echo", False),
            }

        if "postback" in event:
            postback = event["postback"]
            payload = postback.get("payload") or postback.get("title")
            logger.info(f"🔘 Facebook postback from {sender_id}: {payload}")
            return {
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message": payload,
                "message_id": None,
                "timestamp": timestamp,
                "message_type": "postback",
                "is_echo": False,
            }

        return None
    except Exception as e:
        logger.error(f"Error parsing Facebook webhook payload: {e}")
        return None


# ==================== SESSIONS ====================

def get_session_id(sender_id: str) -> str:
    return _get_session_id("facebook", sender_id)


def get_yesterday_session_id(sender_id: str) -> str:
    return _get_yesterday_session_id("facebook", sender_id)


def get_previous_summary(sender_id: str, session_store=None) -> Optional[str]:
    return carry_over_context("facebook", sender_id, session_store)


# ==================== API CALLS ====================

async def _post(payload: dict, what: str) -> bool:
    if not is_configured():
        logger.error("Facebook Page Access Token not configured")
        return False
    url = f"{GRAPH_API_URL}/me/messages"
    try:
        response = await _client().post(
            url, params={"access_token": FACEBOOK_PAGE_ACCESS_TOKEN}, json=payload
        )
        if response.status_code == 200:
            return True
        logger.error(f"{what} failed: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        logger.error(f"{what} error: {e}")
        return False


async def mark_as_seen(sender_id: str) -> bool:
    return await _post({"recipient": {"id": sender_id}, "sender_action": "mark_seen"}, "Facebook mark-seen")


async def send_typing_indicator(sender_id: str, typing_on: bool = True) -> bool:
    action = "typing_on" if typing_on else "typing_off"
    return await _post({"recipient": {"id": sender_id}, "sender_action": action}, "Facebook typing")


async def send_message(sender_id: str, text: str) -> bool:
    ok = await _post(
        {"recipient": {"id": sender_id}, "message": {"text": truncate(text, MAX_LENGTH)}},
        "Facebook send",
    )
    if ok:
        logger.info(f"✅ Facebook message sent to {sender_id}")
    return ok


async def get_user_profile(sender_id: str) -> Optional[Dict]:
    """Public profile of the sender (name, profile pic). Fails soft."""
    if not is_configured():
        return None
    url = f"{GRAPH_API_URL}/{sender_id}"
    try:
        response = await _client().get(
            url,
            params={
                "fields": "first_name,last_name,profile_pic",
                "access_token": FACEBOOK_PAGE_ACCESS_TOKEN,
            },
        )
        if response.status_code != 200:
            logger.warning(f"Facebook profile fetch failed: {response.status_code} - {response.text}")
            return None
        data = response.json()
        name = " ".join(x for x in [data.get("first_name"), data.get("last_name")] if x)
        return {"name": name, "first_name": data.get("first_name"), "last_name": data.get("last_name")}
    except Exception as e:
        logger.warning(f"Error fetching Facebook profile: {e}")
        return None


send_facebook_message = send_message
