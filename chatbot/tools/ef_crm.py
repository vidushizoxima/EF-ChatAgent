"""
ef_crm.py — Eureka Forbes business logic over Dataverse.

Everything here is plain async functions returning plain dicts: no LLM concepts,
no LangChain. The @tool wrappers in ef_tools.py and the background interaction
logger both call into this module, so the CRM rules live in exactly one place.

Entity-set names and option-set values come from client/ef_schema.py — never
hard-coded.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from client import ef_schema as S
from client.dataverse_client import dataverse

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

RENEWAL_WINDOW_DAYS = 183      # "expiring in the next 6 months"
CONSUMABLE_WINDOW_DAYS = 60    # filter/consumable due soon


# ==================== PHONE ====================

def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


def normalise_phone(phone: str) -> str:
    """Last 10 digits — the only part that is stable across formats."""
    d = digits(phone)
    return d[-10:] if len(d) >= 10 else d


def format_phone(phone: str) -> str:
    """Write numbers the way this org already stores them: '+91 98450 71284'."""
    ten = normalise_phone(phone)
    if len(ten) != 10:
        return phone
    return f"+91 {ten[:5]} {ten[5:]}"


def _phone_filter(phone: str) -> Optional[str]:
    """Server-side narrowing filter.

    The org stores both '+91 98450 71284' and '9893984982', so `ef_phone eq ...`
    matches almost nothing. The last five digits survive every separator style
    seen in the data, so filter on those and confirm exactly in Python.
    """
    ten = normalise_phone(phone)
    if len(ten) != 10:
        return None
    return f"contains(ef_phone,'{ten[-5:]}')"


def _same_phone(a: str, b: str) -> bool:
    return normalise_phone(a) == normalise_phone(b) and len(normalise_phone(a)) == 10


# ==================== IDENTITY ====================

CUSTOMER_SELECT = [
    "ef_customerid", "ef_fullname", "ef_phone", "ef_email", "ef_city", "ef_pincode",
    "ef_address", "ef_customernumber", "ef_customertype", "ef_status", "ef_branch",
    "ef_preferredchannel", "ef_firstpurchasedate", "ef_lastinteractiondate",
    "ef_totalinteractions", "ef_inboundcount", "ef_avgsentiment", "ef_escalationcount",
]
LEAD_SELECT = [
    "ef_leadid", "ef_fullname", "ef_phone", "ef_email", "ef_pincode",
    "ef_leadnumber", "ef_productinterest", "ef_source", "ef_status",
    "ef_qualificationscore", "ef_lastinteractiondate", "ef_totalinteractions",
]
PROSPECT_SELECT = [
    "ef_prospectid", "ef_fullname", "ef_phone", "ef_email", "ef_city", "ef_pincode",
    "ef_consentstatus", "ef_dndflag", "ef_status", "ef_source",
]


async def _search(entity: str, select: List[str], phone: str, top: int = 10) -> List[dict]:
    filt = _phone_filter(phone)
    if not filt:
        return []
    try:
        rows = await dataverse.query(S.apiset(entity), select=select, filter=filt, top=top)
    except Exception as e:
        logger.warning(f"CRM search failed on {entity}: {e}")
        return []
    return [r for r in rows if _same_phone(r.get("ef_phone", ""), phone)]


async def find_by_phone(phone: str) -> Dict[str, Any]:
    """Customer first, then lead, then prospect — highest-value identity wins."""
    for kind, entity, select in (
        ("customer", "ef_customer", CUSTOMER_SELECT),
        ("lead", "ef_lead", LEAD_SELECT),
        ("prospect", "ef_prospect", PROSPECT_SELECT),
    ):
        rows = await _search(entity, select, phone)
        if rows:
            # Most recently touched wins when a phone has several records
            rows.sort(key=lambda r: r.get("ef_lastinteractiondate") or "", reverse=True)
            record = rows[0]
            return {
                "type": kind,
                "entity": entity,
                "record_id": record.get(S.pk(entity)),
                "record": record,
                "all_matches": len(rows),
            }
    return {"type": "unknown", "entity": None, "record_id": None, "record": None, "all_matches": 0}


# ==================== CUSTOMER 360 ====================

def _days_until(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        d = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (d.date() - datetime.now(timezone.utc).date()).days
    except (ValueError, TypeError):
        return None


async def get_customer_360(customer_id: str) -> Dict[str, Any]:
    """Assets, contracts and open cases for a customer, plus what is expiring.

    Renewal detection is done here in code rather than left to the model: a
    contract counts as renewable if it expires within six months, or the CRM
    already marks it Expiring or Lapsed.
    """
    filt = f"_ef_customer_value eq {customer_id}"

    async def safe(entity, select, extra_filter=None, order=None):
        try:
            return await dataverse.query(
                S.apiset(entity),
                select=select,
                filter=f"{filt} and {extra_filter}" if extra_filter else filt,
                order_by=order,
                top=50,
            )
        except Exception as e:
            logger.warning(f"360 fetch failed for {entity}: {e}")
            return []

    assets = await safe("ef_productasset", [
        "ef_productassetid", "ef_productname", "ef_productcategory", "ef_modelcode",
        "ef_serialnumber", "ef_installationdate", "ef_purchasedate", "ef_status",
        "ef_warrantyexpirydate", "ef_lastfilterchangedate", "ef_nextconsumabledate",
    ])
    contracts = await safe("ef_servicecontract", [
        "ef_servicecontractid", "ef_contractid", "ef_contracttype", "ef_contracttier",
        "ef_startdate", "ef_expirydate", "ef_status", "ef_contractvalue", "ef_lastrenewaldate",
        # the lookup's raw value must be selected explicitly, or the asset name is lost
        "_ef_asset_value",
    ])
    cases = await safe("ef_servicerequest", [
        "ef_servicerequestid", "ef_caseid", "ef_requesttype", "ef_category", "ef_status",
        "ef_priority", "ef_raisedon", "ef_visitdate", "ef_visitstatus", "ef_technicianname",
    ], order="ef_raisedon desc")

    asset_names = {a.get("ef_productassetid"): a.get("ef_productname") for a in assets}
    # Pricing is keyed on the CRM's own category label, so carry it through with the
    # renewal rather than trying to infer "this is a water purifier" from the name.
    asset_categories = {
        a.get("ef_productassetid"): S.label("ef_productasset", "ef_productcategory", a.get("ef_productcategory"))
        for a in assets
    }

    renewals = []
    for c in contracts:
        days = _days_until(c.get("ef_expirydate"))
        status_label = S.label("ef_servicecontract", "ef_status", c.get("ef_status"))
        expiring_soon = days is not None and 0 <= days <= RENEWAL_WINDOW_DAYS
        already_flagged = status_label in ("Expiring", "Lapsed")
        lapsed = days is not None and days < 0
        if expiring_soon or already_flagged or lapsed:
            renewals.append({
                "contract_record_id": c.get("ef_servicecontractid"),
                "contract_id": c.get("ef_contractid"),
                "type": S.label("ef_servicecontract", "ef_contracttype", c.get("ef_contracttype")),
                "tier": S.label("ef_servicecontract", "ef_contracttier", c.get("ef_contracttier")),
                "expiry_date": (c.get("ef_expirydate") or "")[:10],
                "days_left": days,
                "status": status_label,
                "value": c.get("ef_contractvalue"),
                "asset": asset_names.get(c.get("_ef_asset_value")),
                "asset_category": asset_categories.get(c.get("_ef_asset_value")),
                "asset_record_id": c.get("_ef_asset_value"),
            })
    renewals.sort(key=lambda r: r["days_left"] if r["days_left"] is not None else 9999)

    consumables_due = []
    for a in assets:
        days = _days_until(a.get("ef_nextconsumabledate"))
        if days is not None and days <= CONSUMABLE_WINDOW_DAYS:
            consumables_due.append({
                "asset": a.get("ef_productname"),
                "due_date": (a.get("ef_nextconsumabledate") or "")[:10],
                "days_left": days,
            })

    open_cases = [
        {
            "case_id": c.get("ef_caseid"),
            "type": S.label("ef_servicerequest", "ef_requesttype", c.get("ef_requesttype")),
            "category": c.get("ef_category"),
            "status": S.label("ef_servicerequest", "ef_status", c.get("ef_status")),
            "raised_on": (c.get("ef_raisedon") or "")[:10],
            "visit_date": (c.get("ef_visitdate") or "")[:16],
            "visit_status": S.label("ef_servicerequest", "ef_visitstatus", c.get("ef_visitstatus")),
        }
        for c in cases
        if S.label("ef_servicerequest", "ef_status", c.get("ef_status")) in ("Open", "InProgress")
    ]

    return {
        "assets": [
            {
                "asset_record_id": a.get("ef_productassetid"),
                "name": a.get("ef_productname"),
                "category": S.label("ef_productasset", "ef_productcategory", a.get("ef_productcategory")),
                "model_code": a.get("ef_modelcode"),
                "installed_on": (a.get("ef_installationdate") or "")[:10],
                "warranty_expiry": (a.get("ef_warrantyexpirydate") or "")[:10],
                "next_consumable_date": (a.get("ef_nextconsumabledate") or "")[:10],
                "status": S.label("ef_productasset", "ef_status", a.get("ef_status")),
            }
            for a in assets
        ],
        "contracts": [
            {
                "contract_id": c.get("ef_contractid"),
                "type": S.label("ef_servicecontract", "ef_contracttype", c.get("ef_contracttype")),
                "tier": S.label("ef_servicecontract", "ef_contracttier", c.get("ef_contracttier")),
                "expiry_date": (c.get("ef_expirydate") or "")[:10],
                "status": S.label("ef_servicecontract", "ef_status", c.get("ef_status")),
                "value": c.get("ef_contractvalue"),
            }
            for c in contracts
        ],
        "open_cases": open_cases,
        "renewals": renewals,
        "consumables_due": consumables_due,
    }


# ==================== WRITES ====================

CHANNEL_TO_CHOICE = {"whatsapp": "WhatsApp", "instagram": "MetaDM", "facebook": "MetaDM"}

# Words that appear in every product name and so identify nothing on their own.
_ASSET_STOPWORDS = {"aquaguard", "eureka", "forbes", "the", "my", "our", "water", "purifier", "and"}


def match_asset(text: str, assets: List[dict]) -> Optional[str]:
    """Pick the asset the customer is talking about, by name or model code.

    With one asset on file it is unambiguous. With several, a case bound to the
    wrong product is worse than one bound to none, so an unclear reference stays
    unbound.
    """
    if not assets:
        return None
    if len(assets) == 1:
        return assets[0].get("asset_record_id")

    words = {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}
    words -= _ASSET_STOPWORDS
    if not words:
        return None

    best, best_score = None, 0
    for asset in assets:
        haystack = f"{asset.get('name','')} {asset.get('model_code','')}".lower()
        hits = {w for w in words if w in haystack}
        if len(hits) > best_score:
            best, best_score = asset.get("asset_record_id"), len(hits)
        elif len(hits) == best_score and best_score > 0:
            best = None          # tie — ambiguous, bind nothing
    return best if best_score else None


def channel_choice(channel: str, column: str = "ef_channel", entity: str = "ef_interaction") -> int:
    return S.choice(entity, column, CHANNEL_TO_CHOICE.get((channel or "").lower(), "WhatsApp"))


def lead_source_choice(channel: str) -> int:
    return S.choice("ef_lead", "ef_source", CHANNEL_TO_CHOICE.get((channel or "").lower(), "WhatsApp"))


def qualification_score(name: str, phone: str, product_interest: str, city_or_pin: str, intent: str) -> float:
    """0–1 completeness/intent score. Deliberately simple and explainable:
    contactability is half of it, stated product and location are the rest."""
    score = 0.0
    if normalise_phone(phone) and len(normalise_phone(phone)) == 10:
        score += 0.35
    if name and len(name.strip()) > 2:
        score += 0.15
    if product_interest:
        score += 0.25
    if city_or_pin:
        score += 0.10
    if intent and intent.lower() not in ("unknown", "general", ""):
        score += 0.15
    return round(min(score, 1.0), 2)


async def create_lead(
    name: str,
    phone: str,
    channel: str,
    product_interest: str = "",
    email: str = "",
    pincode: str = "",
    intent: str = "",
    prospect_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an ef_lead. Binds to the prospect when this phone was a known prospect."""
    now = datetime.now(timezone.utc)
    payload = {
        "ef_fullname": (name or "Unknown").strip()[:200],
        "ef_phone": format_phone(phone)[:20],
        "ef_source": lead_source_choice(channel),
        "ef_status": S.choice("ef_lead", "ef_status", "New"),
        "ef_lastinteractiondate": S.datetime_value("ef_lead", "ef_lastinteractiondate", now),
        "ef_totalinteractions": 1,
        "ef_qualificationscore": qualification_score(name, phone, product_interest, pincode, intent),
    }
    if product_interest:
        payload["ef_productinterest"] = product_interest[:150]
    if email:
        payload["ef_email"] = email[:100]
    if pincode:
        payload["ef_pincode"] = str(pincode)[:10]
    if prospect_id:
        payload[S.bind_key("ef_lead", "ef_prospect")] = S.bind("ef_prospect", prospect_id)

    record = await dataverse.create(S.apiset("ef_lead"), payload, return_record=True)
    lead_id = record.get("ef_leadid") if isinstance(record, dict) else record
    lead_number = record.get("ef_leadnumber") if isinstance(record, dict) else None
    logger.info(f"🆕 Lead {lead_number or lead_id} created for {payload['ef_phone']} via {channel}")
    return {"status": "success", "lead_id": lead_id, "lead_number": lead_number, "payload": payload}


