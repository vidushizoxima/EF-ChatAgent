"""Full conversation through the real LLM into the real CRM, then the idle flush.
CREATES records and deletes them. Needs AZURE_LLM_* and DATAVERSE_* set.
"""
"""Full conversation → real LLM → real CRM → interaction logged on idle → cleanup."""
import asyncio, json, os, sys
ROOT = "/Users/vidushisharma/Downloads/Eureka Forbes/EurekaForbesChat/ef-chatbot"
os.chdir(ROOT); sys.path.insert(0, os.path.join(ROOT, "chatbot"))
from dotenv import load_dotenv; load_dotenv(os.path.join(ROOT, ".env"))
import logging
logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "client.store", "client.prompt_loader", "client.ef_schema", "client.config"):
    logging.getLogger(noisy).setLevel("WARNING")

from agent import process_query
from client import ef_schema as S
from client.dataverse_client import dataverse
from client.interaction_logger import flush_due
from client.store import SessionStore
created = []

async def say(text, sid, channel):
    out = ""
    async for c in process_query(text, sid, channel=channel):
        if c["type"] == "token": out += c["content"]
        elif c["type"] == "tool_start": print(f"      🛠️  {c['tool_name']}")
        elif c["type"] == "error": print(f"      ❌ {c['error']}")
    print(f"   you › {text}\n   bot › {out}\n")
    return out

async def scenario_existing_customer():
    print("\n" + "="*72 + "\n  SCENARIO 1 — existing customer, AMC expiring in 10 days (WhatsApp)\n" + "="*72)
    sid = "whatsapp:9845071284:e2e"
    store = SessionStore(sid, channel="whatsapp"); store.clear_session()
    store.update_user_info({"phone": "+91 98450 71284", "sender_id": "9845071284", "source": "whatsapp"})
    await say("hi", sid, "whatsapp")
    await say("Aditi Raghunathan", sid, "whatsapp")
    reply = await say("yes please renew it", sid, "whatsapp")
    assert store.get("crm_type") == "customer", "customer not identified"
    print(f"   → identified as {store.get('crm_type')} {store.get('crm_record_id')[:8]}…")
    if store.exists("tool_done:start_amc_renewal"):
        print("   → renewal started ✓ (lead + service request created)")
    return sid, store

async def scenario_new_person():
    print("\n" + "="*72 + "\n  SCENARIO 2 — unknown number (Facebook)\n" + "="*72)
    sid = "facebook:e2etest:e2e"
    store = SessionStore(sid, channel="facebook"); store.clear_session()
    store.update_user_info({"sender_id": "e2etest", "source": "facebook"})
    await say("do you have a water purifier for hard water?", sid, "facebook")
    await say("I'm Priya Nair, my number is 9000000077", sid, "facebook")
    await say("I'm in Pune, 411001. what's the price of the Magna?", sid, "facebook")
    print(f"   → crm_type={store.get('crm_type')} record={store.get('crm_record_id')}")
    return sid, store

async def main():
    try:
        sid1, store1 = await scenario_existing_customer()
        for key in ("renewal_lead", "service_request_id"):
            pass
        if store1.get("service_request_id"):
            created.append((S.apiset("ef_servicerequest"), store1.get("service_request_id")))

        sid2, store2 = await scenario_new_person()
        new_lead = store2.get("crm_record_id")
        if new_lead and store2.get("crm_type") == "lead":
            created.append((S.apiset("ef_lead"), new_lead))
            row = (await dataverse.query(S.apiset("ef_lead"),
                   select=["ef_leadnumber","ef_fullname","ef_phone","ef_pincode","ef_productinterest","ef_source","ef_qualificationscore"],
                   filter=f"ef_leadid eq {new_lead}", top=1))[0]
            print(f"   → lead {row['ef_leadnumber']}: {row['ef_fullname']} / {row['ef_phone']} / "
                  f"pin={row['ef_pincode']} / interest={row['ef_productinterest']!r} / score={row['ef_qualificationscore']}")
            assert S.label("ef_lead","ef_source",row["ef_source"]) == "MetaDM"

        print("\n" + "="*72 + "\n  INTERACTION LOGGING (idle flush)\n" + "="*72)
        result = await flush_due(idle_seconds=0)
        print(f"   scanned={result['scanned']} logged={len(result['logged'])}")
        for entry in result["logged"]:
            iid = entry["interaction_id"]
            created.append((S.apiset("ef_interaction"), iid))
            row = (await dataverse.query(S.apiset("ef_interaction"),
                   select=["ef_interactionnumber","ef_channel","ef_disposition","ef_intentdetected",
                           "ef_sentimentscore","ef_messagebody","ef_transcriptref","_ef_customer_value","_ef_lead_value"],
                   filter=f"ef_interactionid eq {iid}", top=1))[0]
            bound = "customer" if row["_ef_customer_value"] else ("lead" if row["_ef_lead_value"] else "none")
            print(f"\n   {row['ef_interactionnumber']} ← {entry['session_id']}")
            print(f"      channel={S.label('ef_interaction','ef_channel',row['ef_channel'])} "
                  f"disposition={S.label('ef_interaction','ef_disposition',row['ef_disposition'])} "
                  f"intent={row['ef_intentdetected']} sentiment={row['ef_sentimentscore']} bound_to={bound}")
            print(f"      summary: {(row['ef_messagebody'] or '')[:220]}")
            assert bound != "none", "interaction not linked to any record"
            assert row["ef_transcriptref"] == entry["session_id"]

        # a second flush must log nothing new (watermark holds)
        again = await flush_due(idle_seconds=0)
        assert not again["logged"], f"double-logged: {again}"
        print("\n   ✅ re-flush logged nothing — watermark prevents duplicates")

    finally:
        print("\n🧹 cleanup")
        # renewal artefacts created by the agent during scenario 1
        for sid in ("whatsapp:9845071284:e2e",):
            st = SessionStore(sid)
            for key, ent in (("service_request_id", "ef_servicerequest"),):
                v = st.get(key)
                if v: created.append((S.apiset(ent), v))
        seen = set()
        for entity_set, rid in reversed(created):
            if not rid or (entity_set, rid) in seen: continue
            seen.add((entity_set, rid))
            try:
                await dataverse.delete(entity_set, rid); print(f"   deleted {entity_set}({rid[:8]}…)")
            except Exception as e:
                print(f"   ⚠️ {entity_set}({rid[:8]}…): {str(e)[:70]}")
        await dataverse.close()

asyncio.run(main())
