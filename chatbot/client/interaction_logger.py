"""
interaction_logger.py — writes one ef_interaction row per conversation.

A social conversation has no "end" event, so idleness is the signal: when a
session has had no activity for INTERACTION_IDLE_SECONDS (default 120), the whole
exchange is summarised, classified, and pushed to the CRM, and the parent record's
counters roll forward.

If the customer comes back later on the same day, the next quiet spell produces a
SECOND row covering only the new messages — the watermark (`interaction_upto`)
tracks how far the CRM has been told about, so nothing is logged twice and nothing
is missed.

Classification is rules-first: anything the tools already established (a renewal
was started, a complaint was logged, the bot escalated) wins outright, and the LLM
is only asked for what genuinely needs reading the conversation — intent, sentiment,
a summary, and a disposition when no rule applies.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from client import ef_schema as S
from client.config import AGENT_NAME
from client.store import SessionStore, SqlitePool
from tools import ef_crm

logger = logging.getLogger(__name__)

IDLE_SECONDS = int(os.getenv("INTERACTION_IDLE_SECONDS", "120"))
# How long to hold an unidentified conversation before logging it unlinked.
ORPHAN_HOURS = int(os.getenv("INTERACTION_ORPHAN_HOURS", "24"))
SCAN_INTERVAL = int(os.getenv("INTERACTION_SCAN_SECONDS", "30"))

# Disposition decided by what actually happened, before asking the model anything.
RULE_DISPOSITIONS = [
    ("renewal_started", "ConvertedRenewal"),
    ("visit_booked", "ConvertedVisit"),
    ("escalated", "Escalated"),
    ("callback_requested", "CallbackRequested"),
    ("service_request_id", "Resolved"),
]

VALID_DISPOSITIONS = set(S.labels("ef_interaction", "ef_disposition"))


def _iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def due_sessions(idle_seconds: int = IDLE_SECONDS) -> List[Dict[str, Any]]:
    """Sessions quiet long enough to be worth logging, with unlogged messages."""
    cutoff = (datetime.now(timezone(timedelta(hours=5, minutes=30))) - timedelta(seconds=idle_seconds)).isoformat()
    rows = SqlitePool.query(
        "SELECT session_id, channel, created_at, last_activity, msg_count FROM sessions "
        "WHERE last_activity < ? AND msg_count > 0",
        (cutoff,),
    )
    due = []
    for row in rows:
        store = SessionStore(row["session_id"], channel=row["channel"] or "whatsapp")
        watermark = int(store.get("interaction_upto") or 0)
        newest = SqlitePool.query(
            "SELECT MAX(id) AS max_id FROM messages WHERE session_id = ?", (row["session_id"],)
        )
        max_id = (newest[0]["max_id"] if newest else 0) or 0
        if max_id > watermark:
            due.append({**dict(row), "watermark": watermark, "max_id": max_id})
    return due


def _unlogged_messages(session_id: str, watermark: int) -> List[Dict[str, Any]]:
    rows = SqlitePool.query(
        "SELECT id, role, content, tokens, created_at FROM messages "
        "WHERE session_id = ? AND id > ? ORDER BY id",
        (session_id, watermark),
    )
    return [dict(r) for r in rows]


async def _classify(transcript: str, store: SessionStore) -> Dict[str, Any]:
    """One cheap LLM call for what only reading the conversation can tell us."""
    from client.config import create_summary_llm

    prompt = (
        "You are classifying a finished customer-service conversation for a CRM.\n"
        "Return ONLY a JSON object, no prose, with exactly these keys:\n"
        '  "summary": what the customer wanted and what was done, third person, under 150 words. '
        'WRITE IT IN ENGLISH even when the conversation was in Hindi, Hinglish, Tamil or any '
        'other language — translate rather than transcribe. Nobody reading this row in the CRM '
        'has the transcript, and a summary they cannot read is the same as no summary. Keep '
        'product names, model numbers and case numbers exactly as they appear.\n'
        '  "intent": one snake_case label, e.g. service_complaint, amc_enquiry, new_purchase, '
        'product_info, filter_change, order_status, other\n'
        '  "sentiment": number from -1 (angry) to 1 (delighted)\n'
        f'  "disposition": one of {sorted(VALID_DISPOSITIONS)}\n'
        '  "escalated": true if the customer was handed off to a human or asked for one\n'
        '  "product": the product or model discussed, or "" if none. English, as the catalogue '
        'spells it\n\n'
        "Disposition guidance: Resolved = their problem was answered or logged; "
        "Interested = they want something but did not commit; Qualified = a genuine "
        "sales lead with contact details; Engaged = a normal exchange with no clear "
        "outcome; NoResponse = they never replied; Declined = they said no.\n\n"
        f"CONVERSATION:\n{transcript[:6000]}\n\nJSON:"
    )

    fallback = {
        "summary": transcript[:1500],
        "intent": store.get("intent") or "other",
        "sentiment": None,
        "disposition": "Engaged",
        "escalated": False,
        "product": store.get("product_interest") or "",
    }

    try:
        llm = create_summary_llm()
        response = await asyncio.wait_for(llm.ainvoke(prompt), timeout=45)
        text = response.content if hasattr(response, "content") else str(response)
        if isinstance(text, list):
            text = "".join(b.get("text", "") for b in text if isinstance(b, dict))
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1].removeprefix("json").strip()
        data = json.loads(text)
    except Exception as e:
        logger.warning(f"Interaction classification failed, using fallback: {e}")
        return fallback

    if data.get("disposition") not in VALID_DISPOSITIONS:
        data["disposition"] = "Engaged"
    return {**fallback, **{k: v for k, v in data.items() if v is not None}}


def _resolve_disposition(store: SessionStore, classified: str) -> str:
    """Facts beat opinions: a tool that ran tells us more than the model's reading."""
    hint = store.get("disposition_hint")
    if hint in VALID_DISPOSITIONS:
        return hint
    for key, disposition in RULE_DISPOSITIONS:
        if store.exists(key) or store.get(key):
            return disposition
    return classified