async def update_lead(lead_id: str, **fields) -> bool:
    """Patch a lead with whatever the conversation revealed."""
    payload = {}
    mapping = {
        "name": ("ef_fullname", 200),
        "email": ("ef_email", 100),
        "pincode": ("ef_pincode", 10),
        "product_interest": ("ef_productinterest", 150),
    }
    for key, (column, limit) in mapping.items():
        value = fields.get(key)
        if value:
            payload[column] = str(value)[:limit]
    if fields.get("status"):
        payload["ef_status"] = S.choice("ef_lead", "ef_status", fields["status"])
    if fields.get("qualification_score") is not None:
        payload["ef_qualificationscore"] = fields["qualification_score"]
    if fields.get("touch"):
        payload["ef_lastinteractiondate"] = S.datetime_value(
            "ef_lead", "ef_lastinteractiondate", datetime.now(timezone.utc)
        )
    if not payload:
        return False
    await dataverse.update(S.apiset("ef_lead"), lead_id, payload)
    return True


# ==================== SERVICE VISIT SLOTS ====================
#
# ef_servicerequest holds a single ef_visitdate timestamp — there is no slot or
# window field — so a booked slot is stored as its START time and the window is
# what we say to the customer. If the service team needs the window itself in the
# CRM, that wants a new column on the table.

