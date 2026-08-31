#!/usr/bin/env python3
"""
send_amc_nudges.py — AMC reminder nudges to one handset, triggered by the customer.

Not the template path. No templates are approved on the production WABA and none are
needed: the customer opens the 24-hour service window by messaging in, and that same
message is what starts the sequence. Plain text, sent inside a window the customer
themselves opened.

    .venv/bin/python scripts/send_amc_nudges.py --status    # triggers, slots, ledger
    .venv/bin/python scripts/send_amc_nudges.py --dry-run   # resolve + print, send nothing
    .venv/bin/python scripts/send_amc_nudges.py             # send whatever is due

HOW THE TRIGGER WORKS
Any inbound message from the recipient on an active date arms that date. The first
nudge goes on the next tick, then every 2 hours, with the last one no later than
20:00 IST. Each date arms independently — silence on a date means nothing is sent
on it, by design.

The trigger is read from the chatbot's own session DB (`messages` rows with
role='user'), so this stays decoupled from the webhook server. It follows that
**the ef-chatbot webhook server must be running and publicly reachable**, or no
inbound message is ever recorded and nothing here fires.

Run this every few minutes from launchd. Four properties that matter:

1. **The token is re-read from .env every run.** It expires roughly daily and is
   replaced by hand; nothing here caches it.
2. **A slot is sent at most once, ever.** The ledger is written before the send is
   attempted, so a crash mid-send cannot become a double-send.
3. **The anchor is frozen on first sight.** Once a date's trigger is recorded, later
   messages that day do not shift the schedule.
4. **A slot missed while the machine slept still goes, if under GRACE_MINUTES late.**
   Older than that it is marked missed — a 2am delivery of a 4pm nudge helps nobody.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "chatbot"))

from client import amc_templates as T  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
GRAPH = "https://graph.facebook.com/v23.0"

ENV_PATH = os.path.join(ROOT, ".env")
LEDGER = os.path.join(ROOT, "data", "amc_nudge_ledger.json")

# Production WABA 2636713123428479 / +91 95995 59646, VERIFIED on Cloud API.
# Hardcoded on purpose: WHATSAPP_PHONE_NUMBER_ID in .env still points at the +1 555
# test number, and a nudge that silently went out from there would look like it
# worked while reaching nobody.
PHONE_NUMBER_ID = "1302822852910141"
SENDER_LABEL = "+91 95995 59646"

RECIPIENT = "919899643944"

ACTIVE_DATES = ["2026-08-27", "2026-08-29"]
SPACING_HOURS = 2
CUTOFF_HOUR = 20            # 20:00 IST — no nudge may go after this
MAX_NUDGES_PER_DAY = 6      # one full pass of the ladder, no repeats
GRACE_MINUTES = 60

# Messages already in the DB before this moment are test chatter from local_chat.py,
# not a customer arriving. Without this the sequence would fire the instant it was
# installed, off a row from earlier this morning.
ARM_FROM = datetime(2026, 8, 27, 8, 37, tzinfo=IST)

ROTATION = list(T.LADDER)

SAMPLE_NAME = "Ramesh"
SAMPLE_PRODUCT = "Aquaguard Marvel"


# ==================== TRIGGER ====================

def db_path() -> str:
    raw = (dotenv_values(ENV_PATH) or {}).get("EF_DB_PATH", "./data/ef_chat.db")
    return raw if os.path.isabs(raw) else os.path.normpath(os.path.join(ROOT, raw))


def inbound_on(date_str: str):
    """Earliest inbound message from RECIPIENT on date_str, at/after ARM_FROM.

    Session ids look like `whatsapp:<phone>:<date>`, and the phone shows up both with
    and without the 91 prefix, so match on the last ten digits rather than equality.
    """
    path = db_path()
    if not os.path.exists(path):
        return None
    tail = RECIPIENT[-10:]
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        rows = con.execute(
            "SELECT session_id, created_at FROM messages "
            "WHERE role='user' AND session_id LIKE 'whatsapp:%' ORDER BY created_at"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        # A locked or missing DB must read as "no trigger yet", never as a trigger.
        return None
    for session_id, created in rows:
        parts = session_id.split(":")
        if len(parts) < 3 or parts[1][-10:] != tail or parts[2] != date_str:
            continue
        try:
            when = datetime.fromisoformat(created)
        except ValueError:
            continue
        when = when.astimezone(IST) if when.tzinfo else when.replace(tzinfo=IST)
        if when >= ARM_FROM:
            return when
    return None


def anchor_for(date_str: str, led: dict):
    """The frozen start time for a date, recording it the first time it is seen."""
    key = f"trigger:{date_str}"
    if key in led:
        return datetime.fromisoformat(led[key]["at"]), False
    found = inbound_on(date_str)
    if found is None:
        return None, False
    led[key] = {"at": found.isoformat(), "noticed": datetime.now(IST).isoformat()}
    return found, True


def slots_for(date_str: str, anchor: datetime):
    """(slot_id, when, template) every SPACING_HOURS from the anchor, up to the cutoff."""
    y, m, d = (int(x) for x in date_str.split("-"))
    cutoff = datetime(y, m, d, CUTOFF_HOUR, 0, tzinfo=IST)
    out, when, i = [], anchor, 0
    while i < MAX_NUDGES_PER_DAY and when <= cutoff:
        out.append((f"{date_str}#{i}", when, ROTATION[i % len(ROTATION)]))
        when += timedelta(hours=SPACING_HOURS)
        i += 1
    return out


# ==================== MESSAGE ====================

def customer_facts():
    """Real name / product / expiry for the recipient, from the chatbot's own session kv.

    The agent resolves all three against CRM before it replies, so reading them back
    here beats a second Dataverse round trip and guarantees the nudge agrees with what
    the customer was just told. Returns None if no conversation has resolved them yet.
    """
    path = db_path()
    if not os.path.exists(path):
        return None
    tail = RECIPIENT[-10:]
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        rows = con.execute(
            "SELECT session_id, key, value FROM kv "
            "WHERE key IN ('pending_renewal','existing_lead_data') "
            "AND session_id LIKE 'whatsapp:%'"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return None

    latest = {}
    for session_id, key, value in sorted(rows, key=lambda r: r[0]):
        parts = session_id.split(":")
        if len(parts) < 3 or parts[1][-10:] != tail:
            continue
        try:
            latest[key] = json.loads(value)   # session ids end in the date, so newest wins
        except (json.JSONDecodeError, TypeError):
            continue

    renewal, lead = latest.get("pending_renewal"), latest.get("existing_lead_data")
    if not renewal or not renewal.get("expiry_date"):
        return None

    # ef_productname carries the serial in parentheses; strip it before it reaches a
    # customer — "Aquaguard Magna HD RO+UV (EFAG2694118)".
    product = re.sub(r"\s*\([^)]*\)\s*$", "", renewal.get("asset") or "").strip()
    full_name = (lead or {}).get("name") or ""
    expiry = datetime.strptime(renewal["expiry_date"], "%Y-%m-%d").date()
    return {
        "name": full_name.split()[0] if full_name else SAMPLE_NAME,
        "product": product or SAMPLE_PRODUCT,
        "expiry": expiry,
        "days_left": (expiry - datetime.now(IST).date()).days,
        "contract_id": renewal.get("contract_id"),
    }


def render(tmpl: T.Template, facts) -> str:
    """Fill {{1}}/{{2}}/{{3}} from real CRM facts, then flatten to plain text.

    Every body carries the customer's own expiry date. The ladder's own `days` offset
    is deliberately NOT used here: the six bodies still rotate, so four of them make
    claims ("about two weeks away", "expires tomorrow", "lapsed on") that will not
    match a contract at a different distance. That is a known and accepted trade for
    exercising all six against a test handset.
    """
    text = tmpl.body
    values = (facts["name"], facts["product"], facts["expiry"].strftime("%d %B %Y"))
    for slot, value in zip(("{{1}}", "{{2}}", "{{3}}"), values):
        text = text.replace(slot, value)
    if tmpl.footer:
        text += f"\n\n{tmpl.footer}"
    return text


# ==================== LEDGER ====================

def load_ledger() -> dict:
    if not os.path.exists(LEDGER):
        return {}
    try:
        with open(LEDGER) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # A truncated ledger must not read as "nothing sent yet" — that would replay
        # every past slot on the next run.
        print(f"!! ledger at {LEDGER} is unreadable; refusing to run", file=sys.stderr)
        sys.exit(1)


def save_ledger(led: dict) -> None:
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=2, sort_keys=True)
    os.replace(tmp, LEDGER)


# ==================== SEND ====================

def token() -> str:
    """Read straight from .env every call — never cached, never from os.environ."""
    return (dotenv_values(ENV_PATH) or {}).get("WHATSAPP_TOKEN", "") or ""


def send(text: str, tok: str):
    """-> (ok, detail, retryable).

    `retryable` separates "this send failed because the setup is momentarily broken"
    — expired token, network blip — from a real rejection by Meta. Only the latter
    should burn the slot: an expired token is fixed by pasting a new one, and the
    nudge should still go when it is.
    """
    if not tok:
        return False, "WHATSAPP_TOKEN is empty in .env", True
    try:
        r = httpx.post(
            f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
            headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": RECIPIENT,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        return False, f"network: {e}", True
    if r.status_code == 200:
        return True, r.json().get("messages", [{}])[0].get("id", "?"), False
    try:
        err = r.json().get("error", {})
        code, msg = err.get("code"), err.get("message", r.text)
        retryable = False
        if code == 190:
            msg = f"token expired or invalid — put a fresh one in .env ({msg})"
            retryable = True
        elif code == 131047:
            # The window is genuinely shut; retrying every five minutes would just
            # pile up identical rejections until it reopens.
            msg = f"24-hour window closed for {RECIPIENT}; free-form text refused ({msg})"
        elif r.status_code >= 500 or code in (4, 80007, 131056):
            msg = f"rate-limited or upstream error ({msg})"
            retryable = True
        return False, f"{code}: {msg}", retryable
    except ValueError:
        return False, f"{r.status_code} {r.text}", r.status_code >= 500


# ==================== MAIN ====================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show triggers and slots, then exit")
    ap.add_argument("--dry-run", action="store_true", help="resolve due slots, print, send nothing")
    args = ap.parse_args()

    now = datetime.now(IST)
    led = load_ledger()

    if args.status:
        print(f"\n  now {now:%Y-%m-%d %H:%M} IST   {SENDER_LABEL} -> +{RECIPIENT}")
        print(f"  armed from {ARM_FROM:%Y-%m-%d %H:%M} · every {SPACING_HOURS}h "
              f"· last by {CUTOFF_HOUR}:00 · max {MAX_NUDGES_PER_DAY}/day")
        print(f"  watching {db_path()}\n")
        for date_str in ACTIVE_DATES:
            anchor, _ = anchor_for(date_str, dict(led))
            if anchor is None:
                print(f"  {date_str}  no inbound message yet — waiting for the customer\n")
                continue
            print(f"  {date_str}  triggered {anchor:%H:%M} IST")
            for sid, when, tmpl in slots_for(date_str, anchor):
                rec = led.get(sid)
                if rec:
                    state = f"{rec['status']} {rec.get('detail', '')}".strip()
                elif when > now:
                    state = f"scheduled (in {str(when - now).split('.')[0]})"
                elif now - when <= timedelta(minutes=GRACE_MINUTES):
                    state = "DUE NOW"
                else:
                    state = "missed (never ran)"
                print(f"     {when:%H:%M}  {tmpl.name:<22} {state}")
            print()
        return 0

    due = []
    for date_str in ACTIVE_DATES:
        anchor, fresh = anchor_for(date_str, led)
        if anchor is None:
            continue
        if fresh:
            print(f"[{now:%Y-%m-%d %H:%M}] {date_str} armed by inbound at {anchor:%H:%M} IST")
        for sid, when, tmpl in slots_for(date_str, anchor):
            if sid in led or when > now:
                continue
            late = now - when
            if late > timedelta(minutes=GRACE_MINUTES):
                led[sid] = {"status": "missed", "detail": f"{int(late.total_seconds() // 60)}m late",
                            "at": now.isoformat()}
                continue
            due.append((sid, tmpl))

    save_ledger(led)
    if not due:
        return 0

    facts = customer_facts()
    if facts is None:
        # Sending sample copy to a real handset is worse than sending nothing: it
        # contradicts whatever the agent just told them. Leave the slots unmarked so
        # they still go once a conversation has resolved the contract.
        print(f"[{now:%Y-%m-%d %H:%M}] no resolved contract for +{RECIPIENT} yet — "
              f"holding {len(due)} slot(s)")
        return 0
    print(f"[{now:%Y-%m-%d %H:%M}] {facts['name']} · {facts['product']} · "
          f"{facts['contract_id']} expires {facts['expiry']:%d %b %Y} "
          f"({facts['days_left']}d)")

    tok = token()
    for sid, tmpl in due:
        text = render(tmpl, facts)
        print(f"[{now:%Y-%m-%d %H:%M}] slot {sid} -> {tmpl.name}")
        if args.dry_run:
            print(f"  would send to +{RECIPIENT}:\n  {text}\n")
            continue
        # Written before the attempt: a crash between here and the response must not
        # leave the slot looking unsent.
        led[sid] = {"status": "sending", "at": now.isoformat(), "template": tmpl.name}
        save_ledger(led)
        ok, detail, retryable = send(text, tok)
        if not ok and retryable:
            # Leave no ledger entry at all, so the next tick picks the slot up again —
            # subject to the same grace window, which stops it retrying forever.
            led.pop(sid, None)
            save_ledger(led)
            print(f"  RETRYABLE {detail}")
            continue
        led[sid] = {"status": "sent" if ok else "failed", "detail": detail,
                    "at": now.isoformat(), "template": tmpl.name}
        save_ledger(led)
        print(f"  {'OK' if ok else 'FAILED'} {detail}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
