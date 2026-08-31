#!/usr/bin/env python3
"""
demo.py — run the agent's flows in a terminal, with no dependency on Meta.

Same agent, same prompt, same tools, same Dataverse as a real WhatsApp message.
The ONLY thing missing is the WhatsApp transport — so an expired token, a dead
ngrok tunnel or a template still in review cannot spoil a demo.

    .venv/bin/python scripts/demo.py --list
    .venv/bin/python scripts/demo.py renewal
    .venv/bin/python scripts/demo.py newlead --slow
    .venv/bin/python scripts/demo.py --all

`--slow` types the conversation out at reading pace, which is what you want in
front of an audience. Ctrl-C stops it.

Every scenario runs against the real CRM, so it creates real records. Use
`scripts/cleanup_test_records.py` afterwards, or run with `--phone` set to a number
you are happy to see leads against.
"""

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chatbot"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import logging  # noqa: E402

logging.basicConfig(level=os.getenv("DEMO_LOG_LEVEL", "ERROR"),
                    format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "client", "agent", "tools"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

from agent import process_query                      # noqa: E402
from client import amc_reminders as reminders        # noqa: E402
from client import whatsapp_connection as wa         # noqa: E402
from client.store import SessionStore                # noqa: E402

CYAN, GREEN, GREY, YELLOW, BOLD, OFF = (
    "\033[36m", "\033[32m", "\033[90m", "\033[33m", "\033[1m", "\033[0m")

# A known customer in the demo org: Aditi, AMC-000502, Aquaguard Magna, expiring 31 Aug.
KNOWN_CUSTOMER = "919899643944"
# Any number that is not in Dataverse exercises the new-caller path.
NEW_CALLER = "919000000321"


SCENARIOS = {
    "offer": {
        "title": "Somebody asks what the offer is",
        "shows": "20% on everything, brochure, purchase interest logged for sales",
        "phone": NEW_CALLER,
        "name": "",
        "script": [
            "hi, are there any offers going on?",
            "Rohan",
            "which products does it cover?",
            "ok i'm interested, i want to buy one",
        ],
    },
    "newlead": {
        "title": "A new person with a broken purifier",
        "shows": "name captured, lead created, troubleshooting, case number, visit booked",
        "phone": NEW_CALLER,
        "name": "",
        "script": [
            "hi my water purifier is not giving water properly",
            "Meera",
            "i changed the filter maybe 8 months back",
            "still the same problem after checking",
            "friday morning works",
        ],
    },
    "renewal": {
        "title": "An existing customer replies to an AMC reminder",
        "shows": "CRM lookup, plan options, renewal logged against the contract",
        "phone": KNOWN_CUSTOMER,
        "name": "Vidushi",
        "reminder": ("amc_renewal_offer_7d_v2", "AMC-000502", 7),
        "script": [
            "what offer",
            "what are my options and how much",
            "2 year amc sounds good",
        ],
    },
    "objection": {
        "title": "The customer says a local technician is cheaper",
        "shows": "one honest counter, then the no is accepted and logged with a reason",
        "phone": KNOWN_CUSTOMER,
        "name": "Vidushi",
        "reminder": ("amc_renewal_offer_7d_v2", "AMC-000502", 7),
        "script": [
            "what offer",
            "i get it serviced from a local guy near me, much cheaper",
            "no i'll stick with him thanks",
        ],
    },
    "escalate": {
        "title": "An unhappy customer wants a human",
        "shows": "the agent stops selling and hands over, flagged in the CRM",
        "phone": KNOWN_CUSTOMER,
        "name": "Vidushi",
        "script": [
            "the last technician who came was rude and did a bad job",
            "i want to speak to a real person not a bot",
        ],
    },
}

INTERESTING_KEYS = [
    ("crm_type", "identified as"),
    ("case_number", "case raised"),
    ("visit_booked", "visit booked"),
    ("renewal_started", "renewal logged for"),
    ("purchase_interest", "sales notified"),
    ("brochure_sent", "brochure sent"),
    ("escalated", "escalated to a human"),
    ("renewal_objection", "objection recorded"),
    ("disposition_hint", "CRM disposition"),
]


def typewriter(text: str, slow: bool, colour: str = ""):
    if not slow:
        print(f"{colour}{text}{OFF}")
        return
    print(colour, end="", flush=True)
    for ch in text:
        print(ch, end="", flush=True)
        time.sleep(0.012)
    print(OFF, flush=True)


async def run(key: str, slow: bool, phone_override: str = ""):
    scenario = SCENARIOS[key]
    phone = phone_override or scenario["phone"]

    print(f"\n{BOLD}{'─' * 68}{OFF}")
    print(f"{BOLD}  {scenario['title']}{OFF}")
    print(f"{GREY}  shows: {scenario['shows']}{OFF}")
    print(f"{GREY}  from:  +{phone}{OFF}")
    print(f"{BOLD}{'─' * 68}{OFF}")

    session_id = wa.get_session_id(phone)
    store = SessionStore(session_id, channel="whatsapp")
    store.clear_session()
    store.update_user_info({"phone": phone, "name": scenario.get("name", ""), "source": "whatsapp"})

    nudge = scenario.get("reminder")
    if nudge:
        template, contract, days = nudge
        reminders.record_last_reminder(phone, template, contract, days)
        store.set("reminder_template", template)
        store.set("reminder_contract", contract)
        print(f"{GREY}  [reminder {template} was sent for {contract}]{OFF}")

    for message in scenario["script"]:
        print()
        typewriter(f"  {message}", slow, CYAN)
        reply = ""
        async for chunk in process_query(message, session_id, channel="whatsapp"):
            if chunk.get("type") == "token":
                reply += chunk.get("content", "")
            elif chunk.get("type") == "error":
                reply += f"[error: {chunk.get('error')}]"
        print()
        for line in (reply.strip() or "(no reply)").split("\n"):
            typewriter(f"    {line}", slow, GREEN)

    written = [(label, store.get(k)) for k, label in INTERESTING_KEYS if store.get(k)]
    if written:
        print(f"\n{YELLOW}  What reached the CRM:{OFF}")
        for label, value in written:
            print(f"{YELLOW}    - {label}: {value}{OFF}")
    print()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="?", help="which scenario to run")
    ap.add_argument("--all", action="store_true", help="run every scenario in order")
    ap.add_argument("--list", action="store_true", help="list the scenarios")
    ap.add_argument("--slow", action="store_true", help="type it out at reading pace")
    ap.add_argument("--phone", default="", help="override the sending number")
    args = ap.parse_args()

    if args.list or (not args.scenario and not args.all):
        print(f"\n{BOLD}Scenarios{OFF}\n")
        for key, s in SCENARIOS.items():
            print(f"  {BOLD}{key:<11}{OFF} {s['title']}")
            print(f"  {'':<11} {GREY}{s['shows']}{OFF}\n")
        print(f"  {GREY}.venv/bin/python scripts/demo.py <name> [--slow]{OFF}")
        print(f"  {GREY}.venv/bin/python scripts/demo.py --all --slow{OFF}\n")
        return 0

    keys = list(SCENARIOS) if args.all else [args.scenario]
    for key in keys:
        if key not in SCENARIOS:
            print(f"Unknown scenario '{key}'. Try --list.")
            return 1
        await run(key, args.slow, args.phone)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nstopped")
