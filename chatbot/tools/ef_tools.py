"""
ef_tools.py — the tools the agent may call.

Thin wrappers over tools/ef_crm.py. Each one:
  - takes only what the model can actually know from the conversation
  - receives `session_id` by injection (never from the model)
  - returns a JSON string with a "status" key and never raises at the model
  - writes what it learned back onto the session, so the middleware can tell the
    model "you already know this" instead of the model asking twice

The identity flow is deliberately one tool, not three: identify_customer resolves
the person AND returns their full 360 plus anything expiring, so a returning
customer costs one round trip instead of four.
"""

import json
import logging
from typing import Optional

from langchain_core.tools import tool

from client import amc_plans
from client import knowledge
from client import offers
from client import whatsapp_connection as wa
from client.config import SUPPORT_NUMBER
from client.store import SessionStore
from tools import ef_crm

logger = logging.getLogger(__name__)


def _session(session_id: Optional[str]) -> Optional[SessionStore]:
    return SessionStore(session_id) if session_id else None


def _fail(message: str, **extra) -> str:
    return json.dumps({"status": "error", "message": message, **extra})


@tool
async def identify_customer(phone: str, name: str = "", session_id: Optional[str] = None) -> str:
    """Look the person up in the Eureka Forbes CRM by phone number.

    Call this ONCE, as soon as you have their phone number. It searches customers
    first, then leads, then prospects, and for an existing customer it also returns
    their products, service contracts, open complaints, and anything expiring soon.

    Args:
        phone: their phone number, any format.
        name: their name if they have given it (helps when creating a lead later).

    Returns:
        JSON with status (found/not_found), who they are, and for customers their
        full history plus a `renewals` list of contracts expiring within 6 months.
    """
    store = _session(session_id)
    if len(ef_crm.normalise_phone(phone)) != 10:
        return _fail("That does not look like a 10-digit Indian phone number. Ask them to confirm it.")

    if store:
        store.sadd("checked_phones", ef_crm.normalise_phone(phone))

    try:
        match = await ef_crm.find_by_phone(phone)
    except Exception as e:
        logger.error(f"identify_customer failed: {e}", exc_info=True)
        return _fail("The CRM lookup failed. Continue the conversation and collect their details.")

    if match["type"] == "unknown":
        if store:
            store.set("crm_type", "unknown")
            store.update_user_info({"phone": ef_crm.format_phone(phone), "name": name})
        return json.dumps({
            "status": "not_found",
            "phone": ef_crm.format_phone(phone),
            "message": "No customer, lead or prospect with this number. Collect their details and call create_lead.",
        })

    record = match["record"]
    kind = match["type"]
    result = {
        "status": "found",
        "type": kind,
        "record_id": match["record_id"],
        "name": record.get("ef_fullname") or name,
        "phone": record.get("ef_phone"),
        "email": record.get("ef_email") or "",
        "city": record.get("ef_city") or "",
        "pincode": record.get("ef_pincode") or "",
    }

    if kind == "customer":
        try:
            three_sixty = await ef_crm.get_customer_360(match["record_id"])
        except Exception as e:
            logger.warning(f"360 fetch failed: {e}")
            three_sixty = {}
        result.update(three_sixty)
        result["customer_number"] = record.get("ef_customernumber")

        renewals = three_sixty.get("renewals") or []
        if renewals:
            r = renewals[0]
            if r["days_left"] is not None and r["days_left"] < 0:
                result["renewal_hint"] = (
                    f"Their {r['type']} {r['contract_id']} for {r['asset'] or 'their product'} "
                    f"LAPSED on {r['expiry_date']}. Offer to get it reinstated."
                )
            else:
                result["renewal_hint"] = (
                    f"Their {r['type']} {r['contract_id']} for {r['asset'] or 'their product'} "
                    f"expires on {r['expiry_date']} ({r['days_left']} days away). "
                    f"Mention it naturally and offer to start the renewal."
                )
        if three_sixty.get("consumables_due"):
            c = three_sixty["consumables_due"][0]
            result["consumable_hint"] = (
                f"Filter/consumable for {c['asset']} is due on {c['due_date']} ({c['days_left']} days)."
            )

    elif kind == "lead":
        result["lead_number"] = record.get("ef_leadnumber")
        result["product_interest"] = record.get("ef_productinterest") or ""
        result["message"] = "Existing lead — do NOT create another one. Continue where they left off."

    elif kind == "prospect":
        result["message"] = (
            "Known prospect, not yet a lead. If they show interest, call create_lead — "
            "it will link back to this prospect record."
        )

    if store:
        store.set("crm_type", kind)
        store.set("crm_record_id", match["record_id"] or "")
        store.set_lead_id(match["record_id"] or "")
        store.set_existing_lead_data(result)
        store.update_user_info({
            "name": result["name"],
            "phone": result["phone"],
            "email": result["email"],
            "city": result["city"],
            "crm_type": kind,
        })
        if kind == "customer" and result.get("renewals"):
            store.set("pending_renewal", result["renewals"][0])

    logger.info(f"🔎 identify_customer: {phone} → {kind} {match['record_id']}")
    return json.dumps(result, default=str)


