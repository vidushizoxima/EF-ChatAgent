"""
main.py — Eureka Forbes social chat agent (FastAPI).

Endpoints:
    GET/POST  /whatsapp     WhatsApp Business Cloud API webhook
    GET/POST  /facebook     Facebook Messenger webhook
    GET/POST  /instagram    Instagram DM webhook
              ... each also served at /webhook/<channel>
    GET       /health       liveness + dependency status
    GET       /session/{id}/transcript | /stats      (admin key)
    DELETE    /session/{id}                          (admin key)
    GET       /admin/diagnostics                     (admin key)

Single tenant: all credentials come from the environment. Session state lives in
SQLite (chatbot/client/store.py) — no Redis, no Postgres.
"""

import asyncio
import json
import logging
import os
import queue
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener
from typing import Optional

# Make sibling modules importable whether run as `python chatbot/main.py` or from chatbot/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

load_dotenv()

# ==================== LOGGING (non-blocking) ====================

log_queue: queue.Queue = queue.Queue(-1)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
listener = QueueListener(log_queue, stream_handler)
listener.start()

root_logger = logging.getLogger()
root_logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
for h in list(root_logger.handlers):
    root_logger.removeHandler(h)
root_logger.addHandler(QueueHandler(log_queue))
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

from agent import get_or_create_agent, process_query          # noqa: E402
from client import amc_reminders                                # noqa: E402
from client import facebook_connection as fb                   # noqa: E402
from client import instagram_connection as ig                  # noqa: E402
from client import whatsapp_connection as wa                   # noqa: E402
from client.channel_config import get_available_channels, get_channel_config  # noqa: E402
from client.config import ADMIN_API_KEY, APP_ENV, BRAND_NAME, LLM_PROVIDER, PORT  # noqa: E402
from client.dataverse_client import dataverse                  # noqa: E402
from client.interaction_logger import IDLE_SECONDS, flush_due, logger_loop  # noqa: E402
from client.store import SessionStore, SqlitePool              # noqa: E402
from tools import available_tools                              # noqa: E402

# ==================== AUTH ====================

api_key_header = APIKeyHeader(name="X-Admin-API-Key", auto_error=False)


async def verify_admin_api_key(api_key: str = Security(api_key_header)):
    if not ADMIN_API_KEY:
        logger.error("❌ ADMIN_API_KEY not configured — admin endpoints are disabled")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Admin authentication not configured")
    if api_key != ADMIN_API_KEY:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Admin API Key")
    return api_key


# ==================== LIFESPAN ====================

async def _purge_loop():
    """Housekeeping: drop expired keys and idle sessions once an hour."""
    while True:
        try:
            await asyncio.sleep(3600)
            SessionStore.purge_expired()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"Purge loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 70)
    logger.info(f"🚀 {BRAND_NAME} social chat agent starting | env={APP_ENV} | llm={LLM_PROVIDER}")

    SqlitePool.get()
    SessionStore.purge_expired()

    logger.info(f"📡 Channels: {', '.join(get_available_channels())}")
    logger.info(f"🧰 Registered tools: {', '.join(available_tools()) or '(none yet)'}")
    for channel in get_available_channels():
        cfg = get_channel_config(channel)
        logger.info(f"   {channel:<10} prompt={cfg.prompt_id:<12} tools={cfg.tools or '[]'}")

    logger.info(
        "🔌 Credentials: "
        f"whatsapp={'✅' if wa.is_configured() else '❌'} "
        f"facebook={'✅' if fb.is_configured() else '❌'} "
        f"instagram={'✅' if ig.is_configured() else '❌'} "
        f"dataverse={'✅' if dataverse.is_configured() else '❌'}"
    )

    # Warm the default agent so the first customer message is not the one that
    # discovers a broken prompt file or a bad API key.
    try:
        await get_or_create_agent("whatsapp")
    except Exception as e:
        logger.error(f"❌ Agent warm-up failed: {e}")

    purge_task = asyncio.create_task(_purge_loop())
    interaction_task = asyncio.create_task(logger_loop())
    amc_task = asyncio.create_task(amc_reminders.reminder_loop())
    logger.info("=" * 70)

    yield

    # Cancelling the logger triggers a final flush of anything unlogged
    interaction_task.cancel()
    try:
        await interaction_task
    except asyncio.CancelledError:
        pass
    purge_task.cancel()
    amc_task.cancel()
    await wa.close()
    await fb.close()
    await ig.close()
    await dataverse.close()
    SqlitePool.close()
    listener.stop()
    logger.info("👋 Shutdown complete")


app = FastAPI(title=f"{BRAND_NAME} Social Chat Agent", version="1.0.0", lifespan=lifespan)


# ==================== HEALTH ====================

