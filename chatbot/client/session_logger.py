"""
session_logger.py — verbose per-session tracing, development mode only.

In production every method is a no-op, so calls can stay in the hot path.
Files land in logs/sessions/<session>.log.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from client.config import is_dev_mode

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
LOG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "sessions"
)


class SessionLogger:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.enabled = is_dev_mode()
        self._path: Optional[str] = None
        if self.enabled:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
                safe = session_id.replace(":", "_").replace("/", "_")
                self._path = os.path.join(LOG_DIR, f"{safe}.log")
            except Exception as e:
                logger.warning(f"SessionLogger disabled: {e}")
                self.enabled = False

    def _write(self, header: str, body: Any):
        if not self.enabled or not self._path:
            return
        try:
            if not isinstance(body, str):
                body = json.dumps(body, indent=2, ensure_ascii=False, default=str)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 70}\n[{datetime.now(IST).isoformat()}] {header}\n{'=' * 70}\n{body}\n")
        except Exception as e:
            logger.debug(f"SessionLogger write failed: {e}")

    def log_user_info(self, user_info): self._write("USER INFO", user_info or {})
    def log_system_message(self, text): self._write("SYSTEM CONTEXT", text)

    def start_interaction(self, user_query: str, system_prompt: str, context: str):
        self._write("USER QUERY", user_query)
        self._write("BASE PROMPT", system_prompt)
        self._write("CONVERSATION CONTEXT", context)

    def log_tool_call(self, tool_name: str, arguments: Any, result: Any):
        self._write(f"TOOL: {tool_name}", {"arguments": arguments, "result": result})

    def end_interaction(self, response: str, input_tokens: int = 0, output_tokens: int = 0):
        self._write("RESPONSE", f"{response}\n\n-- tokens in={input_tokens} out={output_tokens}")