@tool
async def create_lead(
    name: str,
    phone: str,
    product_interest: str = "",
    email: str = "",
    pincode: str = "",
    intent: str = "",
    session_id: Optional[str] = None,
) -> str:
    """Create a new lead in the CRM for someone with no existing record.

    Call this ONLY after identify_customer returned not_found, and only once you
    have at least their name and phone number.

    Args:
        name: their full name as they gave it.
        product_interest: what they are asking about, e.g. "Aquaguard Magna HD RO+UV"
            or "AMC renewal" — free text, keep it short.
        email: if offered.
        pincode: if offered.
        intent: one-word intent, e.g. new_purchase, service_complaint, amc_enquiry.
    """
    store = _session(session_id)

    if store and store.get("crm_record_id"):
        existing = store.get("crm_record_id")
        return json.dumps({
            "status": "already_exists",
            "record_id": existing,
            "message": "This person already has a CRM record. Do not create another — just continue.",
        })

    if len(ef_crm.normalise_phone(phone)) != 10:
        return _fail("A valid 10-digit phone number is required before a lead can be created.")
    if not name or len(name.strip()) < 2:
        return _fail("Ask for their name before creating the lead.")

    channel = "whatsapp"
    prospect_id = None
    if store:
        info = store.get_user_info() or {}
        channel = info.get("source") or "whatsapp"
        if store.get("crm_type") == "prospect":
            prospect_id = store.get("crm_record_id")

    try:
        result = await ef_crm.create_lead(
            name=name, phone=phone, channel=channel, product_interest=product_interest,
            email=email, pincode=pincode, intent=intent, prospect_id=prospect_id,
        )
    except Exception as e:
        logger.error(f"create_lead failed: {e}", exc_info=True)
        return _fail("Could not create the lead right now. Carry on helping them; it will be captured.")

    if store:
        store.set("crm_type", "lead")
        store.set("crm_record_id", result["lead_id"])
        store.set_lead_id(result["lead_id"])
        store.set("tool_done:create_lead", "1")
        store.update_user_info({
            "name": name, "phone": ef_crm.format_phone(phone),
            "email": email, "pincode": pincode, "crm_type": "lead",
        })
        if product_interest:
            store.set("product_interest", product_interest)
        if intent:
            store.set("intent", intent)

    return json.dumps({
        "status": "success",
        "lead_id": result["lead_id"],
        "message": "Lead created. Do not mention record ids or CRM internals to the customer.",
    })


