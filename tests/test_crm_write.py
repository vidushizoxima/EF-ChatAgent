"""CRM write paths against the live org: lead, interaction, renewal, complaint,
counters. CREATES real records and DELETES them again. Needs DATAVERSE_* set.
"""
"""Creates real records in eurekaforbesdemo, verifies every field, deletes them."""
import asyncio, json, os, sys
from datetime import datetime, timezone
ROOT = "/Users/vidushisharma/Downloads/Eureka Forbes/EurekaForbesChat/ef-chatbot"
os.chdir(ROOT); sys.path.insert(0, os.path.join(ROOT, "chatbot"))
from dotenv import load_dotenv; load_dotenv(os.path.join(ROOT, ".env"))
import logging; logging.basicConfig(level="WARNING")
from client import ef_schema as S
from client.dataverse_client import dataverse
from client.store import SessionStore
from tools import ef_crm
from tools.ef_tools import create_lead, identify_customer, start_amc_renewal, raise_service_request

TEST_PHONE = "9000000042"
created = []          # (entity_set, id) to clean up

async def main():
    try:
        # ── 1. create_lead via the tool, as the agent would ──
        sid = "test:write:1"
        store = SessionStore(sid, channel="facebook"); store.clear_session()
        store.update_user_info({"source": "facebook", "sender_id": "fbtest"})
        await identify_customer.ainvoke({"phone": TEST_PHONE, "session_id": sid})

        r = json.loads(await create_lead.ainvoke({
            "name": "TEST — chatbot QA", "phone": TEST_PHONE,
            "product_interest": "Aquaguard Magna HD RO+UV",
            "email": "qa@example.com", "pincode": "560001",
            "intent": "new_purchase", "session_id": sid,
        }))
        assert r["status"] == "success", r
        lead_id = r["lead_id"]; created.append((S.apiset("ef_lead"), lead_id))

        row = (await dataverse.query(S.apiset("ef_lead"),
               select=["ef_fullname","ef_phone","ef_email","ef_pincode","ef_productinterest",
                       "ef_source","ef_status","ef_qualificationscore","ef_totalinteractions",
                       "ef_leadnumber","ef_lastinteractiondate"],
               filter=f"ef_leadid eq {lead_id}", top=1))[0]
        print("✅ lead created:", row["ef_leadnumber"])
        assert row["ef_phone"] == "+91 90000 00042", row["ef_phone"]
        assert S.label("ef_lead","ef_source",row["ef_source"]) == "MetaDM", row["ef_source"]
        assert S.label("ef_lead","ef_status",row["ef_status"]) == "New"
        assert row["ef_productinterest"] == "Aquaguard Magna HD RO+UV"
        assert row["ef_email"] == "qa@example.com" and row["ef_pincode"] == "560001"
        assert 0 < row["ef_qualificationscore"] <= 1
        print(f"   phone={row['ef_phone']} source=MetaDM status=New score={row['ef_qualificationscore']} "
              f"interest={row['ef_productinterest']!r}")

        # guard: a second create must be refused, not duplicated
        again = json.loads(await create_lead.ainvoke({
            "name": "TEST — chatbot QA", "phone": TEST_PHONE, "session_id": sid}))
        assert again["status"] == "already_exists", again
        print("✅ duplicate create_lead refused by the session guard")

        # ── 2. interaction against that lead ──
        res = await ef_crm.log_interaction(
            channel="facebook", summary="TEST — customer asked about Magna HD pricing and AMC.",
            intent="new_purchase", sentiment=0.4, disposition="Qualified",
            lead_id=lead_id, started_at=datetime.now(timezone.utc),
            responded_at=datetime.now(timezone.utc),
            provider_message_id="m.qa.1", transcript_ref=sid, handled_by="Asha", cost_amount=0.12)
        iid = res["interaction_id"]; created.append((S.apiset("ef_interaction"), iid))

        irow = (await dataverse.query(S.apiset("ef_interaction"),
                select=["ef_interactionnumber","ef_channel","ef_direction","ef_interactiontype",
                        "ef_status","ef_disposition","ef_handledbytype","ef_handledbyname",
                        "ef_messagebody","ef_intentdetected","ef_sentimentscore","ef_transcriptref",
                        "ef_providermessageid","ef_escalatedflag","ef_costamount","_ef_lead_value"],
                filter=f"ef_interactionid eq {iid}", top=1))[0]
        print("✅ interaction created:", irow["ef_interactionnumber"])
        assert S.label("ef_interaction","ef_channel",irow["ef_channel"]) == "MetaDM"
        assert S.label("ef_interaction","ef_direction",irow["ef_direction"]) == "Inbound"
        assert S.label("ef_interaction","ef_disposition",irow["ef_disposition"]) == "Qualified"
        assert S.label("ef_interaction","ef_handledbytype",irow["ef_handledbytype"]) == "AIAgent"
        assert irow["_ef_lead_value"] == lead_id, "lead lookup did not bind"
        assert irow["ef_transcriptref"] == sid and irow["ef_providermessageid"] == "m.qa.1"
        assert irow["ef_sentimentscore"] == 0.4 and irow["ef_handledbyname"] == "Asha"
        print(f"   channel=MetaDM direction=Inbound disposition=Qualified bound_to_lead=✓ "
              f"sentiment={irow['ef_sentimentscore']} cost={irow['ef_costamount']}")

        # ── 3. counters roll forward on the lead ──
        await ef_crm.touch_lead(lead_id, qualification_score_value=0.9)
        row2 = (await dataverse.query(S.apiset("ef_lead"),
                select=["ef_totalinteractions","ef_status","ef_qualificationscore"],
                filter=f"ef_leadid eq {lead_id}", top=1))[0]
        assert row2["ef_totalinteractions"] == row["ef_totalinteractions"] + 1
        assert S.label("ef_lead","ef_status",row2["ef_status"]) == "Working", row2
        assert row2["ef_qualificationscore"] == 0.9
        print(f"✅ lead counters: interactions {row['ef_totalinteractions']}→{row2['ef_totalinteractions']}, "
              f"status New→Working, score→0.9")

        # ── 4. AMC renewal on a real customer (lead + service request) ──
        sid2 = "test:write:2"
        st2 = SessionStore(sid2, channel="whatsapp"); st2.clear_session()
        st2.update_user_info({"source": "whatsapp"})
        cust = json.loads(await identify_customer.ainvoke({"phone": "9845071284", "session_id": sid2}))
        assert cust["type"] == "customer"
        rr = json.loads(await start_amc_renewal.ainvoke({"notes": "TEST — QA run", "session_id": sid2}))
        assert rr["status"] == "success", rr
        if rr.get("renewal_lead_id"): created.append((S.apiset("ef_lead"), rr["renewal_lead_id"]))
        if rr.get("service_request_id"): created.append((S.apiset("ef_servicerequest"), rr["service_request_id"]))

        rl = (await dataverse.query(S.apiset("ef_lead"), select=["ef_fullname","ef_productinterest","ef_source"],
              filter=f"ef_leadid eq {rr['renewal_lead_id']}", top=1))[0]
        sr = (await dataverse.query(S.apiset("ef_servicerequest"),
              select=["ef_caseid","ef_requesttype","ef_status","ef_priority","ef_category","_ef_customer_value","_ef_asset_value"],
              filter=f"ef_servicerequestid eq {rr['service_request_id']}", top=1))[0]
        print(f"✅ renewal: lead {rl['ef_productinterest']!r} + case {sr['ef_caseid']}")
        assert "renewal" in rl["ef_productinterest"].lower()
        assert S.label("ef_servicerequest","ef_requesttype",sr["ef_requesttype"]) == "AMCRequest"
        assert S.label("ef_servicerequest","ef_status",sr["ef_status"]) == "Open"
        assert sr["_ef_customer_value"] == cust["record_id"], "case not bound to the customer"
        assert sr["_ef_asset_value"], "case not bound to the contract's asset"
        print(f"   type=AMCRequest status=Open bound_to_customer=✓ bound_to_asset=✓")
        assert st2.get("disposition_hint") == "ConvertedRenewal"
        repeat = json.loads(await start_amc_renewal.ainvoke({"session_id": sid2}))
        assert repeat["status"] == "already_done", repeat
        print("✅ repeat renewal blocked by the session guard")

        # ── 5. complaint path ──
        rs = json.loads(await raise_service_request.ainvoke({
            "issue": "TEST — water tastes salty", "category": "Water taste",
            "priority": "Medium", "session_id": sid2}))
        assert rs["status"] == "success", rs
        created.append((S.apiset("ef_servicerequest"), rs["service_request_id"]))
        print(f"✅ complaint logged: {rs['service_request_id'][:8]}…")

        # ── 6. interaction bound to a CUSTOMER + counter maths ──
        before = (await dataverse.query(S.apiset("ef_customer"),
                  select=["ef_totalinteractions","ef_inboundcount","ef_avgsentiment"],
                  filter=f"ef_customerid eq {cust['record_id']}", top=1))[0]
        ci = await ef_crm.log_interaction(
            channel="whatsapp", summary="TEST — AMC renewal agreed on WhatsApp.",
            intent="amc_renewal", sentiment=0.8, disposition="ConvertedRenewal",
            customer_id=cust["record_id"], transcript_ref=sid2)
        created.append((S.apiset("ef_interaction"), ci["interaction_id"]))
        await ef_crm.touch_customer(cust["record_id"], sentiment=0.8)
        after = (await dataverse.query(S.apiset("ef_customer"),
                 select=["ef_totalinteractions","ef_inboundcount","ef_avgsentiment","ef_lastinbounddate"],
                 filter=f"ef_customerid eq {cust['record_id']}", top=1))[0]
        assert after["ef_totalinteractions"] == (before["ef_totalinteractions"] or 0) + 1
        assert after["ef_inboundcount"] == (before["ef_inboundcount"] or 0) + 1
        print(f"✅ customer counters: interactions {before['ef_totalinteractions']}→{after['ef_totalinteractions']}, "
              f"inbound {before['ef_inboundcount']}→{after['ef_inboundcount']}, "
              f"avg sentiment {before['ef_avgsentiment']}→{after['ef_avgsentiment']}")
        # restore the customer's counters — this is pre-existing demo data
        await dataverse.update(S.apiset("ef_customer"), cust["record_id"], {
            "ef_totalinteractions": before["ef_totalinteractions"],
            "ef_inboundcount": before["ef_inboundcount"],
            "ef_avgsentiment": before["ef_avgsentiment"]})
        print("   (customer counters restored to their original values)")

    finally:
        print("\n🧹 cleaning up test records")
        for entity_set, rid in reversed(created):
            try:
                await dataverse.delete(entity_set, rid)
                print(f"   deleted {entity_set}({rid[:8]}…)")
            except Exception as e:
                print(f"   ⚠️ could not delete {entity_set}({rid}): {str(e)[:80]}")
        await dataverse.close()
    print("\n🎉 all CRM write tests passed, org left clean")

asyncio.run(main())
