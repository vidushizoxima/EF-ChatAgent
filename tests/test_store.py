"""Session store: kv/TTL, sets, dedupe, transcript, summary lock. Run from the repo root."""
import asyncio, os, sys, tempfile
os.environ["EF_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["SUMMARY_BATCH_SIZE"] = "2"
sys.path.insert(0, "chatbot")
from client.store import SessionStore, SqlitePool

async def main():
    s = SessionStore("whatsapp:9199:2026-08-21", channel="whatsapp")
    s.update_user_info({"phone": "919999", "name": "Asha Test", "email": ""})
    assert s.get_user_info()["name"] == "Asha Test"
    s.update_user_info({"name": ""})  # empty must not clobber
    assert s.get_user_info()["name"] == "Asha Test", s.get_user_info()
    assert "email" not in s.get_user_info()

    s.set("flag", "1", ttl=0)
    assert s.get("flag") is None, "ttl=0 key should be expired"

    s.sadd("checked_phones", "9876543210"); s.sadd("checked_phones", "9876543210")
    assert s.smembers("checked_phones") == ["9876543210"]

    assert SessionStore.seen_message("wa:mid1") is False
    assert SessionStore.seen_message("wa:mid1") is True

    s.set_lead_id("guid-1"); s.set_existing_lead_data({"status": "found", "name": "X"})
    assert s.get_lead_id() == "guid-1" and s.get_existing_lead_data()["name"] == "X"

    await s.add_message("user", "my purifier is leaking")
    await s.add_message("assistant", "Since when?")
    ctx = s.get_context_for_chat()
    assert "leaking" in ctx and "RECENT ACTUAL MESSAGES" in ctx, ctx
    assert s.get_context_for_chat(exclude_last=True).count("Since when") == 0

    stats = s.get_session_stats()
    assert stats["message_count"] == 2 and stats["user_message_count"] == 1, stats
    assert len(s.get_full_transcript()) == 2
    assert "USER:" in s.get_transcript_formatted()

    # summary trigger fires at the 2nd user message (batch size 2) — no API key, so it
    # must fail softly and leave the flag clear
    await s.add_message("user", "yesterday")
    await asyncio.sleep(6)
    rows = SqlitePool.query("SELECT summary_in_progress FROM sessions WHERE session_id=?", (s.session_id,))
    assert rows[0]["summary_in_progress"] == 0, "stale summary lock left behind"

    s.clear_session()
    assert s.get_session_stats()["exists"] is False
    print("✅ store: all assertions passed")

asyncio.run(main())
