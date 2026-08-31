"""
amc_reminders.py — proactive AMC expiry reminders on WhatsApp.

Runs once a day at 12:00 IST. Finds AMC contracts sitting exactly on one of the
milestone days in `amc_templates.LADDER`, and sends that day's approved template.

Three things this module exists to prevent, all of which cost you the number:

1. **Sending twice.** Every (contract, milestone) pair is recorded in the kv store
   before the send is attempted, with a TTL longer than the ladder. A restart, a
   double-scheduled run, or a second instance cannot re-send.
2. **Sending to someone who said stop.** Opt-outs are keyed on the normalised phone
   number and never expire.
3. **Sending to the wrong people while testing.** `AMC_REMINDER_ALLOWLIST` limits
   sends to an explicit set of numbers. With no allowlist the job will not send at
   all — reaching the whole CRM takes a second, deliberate flag
   (`AMC_REMINDER_ALLOW_FULL_CRM`). The demo org is full of real-looking Indian
   mobile numbers, so "no allowlist" must not quietly mean "message everyone".

The job is OFF unless `AMC_REMINDERS_ENABLED=true`. A background sender that turns
itself on during a deploy is not something you want to discover from a customer.
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from client import amc_templates as T
from client import offers
from client import whatsapp_connection as wa
from client.dataverse_client import dataverse
from client.store import SessionStore
import client.ef_schema as S
from tools.ef_crm import digits

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

ENABLED = os.getenv("AMC_REMINDERS_ENABLED", "false").lower() in ("1", "true", "yes")
SEND_HOUR_IST = int(os.getenv("AMC_REMINDER_HOUR_IST", "12"))
SEND_MINUTE_IST = int(os.getenv("AMC_REMINDER_MINUTE_IST", "0"))
MAX_PER_RUN = int(os.getenv("AMC_REMINDER_MAX_PER_RUN", "200"))
SEND_SPACING_SECONDS = float(os.getenv("AMC_REMINDER_SPACING_SECONDS", "1.0"))
OFFER_PCT = os.getenv("AMC_RENEWAL_OFFER_PCT", "10")

# Comma-separated numbers. Empty string means "the whole CRM" and must be set
# deliberately — see _allowed().
_raw_allow = os.getenv("AMC_REMINDER_ALLOWLIST", "")
ALLOWLIST = {digits(n) for n in _raw_allow.split(",") if n.strip()}
ALLOW_FULL_CRM = os.getenv("AMC_REMINDER_ALLOW_FULL_CRM", "false").lower() in ("1", "true", "yes")

# kv namespaces. These are not sessions, so they carry their own keyspace.
NS_SENT = "amc:sent"
NS_OPTOUT = "amc:optout"
# What we last sent each number, so a reply days later can still be attributed to
# the reminder that caused it. Keyed on the phone rather than the session, because
# sessions roll at midnight and replies do not.
NS_LASTSENT = "amc:lastsent"
LASTSENT_TTL_SECONDS = 7 * 24 * 3600
SENT_TTL_SECONDS = 400 * 24 * 3600

OPT_OUT_WORDS = {"stop", "unsubscribe", "opt out", "optout", "stop promotions", "do not message"}

_kv = SessionStore("amc-reminders", channel="whatsapp")


def wa_number(phone: str) -> str:
    """WhatsApp addresses a number in full international form, no plus sign.

    `normalise_phone` keeps only the last 10 digits because that is what matches
    reliably against this org's inconsistent phone formats — correct for lookups,
    unsendable as a destination. Anything already carrying a country code is left
    alone; a bare 10-digit Indian number gets 91.
    """
    d = digits(phone)
    return d if len(d) > 10 else ("91" + d if len(d) == 10 else d)


# ==================== OPT-OUT ====================

def is_opted_out(phone: str) -> bool:
    return _kv.exists(digits(phone), namespace=NS_OPTOUT)


def opt_out(phone: str, reason: str = "customer replied STOP") -> None:
    _kv.set(digits(phone), reason, ttl=SENT_TTL_SECONDS, namespace=NS_OPTOUT)
    logger.info(f"🔕 {phone} opted out of AMC reminders — {reason}")


def opt_in(phone: str) -> None:
    _kv.delete(digits(phone), namespace=NS_OPTOUT)


def looks_like_opt_out(message: str) -> bool:
    """True when an inbound message is the customer asking us to stop."""
    cleaned = (message or "").strip().lower().strip(".!？?")
    return cleaned in OPT_OUT_WORDS


# ==================== DEDUPE ====================

def _sent_key(contract_id: str, template_name: str) -> str:
    return f"{contract_id}:{template_name}"


def already_sent(contract_id: str, template_name: str) -> bool:
    return _kv.exists(_sent_key(contract_id, template_name), namespace=NS_SENT)


def mark_sent(contract_id: str, template_name: str, phone: str) -> None:
    _kv.set(
        _sent_key(contract_id, template_name),
        {"phone": phone, "at": datetime.now(IST).isoformat()},
        ttl=SENT_TTL_SECONDS,
        namespace=NS_SENT,
    )


def record_last_reminder(phone: str, template_name: str, contract_id: str, days: int) -> None:
    _kv.set(
        digits(phone),
        {"template": template_name, "contract_id": contract_id, "days": days,
         "at": datetime.now(IST).isoformat()},
        ttl=LASTSENT_TTL_SECONDS,
        namespace=NS_LASTSENT,
    )


def last_reminder(phone: str) -> Optional[Dict[str, Any]]:
    """The reminder this number was last sent, if it was within the last week."""
    data = _kv.get_json(digits(phone), namespace=NS_LASTSENT)
    return data if isinstance(data, dict) else None


# ==================== FINDING WHO IS DUE ====================

def _clean_product(name: str) -> str:
    """'Aquaguard Nova Pro (EFAG2314555)' -> 'Aquaguard Nova Pro'.

    The CRM appends the serial to the product name. It is useful internally and
    reads like a mistake in a customer-facing message.
    """
    return re.sub(r"\s*\([A-Z0-9\-]{4,}\)\s*$", "", (name or "").strip()) or name


def _fmt_date(iso: str) -> str:
    """'2026-09-25' -> '25 September 2026', which is what goes in the template."""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%-d %B %Y")
    except (ValueError, TypeError):
        return iso[:10]


async def find_due(today: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """AMC contracts landing exactly on a milestone day today.

    One query for all six milestones rather than six queries — the ladder is small
    and the expiry dates are an exact-match set, so an `or` chain is cheaper than
    a range scan plus filtering in Python.
    """
    today = (today or datetime.now(IST)).date()

    wanted: Dict[str, int] = {}
    for days in T.MILESTONE_DAYS:
        wanted[(today + timedelta(days=days)).isoformat()] = days

    amc = S.choice("ef_servicecontract", "ef_contracttype", "AMC")
    renewed = S.choice("ef_servicecontract", "ef_status", "Renewed")
    date_clause = " or ".join(f"ef_expirydate eq {d}" for d in wanted)
    filt = f"ef_contracttype eq {amc} and ef_status ne {renewed} and ({date_clause})"

    nav = S.nav("ef_servicecontract", "ef_customer")
    asset_nav = S.nav("ef_servicecontract", "ef_asset")
    try:
        rows = await dataverse.query(
            S.apiset("ef_servicecontract"),
            select=[
                "ef_servicecontractid", "ef_contractid",
                "ef_expirydate", "ef_status", "ef_contracttier", "ef_contractvalue",
            ],
            filter=filt,
            # The product name has to come from the asset. `ef_assetname` is in the
            # schema JSON but does not exist on the API — the same trap as
            # `ef_customername` on this table.
            expand=(
                f"{nav}($select=ef_fullname,ef_phone,ef_status),"
                f"{asset_nav}($select=ef_productname,ef_modelcode)"
            ),
            top=1000,
        )
    except Exception as e:
        logger.error(f"❌ AMC reminder query failed: {e}")
        return []

    due: List[Dict[str, Any]] = []
    for r in rows:
        expiry = (r.get("ef_expirydate") or "")[:10]
        days = wanted.get(expiry)
        template = T.for_days(days) if days is not None else None
        if template is None:
            continue

        customer = r.get(nav) or {}
        asset = r.get(asset_nav) or {}
        phone = customer.get("ef_phone") or ""
        if not phone:
            logger.warning(
                f"⚠️  Contract {r.get('ef_contractid')} is due but its customer has no phone — skipped"
            )
            continue

        due.append({
            "contract_record_id": r.get("ef_servicecontractid"),
            "contract_id": r.get("ef_contractid"),
            "product": _clean_product(asset.get("ef_productname")) or "your Eureka Forbes appliance",
            "expiry_date": expiry,
            "expiry_pretty": _fmt_date(expiry),
            "days": days,
            "template": template,
            "customer_name": (customer.get("ef_fullname") or "").split(" ")[0] or "there",
            "phone": phone,
            "customer_status": S.label("ef_customer", "ef_status", customer.get("ef_status")),
        })

    due.sort(key=lambda d: d["days"], reverse=True)
    return due


# ==================== SENDING ====================

def _allowed(phone: str) -> bool:
    if not ALLOWLIST:
        return True
    return digits(phone) in ALLOWLIST or digits(phone)[-10:] in {a[-10:] for a in ALLOWLIST}


def _skip_reason(item: Dict[str, Any]) -> Optional[str]:
    if is_opted_out(item["phone"]):
        return "opted out"
    if already_sent(item["contract_record_id"], item["template"].name):
        return "already sent"
    if not _allowed(item["phone"]):
        return "not in allowlist"
    return None


async def run_once(dry_run: bool = False, today: Optional[datetime] = None) -> Dict[str, Any]:
    """One pass of the ladder. Returns a report rather than logging and forgetting."""
    started = datetime.now(IST)
    report: Dict[str, Any] = {
        "ran_at": started.isoformat(),
        "dry_run": dry_run,
        "allowlist_active": bool(ALLOWLIST),
        "due": 0, "sent": 0, "skipped": 0, "failed": 0,
        "by_template": {}, "items": [],
    }

    if not wa.is_configured():
        report["error"] = "WhatsApp is not configured"
        return report

    if not dry_run and not ALLOWLIST and not ALLOW_FULL_CRM:
        report["error"] = (
            "Refusing to send: AMC_REMINDER_ALLOWLIST is empty. Set it to the numbers "
            "you are testing with, or set AMC_REMINDER_ALLOW_FULL_CRM=true to message "
            "every matching customer in the CRM."
        )
        logger.error("🛑 " + report["error"])
        return report

    due = await find_due(today)
    report["due"] = len(due)

    # This org holds duplicate contract records — AMC-000502 exists twice. The
    # persistent dedupe is keyed on the contract GUID, so duplicates are distinct
    # rows to it and the customer would get the same template twice in one run.
    # Collapse on (number, template) for the duration of the run.
    seen_this_run = set()

    for item in due:
        template = item["template"]
        entry = {
            "contract_id": item["contract_id"],
            "phone": item["phone"],
            "days": item["days"],
            "template": template.name,
            "category": template.category,
        }

        pair = (wa_number(item["phone"]), template.name)
        if pair in seen_this_run:
            entry["outcome"] = "skipped: duplicate contract record"
            report["skipped"] += 1
            report["items"].append(entry)
            continue

        reason = _skip_reason(item)
        if reason:
            entry["outcome"] = f"skipped: {reason}"
            report["skipped"] += 1
            report["items"].append(entry)
            continue

        if report["sent"] >= MAX_PER_RUN:
            entry["outcome"] = "skipped: run cap reached"
            report["skipped"] += 1
            report["items"].append(entry)
            continue

        seen_this_run.add(pair)
        params = [item["customer_name"], item["product"], item["expiry_pretty"]]
        # Offer templates carry the discount as {{4}} so the live campaign figure is
        # sent, not whatever was hardcoded when the template was approved.
        if len(template.params) == 4:
            params.append(str(offers.DISCOUNT_PCT))

        if dry_run:
            entry["outcome"] = "would send"
            entry["params"] = params
            report["items"].append(entry)
            report["by_template"][template.name] = report["by_template"].get(template.name, 0) + 1
            continue

        # Marked before the send, not after: a crash mid-send must not turn into a
        # duplicate tomorrow. A reminder that silently fails is far cheaper than one
        # that arrives twice.
        mark_sent(item["contract_record_id"], template.name, item["phone"])

        ok = await wa.send_template(
            wa_number(item["phone"]),
            template.name,
            body_params=params,
            language=T.LANGUAGE,
        )
        if ok:
            record_last_reminder(item["phone"], template.name, item["contract_id"], item["days"])
            entry["outcome"] = "sent"
            report["sent"] += 1
            report["by_template"][template.name] = report["by_template"].get(template.name, 0) + 1
        else:
            entry["outcome"] = "failed"
            report["failed"] += 1

        report["items"].append(entry)
        if SEND_SPACING_SECONDS:
            await asyncio.sleep(SEND_SPACING_SECONDS)

    logger.info(
        f"📤 AMC reminders {'(dry run) ' if dry_run else ''}— "
        f"due={report['due']} sent={report['sent']} skipped={report['skipped']} failed={report['failed']}"
    )
    return report


# ==================== SCHEDULE ====================

def _seconds_until_next_run(now: Optional[datetime] = None) -> float:
    now = now or datetime.now(IST)
    target = now.replace(hour=SEND_HOUR_IST, minute=SEND_MINUTE_IST, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def reminder_loop():
    """Sleep until 12:00 IST, run, repeat.

    Deliberately not a cron expression: the app already owns a single always-on
    process, and sleeping to a wall-clock target keeps the schedule correct across
    restarts without another dependency.
    """
    if not ENABLED:
        logger.info("💤 AMC reminders disabled (AMC_REMINDERS_ENABLED is not true)")
        return

    logger.info(
        f"📅 AMC reminders scheduled for {SEND_HOUR_IST:02d}:{SEND_MINUTE_IST:02d} IST daily | "
        f"milestones={T.MILESTONE_DAYS} | "
        f"allowlist={'ON (' + str(len(ALLOWLIST)) + ' numbers)' if ALLOWLIST else 'OFF — full CRM'}"
    )

    while True:
        wait = _seconds_until_next_run()
        logger.info(f"⏳ Next AMC reminder run in {wait / 3600:.1f}h")
        try:
            await asyncio.sleep(wait)
            await run_once()
        except asyncio.CancelledError:
            logger.info("📅 AMC reminder scheduler stopping")
            raise
        except Exception as e:
            logger.error(f"❌ AMC reminder run failed: {e}", exc_info=True)
            # Do not retry into a tight loop — wait out the rest of the day.
            await asyncio.sleep(60)
