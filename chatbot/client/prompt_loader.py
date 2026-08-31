"""
prompt_loader.py — prompts live in files, not in a database.

The reference project pulled prompts from Postgres through MCP and needed a cache
invalidation endpoint. Here a prompt is prompts/<prompt_id>.md; the loader stamps
the file's mtime, so editing a prompt and sending the next message picks it up —
no restart, no invalidation call.
"""

import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

PROMPTS_DIR = os.getenv(
    "PROMPTS_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "prompts"),
)

# prompt_id -> (mtime, text)
_cache: Dict[str, Tuple[float, str]] = {}


def prompt_path(prompt_id: str) -> str:
    return os.path.join(PROMPTS_DIR, f"{prompt_id}.md")


def load_prompt(prompt_id: str) -> str:
    """Return prompt text, re-reading the file when it changes on disk."""
    path = prompt_path(prompt_id)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        logger.error(f"❌ Prompt file not found: {path}")
        return ""

    cached = _cache.get(prompt_id)
    if cached and cached[0] == mtime:
        return cached[1]

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    if not text.strip():
        logger.error(f"❌ Prompt '{prompt_id}' is empty ({path})")

    _cache[prompt_id] = (mtime, text)
    logger.info(f"📄 Prompt loaded: {prompt_id} ({len(text)} chars) from {path}")
    return text


def prompt_changed(prompt_id: str) -> bool:
    """True when the file on disk is newer than what was cached."""
    cached = _cache.get(prompt_id)
    if not cached:
        return True
    try:
        return os.path.getmtime(prompt_path(prompt_id)) != cached[0]
    except OSError:
        return False


def prompt_fingerprint(prompt_id: str) -> Optional[float]:
    cached = _cache.get(prompt_id)
    return cached[0] if cached else None