async def flush_session(session: Dict[str, Any]) -> Optional[str]:
    """Log one session's unlogged messages as a single ef_interaction."""
    session_id = session["session_id"]
    channel = session.get("channel") or "whatsapp"
    store = SessionStore(session_id, channel=channel)

    messages = _unlogged_messages(session_id, session["watermark"])
    if not messages:
        return None
    if not any(m["role"].lower() == "user" for m in messages):
        # Nothing the customer said — not worth a CRM row
        store.set("interaction_upto", str(session["max_id"]), ttl=86400 * 7)
        return None

    transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in messages)

    classified = await _classify(transcript, store)
    disposition = _resolve_disposition(store, classified.get("disposition", "Engaged"))

    info = store.get_user_info() or {}
    crm_type = store.get("crm_type")
    record_id = store.get("crm_record_id")

    # No CRM record yet: the customer never gave a number, or the lookup has not
    # happened. Logging now would create a row attached to nobody, so wait — if
    # they come back and identify themselves, the whole conversation lands on the
    # right record. Past ORPHAN_HOURS that is not going to happen, so log it
    # unlinked rather than lose it.
    if not record_id:
        age = datetime.now(timezone.utc) - (_iso(messages[0]["created_at"]) or datetime.now(timezone.utc))
        if age < timedelta(hours=ORPHAN_HOURS):
            logger.info(
                f"⏸️  {session_id}: nobody identified yet — holding {len(messages)} message(s) "
                f"for up to {ORPHAN_HOURS}h"
            )
            return None
        logger.warning(f"🔗 {session_id}: still unidentified after {ORPHAN_HOURS}h — logging unlinked")

    started = _iso(messages[0]["created_at"])
    first_reply = next((m for m in messages if m["role"].lower() == "assistant"), None)
    responded = _iso(first_reply["created_at"]) if first_reply else None

    cost = 0.0
    for m in messages:
        try:
            tokens = json.loads(m["tokens"]) if m["tokens"] else {}
            cost += _token_cost(tokens)
        except (json.JSONDecodeError, TypeError):
            pass

    body = classified.get("summary") or transcript

    # What the conversation actually produced in the CRM. The summary is written by a
    # model and may or may not mention these; the case number especially is how anyone
    # picks the work up later, so it is appended rather than hoped for.
    case = store.get("case_number")
    booked = store.get("visit_booked")
    if case:
        body = f"{body}\n\nService request {case} raised."
    if booked:
        body = f"{body}\n\nTechnician visit booked for {booked}." if not case \
            else f"{body} Technician visit booked for {booked}."

    # Attribution and objection are the two things you cannot recover later by
    # re-reading the transcript at scale, so they go in as structured-ish text
    # rather than being left implicit in the summary prose.
    intent = classified.get("intent", "")
    nudge_template = store.get("reminder_template")
    if nudge_template:
        contract = store.get("reminder_contract") or ""
        body = f"{body}\n\nReplied to reminder {nudge_template}" + (f" for {contract}." if contract else ".")
        intent = f"{intent}|reminder:{nudge_template}"[:120]

    objection = store.get("renewal_objection")
    if objection:
        notes = store.get("renewal_outcome_notes") or ""
        outcome = store.get("renewal_outcome") or ""
        body = f"{body}\n\nRenewal not closed — outcome={outcome}, objection={objection}." + (
            f' They said: "{notes}"' if notes else ""
        )
        intent = f"{intent}|objection:{objection}"[:120]

    try:
        result = await ef_crm.log_interaction(
            channel=channel,
            summary=body,
            intent=intent,
            sentiment=classified.get("sentiment"),
            disposition=disposition,
            customer_id=record_id if crm_type == "customer" else None,
            lead_id=record_id if crm_type == "lead" else None,
            prospect_id=record_id if crm_type == "prospect" else None,
            started_at=started,
            responded_at=responded,
            provider_message_id=store.get("first_message_id") or "",
            transcript_ref=session_id,
            escalated=bool(classified.get("escalated")),
            handled_by=AGENT_NAME,
            cost_amount=cost,
        )
    except Exception as e:
        # ef_providermessageid is an alternate key on this table. A session that gets
        # cleared and rebuilt reuses the same first wamid, so the second write
        # collides with the first. The id is only there for dedupe — losing it on
        # the retry is much cheaper than losing the interaction.
        if "Provider Message ID" in str(e) or "0x80060892" in str(e):
            logger.warning(
                f"⚠️  {session_id}: provider message id already used — retrying without it"
            )
            try:
                result = await ef_crm.log_interaction(
                    channel=channel,
                    summary=body,
                    intent=intent,
                    sentiment=classified.get("sentiment"),
                    disposition=disposition,
                    customer_id=record_id if crm_type == "customer" else None,
                    lead_id=record_id if crm_type == "lead" else None,
                    prospect_id=record_id if crm_type == "prospect" else None,
                    started_at=started,
                    responded_at=responded,
                    provider_message_id="",
                    transcript_ref=session_id,
                    escalated=bool(classified.get("escalated")),
                    handled_by=AGENT_NAME,
                    cost_amount=cost,
                )
            except Exception as retry_error:
                e = retry_error
                result = None
        else:
            result = None

        if result is None:
            logger.error(f"❌ Could not log interaction for {session_id}: {e}", exc_info=True)
            # Advance the watermark anyway. Leaving it put means this session is
            # retried every scan for ever — 298 identical failures in one afternoon
            # is how this was found. One lost interaction beats an infinite loop.
            store.set("interaction_upto", str(session["max_id"]), ttl=86400 * 7)
            return None

    # Advance the watermark only after the CRM accepted the row
    store.set("interaction_upto", str(session["max_id"]), ttl=86400 * 7)

    try:
        if crm_type == "customer" and record_id:
            await ef_crm.touch_customer(
                record_id,
                sentiment=classified.get("sentiment"),
                escalated=bool(classified.get("escalated")),
            )
        elif crm_type == "lead" and record_id:
            score = ef_crm.qualification_score(
                info.get("name", ""), info.get("phone", ""),
                classified.get("product") or store.get("product_interest") or "",
                info.get("pincode", "") or info.get("city", ""),
                classified.get("intent", ""),
            )
            await ef_crm.touch_lead(record_id, qualification_score_value=score)
            # Details the customer gave AFTER the lead was created (a pincode, an
            # email, a firmer product) would otherwise never reach the CRM.
            late = {}
            if classified.get("product"):
                late["product_interest"] = classified["product"]
            if info.get("pincode"):
                late["pincode"] = info["pincode"]
            if info.get("email"):
                late["email"] = info["email"]
            if late:
                await ef_crm.update_lead(record_id, **late)
    except Exception as e:
        logger.warning(f"Counter update failed for {session_id}: {e}")

    return result["interaction_id"]


