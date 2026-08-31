"""
whatsapp_connection.py — WhatsApp Business Cloud API.

Webhook verification, payload parsing, read receipts, typing indicator, sending.
Single tenant: credentials come from the environment.
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

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")

MAX_LENGTH = 4096

_http_client: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    """Shared client — reuses the TCP+TLS connection across messages."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {WHATSAPP_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
    return _http_client


async def close():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def is_configured() -> bool:
    return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)


# ==================== WEBHOOK ====================

def verify_webhook(mode: Optional[str], token: Optional[str], challenge: Optional[str]) -> Tuple[bool, str]:
    return _verify_webhook(mode, token, WHATSAPP_VERIFY_TOKEN, challenge, "WhatsApp")


def parse_webhook_payload(body: dict) -> Optional[Dict]:
    """Extract one text message from a webhook body, or None if there is nothing to answer."""
    try:
        entry = body.get("entry", [{}])[0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        messages = value.get("messages", [])
        contacts = value.get("contacts", [])

        if not messages:
            return None

        msg = messages[0]
        message_type = msg.get("type")

        # A tapped button is not a text message. Dropping these means the customer
        # taps "Renew Now" and gets silence — worse than never offering the button.
        # The reply id is what we routed on when we sent it; the title is what they
        # saw, and is what the agent should read.
        reply_id = None
        if message_type == "text":
            text = msg.get("text", {}).get("body", "")
        elif message_type == "interactive":
            interactive = msg.get("interactive", {})
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            text = reply.get("title", "")
            reply_id = reply.get("id")
        elif message_type == "button":
            # Quick-reply buttons attached to a template come back in their own shape.
            text = msg.get("button", {}).get("text", "")
            reply_id = msg.get("button", {}).get("payload")
        else:
            logger.info(f"📎 Ignoring unsupported WhatsApp message type: {message_type}")
            return None

        if not text and not reply_id:
            return None

        profile_name = contacts[0].get("profile", {}).get("name") if contacts else None

        return {
            "phone": msg.get("from"),
            "message": text,
            "reply_id": reply_id,
            "message_id": msg.get("id"),
            "message_type": message_type,
            "profile_name": profile_name,
            "timestamp": msg.get("timestamp"),
            "phone_number_id": value.get("metadata", {}).get("phone_number_id"),
        }
    except Exception as e:
        logger.error(f"Error parsing WhatsApp webhook payload: {e}")
        return None


# ==================== SESSIONS ====================

def get_session_id(phone: str) -> str:
    return _get_session_id("whatsapp", phone)


def get_yesterday_session_id(phone: str) -> str:
    return _get_yesterday_session_id("whatsapp", phone)


def get_previous_summary(phone: str, session_store=None) -> Optional[str]:
    return carry_over_context("whatsapp", phone, session_store)


# ==================== API CALLS ====================

async def _post(payload: dict, what: str, phone_number_id: Optional[str] = None) -> bool:
    """Send via a specific number, defaulting to the configured one.

    One app can serve several numbers — a test number and the real business number
    at the same time. Replying always from the configured default means a customer
    who wrote to the business number gets answered by a stranger, so the caller
    passes back whichever number actually received the message.
    """
    if not is_configured():
        logger.error("WhatsApp credentials not configured")
        return False
    url = f"{GRAPH_API_URL}/{phone_number_id or WHATSAPP_PHONE_NUMBER_ID}/messages"
    try:
        response = await _client().post(url, json=payload)
        if response.status_code == 200:
            return True
        logger.error(f"{what} failed: {response.status_code} - {response.text}")
        return False
    except Exception as e:
        logger.error(f"{what} error: {e}")
        return False


async def mark_as_read(message_id: str, phone_number_id: Optional[str] = None) -> bool:
    """Blue ticks."""
    return await _post(
        {"messaging_product": "whatsapp", "status": "read", "message_id": message_id},
        "WhatsApp mark-as-read",
        phone_number_id,
    )


async def send_typing_indicator(phone: str, message_id: str, typing_on: bool = True,
                                phone_number_id: Optional[str] = None) -> bool:
    """Typing dots. Auto-expires after 25s or when the reply is sent, so there is
    nothing to turn off — typing_on=False is a no-op."""
    if not typing_on:
        return True
    ok = await _post(
        {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        },
        "WhatsApp typing indicator",
        phone_number_id,
    )
    if ok:
        logger.info(f"⌨️ Typing ON for {phone}")
    return ok


async def send_template(
    phone: str,
    template_name: str,
    body_params: Optional[list] = None,
    language: str = "en",
    phone_number_id: Optional[str] = None,
) -> bool:
    """Send a pre-approved template.

    Free-form text only reaches a customer inside the 24-hour service window; a
    reminder is by definition outside it. Templates are the only way through, and
    the body is fixed at approval time — we may only fill the {{n}} slots.
    """
    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            **({"components": components} if components else {}),
        },
    }
    ok = await _post(payload, f"WhatsApp template '{template_name}'", phone_number_id)
    if ok:
        logger.info(f"\u2705 Template '{template_name}' sent to {phone}")
    return ok


async def send_message(phone: str, text: str, phone_number_id: Optional[str] = None) -> bool:
    ok = await _post(
        {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"body": truncate(text, MAX_LENGTH)},
        },
        "WhatsApp send",
        phone_number_id,
    )
    if ok:
        logger.info(f"✅ WhatsApp message sent to {phone}")
    return ok


# ==================== MEDIA ====================

async def upload_media(path: str, mime_type: str = "application/pdf",
                       phone_number_id: Optional[str] = None) -> Optional[str]:
    """Upload a local file and return its media id.

    Media ids last 30 days and are scoped to the number that uploaded them, so the
    caller caches the id rather than re-uploading a 1 MB brochure per customer.
    """
    if not is_configured():
        logger.error("WhatsApp credentials not configured")
        return None
    if not os.path.exists(path):
        logger.error(f"Cannot upload — file not found: {path}")
        return None

    url = f"{GRAPH_API_URL}/{phone_number_id or WHATSAPP_PHONE_NUMBER_ID}/media"
    try:
        with open(path, "rb") as handle:
            files = {
                "file": (os.path.basename(path), handle, mime_type),
                "messaging_product": (None, "whatsapp"),
                "type": (None, mime_type),
            }
            # The shared client sets Content-Type: application/json, which breaks a
            # multipart upload — this one call needs its own client.
            async with httpx.AsyncClient(timeout=120.0) as upload_client:
                response = await upload_client.post(
                    url, files=files,
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                )
        if response.status_code == 200:
            media_id = response.json().get("id")
            logger.info(f"\U0001f4c4 Uploaded {os.path.basename(path)} -> media id {media_id}")
            return media_id
        logger.error(f"Media upload failed: {response.status_code} - {response.text}")
    except Exception as e:
        logger.error(f"Media upload error: {e}")
    return None


async def send_document(phone: str, media_id: str, filename: str, caption: str = "",
                        phone_number_id: Optional[str] = None) -> bool:
    """Send an already-uploaded document by media id."""
    document = {"id": media_id, "filename": filename[:240]}
    if caption:
        document["caption"] = truncate(caption, 1024)
    ok = await _post(
        {"messaging_product": "whatsapp", "to": phone, "type": "document", "document": document},
        "WhatsApp document",
        phone_number_id,
    )
    if ok:
        logger.info(f"\u2705 Document '{filename}' sent to {phone}")
    return ok


# Backwards-compatible alias matching the reference project's naming
send_whatsapp_message = send_message