@tool
async def start_amc_renewal(
    contract_id: str = "",
    notes: str = "",
    session_id: Optional[str] = None,
) -> str:
    """Start an AMC/CMC renewal for an existing customer who has said yes.

    Call this only when the customer has actually agreed to renew or asked to be
    contacted about it. Creates a renewal lead for sales AND a service request for
    the service team, both linked to their record.

    Args:
        contract_id: the contract they are renewing, e.g. "AMC-000502". Optional —
            defaults to the one that came back as expiring.
        notes: anything they said about it, e.g. "wants premium tier this time".
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    if store.exists("tool_done:start_amc_renewal"):
        return json.dumps({
            "status": "already_done",
            "message": "Renewal already started in this conversation. Do not repeat it.",
        })

    # A renewal needs a contract, which a lead does not have — this one genuinely
    # is customers only.
    if store.get("crm_type") != "customer":
        return _fail("Renewals apply to existing customers only. Use create_lead instead.")

    customer_id = store.get("crm_record_id")
    info = store.get_user_info() or {}
    renewal = store.get_json("pending_renewal") or {}
    contract = contract_id or renewal.get("contract_id") or ""
    asset = renewal.get("asset") or ""
    channel = info.get("source") or "whatsapp"

    product_interest = f"{renewal.get('type', 'AMC')} renewal · {asset or contract or 'existing contract'}"[:150]

    outcome = {"status": "success"}
    try:
        lead = await ef_crm.create_lead(
            name=info.get("name") or "Customer",
            phone=info.get("phone") or "",
            channel=channel,
            product_interest=product_interest,
            email=info.get("email", ""),
            intent="amc_renewal",
        )
        outcome["renewal_lead_id"] = lead["lead_id"]
    except Exception as e:
        logger.error(f"Renewal lead creation failed: {e}")
        outcome["renewal_lead_id"] = None

    try:
        case = await ef_crm.create_service_request(
            customer_id=customer_id,
            request_type="AMCRequest",
            category=f"AMC renewal · {contract or asset or 'contract'}"[:80],
            priority="Medium",
            asset_id=renewal.get("asset_record_id"),
        )
        outcome["service_request_id"] = case["service_request_id"]
    except Exception as e:
        logger.error(f"Renewal service request failed: {e}")
        outcome["service_request_id"] = None

    if not outcome["renewal_lead_id"] and not outcome["service_request_id"]:
        return _fail(
            f"Could not log the renewal. Ask them to reach us on {SUPPORT_NUMBER} and we will pick it up."
        )

    store.set("tool_done:start_amc_renewal", "1")
    store.set("renewal_started", contract or "yes")
    # Keep the ids on the session: the interaction logger and anyone debugging a
    # conversation should be able to see exactly what was created.
    if outcome.get("renewal_lead_id"):
        store.set("renewal_lead_id", outcome["renewal_lead_id"])
    if outcome.get("service_request_id"):
        store.set("service_request_id", outcome["service_request_id"])
    store.set("disposition_hint", "ConvertedRenewal")
    if notes:
        store.set("renewal_notes", notes[:500])

    outcome["message"] = (
        "Renewal logged. Tell them our team will reach out to confirm the plan and price. "
        "Do not quote a renewal amount yourself."
    )
    return json.dumps(outcome)


# Reason codes. Free text here would make the CRM unqueryable within a month —
# "too costly" / "price high" / "expensive" are the same objection and should
# aggregate as one.
RENEWAL_OBJECTIONS = {
    "price": "thinks the renewal costs too much",
    "renewing_locally": "using a local technician instead",
    "product_unused": "not using the appliance any more",
    "product_sold": "sold or disposed of the appliance",
    "service_unhappy": "unhappy with past service",
    "needs_time": "wants to think about it",
    "spouse_decision": "someone else in the household decides",
    "other": "anything that does not fit the above",
}

RENEWAL_OUTCOMES = {
    "declined": "Declined",
    "considering": "Interested",
    "callback": "CallbackRequested",
    "lost": "Lost",
}


@tool
async def log_renewal_outcome(
    outcome: str,
    objection: str = "other",
    notes: str = "",
    session_id: Optional[str] = None,
) -> str:
    """Record how a renewal conversation ended when the customer did NOT agree to renew.

    Call this once, at the point the outcome is clear — they said no, they want to
    think, they asked for a call back, or they are going elsewhere. Do NOT call it
    when they agree to renew: use start_amc_renewal for that.

    Never announce this to the customer and never ask them to pick a reason code —
    infer it from what they already said.

    Args:
        outcome: one of "declined" (a clear no), "considering" (wants time but open),
            "callback" (asked for a human to call), "lost" (renewing elsewhere or
            no longer has the product).
        objection: why, as one of: price, renewing_locally, product_unused,
            product_sold, service_unhappy, needs_time, spouse_decision, other.
        notes: their own words, briefly — what they actually said.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    if store.exists("tool_done:log_renewal_outcome"):
        return json.dumps({
            "status": "already_done",
            "message": "Outcome already recorded for this conversation.",
        })

    outcome = (outcome or "").strip().lower()
    if outcome not in RENEWAL_OUTCOMES:
        return _fail(
            f"'{outcome}' is not a valid outcome.",
            valid=sorted(RENEWAL_OUTCOMES),
        )

    objection = (objection or "other").strip().lower()
    if objection not in RENEWAL_OBJECTIONS:
        objection = "other"

    disposition = RENEWAL_OUTCOMES[outcome]

    store.set("tool_done:log_renewal_outcome", "1")
    store.set("renewal_outcome", outcome)
    store.set("renewal_objection", objection)
    # The interaction logger prefers a hint over the classifier's reading, so this
    # is what lands in ef_disposition.
    store.set("disposition_hint", disposition)
    if notes:
        store.set("renewal_outcome_notes", notes[:500])

    logger.info(
        f"📉 Renewal outcome '{outcome}' | objection={objection} | disposition={disposition}"
    )
    return json.dumps({
        "status": "success",
        "outcome": outcome,
        "objection": objection,
        "disposition": disposition,
        "message": (
            "Outcome recorded. Do not mention this to the customer. Keep helping them "
            "with anything else they need, and leave the door open on the renewal."
        ),
    })


