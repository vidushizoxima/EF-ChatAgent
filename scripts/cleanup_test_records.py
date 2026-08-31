"""
cleanup_test_records.py — find, and optionally delete, records the test suite created.

DEFAULT IS A DRY RUN. It only ever deletes records it can positively identify as
test data, because "everything created today" also matches real customers once the
bot is live.

A record is test data if it carries one of the markers below:
  · a lead whose name contains "TEST" or whose phone is in the 9000000000 range
  · a case whose category starts with "TEST"
  · an interaction whose transcript ref points at a test session (test:*, *:e2e, *:visit)

    python scripts/cleanup_test_records.py              # list what matches
    python scripts/cleanup_test_records.py --delete     # delete those matches
    python scripts/cleanup_test_records.py --id <guid> --entity ef_leads --delete
"""

import argparse
import asyncio
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "chatbot"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import logging  # noqa: E402

logging.basicConfig(level="WARNING")

from client import ef_schema as S  # noqa: E402
from client.dataverse_client import dataverse  # noqa: E402

TEST_SESSION = re.compile(r"^(test:|.*:(e2e|visit)$)")
TEST_PHONE = re.compile(r"\+?91?\s?90000\s?000\d\d")


def is_test_lead(row) -> bool:
    name = (row.get("ef_fullname") or "").upper()
    phone = row.get("ef_phone") or ""
    return "TEST" in name or bool(TEST_PHONE.search(phone))


def is_test_case(row) -> bool:
    return (row.get("ef_category") or "").upper().startswith("TEST")


def is_test_interaction(row) -> bool:
    return bool(TEST_SESSION.match(row.get("ef_transcriptref") or ""))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true", help="actually delete the matches")
    parser.add_argument("--id", help="delete one specific record id")
    parser.add_argument("--entity", help="entity set for --id, e.g. ef_leads")
    parser.add_argument("--days", type=int, default=1, help="how far back to look (default 1)")
    args = parser.parse_args()

    if args.id:
        if not args.entity:
            print("--id needs --entity"); return
        if not args.delete:
            print(f"dry run: would delete {args.entity}({args.id}). Add --delete."); return
        await dataverse.delete(args.entity, args.id)
        print(f"deleted {args.entity}({args.id})")
        await dataverse.close()
        return

    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    matches = []

    leads = await dataverse.query(S.apiset("ef_lead"),
        select=["ef_leadid", "ef_leadnumber", "ef_fullname", "ef_phone", "ef_productinterest"],
        filter=f"createdon ge {since}", top=200)
    cases = await dataverse.query(S.apiset("ef_servicerequest"),
        select=["ef_servicerequestid", "ef_caseid", "ef_category"],
        filter=f"createdon ge {since}", top=200)
    inters = await dataverse.query(S.apiset("ef_interaction"),
        select=["ef_interactionid", "ef_interactionnumber", "ef_transcriptref"],
        filter=f"createdon ge {since}", top=200)

    print(f"Records created since {since}:\n")
    for r in leads:
        flag = "TEST" if is_test_lead(r) else "real"
        print(f"  [{flag}] LEAD {r['ef_leadnumber']} {r['ef_fullname']} {r['ef_phone']}")
        if is_test_lead(r):
            matches.append((S.apiset("ef_lead"), r["ef_leadid"], r["ef_leadnumber"]))
    for r in cases:
        flag = "TEST" if is_test_case(r) else "real"
        print(f"  [{flag}] CASE {r['ef_caseid']} {r['ef_category']!r}")
        if is_test_case(r):
            matches.append((S.apiset("ef_servicerequest"), r["ef_servicerequestid"], r["ef_caseid"]))
    for r in inters:
        flag = "TEST" if is_test_interaction(r) else "real"
        print(f"  [{flag}] INT  {r['ef_interactionnumber']} ref={r['ef_transcriptref']}")
        if is_test_interaction(r):
            matches.append((S.apiset("ef_interaction"), r["ef_interactionid"], r["ef_interactionnumber"]))

    real = (len(leads) + len(cases) + len(inters)) - len(matches)
    print(f"\n{len(matches)} test record(s), {real} real record(s) left alone.")

    if not matches:
        pass
    elif not args.delete:
        print("Dry run — add --delete to remove the test records.")
    else:
        print("\nDeleting test records only:")
        for entity, rid, label in matches:
            try:
                await dataverse.delete(entity, rid)
                print(f"   deleted {label}")
            except Exception as e:
                print(f"   ⚠️ {label}: {str(e)[:80]}")

    await dataverse.close()


asyncio.run(main())
