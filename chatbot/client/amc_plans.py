"""
amc_plans.py — the renewal price list.

The agent is not allowed to invent a price. This file is the only place a rupee
figure may come from, so that quoting one is a lookup rather than a guess.

⚠️  THE FIGURES BELOW ARE PLACEHOLDERS taken from the campaign mockup. They are NOT
    Eureka Forbes card rates. Replace every one with the real published price before
    this is pointed at a customer, and keep `PRICES_CONFIRMED = False` until someone
    who owns pricing has signed them off — while it is False the agent will describe
    plans without quoting any amount.

Keys are the CRM's own option-set labels (ef_productasset.ef_productcategory), so a
category that exists in Dataverse and not here fails loudly rather than silently
falling back to water-purifier pricing.
"""

import os

from client import offers
from typing import Any, Dict, List, Optional

# Flip to True only when the amounts below are the real, signed-off rates.
PRICES_CONFIRMED = os.getenv("AMC_PRICES_CONFIRMED", "false").lower() in ("1", "true", "yes")

# Read from the campaign, never from a second env var. Two independent discount
# settings is how a customer gets quoted 10% off in the price list while the
# template that brought them here promised 20%.
OFFER_PCT = offers.DISCOUNT_PCT

# category -> list of plans, cheapest first.
#   code     : stable id, used in logs and CRM notes
#   label    : what the customer hears
#   months   : contract length
#   type     : AMC | CMC  (matches ef_servicecontract.ef_contracttype)
#   tier     : Basic | Premium  (matches ef_contracttier)
#   price    : list price in rupees, before any discount
PLANS: Dict[str, List[Dict[str, Any]]] = {
    "WaterPurifier": [
        {"code": "wp_1yr_amc", "label": "1-year AMC",  "months": 12, "type": "AMC", "tier": "Basic",   "price": 2499},
        {"code": "wp_2yr_amc", "label": "2-year AMC",  "months": 24, "type": "AMC", "tier": "Basic",   "price": 4499},
        {"code": "wp_2yr_cmc", "label": "2-year CMC",  "months": 24, "type": "CMC", "tier": "Premium", "price": 6999},
    ],
    "AirPurifier": [
        {"code": "ap_1yr_amc", "label": "1-year AMC",  "months": 12, "type": "AMC", "tier": "Basic",   "price": 1999},
        {"code": "ap_2yr_amc", "label": "2-year AMC",  "months": 24, "type": "AMC", "tier": "Basic",   "price": 3599},
    ],
    "VacuumCleaner": [
        {"code": "vc_1yr_amc", "label": "1-year AMC",  "months": 12, "type": "AMC", "tier": "Basic",   "price": 1499},
        {"code": "vc_2yr_amc", "label": "2-year AMC",  "months": 24, "type": "AMC", "tier": "Basic",   "price": 2699},
    ],
}

# What separates an AMC from a CMC, in the customer's terms. The agent explains the
# difference from here rather than improvising it.
WHAT_IS_COVERED = {
    "AMC": "scheduled service visits, unlimited breakdown calls, and labour",
    "CMC": "everything in the AMC plus all spare parts and filter cartridges",
}


def categories() -> List[str]:
    return sorted(PLANS)


def for_category(category: Optional[str]) -> List[Dict[str, Any]]:
    """Plans for a CRM product category, with the standing offer applied.

    Returns [] for an unknown or missing category — the caller must treat that as
    "cannot quote", never as "quote the default".
    """
    if not category:
        return []
    plans = PLANS.get(category)
    if not plans:
        return []

    out = []
    for plan in plans:
        price = plan["price"]
        offer = round(price * (100 - OFFER_PCT) / 100)
        out.append({
            **plan,
            "offer_pct": OFFER_PCT,
            "offer_price": offer,
            "you_save": price - offer,
            "covers": WHAT_IS_COVERED.get(plan["type"], ""),
        })
    return out