@tool
async def get_renewal_plans(product_category: str = "", session_id: Optional[str] = None) -> str:
    """Fetch the renewal plans and prices for the customer's appliance.

    Call this before you discuss options or say any amount. It is the ONLY source of
    a rupee figure — never state a price that did not come back from this tool.

    Args:
        product_category: leave empty to use the appliance on their expiring
            contract. Only pass a value if they ask about a different appliance:
            WaterPurifier, AirPurifier or VacuumCleaner.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    category = product_category.strip()
    if not category:
        pending = store.get_json("pending_renewal") or {}
        category = pending.get("asset_category") or ""

    plans = amc_plans.for_category(category)
    if not plans:
        return json.dumps({
            "status": "no_plans",
            "category": category or "unknown",
            "known_categories": amc_plans.categories(),
            "message": (
                "No price list for this appliance. Do not guess a price or a plan. "
                "Tell them our team will confirm the options and the amount."
            ),
        })

    if not amc_plans.PRICES_CONFIRMED:
        # The figures exist but nobody has signed them off yet. Describe the shape of
        # the options without turning an unverified number into a quote.
        return json.dumps({
            "status": "plans_without_prices",
            "category": category,
            "plans": [
                {"code": p["code"], "label": p["label"], "type": p["type"], "covers": p["covers"]}
                for p in plans
            ],
            "offer_pct": amc_plans.OFFER_PCT,
            "message": (
                "Prices are NOT confirmed. You may name the plans and say what each "
                "covers, and you may mention the discount as a percentage. You must "
                "NOT state any rupee amount. Say our team will confirm the price."
            ),
        })

    return json.dumps({
        "status": "success",
        "category": category,
        "offer_pct": amc_plans.OFFER_PCT,
        "plans": plans,
        "message": (
            "Offer two or three of these in plain sentences, cheapest first, with what "
            "each covers. One question at a time. Quote only these amounts."
        ),
    })


@tool
async def escalate_to_human(reason: str, notes: str = "", session_id: Optional[str] = None) -> str:
    """Hand the conversation to a person.

    Call this when they ask to speak to someone, when they are clearly unhappy, or
    when they want something you cannot do. Tell them our team will call — do not
    promise a time.

    Args:
        reason: short, e.g. "asked for an agent", "unhappy with past service",
            "wants a custom plan".
        notes: anything the team should know before they call.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    if store.exists("tool_done:escalate_to_human"):
        return json.dumps({
            "status": "already_done",
            "message": "Already escalated in this conversation. Do not repeat it.",
        })

    store.set("tool_done:escalate_to_human", "1")
    store.set("escalated", "1")
    store.set("escalation_reason", reason[:200])
    if notes:
        store.set("escalation_notes", notes[:500])
    store.set("disposition_hint", "Escalated")

    logger.info(f"🙋 Escalated to human — {reason}")
    return json.dumps({
        "status": "success",
        "message": (
            "Escalation recorded; it reaches the team with the whole conversation. "
            "Tell them someone will call. Do not promise a time or a name. "
            f"If they would rather call us, the number is {SUPPORT_NUMBER}."
        ),
        "support_number": SUPPORT_NUMBER,
    })


