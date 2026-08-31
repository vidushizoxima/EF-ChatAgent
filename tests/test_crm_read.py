"""CRM read paths against the live org: identity resolution across phone formats,
customer 360, renewal detection. Read-only — creates nothing. Needs DATAVERSE_* set.
"""
import asyncio, json, os, sys
ROOT = "/Users/vidushisharma/Downloads/Eureka Forbes/EurekaForbesChat/ef-chatbot"
os.chdir(ROOT); sys.path.insert(0, os.path.join(ROOT, "chatbot"))
from dotenv import load_dotenv; load_dotenv(os.path.join(ROOT, ".env"))
import logging; logging.basicConfig(level="WARNING")
from client.store import SessionStore
from tools.ef_tools import identify_customer
from tools import ef_crm

async def main():
    # 1. known customer with an expiring AMC, phone in "+91 98450 71284" form
    sid = "test:read:1"
    SessionStore(sid).clear_session()
    r = json.loads(await identify_customer.ainvoke({"phone": "9845071284", "session_id": sid}))
    assert r["status"] == "found" and r["type"] == "customer", r
    print(f"✅ customer resolved: {r['name']} ({r['phone']}) — {len(r['assets'])} assets, "
          f"{len(r['contracts'])} contracts, {len(r['open_cases'])} open cases")
    assert r["renewals"], "expected AMC-000502 to be flagged"
    for x in r["renewals"]:
        print(f"   renewal: {x['contract_id']} {x['type']}/{x['tier']} expires {x['expiry_date']} "
              f"in {x['days_left']}d (status {x['status']}) asset={x['asset']}")
    print("   hint →", r["renewal_hint"])
    if r.get("consumable_hint"): print("   hint →", r["consumable_hint"])

    # session must now carry the identity for the middleware and the logger
    store = SessionStore(sid)
    assert store.get("crm_type") == "customer" and store.get("crm_record_id") == r["record_id"]
    assert store.get_json("pending_renewal")["contract_id"] == r["renewals"][0]["contract_id"]
    print("✅ session primed: crm_type/record_id/pending_renewal all set")

    # 2. unformatted phone must resolve identically
    sid2 = "test:read:2"; SessionStore(sid2).clear_session()
    r2 = json.loads(await identify_customer.ainvoke({"phone": "+91 98450 71284", "session_id": sid2}))
    assert r2["record_id"] == r["record_id"], "format variance broke matching"
    r3 = json.loads(await identify_customer.ainvoke({"phone": "09845071284", "session_id": sid2}))
    assert r3["record_id"] == r["record_id"]
    print("✅ phone matching stable across '9845071284' / '+91 98450 71284' / '09845071284'")

    # 3. the bare-format customer
    sid3 = "test:read:3"; SessionStore(sid3).clear_session()
    r4 = json.loads(await identify_customer.ainvoke({"phone": "9893984982", "session_id": sid3}))
    assert r4["status"] == "found", r4
    print(f"✅ bare-format number resolved: {r4['name']}")

    # 4. an unknown number
    sid4 = "test:read:4"; SessionStore(sid4).clear_session()
    r5 = json.loads(await identify_customer.ainvoke({"phone": "9000000001", "session_id": sid4}))
    assert r5["status"] == "not_found", r5
    print("✅ unknown number → not_found (create_lead path)")

    # 5. prospect / lead resolution
    sid5 = "test:read:5"; SessionStore(sid5).clear_session()
    r6 = json.loads(await identify_customer.ainvoke({"phone": "9857060637", "session_id": sid5}))
    print(f"✅ prospect lookup: {r6['status']} type={r6.get('type')} {r6.get('name','')}")

    # 6. bad input
    r7 = json.loads(await identify_customer.ainvoke({"phone": "12345", "session_id": sid4}))
    assert r7["status"] == "error", r7
    print("✅ short number rejected without hitting the CRM")

    from client.dataverse_client import dataverse; await dataverse.close()

asyncio.run(main())
