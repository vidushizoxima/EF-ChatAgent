"""
store.py — SQLite-backed session store.

Drop-in replacement for the Redis layer in the GoEd reference project. Same
responsibilities, one file, no server to run:

  - conversation transcript + active buffer (rolling summary pattern)
  - per-session key/value with TTL (user_info, lead_id, flags, dedupe keys)
  - set semantics for "already checked" bookkeeping (checked_phones/emails)

Rolling summary pattern (unchanged from the reference):
    messages 1-5   → agent sees raw buffer
    after msg 5    → LLM summarises buffer, buffer is cleared
    messages 6-10  → agent sees SUMMARY + buffer
    ...

One process owns the file. WAL mode + a re-entrant lock make it safe across the
FastAPI thread pool; it is NOT safe across replicas — run a single instance, or
swap this class for Redis if you ever scale out horizontally.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

DB_PATH = os.getenv("EF_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "ef_chat.db"))
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_DAYS", "2")) * 86400
SUMMARY_BATCH_SIZE = int(os.getenv("SUMMARY_BATCH_SIZE", "5"))
# Hard ceiling on how long a turn may wait for an in-flight summary.
SUMMARY_TIMEOUT_SECONDS = int(os.getenv("SUMMARY_TIMEOUT_SECONDS", "15"))

GLOBAL_NS = "__global__"  # namespace for cross-session keys (webhook dedupe)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    session_id  TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT,
    expires_at  REAL,
    PRIMARY KEY (session_id, key)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    tokens      TEXT,
    in_buffer   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    channel       TEXT,
    created_at    TEXT,
    last_activity TEXT,
    msg_count     INTEGER NOT NULL DEFAULT 0,
    user_count    INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,
    summary_in_progress INTEGER NOT NULL DEFAULT 0
);
"""


