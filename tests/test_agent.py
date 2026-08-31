"""Agent: context injection, history, tool wiring, guards, prompt hot-reload.

Uses a scripted fake model — no API key needed. Run from the repo root.
"""
import asyncio, json, os, sys, tempfile
os.environ["EF_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["APP_ENV"] = "production"
sys.path.insert(0, "chatbot")

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import agent as A
from client.store import SessionStore
from client.channel_config import CHANNEL_CONFIGS

captured = {}


class ScriptedChat(BaseChatModel):
    """Returns pre-scripted AIMessages and records the system prompt it was given."""

    replies: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        captured["system"] = messages[0].content
        captured["messages"] = messages
        reply = self.replies.pop(0)
        return ChatResult(generations=[ChatGeneration(message=reply)])

    def bind_tools(self, tools, **kwargs):
        return self


@tool
async def fake_lookup(phone: str, session_id: str = None) -> str:
    """Look up a customer by phone."""
    captured["tool_session_id"] = session_id
    return json.dumps({"status": "found", "record_id": "guid-42", "name": "Ramesh"})

A.REGISTRY_PATCH = None
import tools as T
T.REGISTRY["fake_lookup"] = fake_lookup

async def run(channel, replies, query="my aquaguard is leaking"):
    A._agent_instances.clear()
    A.create_chat_llm = lambda *a, **k: ScriptedChat(replies=list(replies))
    sid = f"{channel}:9199:test"
    store = SessionStore(sid, channel=channel)
    store.update_user_info({"phone": "919999900000", "name": "Ramesh", "sender_id": "919999900000", "source": channel})
    out = []
    async for chunk in A.process_query(query, sid, channel=channel):
        out.append(chunk)
    return sid, store, out

async def main():
    # ── 1. plain answer, no tools ──
    sid, store, out = await run("whatsapp", [AIMessage(content="Sorry to hear that. Which model is it?")])
    text = "".join(c["content"] for c in out if c["type"] == "token")
    assert "Which model" in text, out
    sysmsg = captured["system"]
    assert "CUSTOMER PROFILE" in sysmsg and "Ramesh" in sysmsg, sysmsg[:500]
    assert "CURRENT DATE/TIME (IST)" in sysmsg
    assert "<Rules>" in sysmsg
    assert isinstance(sysmsg, str), "OpenAI-compatible providers need a flat string system prompt"
    transcript = store.get_full_transcript()
    assert [m["role"] for m in transcript] == ["user", "assistant"], transcript
    print("✅ agent: plain turn + context injection")

    # ── 2. history reaches the next turn ──
    sid, store, out = await run("whatsapp", [AIMessage(content="Got it.")], query="the Aquasure model")
    assert "CONVERSATION HISTORY" in captured["system"], "history block missing"
    assert "leaking" in captured["system"], "earlier turn missing from history"
    print("✅ agent: conversation history carried forward")

    # ── 3. tool call: session_id injection, guard, CRM caching ──
    CHANNEL_CONFIGS["whatsapp"].tools = ["fake_lookup"]
    CHANNEL_CONFIGS["whatsapp"].once_per_session = ["fake_lookup"]
    replies = [
        AIMessage(content="", tool_calls=[{"name": "fake_lookup", "args": {"phone": "9876543210"}, "id": "call_1"}]),
        AIMessage(content="Thanks Ramesh, found your record."),
    ]
    sid, store, out = await run("whatsapp", replies, query="check my number 9876543210")
    kinds = [c["type"] for c in out]
    assert "tool_start" in kinds and "tool_result" in kinds, kinds
    assert captured["tool_session_id"] == sid, f"session_id not injected: {captured['tool_session_id']}"
    assert store.get_lead_id() == "guid-42", store.get_lead_id()
    assert store.exists("tool_done:fake_lookup"), "once_per_session flag not set"
    assert store.smembers("checked_phones") == ["9876543210"], store.smembers("checked_phones")
    text = "".join(c["content"] for c in out if c["type"] == "token")
    assert text.strip() == "Thanks Ramesh, found your record.", repr(text)
    print("✅ agent: tool call, session injection, CRM cache, filler dropped")

    # ── 4. the once-per-session guard blocks a repeat ──
    A._agent_instances.clear()
    A.create_chat_llm = lambda *a, **k: ScriptedChat(replies=[
        AIMessage(content="", tool_calls=[{"name": "fake_lookup", "args": {"phone": "9876543210"}, "id": "c2"}]),
        AIMessage(content="Already have it."),
    ])
    out = [c async for c in A.process_query("again", sid, channel="whatsapp")]
    guard = [c for c in out if c["type"] == "tool_result"]
    assert guard and "SYSTEM GUARD" in guard[0]["content"], guard
    assert captured["tool_session_id"] == sid  # unchanged: the real tool never ran
    print("✅ agent: repeat-call guard fires")

    # ── 5. prompt hot-reload rebuilds the agent ──
    A._agent_instances.clear()
    A.create_chat_llm = lambda *a, **k: ScriptedChat(replies=[AIMessage(content="hi")])
    await A.get_or_create_agent("instagram")
    first = A._agent_instances["instagram"]
    p = "prompts/instagram.md"
    original = open(p).read()
    open(p, "w").write(original + "\n<!-- touched -->\n")
    try:
        A.create_chat_llm = lambda *a, **k: ScriptedChat(replies=[AIMessage(content="hi")])
        second = await A.get_or_create_agent("instagram")
        assert second is not first, "prompt change did not rebuild the agent"
    finally:
        open(p, "w").write(original)
    print("✅ agent: prompt hot-reload")

asyncio.run(main())