@tool
async def register_purchase_interest(
    what_they_want: str = "",
    notes: str = "",
    session_id: Optional[str] = None,
) -> str:
    """Log that this person wants to buy something, so the sales team calls them.

    Call this the moment they show buying intent — asking about a new appliance, the
    offer, prices for something they do not own, or saying they want to purchase.
    Then tell them you have notified the team and they will call shortly.

    Do not ask which product they are interested in first. The offer covers
    everything, and making them choose before anyone calls loses people who have not
    decided yet.

    Args:
        what_they_want: their words, e.g. "water purifier for a 3BHK", or "" if they
            have not said.
        notes: anything useful for whoever calls — budget, timing, city.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    if store.exists("tool_done:register_purchase_interest"):
        return json.dumps({
            "status": "already_done",
            "message": "Sales team already notified in this conversation. Do not repeat it.",
        })

    info = store.get_user_info() or {}
    interest = (what_they_want or "").strip() or "Interested in purchase"
    crm_type = store.get("crm_type")
    record_id = store.get("crm_record_id")
    outcome = {"status": "success", "interest": interest}

    # Same Facebook/Instagram trap as raise_service_request: with no CRM record to
    # fall back on, the lead below is created from `info` alone, and on those
    # channels `info` carries a Meta profile name but no phone. "Our team will call
    # you shortly" then goes to a record with no number on it.
    if not record_id and len(ef_crm.normalise_phone(info.get("phone") or "")) != 10:
        return json.dumps({
            "status": "need_phone",
            "message": (
                "Ask for their 10-digit phone number first — the team cannot call them back "
                "without it, so do not promise that they will."
            ),
        })

    # A D365 Service Activity (serviceappointment) cannot be bound to ef_customer —
    # the custom EF tables are not valid regardingobjectid targets, and records do
    # not persist in this org because Service Scheduling is not provisioned. So the
    # work item goes on tables that DO link: a Query service request for a known
    # customer, the lead record otherwise.
    try:
        if crm_type == "customer" and record_id:
            created = await ef_crm.create_service_request(
                customer_id=record_id,
                request_type="Query",
                category=f"Contact customer - {interest}"[:80],
                priority="Medium",
            )
            outcome["service_request_id"] = created.get("service_request_id")
            outcome["case_number"] = created.get("case_number")
            store.set("service_request_id", created.get("service_request_id") or "")
        elif record_id:
            await ef_crm.update_lead(record_id, product_interest=interest[:100])
            outcome["lead_id"] = record_id
        else:
            created = await ef_crm.create_lead(
                name=info.get("name", "") or "WhatsApp enquiry",
                phone=info.get("phone", ""),
                product_interest=interest[:100],
                channel="whatsapp",
                intent="purchase_interest",
            )
            outcome["lead_id"] = created.get("lead_id")
            if created.get("lead_id"):
                store.set_lead_id(created["lead_id"])
                store.set("crm_record_id", created["lead_id"])
                store.set("crm_type", "lead")
    except Exception as e:
        logger.error(f"Could not register purchase interest: {e}", exc_info=True)
        return _fail(
            "Could not log it. Still tell them our team will call, and give them "
            f"{SUPPORT_NUMBER} as a backup."
        )

    store.set("tool_done:register_purchase_interest", "1")
    store.set("purchase_interest", interest[:200])
    if notes:
        store.set("purchase_notes", notes[:500])
    store.set("disposition_hint", "Qualified")

    logger.info(f"🛒 Purchase interest logged — {interest}")
    outcome["message"] = (
        "Logged. Tell them you have notified the team and they will call shortly. "
        "Do not promise a time. Offer to send the brochure if you have not already."
    )
    return json.dumps(outcome)


# Media ids live 30 days on Meta's side; re-upload a little before that.
_BROCHURE_NS = "wa:media"
_BROCHURE_TTL = 25 * 24 * 3600


async def _brochure_media_id(store: SessionStore, phone_number_id: str = "") -> Optional[str]:
    """The uploaded brochure's media id, uploading once and caching it.

    Ids are scoped to the number that uploaded them, so the cache key includes it —
    otherwise the test number's id would be sent from the production number and fail.
    """
    key = f"brochure:{phone_number_id or 'default'}"
    cached = store.get(key, namespace=_BROCHURE_NS)
    if cached:
        return cached
    media_id = await wa.upload_media(offers.BROCHURE_PATH, "application/pdf", phone_number_id or None)
    if media_id:
        store.set(key, media_id, ttl=_BROCHURE_TTL, namespace=_BROCHURE_NS)
    return media_id


@tool
async def send_offer_brochure(caption: str = "", session_id: Optional[str] = None) -> str:
    """Send the Eureka Forbes offer brochure as a PDF.

    Call this when they ask about products, prices, or the offer, or when they show
    any interest in buying. Send it once per conversation.

    Say something in your own words alongside it — the brochure is the attachment,
    not the reply. Never describe what is inside it: you have not read it, and
    inventing contents is worse than sending it bare.

    Args:
        caption: one short line to go with the file.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    if store.exists("tool_done:send_offer_brochure"):
        return json.dumps({
            "status": "already_done",
            "message": "Brochure already sent in this conversation. Do not send it again.",
        })

    info = store.get_user_info() or {}
    phone = info.get("phone") or ""
    if not phone:
        return _fail("No phone number on this session.")

    inbox = store.get("inbox_phone_number_id") or ""
    media_id = await _brochure_media_id(store, inbox)
    if not media_id:
        return _fail(
            "Could not attach the brochure. Carry on without it — tell them what the "
            "offer is in your own words and do not mention a failed attachment."
        )

    sent = await wa.send_document(
        phone,
        media_id,
        offers.BROCHURE_FILENAME,
        caption or f"Our current offers - valid until {offers.pretty_end_date()}",
        inbox or None,
    )
    if not sent:
        return _fail("The brochure did not send. Do not mention it; keep helping them.")

    store.set("tool_done:send_offer_brochure", "1")
    store.set("brochure_sent", "1")
    logger.info(f"📎 Offer brochure sent to {phone}")
    return json.dumps({
        "status": "success",
        "message": (
            "Brochure sent. Do not describe its contents. Ask what they are looking "
            "for, or answer whatever they asked next."
        ),
    })


