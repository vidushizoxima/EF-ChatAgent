#!/usr/bin/env python3
"""
submit_whatsapp_templates.py — create the AMC reminder templates on Meta.

Templates are the only way to reach a customer outside the 24-hour service window,
and Meta has to approve the exact wording first. This submits everything in
`amc_templates.LADDER` and reports what came back.

    .venv/bin/python scripts/submit_whatsapp_templates.py            # show status only
    .venv/bin/python scripts/submit_whatsapp_templates.py --submit   # create missing ones
    .venv/bin/python scripts/submit_whatsapp_templates.py --delete amc_expiry_30d

Approval is usually minutes for UTILITY and up to 24h for MARKETING. A REJECTED
template has to be edited and resubmitted under the same name.

Nothing here sends a message to anybody.
"""

import argparse
import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "chatbot"))
load_dotenv()

from client import amc_templates as T  # noqa: E402

GRAPH = "https://graph.facebook.com/v23.0"
TOKEN = os.getenv("WHATSAPP_TOKEN", "")
WABA_ID = os.getenv("WHATSAPP_WABA_ID", "")

STATUS_MARK = {
    "APPROVED": "✅", "PENDING": "⏳", "REJECTED": "❌",
    "PAUSED": "⏸️", "DISABLED": "🚫",
}


def _headers():
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


class ListFailed(Exception):
    """Could not read the current templates — see fetch_existing."""


async def fetch_existing(client: httpx.AsyncClient) -> dict:
    r = await client.get(
        f"{GRAPH}/{WABA_ID}/message_templates",
        params={"fields": "name,status,category,language,rejected_reason", "limit": 200},
        headers=_headers(),
    )
    if r.status_code != 200:
        # Returning {} here would report every template as "not created" and, with
        # --submit, try to recreate all six. An expired token must stop the run,
        # not look like an empty account.
        raise ListFailed(f"{r.status_code} {r.text}")
    return {t["name"]: t for t in r.json().get("data", [])}


async def create(client: httpx.AsyncClient, tmpl: T.Template) -> bool:
    r = await client.post(
        f"{GRAPH}/{WABA_ID}/message_templates",
        json=tmpl.meta_payload(),
        headers=_headers(),
    )
    if r.status_code in (200, 201):
        body = r.json()
        print(f"   ✅ submitted — id={body.get('id')} status={body.get('status')}")
        return True
    print(f"   ❌ {r.status_code} {r.text}")
    return False


async def delete(client: httpx.AsyncClient, name: str) -> bool:
    r = await client.delete(
        f"{GRAPH}/{WABA_ID}/message_templates",
        params={"name": name},
        headers=_headers(),
    )
    print(f"   {'✅ deleted' if r.status_code == 200 else '❌ ' + r.text}")
    return r.status_code == 200


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--submit", action="store_true", help="create templates that do not exist yet")
    ap.add_argument("--delete", metavar="NAME", help="delete one template by name")
    args = ap.parse_args()

    if not TOKEN or not WABA_ID:
        print("❌ WHATSAPP_TOKEN and WHATSAPP_WABA_ID must both be set in .env")
        return 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        if args.delete:
            print(f"Deleting {args.delete}")
            await delete(client, args.delete)
            return 0

        try:
            existing = await fetch_existing(client)
        except ListFailed as e:
            print(f"❌ Could not list templates: {e}")
            if "expired" in str(e) or "190" in str(e):
                print(
                    "\n   The access token has expired. Templates already submitted are "
                    "unaffected\n   and still under review — this script simply cannot see "
                    "them right now.\n   Put a fresh token in WHATSAPP_TOKEN and re-run."
                )
            return 1
        print(f"\nWABA {WABA_ID} — {len(existing)} template(s) already defined\n")
        print(f"{'TEMPLATE':<26} {'DAY':>5}  {'CATEGORY':<10} STATUS  (category as Meta assigned it)")
        print("-" * 84)

        missing = []
        recategorised = []
        for tmpl in T.ALL_TEMPLATES:
            found = existing.get(tmpl.name)
            # Show the category META assigned, not the one we asked for. Printing our
            # own value made a re-categorisation invisible in the very report meant
            # to catch it — and category decides cost and the marketing cap.
            category = tmpl.category
            if found:
                status = found.get("status", "?")
                category = found.get("category") or tmpl.category
                line = f"{STATUS_MARK.get(status, '•')} {status}"
                if category != tmpl.category:
                    line += f"  ⚠️ submitted as {tmpl.category}"
                    recategorised.append((tmpl.name, tmpl.category, category))
                if status == "REJECTED" and found.get("rejected_reason"):
                    line += f" ({found['rejected_reason']})"
            else:
                line = "— not created"
                missing.append(tmpl)
            print(f"{tmpl.name:<26} {tmpl.days:>+5}  {category:<10} {line}")

        if recategorised:
            print(
                f"\n⚠️  Meta re-categorised {len(recategorised)} template(s) after reading the copy."
                "\n   Category drives cost and the per-user marketing cap, so the split you"
                "\n   designed is not the split that is live."
            )

        if not missing:
            print(f"\nAll {len(T.ALL_TEMPLATES)} templates exist. Nothing to submit.")
            return 0

        if not args.submit:
            print(f"\n{len(missing)} not yet created. Re-run with --submit to create them.")
            return 0

        print(f"\nSubmitting {len(missing)} template(s) for approval:\n")
        for tmpl in missing:
            print(f" • {tmpl.name} ({tmpl.category}) — {tmpl.label}")
            await create(client, tmpl)

        print(
            "\nUTILITY templates usually approve within minutes; MARKETING can take "
            "up to 24 hours.\nRe-run this script to see the outcome."
        )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
