"""
instagram_connection.py — Instagram DMs (Messaging API on the IG Business account).
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

INSTAGRAM_PAGE_ACCESS_TOKEN = os.getenv("INSTAGRAM_PAGE_ACCESS_TOKEN")
INSTAGRAM_ACCOUNT_ID = os.getenv("INSTAGRAM_ACCOUNT_ID")
INSTAGRAM_VERIFY_TOKEN = os.getenv("INSTAGRAM_VERIFY_TOKEN")

MAX_LENGTH = 1000

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
    return bool(INSTAGRAM_PAGE_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID)


# ==================== WEBHOOK ====================

def verify_webhook(mode: Optional[str], token: Optional[str], challenge: Optional[str]) -> Tuple[bool, str]:
    return _verify_webhook(mode, token, INSTAGRAM_VERIFY_TOKEN, challenge, "Instagram")


def parse_webhook_payload(body: dict) -> Optional[Dict]:
    """Text DMs and story mentions. Echoes, reads and media-only messages return None."""
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
            if message_data.get("is_echo"):
                logger.debug("↩️ Skipping Instagram echo message")
                return None

            message_text = message_data.get("text")
            if "attachments" in message_data and not message_text:
                attachment_type = (message_data.get("attachments") or [{}])[0].get("type")
                logger.info(f"📎 Ignoring Instagram {attachment_type} from {sender_id}")
                return None
            if not message_text:
                return None

            return {
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message": message_text,
                "message_id": message_data.get("mid"),
                "timestamp": timestamp,
                "message_type": "text",
            }

        if "story_mention" in event:
            logger.info(f"🏷️ Instagram story mention from {sender_id}")
            return {
                "sender_id": sender_id,
                "recipient_id": recipient_id,
                "message": "[User mentioned you in their story]",
                "message_id": None,
                "timestamp": timestamp,
                "message_type": "story_mention",
            }

        return None
    except Exception as e:
        logger.error(f"Error parsing Instagram webhook payload: {e}")
        return None


# ==================== SESSIONS ====================

def get_session_id(sender_id: str) -> str:
    return _get_session_id("instagram", sender_id)


def get_yesterday_session_id(sender_id: str) -> str:
    return _get_yesterday_session_id("instagram", sender_id)


def get_previous_summary(sender_id: str, session_store=None) -> Optional[str]:
    return carry_over_context("instagram", sender_id, session_store)


# ==================== API CALLS ====================

async def _post(payload: dict, what: str) -> bool:
    if not is_configured():
        logger.error("Instagram configuration incomplete (token or account id missing)")
        return False
    url = f"{GRAPH_API_URL}/{INSTAGRAM_ACCOUNT_ID}/messages"
    try:
        response = await _client().post(
            url, params={"access_token": INSTAGRAM_PAGE_ACCESS_TOKEN}, json=payload
        )
        if response.status_code == 200:
            return True
        logger.error(f"{what} failed: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        logger.error(f"{what} error: {e}")
        return False


async def mark_as_seen(sender_id: str) -> bool:
    return await _post({"recipient": {"id": sender_id}, "sender_action": "mark_seen"}, "Instagram mark-seen")


async def send_typing_indicator(sender_id: str, typing_on: bool = True) -> bool:
    action = "typing_on" if typing_on else "typing_off"
    return await _post({"recipient": {"id": sender_id}, "sender_action": action}, "Instagram typing")


async def send_message(sender_id: str, text: str) -> bool:
    ok = await _post(
        {
            "recipient": {"id": sender_id},
            "messaging_type": "RESPONSE",
            "message": {"text": truncate(text, MAX_LENGTH)},
        },
        "Instagram send",
    )
    if ok:
        logger.info(f"✅ Instagram message sent to {sender_id}")
    return ok


async def get_user_profile(sender_id: str) -> Optional[Dict]:
    """Instagram profile of the sender. Fails soft — name is a nicety, not a gate."""
    if not is_configured():
        return None
    url = f"{GRAPH_API_URL}/{sender_id}"
    try:
        response = await _client().get(
            url,
            params={"fields": "name,username", "access_token": INSTAGRAM_PAGE_ACCESS_TOKEN},
        )
        if response.status_code != 200:
            logger.warning(f"Instagram profile fetch failed: {response.status_code} - {response.text}")
            return None
        data = response.json()
        return {"name": data.get("name", ""), "username": data.get("username", "")}
    except Exception as e:
        logger.warning(f"Error fetching Instagram profile: {e}")
        return None


send_instagram_message = send_message