@tool
async def lookup_knowledge(question: str, session_id: Optional[str] = None) -> str:
    """Look up a product, price, AMC or offer fact in the Eureka Forbes knowledge base.

    Call this BEFORE answering anything factual you are not certain of: what an AMC
    covers, what is charged extra, AMC versus CMC, which products the offer applies
    to, what a model costs, whether the offer can be combined with anything else.

    This is the only place product prices come from. If it returns nothing useful,
    say you will have the team confirm — never fill the gap with a guess.

    Args:
        question: what you need to know, in plain words, e.g. "what is included in
            the AMC", "best selling water purifier", "can the offer be combined".
    """
    hits = knowledge.search(question, limit=3)
    if not hits:
        return json.dumps({
            "status": "not_found",
            "message": (
                "Nothing in the knowledge base covers that. Do not invent an answer. "
                "Say our team will confirm the details."
            ),
        })

    return json.dumps({
        "status": "success",
        # Prices are spelled out in the source because it was written for a voice
        # agent reading them aloud. Converted here — a chat message wants Rs 9,499.
        "facts": [
            {"topic": h["heading"], "source": h["source"], "text": knowledge.to_digits(h["body"])}
            for h in hits
        ],
        "message": (
            "Answer from these facts only, in your own words, briefly. Write prices "
            "as digits. Do not read a section out verbatim and do not list everything "
            "you were given — take only what answers their question."
        ),
    })


