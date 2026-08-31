"""
base_connection.py — helpers shared by the WhatsApp, Facebook and Instagram modules.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v23.0"
GRAPH_API_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


# ==================== SESSION IDS ====================

def get_session_id(prefix: str, identifier: str) -> str:
    """Date-based session id: <channel>:<identifier>:<YYYY-MM-DD>.

    A new day starts a fresh session; yesterday's summary is carried over
    explicitly (see carry_over_context) rather than growing forever.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{prefix}:{identifier}:{today}"


def get_yesterday_session_id(prefix: str, identifier: str) -> str:
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    return f"{prefix}:{identifier}:{yesterday}"


# ==================== CROSS-DAY CONTEXT ====================

def carry_over_context(prefix: str, identifier: str, session_store) -> Optional[str]:
    """Yesterday's rolling summary, so a returning customer is not asked twice."""
    from client.store import SessionStore, SqlitePool

    try:
        prev_id = get_yesterday_session_id(prefix, identifier)
        rows = SqlitePool.query("SELECT summary FROM sessions WHERE session_id = ?", (prev_id,))
        if rows and rows[0]["summary"]:
            logger.info(f"📜 Carried over yesterday's summary for {prefix}:{identifier}")
            return rows[0]["summary"]
    except Exception as e:
        logger.warning(f"Could not fetch yesterday's summary: {e}")
    return None


# ==================== WEBHOOK VERIFICATION ====================

def verify_webhook(
    mode: Optional[str],
    token: Optional[str],
    expected_token: Optional[str],
    challenge: Optional[str],
    channel_name: str = "Unknown",
) -> Tuple[bool, str]:
    """Meta webhook subscription handshake."""
    if not expected_token:
        logger.error(f"❌ {channel_name}: no verify token configured in the environment")
        return False, "Verification failed"

    if mode == "subscribe" and token == expected_token:
        logger.info(f"✅ {channel_name} webhook verified")
        return True, challenge or ""

    logger.warning(
        f"❌ {channel_name} webhook verification failed. mode={mode}, token match={token == expected_token}"
    )
    return False, "Verification failed"


def truncate(text: str, limit: int) -> str:
    """Platforms silently drop over-long messages — cut with a visible marker."""
    if len(text) <= limit:
        return text
    logger.warning(f"Message truncated to {limit} characters")
    return text[: limit - 50] + "\n\n... (message truncated)"
