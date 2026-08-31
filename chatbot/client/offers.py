"""
offers.py — the campaign offer currently running.

One place, read by three things that must never disagree: the reminder templates,
the agent's prompt, and the price list. A discount that lives in three files drifts
within a week, and the version the customer sees is whichever one you forgot.

Every offer has an end date. The agent stops mentioning an offer the day after it
expires without anyone editing a prompt — a bot still promoting a dead campaign is
a promise you have to honour or explain.
"""

import os
from datetime import date, datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ── The live campaign ────────────────────────────────────────────────────────
# Monsoon 2026. Set CAMPAIGN_ACTIVE=false to pull it without a deploy.
NAME = "monsoon_2026"
HEADLINE = "20% off everything, plus a free pre-monsoon check-up"

DISCOUNT_PCT = int(os.getenv("OFFER_DISCOUNT_PCT", "20"))

# ISO date, inclusive — the last day the offer may be mentioned.
ENDS_ON = os.getenv("OFFER_ENDS_ON", "2026-09-08")

ACTIVE = os.getenv("CAMPAIGN_ACTIVE", "true").lower() in ("1", "true", "yes")

# What the offer actually is, in the customer's terms. The agent says the substance
# of these, not the strings verbatim.
TERMS = [
    f"{DISCOUNT_PCT}% off every product, across all ranges — no exceptions",
    f"{DISCOUNT_PCT}% off AMC renewal for existing customers",
    "a free pre-monsoon check-up",
]

# Deliberately no product qualification. Asking "which product are you interested
# in?" before naming the offer loses people who do not yet know what they want, and
# the offer is the same either way.
APPLIES_TO_EVERYTHING = True

BROCHURE_PATH = os.getenv(
    "OFFER_BROCHURE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                 "eureka-forbes-offer-brochure.pdf"),
)
BROCHURE_FILENAME = "Eureka Forbes - Monsoon Offers.pdf"


def ends_on_date() -> Optional[date]:
    try:
        return date.fromisoformat(ENDS_ON)
    except (ValueError, TypeError):
        return None


def is_live(today: Optional[date] = None) -> bool:
    """False once the end date has passed, whatever the config still says."""
    if not ACTIVE:
        return False
    end = ends_on_date()
    if end is None:
        return False
    return (today or datetime.now(IST).date()) <= end


def pretty_end_date() -> str:
    end = ends_on_date()
    return end.strftime("%-d %B") if end else ENDS_ON


def days_left(today: Optional[date] = None) -> Optional[int]:
    end = ends_on_date()
    if end is None:
        return None
    return (end - (today or datetime.now(IST).date())).days


def summary() -> Dict[str, Any]:
    """What the agent is told about the current offer."""
    return {
        "live": is_live(),
        "name": NAME,
        "headline": HEADLINE,
        "discount_pct": DISCOUNT_PCT,
        "terms": TERMS,
        "ends_on": ENDS_ON,
        "ends_on_pretty": pretty_end_date(),
        "days_left": days_left(),
        "applies_to_everything": APPLIES_TO_EVERYTHING,
    }