@tool
async def raise_service_request(
    issue: str,
    category: str = "",
    priority: str = "Medium",
    visit_date: str = "",
    visit_slot: str = "",
    session_id: Optional[str] = None,
) -> str:
    """Log a complaint or service request for an existing customer.

    Use when a customer reports a fault, asks for a technician, or needs a filter
    or consumable changed — after you have understood what is actually wrong.

    If a technician needs to visit, ask them which day and time window suits them
    and pass it here. If they have not chosen yet, leave the visit fields empty —
    the reply will contain the next available slots for you to offer, and you then
    call `book_service_visit` once they pick one.

    Args:
        issue: what is wrong, in one line. Write it in ENGLISH even if they said it in
            Hindi or Hinglish — it lands in the case record, which the service team reads.
        category: short label, in English, e.g. "Water taste", "Not working",
            "Consumable replacement". This is the only free text on the case (80 chars),
            so make it say what is actually wrong.
        priority: Low, Medium or High. Use High only if there is no water at all or a leak.
        visit_date: the day they chose — "tomorrow", "Monday", or 2026-08-25.
        visit_slot: the window they chose — morning, afternoon or evening.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    # A service request can only bind to ef_customer — there is no lead lookup on
    # the table — so anyone not already a customer becomes one here. Refusing would
    # mean a new caller with a broken appliance can never get a case logged or an
    # engineer booked, which is the whole point of them messaging us.
    if store.get("crm_type") != "customer":
        info = store.get_user_info() or {}
        if not info.get("name"):
            return json.dumps({
                "status": "need_name",
                "message": "Ask for their name first — a service record cannot be created without one.",
            })
        # On Facebook and Instagram the webhook seeds `name` from the Meta profile
        # before a word is exchanged, so the name check above passes with no phone
        # behind it. ensure_customer would then write ef_phone="" — either Dataverse
        # rejects it and the customer is told the booking errored, or it succeeds and
        # we have promised an engineer to someone nobody can ring back. Ask instead.
        if len(ef_crm.normalise_phone(info.get("phone") or "")) != 10:
            return json.dumps({
                "status": "need_phone",
                "message": (
                    "Ask for their 10-digit phone number before logging this — the technician "
                    "has no way to reach them without it. Give them a reason: it is how the "
                    "engineer confirms the visit."
                ),
            })
        try:
            made = await ef_crm.ensure_customer(
                name=info.get("name", ""),
                phone=info.get("phone", ""),
                lead_id=store.get_lead_id(),
            )
        except Exception as e:
            logger.error(f"Could not create customer for service request: {e}", exc_info=True)
            return _fail(f"Could not set up their record. Give them {SUPPORT_NUMBER}.")
        store.set("crm_type", "customer")
        store.set("crm_record_id", made["customer_id"])
        store.set("customer_created_here", "1")

    customer_id = store.get("crm_record_id")
    lead_data = store.get_existing_lead_data() or {}
    assets = lead_data.get("assets") or []
    asset_id = ef_crm.match_asset(f"{issue} {category}", assets)

    # A slot given up front is booked with the case; a bad one must not lose the
    # complaint, so the case is still created and the agent re-offers the slots.
    visit = None
    slot_problem = None
    if visit_date or visit_slot:
        resolved = ef_crm.resolve_visit(visit_date, visit_slot)
        if resolved["ok"]:
            visit = resolved
        else:
            slot_problem = resolved

    try:
        case = await ef_crm.create_service_request(
            customer_id=customer_id,
            request_type="Complaint" if priority == "High" else "ServiceRequest",
            category=(category or issue)[:80],
            priority=priority if priority in ("Low", "Medium", "High") else "Medium",
            asset_id=asset_id,
            visit_datetime=visit["visit_datetime"] if visit else None,
        )
    except Exception as e:
        logger.error(f"raise_service_request failed: {e}", exc_info=True)
        return _fail(f"Could not log it right now. Give them {SUPPORT_NUMBER} as a fallback.")

    store.set("tool_done:raise_service_request", "1")
    store.set("disposition_hint", "Resolved")
    store.set("service_request_id", case["service_request_id"])
    if case.get("case_number"):
        store.set("case_number", case["case_number"])

    result = {
        "status": "success",
        "case_number": case.get("case_number"),
        "service_request_id": case["service_request_id"],
        "note": "Quote case_number to the customer. NEVER show service_request_id — it is an internal id.",
    }

    if visit:
        store.set("visit_booked", visit["spoken"])
        store.set("visit_datetime", visit["visit_datetime"].isoformat())
        result["visit"] = {"when": visit["spoken"], "status": "Scheduled"}
        result["message"] = (
            f"Complaint logged and the visit is booked for {visit['spoken']}. "
            f"Confirm that back to them, and tell them the technician will call before arriving."
        )
    elif slot_problem:
        result["visit_not_booked"] = slot_problem["reason"]
        result["available_slots"] = slot_problem["alternatives"]
        result["message"] = (
            f"Complaint logged, but the visit is NOT booked: {slot_problem['reason']} "
            f"Offer them the slots in available_slots, then call book_service_visit."
        )
    else:
        result["available_slots"] = ef_crm.next_available_slots()
        result["message"] = (
            "Complaint logged. If a technician needs to visit, offer them the slots in "
            "available_slots and call book_service_visit once they pick one."
        )
    return json.dumps(result)


@tool
async def book_service_visit(
    visit_date: str,
    visit_slot: str,
    session_id: Optional[str] = None,
) -> str:
    """Book (or move) the technician visit for the complaint logged in this chat.

    Call this once the customer has chosen a day and a time window. Visits run
    Monday to Saturday in three windows: morning (10 AM - 1 PM), afternoon
    (1 PM - 4 PM) and evening (4 PM - 7 PM). Same-day booking is not possible
    after 3 PM.

    If the day or window does not work, the reply lists the next available slots —
    offer those instead of inventing one.

    Args:
        visit_date: the day they chose — "tomorrow", "Monday", or 2026-08-25.
        visit_slot: the window they chose — morning, afternoon or evening.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    service_request_id = store.get("service_request_id")
    if not service_request_id:
        return json.dumps({
            "status": "no_case",
            "message": (
                "There is no service request in this conversation yet. "
                "Understand the problem and call raise_service_request first."
            ),
        })

    resolved = ef_crm.resolve_visit(visit_date, visit_slot)
    if not resolved["ok"]:
        return json.dumps({
            "status": "slot_unavailable",
            "reason": resolved["reason"],
            "available_slots": resolved["alternatives"],
            "message": f"{resolved['reason']} Offer them one of available_slots instead.",
        })

    try:
        await ef_crm.schedule_visit(service_request_id, resolved["visit_datetime"])
    except Exception as e:
        logger.error(f"book_service_visit failed: {e}", exc_info=True)
        return _fail(f"Could not book that slot. Ask them to call {SUPPORT_NUMBER} to confirm a time.")

    rescheduled = bool(store.get("visit_booked"))
    store.set("visit_booked", resolved["spoken"])
    store.set("visit_datetime", resolved["visit_datetime"].isoformat())
    store.set("disposition_hint", "ConvertedVisit")

    return json.dumps({
        "status": "success",
        "case_number": store.get("case_number"),
        "visit": {"when": resolved["spoken"], "status": "Scheduled", "rescheduled": rescheduled},
        "message": (
            f"Visit {'moved to' if rescheduled else 'booked for'} {resolved['spoken']}. "
            f"Confirm it back to them and say the technician will call before arriving."
        ),
    })