# Service hours are 8 AM - 7 PM, Monday to Friday. Four windows rather than three,
# because an 11-hour day split three ways gives the customer a wait too vague to
# plan around.
SLOTS = {
    "morning":   {"label": "8 AM - 11 AM",  "start": 8,  "end": 11},
    "midday":    {"label": "11 AM - 2 PM",  "start": 11, "end": 14},
    "afternoon": {"label": "2 PM - 5 PM",   "start": 14, "end": 17},
    "evening":   {"label": "5 PM - 7 PM",   "start": 17, "end": 19},
}
SLOT_ALIASES = {
    "morning": "morning", "early": "morning", "8": "morning", "8am": "morning",
    "9": "morning", "9am": "morning", "10": "morning", "10am": "morning",
    "first half": "morning", "forenoon": "morning",
    "midday": "midday", "noon": "midday", "11": "midday", "11am": "midday",
    "12": "midday", "lunch": "midday", "before lunch": "midday",
    "afternoon": "afternoon", "2": "afternoon", "2pm": "afternoon",
    "3": "afternoon", "3pm": "afternoon", "post lunch": "afternoon",
    "evening": "evening", "late": "evening", "5": "evening", "5pm": "evening",
    "6": "evening", "6pm": "evening", "pm": "evening", "second half": "evening",
}