@app.get("/health")
async def health():
    return {
        "status": "healthy" if SqlitePool.health() else "degraded",
        "brand": BRAND_NAME,
        "env": APP_ENV,
        "llm_provider": LLM_PROVIDER,
        "store": "sqlite",
        "channels": {
            "whatsapp": wa.is_configured(),
            "facebook": fb.is_configured(),
            "instagram": ig.is_configured(),
        },
        "dataverse_configured": dataverse.is_configured(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Each channel answers on BOTH /<channel> and /webhook/<channel>. Meta's callback URL
# is saved by hand and does not always re-verify when the path is edited, so accepting
# both spellings avoids a silent 404-retry loop on live messages.

# ==================== WHATSAPP ====================

_user_locks: dict = {}


def _user_lock(key: str) -> asyncio.Lock:
    """One in-flight turn per user — a customer sending three messages fast gets
    three answers in order, not three interleaved agent runs."""
    if key not in _user_locks:
        _user_locks[key] = asyncio.Lock()
    return _user_locks[key]


@app.get("/whatsapp")
@app.get("/webhook/whatsapp")
async def whatsapp_verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    ok, response = wa.verify_webhook(mode, token, challenge)
    if ok:
        return int(response) if response.isdigit() else response
    raise HTTPException(403, "Verification failed")


async def _run_whatsapp_turn(phone: str, message_text: str, session_id: str,
                             phone_number_id: Optional[str] = None):
    """Agent + reply, serialised per phone number.

    `phone_number_id` is whichever of our numbers received the message, so the reply
    goes out from the same one the customer wrote to."""
    async with _user_lock(f"wa:{phone}"):
        full_response = ""
        try:
            async for chunk in process_query(message_text, session_id, channel="whatsapp"):
                if chunk.get("type") == "token":
                    full_response += chunk.get("content", "")
                elif chunk.get("type") == "error":
                    logger.error(f"Agent error: {chunk.get('error')}")
        except Exception as e:
            logger.error(f"WhatsApp agent failure for {phone}: {e}", exc_info=True)
            full_response = "Sorry, something went wrong at our end. Please try again in a moment."

        if full_response.strip():
            await wa.send_message(phone, full_response, phone_number_id)
        else:
            logger.warning(f"⚠️ Empty response for {phone} — nothing sent")


@app.post("/whatsapp")
@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """Meta retries anything that is not a fast 200, so acknowledge immediately
    and run the agent in the background."""
    try:
        body = await request.json()
        logger.debug(f"🟢 [WHATSAPP] {json.dumps(body)[:800]}")

        parsed = wa.parse_webhook_payload(body)
        if not parsed:
            return {"status": "ok"}

        phone = parsed["phone"]
        message_text = parsed["message"]
        message_id = parsed["message_id"]

        if SessionStore.seen_message(f"wa:{message_id}"):
            logger.info(f"⏭️ Duplicate WhatsApp message {message_id}")
            return {"status": "ok"}

        # An opt-out is a compliance action, not a conversation. Honour it before
        # the agent runs, and answer with the one confirmation line the customer
        # needs rather than letting the model improvise a reply to "STOP".
        if amc_reminders.looks_like_opt_out(message_text):
            amc_reminders.opt_out(phone, reason="replied '%s'" % message_text.strip()[:40])
            await wa.send_message(
                phone,
                "Done — you will not get any more AMC reminders from us. "
                "You can still message here anytime if you need help.",
                parsed.get("phone_number_id"),
            )
            return {"status": "ok"}

        # Anyone who writes back to a reminder is engaged, so a previous opt-out
        # from an earlier campaign should not silence them forever.
        session_id = wa.get_session_id(phone)
        logger.info(f"📩 WhatsApp from {phone} ({parsed.get('profile_name')}): {message_text[:60]}")

        inbox_id = parsed.get("phone_number_id")
        # Tools that send media need to know which of our numbers this conversation
        # is on — a media id uploaded by one number is not valid from another.
        if inbox_id:
            store_inbox = SessionStore(wa.get_session_id(phone), channel="whatsapp")
            store_inbox.set("inbox_phone_number_id", inbox_id)
        await wa.send_typing_indicator(phone, message_id, typing_on=True, phone_number_id=inbox_id)

        store = SessionStore(session_id, channel="whatsapp")
        store.update_user_info(
            {
                "phone": phone,
                "name": parsed.get("profile_name") or "",
                "sender_id": phone,
                "source": "whatsapp",
            }
        )
        if not store.exists("first_message_id"):
            store.set("first_message_id", message_id or "")

        # If we nudged this number recently, the reply belongs to that nudge. Carry
        # it onto the session so the agent can open on the right contract and the
        # interaction lands in the CRM attributed to the reminder that earned it.
        nudge = amc_reminders.last_reminder(phone)
        if nudge and not store.exists("reminder_template"):
            store.set("reminder_template", nudge.get("template", ""))
            store.set("reminder_contract", nudge.get("contract_id", ""))
            logger.info(
                f"🎯 {phone} is replying to reminder {nudge.get('template')} "
                f"({nudge.get('contract_id')})"
            )
        _carry_over(store, wa.get_previous_summary(phone))

        asyncio.create_task(_run_whatsapp_turn(phone, message_text, session_id, inbox_id))
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"WhatsApp webhook error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


# ==================== FACEBOOK ====================

@app.get("/facebook")
@app.get("/webhook/facebook")
async def facebook_verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    ok, response = fb.verify_webhook(mode, token, challenge)
    if ok:
        return int(response) if response.isdigit() else response
    raise HTTPException(403, "Verification failed")


async def _run_facebook_turn(sender_id: str, message_text: str, session_id: str):
    async with _user_lock(f"fb:{sender_id}"):
        full_response = ""
        try:
            async for chunk in process_query(message_text, session_id, channel="facebook"):
                if chunk.get("type") == "token":
                    full_response += chunk.get("content", "")
                elif chunk.get("type") == "error":
                    logger.error(f"Agent error: {chunk.get('error')}")
        except Exception as e:
            logger.error(f"Facebook agent failure for {sender_id}: {e}", exc_info=True)
            full_response = "Sorry, something went wrong at our end. Please try again in a moment."

        await fb.send_typing_indicator(sender_id, False)
        if full_response.strip():
            await fb.send_message(sender_id, full_response)


@app.post("/facebook")
@app.post("/webhook/facebook")
async def facebook_webhook(request: Request):
    try:
        body = await request.json()
        logger.debug(f"🔵 [FACEBOOK] {json.dumps(body)[:800]}")

        parsed = fb.parse_webhook_payload(body)
        if not parsed:
            return {"status": "ok"}

        sender_id = parsed["sender_id"]
        message_text = parsed["message"]
        message_id = parsed.get("message_id")

        if parsed.get("is_echo"):
            return {"status": "ok"}
        if fb.FACEBOOK_PAGE_ID and sender_id == fb.FACEBOOK_PAGE_ID:
            return {"status": "ok"}  # the page talking to itself
        if message_id and SessionStore.seen_message(f"fb:{message_id}", ttl=86400):
            return {"status": "ok"}

        session_id = fb.get_session_id(sender_id)
        logger.info(f"📩 Facebook from {sender_id}: {message_text[:60]}")

        await fb.mark_as_seen(sender_id)
        await fb.send_typing_indicator(sender_id, True)

        store = SessionStore(session_id, channel="facebook")
        if not store.exists("first_message_id"):
            store.set("first_message_id", message_id or "")
        if not store.get_user_info():
            profile = await fb.get_user_profile(sender_id)
            store.update_user_info(
                {
                    "sender_id": sender_id,
                    "name": (profile or {}).get("name", ""),
                    "source": "facebook",
                }
            )
        _carry_over(store, fb.get_previous_summary(sender_id))

        asyncio.create_task(_run_facebook_turn(sender_id, message_text, session_id))
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Facebook webhook error: {e}", exc_info=True)
        return {"status": "ok"}


# ==================== INSTAGRAM ====================

@app.get("/instagram")
@app.get("/webhook/instagram")
async def instagram_verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    ok, response = ig.verify_webhook(mode, token, challenge)
    if ok:
        return int(response) if response.isdigit() else response
    raise HTTPException(403, "Verification failed")


async def _run_instagram_turn(sender_id: str, message_text: str, session_id: str):
    async with _user_lock(f"ig:{sender_id}"):
        full_response = ""
        try:
            async for chunk in process_query(message_text, session_id, channel="instagram"):
                if chunk.get("type") == "token":
                    full_response += chunk.get("content", "")
                elif chunk.get("type") == "error":
                    logger.error(f"Agent error: {chunk.get('error')}")
        except Exception as e:
            logger.error(f"Instagram agent failure for {sender_id}: {e}", exc_info=True)
            full_response = "Sorry, something went wrong at our end. Please try again in a moment."

        await ig.send_typing_indicator(sender_id, False)
        if full_response.strip():
            await ig.send_message(sender_id, full_response)


@app.post("/instagram")
@app.post("/webhook/instagram")
async def instagram_webhook(request: Request):
    try:
        body = await request.json()
        logger.debug(f"🟣 [INSTAGRAM] {json.dumps(body)[:800]}")

        parsed = ig.parse_webhook_payload(body)
        if not parsed:
            return {"status": "ok"}

        sender_id = parsed["sender_id"]
        message_text = parsed["message"]
        message_id = parsed.get("message_id")

        if ig.INSTAGRAM_ACCOUNT_ID and sender_id == ig.INSTAGRAM_ACCOUNT_ID:
            return {"status": "ok"}
        if message_id and SessionStore.seen_message(f"ig:{message_id}"):
            return {"status": "ok"}

        session_id = ig.get_session_id(sender_id)
        logger.info(f"📩 Instagram from {sender_id}: {message_text[:60]}")

        await ig.mark_as_seen(sender_id)
        await ig.send_typing_indicator(sender_id, True)

        store = SessionStore(session_id, channel="instagram")
        if not store.exists("first_message_id"):
            store.set("first_message_id", message_id or "")
        if not store.get_user_info():
            profile = await ig.get_user_profile(sender_id)
            store.update_user_info(
                {
                    "sender_id": sender_id,
                    "name": (profile or {}).get("name", ""),
                    "username": (profile or {}).get("username", ""),
                    "source": "instagram",
                }
            )
        _carry_over(store, ig.get_previous_summary(sender_id))

        asyncio.create_task(_run_instagram_turn(sender_id, message_text, session_id))
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Instagram webhook error: {e}", exc_info=True)
        return {"status": "ok"}


def _carry_over(store: SessionStore, summary: Optional[str]):
    """Attach yesterday's summary to a fresh session, once."""
    if summary and not store.exists("carried_summary"):
        store.set("carried_summary", summary)


# ==================== SESSION / ADMIN ====================

@app.get("/session/{session_id}/transcript", dependencies=[Depends(verify_admin_api_key)])
async def get_transcript(session_id: str):
    return {"session_id": session_id, "messages": SessionStore(session_id).get_full_transcript()}


@app.get("/session/{session_id}/transcript/formatted", dependencies=[Depends(verify_admin_api_key)])
async def get_transcript_formatted(session_id: str):
    return {"session_id": session_id, "transcript": SessionStore(session_id).get_transcript_formatted()}


@app.get("/session/{session_id}/stats", dependencies=[Depends(verify_admin_api_key)])
async def get_stats(session_id: str):
    return SessionStore(session_id).get_session_stats()


@app.delete("/session/{session_id}", dependencies=[Depends(verify_admin_api_key)])
async def clear_session(session_id: str):
    SessionStore(session_id).clear_session()
    return {"status": "cleared", "session_id": session_id}


@app.get("/admin/diagnostics", dependencies=[Depends(verify_admin_api_key)])
async def diagnostics():
    from client.store import SqlitePool as P

    sessions = P.query(
        "SELECT session_id, channel, last_activity, msg_count, user_count FROM sessions "
        "ORDER BY last_activity DESC LIMIT 25"
    )
    counts = P.query("SELECT COUNT(*) AS c FROM sessions")[0]["c"]
    return {
        "store": {"healthy": P.health(), "sessions_total": counts},
        "dataverse": await dataverse.health(),
        "channels": {
            channel: {
                "prompt_id": get_channel_config(channel).prompt_id,
                "tools": get_channel_config(channel).tools,
                "response_format": get_channel_config(channel).response_format,
            }
            for channel in get_available_channels()
        },
        "registered_tools": available_tools(),
        "interaction_logger": {"idle_seconds": IDLE_SECONDS},
        "recent_sessions": [dict(r) for r in sessions],
    }


@app.post("/admin/flush-interactions", dependencies=[Depends(verify_admin_api_key)])
async def flush_interactions(idle_seconds: int = IDLE_SECONDS):
    """Log quiet conversations to the CRM now instead of waiting for the timer.

    Pass idle_seconds=0 to flush every session with unlogged messages.
    """
    return await flush_due(idle_seconds)


@app.get("/admin/amc-reminders/preview", dependencies=[Depends(verify_admin_api_key)])
async def amc_preview(as_of: Optional[str] = None):
    """Who would be messaged, and with what. Sends nothing.

    `as_of=YYYY-MM-DD` runs the ladder against a different date, which is the only
    practical way to exercise it — real milestones land on a handful of days a year.
    """
    when = datetime.fromisoformat(f"{as_of}T12:00:00+05:30") if as_of else None
    return await amc_reminders.run_once(dry_run=True, today=when)


@app.post("/admin/amc-reminders/run", dependencies=[Depends(verify_admin_api_key)])
async def amc_run(as_of: Optional[str] = None):
    """Send the ladder now, off-schedule. Real messages."""
    when = datetime.fromisoformat(f"{as_of}T12:00:00+05:30") if as_of else None
    return await amc_reminders.run_once(dry_run=False, today=when)


@app.post("/admin/amc-reminders/opt-in/{phone}", dependencies=[Depends(verify_admin_api_key)])
async def amc_opt_in(phone: str):
    """Undo an opt-out — only on the customer's own request."""
    amc_reminders.opt_in(phone)
    return {"status": "ok", "phone": phone, "opted_out": amc_reminders.is_opted_out(phone)}


@app.post("/admin/purge", dependencies=[Depends(verify_admin_api_key)])
async def purge():
    return SessionStore.purge_expired()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None)