@tool
async def update_lead_details(
    product_interest: str = "",
    email: str = "",
    pincode: str = "",
    city: str = "",
    session_id: Optional[str] = None,
) -> str:
    """Save extra details a lead gives you AFTER their record was created.

    A lead is created as soon as you have a name and number, so anything they say
    later — their pincode, their email, the model they settled on — would otherwise
    never reach the CRM. Call this when you learn something new and concrete.
    Pass only the fields you actually learned.

    Args:
        product_interest: the product or model they are now focused on.
        email: their email address.
        pincode: their 6-digit PIN code.
        city: their city.
    """
    store = _session(session_id)
    if not store:
        return _fail("No session context available.")

    if store.get("crm_type") != "lead":
        return json.dumps({
            "status": "skipped",
            "message": "Only leads are updated this way. Nothing to do — carry on.",
        })

    lead_id = store.get("crm_record_id")
    if not lead_id:
        return _fail("No lead on this session yet.")

    fields = {
        "product_interest": product_interest.strip(),
        "email": email.strip(),
        "pincode": pincode.strip(),
    }
    fields = {k: v for k, v in fields.items() if v}
    if not fields:
        return json.dumps({"status": "skipped", "message": "Nothing new to save."})

    try:
        await ef_crm.update_lead(lead_id, touch=True, **fields)
    except Exception as e:
        logger.error(f"update_lead_details failed: {e}", exc_info=True)
        return _fail("Could not save that right now — it will be captured at the end of the chat.")

    store.update_user_info({k: v for k, v in
                            {"email": email, "pincode": pincode, "city": city}.items() if v})
    if product_interest:
        store.set("product_interest", product_interest)

    return json.dumps({
        "status": "success",
        "updated": sorted(fields) + (["city"] if city else []),
        "message": "Saved. Do not mention the CRM to the customer.",
    })