# Monday to Friday only — Saturday and Sunday are closed.
# Same-day booking closes at this hour IST.
CLOSED_WEEKDAYS = {5, 6}       # Monday=0 … Saturday=5, Sunday=6
SAME_DAY_CUTOFF_HOUR = 15
BOOKING_HORIZON_DAYS = 30


def _today_ist() -> datetime:
    return datetime.now(IST)


def resolve_slot(slot: str) -> Optional[str]:
    """Map whatever the customer said to one of our three windows."""
    key = (slot or "").strip().lower()
    if key in SLOTS:
        return key
    if key in SLOT_ALIASES:
        return SLOT_ALIASES[key]
    for alias, canonical in SLOT_ALIASES.items():
        if alias in key:
            return canonical
    return None


def parse_visit_date(date_str: str) -> Optional[datetime]:
    """Accept an ISO date, a common Indian format, or a relative day."""
    raw = (date_str or "").strip().lower()
    today = _today_ist()

    if raw in ("today", "aaj"):
        return today
    if raw in ("tomorrow", "kal", "tmrw"):
        return today + timedelta(days=1)
    if raw in ("day after tomorrow", "day after", "parso"):
        return today + timedelta(days=2)

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for index, day in enumerate(weekdays):
        if day in raw:
            ahead = (index - today.weekday()) % 7
            return today + timedelta(days=ahead or 7)

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def next_available_slots(count: int = 4) -> List[Dict[str, str]]:
    """The next few bookable windows, for the agent to offer."""
    out = []
    day = _today_ist()
    for _ in range(BOOKING_HORIZON_DAYS):
        for key, slot in SLOTS.items():
            if len(out) >= count:
                return out
            if day.weekday() in CLOSED_WEEKDAYS:
                break
            is_today = day.date() == _today_ist().date()
            if is_today and (_today_ist().hour >= SAME_DAY_CUTOFF_HOUR or slot["start"] <= _today_ist().hour):
                continue
            out.append({
                "date": day.strftime("%Y-%m-%d"),
                "day": day.strftime("%A"),
                "slot": key,
                "window": slot["label"],
            })
        day += timedelta(days=1)
    return out


