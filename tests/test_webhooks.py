"""Webhooks: verification, dedupe, echo filtering, profile capture, admin auth.

Stubs the agent and the Meta send calls — nothing leaves the machine. Run from the repo root.
"""
import asyncio, json, os, sys, tempfile
os.environ["EF_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["WHATSAPP_VERIFY_TOKEN"] = "vt-wa"
os.environ["WHATSAPP_TOKEN"] = "x"; os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "PNID"
os.environ["FACEBOOK_VERIFY_TOKEN"] = "vt-fb"; os.environ["FACEBOOK_PAGE_ACCESS_TOKEN"] = "x"; os.environ["FACEBOOK_PAGE_ID"] = "PAGE1"
os.environ["INSTAGRAM_VERIFY_TOKEN"] = "vt-ig"; os.environ["INSTAGRAM_PAGE_ACCESS_TOKEN"] = "x"; os.environ["INSTAGRAM_ACCOUNT_ID"] = "IGACC"
os.environ["ADMIN_API_KEY"] = "admin-secret"
sys.path.insert(0, "chatbot")

from fastapi.testclient import TestClient
import main
from client.store import SessionStore

sent = []
turns = []

async def fake_process_query(query, session_id, channel="whatsapp", user_id=None):
    turns.append((channel, session_id, query))
    store = SessionStore(session_id, channel=channel)
    await store.add_message("user", query)
    yield {"type": "token", "content": f"reply to '{query}'"}
    await store.add_message("assistant", f"reply to '{query}'")

main.process_query = fake_process_query
# phone_number_id: whichever of our numbers received the message, so the reply
# goes back out from the same one.
async def cap_wa(phone, text, phone_number_id=None):
    sent.append(("wa", phone, text)); return True
async def cap_fb(sid, text): sent.append(("fb", sid, text)); return True
async def cap_ig(sid, text): sent.append(("ig", sid, text)); return True
main.wa.send_message = cap_wa
main.fb.send_message = cap_fb
main.ig.send_message = cap_ig
for mod in (main.wa, main.fb, main.ig):
    async def noop(*a, **k): return True
    mod.send_typing_indicator = noop
    if hasattr(mod, "mark_as_seen"): mod.mark_as_seen = noop
main.fb.get_user_profile = lambda sid: asyncio.sleep(0, result={"name": "FB User"})
main.ig.get_user_profile = lambda sid: asyncio.sleep(0, result={"name": "IG User", "username": "iguser"})

WA = {"entry": [{"changes": [{"value": {
    "metadata": {"phone_number_id": "PNID"},
    "contacts": [{"profile": {"name": "Ramesh"}}],
    "messages": [{"from": "919876543210", "id": "wamid.1", "type": "text", "text": {"body": "AMC price?"}}],
}}]}]}
FB = {"entry": [{"messaging": [{"sender": {"id": "fbuser1"}, "recipient": {"id": "PAGE1"},
    "timestamp": 1, "message": {"mid": "m.1", "text": "vacuum not working"}}]}]}
FB_ECHO = {"entry": [{"messaging": [{"sender": {"id": "PAGE1"}, "recipient": {"id": "fbuser1"},
    "timestamp": 2, "message": {"mid": "m.2", "text": "our reply", "is_echo": True}}]}]}
IG = {"entry": [{"messaging": [{"sender": {"id": "iguser1"}, "recipient": {"id": "IGACC"},
    "timestamp": 3, "message": {"mid": "ig.1", "text": "price of aquaguard?"}}]}]}

with TestClient(main.app) as c:
    h = c.get("/health").json()
    assert h["status"] == "healthy" and h["store"] == "sqlite", h
    assert h["channels"] == {"whatsapp": True, "facebook": True, "instagram": True}, h

    # verification handshake
    for path, tok in (("/whatsapp", "vt-wa"), ("/facebook", "vt-fb"), ("/instagram", "vt-ig")):
        r = c.get(path, params={"hub.mode": "subscribe", "hub.verify_token": tok, "hub.challenge": "12345"})
        assert r.status_code == 200 and r.json() == 12345, (path, r.status_code, r.text)
        bad = c.get(path, params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"})
        assert bad.status_code == 403, (path, bad.status_code)
    print("✅ webhooks: verification handshake (accept + reject)")

    assert c.post("/whatsapp", json=WA).json() == {"status": "ok"}
    assert c.post("/whatsapp", json=WA).json() == {"status": "ok"}   # duplicate mid
    assert c.post("/facebook", json=FB).json() == {"status": "ok"}
    assert c.post("/facebook", json=FB_ECHO).json() == {"status": "ok"}  # echo ignored
    assert c.post("/instagram", json=IG).json() == {"status": "ok"}
    assert c.post("/whatsapp", json={"entry": [{"changes": [{"value": {}}]}]}).json() == {"status": "ok"}

    for _ in range(50):
        if len(sent) >= 3: break
        c.get("/health")

    channels = sorted(t[0] for t in turns)
    assert channels == ["facebook", "instagram", "whatsapp"], turns
    assert len(turns) == 3, f"duplicate/echo not filtered: {turns}"
    assert sorted(s[0] for s in sent) == ["fb", "ig", "wa"], sent
    assert all("reply to" in s[2] for s in sent), sent
    print("✅ webhooks: 3 channels answered, duplicate + echo dropped")

    wa_session = [t[1] for t in turns if t[0] == "whatsapp"][0]
    store = SessionStore(wa_session)
    assert store.get_user_info()["name"] == "Ramesh", store.get_user_info()
    assert store.get_user_info()["phone"] == "919876543210"
    fb_session = [t[1] for t in turns if t[0] == "facebook"][0]
    assert SessionStore(fb_session).get_user_info()["name"] == "FB User"
    print("✅ webhooks: profile captured into the session")

    assert c.get(f"/session/{wa_session}/stats").status_code == 401
    hdr = {"X-Admin-API-Key": "admin-secret"}
    stats = c.get(f"/session/{wa_session}/stats", headers=hdr).json()
    assert stats["message_count"] == 2 and stats["channel"] == "whatsapp", stats
    tr = c.get(f"/session/{wa_session}/transcript", headers=hdr).json()
    assert tr["messages"][0]["content"] == "AMC price?", tr
    diag = c.get("/admin/diagnostics", headers=hdr).json()
    assert diag["store"]["healthy"] and diag["store"]["sessions_total"] == 3, diag["store"]
    assert set(diag["channels"]) == {"whatsapp", "facebook", "instagram"}
    assert c.delete(f"/session/{wa_session}", headers=hdr).json()["status"] == "cleared"
    assert c.get(f"/session/{wa_session}/stats", headers=hdr).json()["exists"] is False
    print("✅ admin: auth enforced, transcript/stats/diagnostics/delete work")

print("\n🎉 all webhook tests passed")

# ── alias paths: Meta may be configured with /webhook/<channel> ──
import importlib
with TestClient(main.app) as c:
    for path, tok in (("/webhook/whatsapp", "vt-wa"), ("/webhook/facebook", "vt-fb"), ("/webhook/instagram", "vt-ig")):
        r = c.get(path, params={"hub.mode": "subscribe", "hub.verify_token": tok, "hub.challenge": "999"})
        assert r.status_code == 200 and r.json() == 999, (path, r.status_code, r.text)
    before = len(turns)
    r = c.post("/webhook/facebook", json={"entry": [{"messaging": [{"sender": {"id": "fbuser2"},
        "recipient": {"id": "PAGE1"}, "timestamp": 9, "message": {"mid": "m.alias", "text": "hello"}}]}]})
    assert r.status_code == 200 and r.json() == {"status": "ok"}, r.text
    for _ in range(50):
        if len(turns) > before: break
        c.get("/health")
    assert turns[-1][0] == "facebook" and turns[-1][2] == "hello", turns[-1]
    print("✅ webhooks: /webhook/<channel> aliases serve the same handlers")
