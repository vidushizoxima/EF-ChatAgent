"""
local_chat.py — talk to the agent from a terminal, no Meta webhooks involved.

    python scripts/local_chat.py                 # whatsapp channel, fake number
    python scripts/local_chat.py instagram       # pick a channel
    python scripts/local_chat.py whatsapp 919876543210

Uses the same session store and the same code path as a real webhook, so what you
see here is what a customer gets. Ctrl-C to exit; `/reset` clears the session.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chatbot"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import logging  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

from agent import process_query  # noqa: E402
from client.base_connection import get_session_id  # noqa: E402
from client.store import SessionStore  # noqa: E402


async def main():
    channel = sys.argv[1] if len(sys.argv) > 1 else "whatsapp"
    identifier = sys.argv[2] if len(sys.argv) > 2 else "919999900000"
    session_id = get_session_id(channel, identifier)

    store = SessionStore(session_id, channel=channel)
    store.update_user_info({"phone": identifier, "sender_id": identifier, "source": channel, "name": "Test User"})

    print(f"\n💬 channel={channel}  session={session_id}\n   /reset clears it, Ctrl-C exits\n")

    while True:
        try:
            query = input("you › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not query:
            continue
        if query == "/reset":
            store.clear_session()
            print("   session cleared\n")
            continue

        print("bot › ", end="", flush=True)
        async for chunk in process_query(query, session_id, channel=channel):
            kind = chunk.get("type")
            if kind == "token":
                print(chunk["content"], end="", flush=True)
            elif kind == "tool_start":
                print(f"\n   🛠️  {chunk['tool_name']}(…)", flush=True)
            elif kind == "tool_result":
                print(f"   ↩️  {chunk['content'][:200]}\nbot › ", end="", flush=True)
            elif kind == "error":
                print(f"\n   ❌ {chunk['error']}", flush=True)
        print("\n")


if __name__ == "__main__":
    asyncio.run(main())