def resolve_visit(date_str: str, slot: str) -> Dict[str, Any]:
    """Turn "tomorrow" + "morning" into a concrete, bookable UTC timestamp.

    Returns either {"ok": True, ...} or {"ok": False, "reason": ..., "alternatives": [...]}
    so the agent can offer something else instead of guessing.
    """
    slot_key = resolve_slot(slot)
    if not slot_key:
        return {
            "ok": False,
            "reason": f"'{slot}' is not one of our visit windows.",
            "alternatives": next_available_slots(),
        }

    day = parse_visit_date(date_str)
    if not day:
        return {
            "ok": False,
            "reason": f"Could not understand the date '{date_str}'.",
            "alternatives": next_available_slots(),
        }

    window = SLOTS[slot_key]
    when = day.replace(hour=window["start"], minute=0, second=0, microsecond=0, tzinfo=IST)
    now = _today_ist()

    if when.date() < now.date():
        return {"ok": False, "reason": "That date has already passed.",
                "alternatives": next_available_slots()}
    if when.weekday() in CLOSED_WEEKDAYS:
        return {"ok": False, "reason": "We do not run service visits on Sundays.",
                "alternatives": next_available_slots()}
    if (when.date() - now.date()).days > BOOKING_HORIZON_DAYS:
        return {"ok": False, "reason": f"We only book up to {BOOKING_HORIZON_DAYS} days ahead.",
                "alternatives": next_available_slots()}
    if when.date() == now.date() and (now.hour >= SAME_DAY_CUTOFF_HOUR or window["start"] <= now.hour):
        return {"ok": False, "reason": "That slot is too soon for a same-day visit.",
                "alternatives": next_available_slots()}

    return {
        "ok": True,
        "slot": slot_key,
        "window": window["label"],
        "date": when.strftime("%Y-%m-%d"),
        "day": when.strftime("%A"),
        "spoken": f"{when.strftime('%A, %d %B')} between {window['label']}",
        "visit_datetime": when.astimezone(timezone.utc),
    }