class SqlitePool:
    """Single shared connection, WAL mode, guarded by a re-entrant lock."""

    _conn: Optional[sqlite3.Connection] = None
    _lock = threading.RLock()

    @classmethod
    def get(cls) -> sqlite3.Connection:
        if cls._conn is None:
            with cls._lock:
                if cls._conn is None:
                    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
                    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30.0)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA busy_timeout=30000")
                    conn.executescript(_SCHEMA)
                    conn.commit()
                    cls._conn = conn
                    logger.info(f"🗄️  SQLite session store ready at {DB_PATH}")
        return cls._conn

    @classmethod
    def execute(cls, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        conn = cls.get()
        with cls._lock:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    @classmethod
    def query(cls, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        conn = cls.get()
        with cls._lock:
            return conn.execute(sql, params).fetchall()

    @classmethod
    def close(cls):
        with cls._lock:
            if cls._conn is not None:
                cls._conn.close()
                cls._conn = None

    @classmethod
    def health(cls) -> bool:
        try:
            cls.query("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"SQLite health check failed: {e}")
            return False


class SessionStore:
    """Per-session view over the SQLite store.

    Mirrors the reference project's RedisMessageManager surface so the agent and
    webhook code read the same way.
    """

    def __init__(self, session_id: str, channel: str = "unknown"):
        self.session_id = session_id
        self.channel = channel
        self.ttl = SESSION_TTL_SECONDS
        self.summary_batch_size = SUMMARY_BATCH_SIZE
        self.summary_timeout = SUMMARY_TIMEOUT_SECONDS

    # ==================== KEY / VALUE ====================

    def set(self, key: str, value: Any, ttl: Optional[int] = None, namespace: Optional[str] = None):
        """Set a key. ttl=None means it lives as long as the session TTL."""
        expires = time.time() + (ttl if ttl is not None else self.ttl)
        payload = value if isinstance(value, str) else json.dumps(value)
        SqlitePool.execute(
            "INSERT INTO kv (session_id, key, value, expires_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
            (namespace or self.session_id, key, payload, expires),
        )

    def get(self, key: str, namespace: Optional[str] = None) -> Optional[str]:
        rows = SqlitePool.query(
            "SELECT value, expires_at FROM kv WHERE session_id = ? AND key = ?",
            (namespace or self.session_id, key),
        )
        if not rows:
            return None
        if rows[0]["expires_at"] and rows[0]["expires_at"] < time.time():
            self.delete(key, namespace=namespace)
            return None
        return rows[0]["value"]

    def get_json(self, key: str, namespace: Optional[str] = None) -> Optional[Any]:
        raw = self.get(key, namespace=namespace)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    def exists(self, key: str, namespace: Optional[str] = None) -> bool:
        return self.get(key, namespace=namespace) is not None

    def delete(self, key: str, namespace: Optional[str] = None):
        SqlitePool.execute(
            "DELETE FROM kv WHERE session_id = ? AND key = ?",
            (namespace or self.session_id, key),
        )

    def sadd(self, key: str, member: str):
        """Append to a set stored as a JSON array."""
        current = self.get_json(key) or []
        if not isinstance(current, list):
            current = []
        if member not in current:
            current.append(member)
            self.set(key, current)

    def smembers(self, key: str) -> List[str]:
        current = self.get_json(key) or []
        return current if isinstance(current, list) else []

    # ==================== DEDUPE (cross-session) ====================

    @staticmethod
    def seen_message(message_id: str, ttl: int = 3600) -> bool:
        """True if this platform message id was already processed. Marks it seen."""
        if not message_id:
            return False
        store = SessionStore(GLOBAL_NS)
        if store.exists(f"mid:{message_id}", namespace=GLOBAL_NS):
            return True
        store.set(f"mid:{message_id}", "1", ttl=ttl, namespace=GLOBAL_NS)
        return False

    # ==================== USER / LEAD CONTEXT ====================

    def get_user_info(self) -> Optional[Dict[str, str]]:
        """User profile captured by the webhook (name, phone, sender_id, source…)."""
        info = self.get_json("user_info")
        return info if isinstance(info, dict) and info else None

    def update_user_info(self, updates: Dict[str, Any]):
        """Merge into the stored profile; empty values never overwrite good ones."""
        info = self.get_json("user_info") or {}
        if not isinstance(info, dict):
            info = {}
        for k, v in updates.items():
            if v is not None and str(v).strip() != "":
                info[k] = str(v)
        self.set("user_info", info)

    def set_lead_id(self, lead_id: str):
        self.set("lead_id", lead_id)

    def get_lead_id(self) -> Optional[str]:
        return self.get("lead_id")

    def set_existing_lead_data(self, lead_data: dict):
        self.set("existing_lead_data", lead_data)

    def get_existing_lead_data(self) -> Optional[dict]:
        data = self.get_json("existing_lead_data")
        return data if isinstance(data, dict) else None

    # ==================== MESSAGES ====================

    async def add_message(
        self,
        role: str,
        content: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
    ) -> Dict:
        """Append a message. Every Nth user message kicks off a background summary."""
        if role.lower() == "user":
            await self._wait_for_summary_if_needed()

        now = datetime.now(IST).isoformat()
        tokens = {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
            "cache_read": cache_read_tokens,
            "cache_creation": cache_creation_tokens,
        }

        SqlitePool.execute(
            "INSERT INTO messages (session_id, role, content, tokens, in_buffer, created_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (self.session_id, role, str(content), json.dumps(tokens), now),
        )

        is_user = 1 if role.lower() == "user" else 0
        SqlitePool.execute(
            "INSERT INTO sessions (session_id, channel, created_at, last_activity, msg_count, user_count) "
            "VALUES (?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "  last_activity = excluded.last_activity, "
            "  msg_count = sessions.msg_count + 1, "
            "  user_count = sessions.user_count + ?, "
            "  channel = COALESCE(sessions.channel, excluded.channel)",
            (self.session_id, self.channel, now, now, is_user, is_user),
        )

        if is_user:
            rows = SqlitePool.query(
                "SELECT user_count FROM sessions WHERE session_id = ?", (self.session_id,)
            )
            user_count = rows[0]["user_count"] if rows else 0
            if user_count > 0 and user_count % self.summary_batch_size == 0:
                asyncio.create_task(self._async_rolling_summarize())

        return {"timestamp": now, "role": role, "content": str(content), "tokens": tokens}

    def get_context_for_chat(self, exclude_last: bool = False) -> str:
        """Summary + unsummarised buffer — what the agent actually sees as history."""
        try:
            rows = SqlitePool.query(
                "SELECT summary FROM sessions WHERE session_id = ?", (self.session_id,)
            )
            summary = rows[0]["summary"] if rows else None

            buffer_rows = SqlitePool.query(
                "SELECT role, content FROM messages WHERE session_id = ? AND in_buffer = 1 ORDER BY id",
                (self.session_id,),
            )
            buffer = [dict(r) for r in buffer_rows]
            if exclude_last and buffer:
                buffer = buffer[:-1]

            parts = []
            if summary:
                parts.append(f"[CONTEXT SUMMARY]\n{summary}")
            if buffer:
                recent = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in buffer)
                parts.append(f"[RECENT ACTUAL MESSAGES]\n{recent}")
            return "\n\n".join(parts)
        except Exception as e:
            logger.error(f"Error building chat context: {e}")
            return ""

    def get_full_transcript(self) -> List[Dict]:
        rows = SqlitePool.query(
            "SELECT role, content, tokens, created_at FROM messages WHERE session_id = ? ORDER BY id",
            (self.session_id,),
        )
        out = []
        for r in rows:
            try:
                tokens = json.loads(r["tokens"]) if r["tokens"] else {}
            except (json.JSONDecodeError, TypeError):
                tokens = {}
            out.append(
                {
                    "timestamp": r["created_at"],
                    "role": r["role"],
                    "content": r["content"],
                    "tokens": tokens,
                }
            )
        return out

    def get_transcript_formatted(self) -> str:
        lines = []
        for m in self.get_full_transcript():
            lines.append(f"[{m['timestamp']}] {m['role'].upper()}: {m['content']}")
        return "\n".join(lines)

    def get_session_stats(self) -> Dict:
        rows = SqlitePool.query("SELECT * FROM sessions WHERE session_id = ?", (self.session_id,))
        if not rows:
            return {"session_id": self.session_id, "exists": False}
        s = dict(rows[0])
        totals = SqlitePool.query(
            "SELECT tokens FROM messages WHERE session_id = ?", (self.session_id,)
        )
        tin = tout = 0
        for r in totals:
            try:
                t = json.loads(r["tokens"]) if r["tokens"] else {}
                tin += int(t.get("input", 0) or 0)
                tout += int(t.get("output", 0) or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return {
            "session_id": self.session_id,
            "exists": True,
            "channel": s.get("channel"),
            "created_at": s.get("created_at"),
            "last_activity": s.get("last_activity"),
            "message_count": s.get("msg_count"),
            "user_message_count": s.get("user_count"),
            "has_summary": bool(s.get("summary")),
            "input_tokens": tin,
            "output_tokens": tout,
        }

    def clear_session(self):
        SqlitePool.execute("DELETE FROM messages WHERE session_id = ?", (self.session_id,))
        SqlitePool.execute("DELETE FROM kv WHERE session_id = ?", (self.session_id,))
        SqlitePool.execute("DELETE FROM sessions WHERE session_id = ?", (self.session_id,))
        logger.info(f"🧹 Cleared session {self.session_id}")

    # ==================== ROLLING SUMMARY ====================

    async def _wait_for_summary_if_needed(self):
        """Block a new user message until an in-flight summary finishes."""
        waited = 0.0
        interval = 0.1
        while waited < self.summary_timeout:
            rows = SqlitePool.query(
                "SELECT summary_in_progress FROM sessions WHERE session_id = ?", (self.session_id,)
            )
            if not rows or not rows[0]["summary_in_progress"]:
                return
            try:
                await asyncio.shield(asyncio.sleep(interval))
            except asyncio.CancelledError:
                return
            waited += interval
        SqlitePool.execute(
            "UPDATE sessions SET summary_in_progress = 0 WHERE session_id = ?", (self.session_id,)
        )
        logger.warning(f"Summary exceeded {self.summary_timeout}s — cleared stale flag and continued")

    async def _async_rolling_summarize(self):
        """Summarise (previous summary + buffer) → new summary, then clear the buffer."""
        try:
            SqlitePool.execute(
                "UPDATE sessions SET summary_in_progress = 1 WHERE session_id = ?", (self.session_id,)
            )

            rows = SqlitePool.query(
                "SELECT summary FROM sessions WHERE session_id = ?", (self.session_id,)
            )
            previous = rows[0]["summary"] if rows else ""

            buffer_rows = SqlitePool.query(
                "SELECT id, role, content FROM messages WHERE session_id = ? AND in_buffer = 1 ORDER BY id",
                (self.session_id,),
            )
            if not buffer_rows:
                return

            conversation = "\n".join(f"{r['role'].upper()}: {r['content']}" for r in buffer_rows)
            last_id = buffer_rows[-1]["id"]

            from client.config import create_summary_llm

            prompt = (
                "You maintain a running summary of a customer conversation for Eureka Forbes.\n"
                "Merge the PREVIOUS SUMMARY with the NEW MESSAGES into one updated summary.\n\n"
                "Keep, verbatim where possible:\n"
                "- customer identity (name, phone, city, address)\n"
                "- products / services owned or discussed, model names, AMC status\n"
                "- the problem or request in the customer's own words\n"
                "- anything already promised, booked, or scheduled (with dates)\n"
                "- what has already been asked, so it is never asked twice\n\n"
                "Drop pleasantries and filler. Under 250 words. Plain text, no headings.\n\n"
                f"PREVIOUS SUMMARY:\n{previous or '(none)'}\n\n"
                f"NEW MESSAGES:\n{conversation}\n\n"
                "UPDATED SUMMARY:"
            )

            llm = create_summary_llm()
            response = await asyncio.wait_for(llm.ainvoke(prompt), timeout=self.summary_timeout)
            new_summary = response.content if hasattr(response, "content") else str(response)
            if isinstance(new_summary, list):
                new_summary = "".join(
                    b.get("text", "") for b in new_summary if isinstance(b, dict)
                )

            SqlitePool.execute(
                "UPDATE sessions SET summary = ? WHERE session_id = ?",
                (new_summary.strip(), self.session_id),
            )
            # Everything up to last_id is now represented by the summary
            SqlitePool.execute(
                "UPDATE messages SET in_buffer = 0 WHERE session_id = ? AND id <= ?",
                (self.session_id, last_id),
            )
            logger.info(f"📝 Rolling summary updated for {self.session_id} ({len(new_summary)} chars)")

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"Summary error for {self.session_id}: {e}")
        finally:
            SqlitePool.execute(
                "UPDATE sessions SET summary_in_progress = 0 WHERE session_id = ?", (self.session_id,)
            )

    # ==================== MAINTENANCE ====================

    # kv rows keyed by these namespaces are NOT conversation state, so a demo reset
    # leaves them alone: amc:* is the record of which reminders have already gone out
    # to real customers — drop it and the next run messages them a second time — and
    # wa:media caches the uploaded brochure id, which costs a 1 MB re-upload to lose.
    KEEP_NAMESPACES = ("amc:sent", "amc:optout", "amc:lastsent", "wa:media")

    @staticmethod
    def reset_all(everything: bool = False) -> Dict[str, int]:
        """Delete every conversation — sessions, messages and their kv rows.

        For demos: afterwards the next message on any channel starts from nothing.
        Unlike `purge_expired` this ignores the TTL, so a chat from a minute ago goes
        too. `everything=True` additionally drops KEEP_NAMESPACES; only use it when
        you also want the AMC send-history forgotten.
        """
        msgs = SqlitePool.execute("DELETE FROM messages")
        sess = SqlitePool.execute("DELETE FROM sessions")
        if everything:
            kv = SqlitePool.execute("DELETE FROM kv")
        else:
            holes = ",".join("?" * len(SessionStore.KEEP_NAMESPACES))
            kv = SqlitePool.execute(
                f"DELETE FROM kv WHERE session_id NOT IN ({holes})",
                SessionStore.KEEP_NAMESPACES,
            )
        result = {
            "sessions_deleted": max(sess.rowcount, 0),
            "messages_deleted": max(msgs.rowcount, 0),
            "kv_deleted": max(kv.rowcount, 0),
            "amc_state_kept": not everything,
        }
        logger.warning(f"🧨 Full reset: {result}")
        return result

    @staticmethod
    def purge_expired() -> Dict[str, int]:
        """Delete expired kv rows and sessions idle beyond the TTL."""
        now = time.time()
        kv_cur = SqlitePool.execute("DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at < ?", (now,))
        cutoff = (datetime.now(IST) - timedelta(seconds=SESSION_TTL_SECONDS)).isoformat()
        stale = SqlitePool.query(
            "SELECT session_id FROM sessions WHERE last_activity < ?", (cutoff,)
        )
        ids = [r["session_id"] for r in stale]
        for sid in ids:
            SqlitePool.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            SqlitePool.execute("DELETE FROM kv WHERE session_id = ?", (sid,))
            SqlitePool.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
        if ids or kv_cur.rowcount > 0:
            logger.info(f"🧹 Purge: {kv_cur.rowcount} kv rows, {len(ids)} sessions")
        return {"kv_deleted": max(kv_cur.rowcount, 0), "sessions_deleted": len(ids)}
