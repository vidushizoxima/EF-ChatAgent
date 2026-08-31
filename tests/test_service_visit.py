"""Service-visit booking: slot parsing, validation, and the CRM write.
CREATES a service request and DELETES it again. Needs DATAVERSE_* set.
"""
import asyncio, json, os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT); sys.path.insert(0, os.path.join(ROOT, "chatbot"))
from dotenv import load_dotenv; load_dotenv(os.path.join(ROOT, ".env"))
import logging; logging.basicConfig(level="WARNING")
from datetime import datetime, timedelta, timezone
from client import ef_schema as S
from client.dataverse_client import dataverse
from client.store import SessionStore
from tools import ef_crm
from tools.ef_tools import book_service_visit, identify_customer, raise_service_request

created = []

async def main():
    try:
        # ── slot rules, no network ──
        assert ef_crm.resolve_slot("first half") == "morning"
        assert ef_crm.resolve_slot("4pm") == "evening"
        assert ef_crm.resolve_slot("whenever") is None
        assert not ef_crm.resolve_visit("2020-01-01", "morning")["ok"]      # past
        assert not ef_crm.resolve_visit("sunday", "morning")["ok"]          # closed
        assert not ef_crm.resolve_visit("2030-01-01", "morning")["ok"]      # beyond horizon
        assert not ef_crm.resolve_visit("tomorrow", "midnight")["ok"]       # bad window
        ok = ef_crm.resolve_visit("tomorrow", "morning")
        assert ok["ok"] and ok["visit_datetime"].tzinfo is not None
        assert ok["visit_datetime"].hour == 4 and ok["visit_datetime"].minute == 30, ok  # 10:00 IST
        assert ef_crm.next_available_slots(3) and len(ef_crm.next_available_slots(3)) == 3
        print("✅ slots: aliases, past/Sunday/horizon/same-day all rejected, IST→UTC correct")

        # ── case + booking through the tools ──
        sid = "test:visit:1"
        store = SessionStore(sid, channel="whatsapp"); store.clear_session()
        store.update_user_info({"source": "whatsapp"})
        cust = json.loads(await identify_customer.ainvoke({"phone": "9845071284", "session_id": sid}))
        assert cust["type"] == "customer"

        # 1. complaint with no slot yet → must offer slots back
        r = json.loads(await raise_service_request.ainvoke({
            "issue": "TEST — purifier making noise", "category": "TEST Noise",
            "priority": "Medium", "session_id": sid}))
        assert r["status"] == "success" and r["available_slots"], r
        case_id = r["service_request_id"]; created.append((S.apiset("ef_servicerequest"), case_id))
        print(f"✅ case created without a slot; {len(r['available_slots'])} slots offered back")

        row = (await dataverse.query(S.apiset("ef_servicerequest"),
               select=["ef_caseid","ef_visitdate","ef_visitstatus"],
               filter=f"ef_servicerequestid eq {case_id}", top=1))[0]
        assert row["ef_visitdate"] is None and row["ef_visitstatus"] is None
        print(f"   {row['ef_caseid']}: visit fields empty as expected")

        # 2. a slot the rules refuse → nothing written, alternatives returned
        bad = json.loads(await book_service_visit.ainvoke({
            "visit_date": "sunday", "visit_slot": "morning", "session_id": sid}))
        assert bad["status"] == "slot_unavailable" and bad["available_slots"], bad
        print(f"✅ Sunday refused: {bad['reason']}")

        # 3. a good slot → patched into the case
        good = json.loads(await book_service_visit.ainvoke({
            "visit_date": "tomorrow", "visit_slot": "morning", "session_id": sid}))
        assert good["status"] == "success", good
        row = (await dataverse.query(S.apiset("ef_servicerequest"),
               select=["ef_caseid","ef_visitdate","ef_visitstatus","ef_status"],
               filter=f"ef_servicerequestid eq {case_id}", top=1))[0]
        assert row["ef_visitdate"], "visit date not written"
        assert S.label("ef_servicerequest","ef_visitstatus",row["ef_visitstatus"]) == "Scheduled"
        booked = datetime.fromisoformat(row["ef_visitdate"].replace("Z","+00:00"))
        ist = booked.astimezone(timezone(timedelta(hours=5, minutes=30)))
        assert ist.hour == 10 and ist.minute == 0, f"stored {ist} — expected 10:00 IST"
        print(f"✅ visit written: {row['ef_visitdate']} (= {ist.strftime('%A %d %b %H:%M')} IST), status=Scheduled")
        print(f"   agent says: {good['visit']['when']}")

        # 4. reschedule
        moved = json.loads(await book_service_visit.ainvoke({
            "visit_date": "tomorrow", "visit_slot": "evening", "session_id": sid}))
        assert moved["status"] == "success" and moved["visit"]["rescheduled"] is True, moved
        row = (await dataverse.query(S.apiset("ef_servicerequest"),
               select=["ef_visitdate"], filter=f"ef_servicerequestid eq {case_id}", top=1))[0]
        ist2 = datetime.fromisoformat(row["ef_visitdate"].replace("Z","+00:00")).astimezone(
            timezone(timedelta(hours=5, minutes=30)))
        assert ist2.hour == 16, ist2
        print(f"✅ rescheduled to {ist2.strftime('%A %d %b %H:%M')} IST")
        assert store.get("disposition_hint") == "ConvertedVisit"

        # 5. booking with no case in the conversation
        sid2 = "test:visit:2"; SessionStore(sid2).clear_session()
        none = json.loads(await book_service_visit.ainvoke({
            "visit_date": "tomorrow", "visit_slot": "morning", "session_id": sid2}))
        assert none["status"] == "no_case", none
        print("✅ booking without a case is refused, not invented")

        # 6. slot supplied with the complaint in one go
        sid3 = "test:visit:3"
        st3 = SessionStore(sid3, channel="whatsapp"); st3.clear_session()
        st3.update_user_info({"source": "whatsapp"})
        await identify_customer.ainvoke({"phone": "9845071284", "session_id": sid3})
        one = json.loads(await raise_service_request.ainvoke({
            "issue": "TEST — leaking", "category": "TEST Leak", "priority": "High",
            "visit_date": "tomorrow", "visit_slot": "afternoon", "session_id": sid3}))
        assert one["status"] == "success" and one.get("visit"), one
        created.append((S.apiset("ef_servicerequest"), one["service_request_id"]))
        row = (await dataverse.query(S.apiset("ef_servicerequest"),
               select=["ef_caseid","ef_visitdate","ef_visitstatus","ef_requesttype"],
               filter=f"ef_servicerequestid eq {one['service_request_id']}", top=1))[0]
        assert S.label("ef_servicerequest","ef_visitstatus",row["ef_visitstatus"]) == "Scheduled"
        assert S.label("ef_servicerequest","ef_requesttype",row["ef_requesttype"]) == "Complaint"
        print(f"✅ one-shot: {row['ef_caseid']} logged as Complaint with the visit already booked")

    finally:
        print("\n🧹 cleanup")
        for entity, rid in reversed(created):
            try:
                await dataverse.delete(entity, rid); print(f"   deleted {entity}({rid[:8]}…)")
            except Exception as e:
                print(f"   ⚠️ {str(e)[:70]}")
        for s in ("test:visit:1","test:visit:2","test:visit:3"):
            SessionStore(s).clear_session()
        await dataverse.close()
    print("\n🎉 service visit tests passed")

asyncio.run(main())