async def schedule_visit(service_request_id: str, visit_datetime: datetime) -> bool:
    """Patch a visit onto an existing case (also used to reschedule)."""
    payload = {
        "ef_visitdate": S.datetime_value("ef_servicerequest", "ef_visitdate", visit_datetime),
        "ef_visitstatus": S.choice("ef_servicerequest", "ef_visitstatus", "Scheduled"),
    }
    await dataverse.update(S.apiset("ef_servicerequest"), service_request_id, payload)
    logger.info(f"📅 Visit scheduled on {service_request_id} for {payload['ef_visitdate']}")
    return True


async def get_service_request(service_request_id: str) -> Optional[dict]:
    rows = await dataverse.query(
        S.apiset("ef_servicerequest"),
        select=["ef_caseid", "ef_status", "ef_visitdate", "ef_visitstatus", "ef_category", "ef_technicianname"],
        filter=f"ef_servicerequestid eq {service_request_id}",
        top=1,
    )
    return rows[0] if rows else None


async def ensure_customer(name: str, phone: str, lead_id: Optional[str] = None) -> Dict[str, Any]:
    """Find or create the ef_customer record a service request has to hang off.

    `ef_servicerequest` binds only to `ef_customer` — there is no lead lookup — so a
    new caller with a broken appliance cannot have a case raised at all until they
    exist as a customer. Someone booking an engineer is a customer in every sense
    that matters here, so we create the record rather than turning them away.

    Idempotent: a matching phone returns the existing record instead of a duplicate.

    Refuses a blank or malformed number. `_phone_filter` returns nothing for one, so
    the search silently finds no match and we would create a fresh customer with
    ef_phone="" — a record the service team cannot act on and the next conversation
    cannot find, producing a second one. Better to fail loudly here.
    """
    if len(normalise_phone(phone)) != 10:
        raise ValueError(f"ensure_customer needs a 10-digit phone, got {phone!r}")

    existing = await _search("ef_customer", CUSTOMER_SELECT, phone)
    if existing:
        record = existing[0]
        return {"status": "found", "customer_id": record.get(S.pk("ef_customer")), "record": record}

    payload = {
        "ef_fullname": (name or "WhatsApp customer")[:100],
        "ef_phone": format_phone(phone),
        "ef_status": S.choice("ef_customer", "ef_status", "Active"),
        "ef_customertype": S.choice("ef_customer", "ef_customertype", "Customer"),
        "ef_preferredchannel": S.choice("ef_customer", "ef_preferredchannel", "WhatsApp"),
        "ef_lastinteractiondate": S.datetime_value(
            "ef_customer", "ef_lastinteractiondate", datetime.now(timezone.utc)
        ),
    }
    record = await dataverse.create(S.apiset("ef_customer"), payload, return_record=True)
    customer_id = record.get("ef_customerid") if isinstance(record, dict) else record
    number = record.get("ef_customernumber") if isinstance(record, dict) else None
    logger.info(f"👤 Created customer {number or customer_id} for {phone} (from WhatsApp)")

    if lead_id:
        try:
            await update_lead(lead_id, status="Converted")
        except Exception as e:
            logger.warning(f"Could not mark lead {lead_id} converted: {e}")

    return {"status": "created", "customer_id": customer_id, "customer_number": number}