def _token_cost(tokens: dict) -> float:
    """Model spend for this message, if rates are configured. Zero otherwise."""
    rate_in = float(os.getenv("COST_PER_1K_INPUT", "0") or 0)
    rate_out = float(os.getenv("COST_PER_1K_OUTPUT", "0") or 0)
    if not rate_in and not rate_out:
        return 0.0
    return (int(tokens.get("input", 0) or 0) / 1000 * rate_in) + (
        int(tokens.get("output", 0) or 0) / 1000 * rate_out
    )


async def flush_due(idle_seconds: int = IDLE_SECONDS) -> Dict[str, Any]:
    """Log every session that has gone quiet. Safe to call by hand."""
    sessions = due_sessions(idle_seconds)
    logged = []
    for session in sessions:
        try:
            interaction_id = await flush_session(session)
            if interaction_id:
                logged.append({"session_id": session["session_id"], "interaction_id": interaction_id})
        except Exception as e:
            logger.error(f"Flush failed for {session['session_id']}: {e}", exc_info=True)
    if logged:
        logger.info(f"📤 Logged {len(logged)} interaction(s) to the CRM")
    return {"scanned": len(sessions), "logged": logged}


async def logger_loop():
    """Background task: scan for quiet conversations and log them."""
    logger.info(
        f"⏱️  Interaction logger running — idle threshold {IDLE_SECONDS}s, scan every {SCAN_INTERVAL}s"
    )
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL)
            await flush_due()
        except asyncio.CancelledError:
            logger.info("Interaction logger stopping — flushing what is pending")
            try:
                await flush_due(idle_seconds=0)
            except Exception as e:
                logger.warning(f"Final flush failed: {e}")
            return
        except Exception as e:
            logger.error(f"Interaction logger error: {e}", exc_info=True)