async def create_service_request(
    customer_id: str,
    request_type: str,
    category: str,
    priority: str = "Medium",
    asset_id: Optional[str] = None,
    visit_datetime: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Raise an ef_servicerequest against a customer (AMC renewal, complaint, query)."""
    payload = {
        S.bind_key("ef_servicerequest", "ef_customer"): S.bind("ef_customer", customer_id),
        "ef_requesttype": S.choice("ef_servicerequest", "ef_requesttype", request_type),
        "ef_status": S.choice("ef_servicerequest", "ef_status", "Open"),
        "ef_priority": S.choice("ef_servicerequest", "ef_priority", priority),
        "ef_category": (category or request_type)[:80],
        "ef_raisedon": S.datetime_value(
            "ef_servicerequest", "ef_raisedon", datetime.now(timezone.utc)
        ),
    }
    if asset_id:
        payload[S.bind_key("ef_servicerequest", "ef_asset")] = S.bind("ef_productasset", asset_id)
    if visit_datetime:
        payload["ef_visitdate"] = S.datetime_value("ef_servicerequest", "ef_visitdate", visit_datetime)
        payload["ef_visitstatus"] = S.choice("ef_servicerequest", "ef_visitstatus", "Scheduled")
    record = await dataverse.create(S.apiset("ef_servicerequest"), payload, return_record=True)
    case_guid = record.get("ef_servicerequestid") if isinstance(record, dict) else record
    case_number = record.get("ef_caseid") if isinstance(record, dict) else None
    logger.info(f"🎫 Service request {case_number or case_guid} ({request_type}) for customer {customer_id}")
    return {"status": "success", "service_request_id": case_guid, "case_number": case_number}


async def log_interaction(
    *,
    channel: str,
    summary: str,
    intent: str = "",
    sentiment: Optional[float] = None,
    disposition: str = "Engaged",
    customer_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    prospect_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    responded_at: Optional[datetime] = None,
    provider_message_id: str = "",
    transcript_ref: str = "",
    escalated: bool = False,
    handled_by: str = "Asha",
    cost_amount: float = 0.0,
) -> Dict[str, Any]:
    """Write one ef_interaction row covering a whole conversation."""
    payload = {
        "ef_timestamp": S.datetime_value(
            "ef_interaction", "ef_timestamp", started_at or datetime.now(timezone.utc)
        ),
        "ef_channel": channel_choice(channel),
        "ef_interactiontype": S.choice("ef_interaction", "ef_interactiontype", "Message"),
        "ef_direction": S.choice("ef_interaction", "ef_direction", "Inbound"),
        "ef_status": S.choice("ef_interaction", "ef_status", "Responded"),
        "ef_disposition": S.choice("ef_interaction", "ef_disposition", disposition),
        "ef_handledbytype": S.choice("ef_interaction", "ef_handledbytype", "AIAgent"),
        "ef_handledbyname": handled_by[:120],
        "ef_messagebody": (summary or "")[:2000],
        "ef_escalatedflag": bool(escalated),
        "ef_costamount": round(float(cost_amount or 0.0), 2),
    }
    if intent:
        payload["ef_intentdetected"] = intent[:120]
    if sentiment is not None:
        payload["ef_sentimentscore"] = max(-1.0, min(1.0, round(float(sentiment), 2)))
    if responded_at:
        payload["ef_respondedon"] = S.datetime_value("ef_interaction", "ef_respondedon", responded_at)
    if provider_message_id:
        payload["ef_providermessageid"] = provider_message_id[:200]
    if transcript_ref:
        payload["ef_transcriptref"] = transcript_ref[:250]

    if customer_id:
        payload[S.bind_key("ef_interaction", "ef_customer")] = S.bind("ef_customer", customer_id)
    elif lead_id:
        payload[S.bind_key("ef_interaction", "ef_lead")] = S.bind("ef_lead", lead_id)
    elif prospect_id:
        payload[S.bind_key("ef_interaction", "ef_prospect")] = S.bind("ef_prospect", prospect_id)

    interaction_id = await dataverse.create(S.apiset("ef_interaction"), payload)
    logger.info(
        f"📝 Interaction {interaction_id} logged | channel={channel} | "
        f"disposition={disposition} | ref={transcript_ref}"
    )
    return {"status": "success", "interaction_id": interaction_id, "payload": payload}


async def touch_customer(customer_id: str, sentiment: Optional[float] = None, escalated: bool = False):
    """Roll the engagement counters forward after an interaction."""
    try:
        rows = await dataverse.query(
            S.apiset("ef_customer"),
            select=["ef_totalinteractions", "ef_inboundcount", "ef_avgsentiment", "ef_escalationcount"],
            filter=f"ef_customerid eq {customer_id}",
            top=1,
        )
        current = rows[0] if rows else {}
    except Exception as e:
        logger.warning(f"Could not read customer counters: {e}")
        current = {}

    total = int(current.get("ef_totalinteractions") or 0)
    now = datetime.now(timezone.utc)
    payload = {
        "ef_totalinteractions": total + 1,
        "ef_inboundcount": int(current.get("ef_inboundcount") or 0) + 1,
        # customer date fields are DateOnly here, unlike the lead's — the schema decides
        "ef_lastinteractiondate": S.datetime_value("ef_customer", "ef_lastinteractiondate", now),
        "ef_lastinbounddate": S.datetime_value("ef_customer", "ef_lastinbounddate", now),
        "ef_consecutivenonresponses": 0,   # they just replied to us
    }
    if sentiment is not None:
        previous = current.get("ef_avgsentiment")
        if previous is None:
            payload["ef_avgsentiment"] = round(float(sentiment), 2)
        else:
            # running mean over all interactions so far
            payload["ef_avgsentiment"] = round(
                (float(previous) * total + float(sentiment)) / (total + 1), 2
            )
    if escalated:
        payload["ef_escalationcount"] = int(current.get("ef_escalationcount") or 0) + 1

    await dataverse.update(S.apiset("ef_customer"), customer_id, payload)


async def touch_lead(lead_id: str, qualification_score_value: Optional[float] = None):
    """Counters for a lead, and New -> Working once they are actually talking."""
    try:
        rows = await dataverse.query(
            S.apiset("ef_lead"),
            select=["ef_totalinteractions", "ef_status"],
            filter=f"ef_leadid eq {lead_id}",
            top=1,
        )
        current = rows[0] if rows else {}
    except Exception as e:
        logger.warning(f"Could not read lead counters: {e}")
        current = {}

    payload = {
        "ef_totalinteractions": int(current.get("ef_totalinteractions") or 0) + 1,
        "ef_lastinteractiondate": S.datetime_value(
            "ef_lead", "ef_lastinteractiondate", datetime.now(timezone.utc)
        ),
    }
    if S.label("ef_lead", "ef_status", current.get("ef_status")) == "New":
        payload["ef_status"] = S.choice("ef_lead", "ef_status", "Working")
    if qualification_score_value is not None:
        payload["ef_qualificationscore"] = qualification_score_value

    await dataverse.update(S.apiset("ef_lead"), lead_id, payload)
