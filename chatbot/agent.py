"""
agent.py — the Eureka Forbes social agent.

Structure mirrors the GoEd reference project:

    channel  →  prompt file + tool list  →  agent singleton
    every model call passes through middleware that injects live session context
    every tool call passes through middleware that injects session_id and guards
    repeats

What is different here: no MCP (tools are local), no Redis (SQLite), no
multi-tenant lookup (one brand, env-driven).
"""

import asyncio
import datetime as _dt
import json
import logging
import os
from typing import Any, Dict, Optional

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain.messages import SystemMessage
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from client.channel_config import get_channel_config
from client import offers
from client.config import BRAND_NAME, AGENT_NAME, SUPPORT_NUMBER, create_chat_llm, supports_prompt_caching


def _payment_block() -> str:
    """The renewal payment link, or an explicit 'there isn't one'."""
    if not offers.PAYMENT_LINK:
        return (
            "There is NO payment link available. Do not send one, do not invent a URL, "
            "and do not ask for card or UPI details — tell them our team will call to "
            "take payment."
        )
    return (
        f"The renewal payment link is {offers.PAYMENT_LINK} — send it as plain text, "
        f"exactly as written. Never ask for card, UPI or bank details yourself, and "
        f"never say the renewal is active until the payment has actually gone through."
    )


def _offer_block() -> str:
    """The live campaign, as the agent should hear it — or an explicit 'nothing on'."""
    if not offers.is_live():
        return (
            "There is NO promotional offer running right now. Do not invent one, and "
            "do not repeat an offer from a previous campaign."
        )
    terms = "\n".join(f"  - {line}" for line in offers.TERMS)
    return (
        f"A campaign is running until {offers.pretty_end_date()}:\n{terms}\n"
        f"  - It applies to every product and every range. Do NOT ask which product "
        f"they want before telling them about it."
    )
from client.prompt_loader import load_prompt, prompt_changed, prompt_fingerprint
from client.session_logger import SessionLogger
from client.store import SessionStore
from state import AgentContext
from tools import get_tools

logger = logging.getLogger(__name__)

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# channel -> agent singleton, plus the prompt fingerprint it was built from
_agent_instances: Dict[str, Any] = {}
_agent_fingerprints: Dict[str, Optional[float]] = {}
_channel_prompts: Dict[str, str] = {}
_channel_tools: Dict[str, list] = {}
_agent_lock = asyncio.Lock()


# ==================== MIDDLEWARE ====================

class InjectSessionContext(AgentMiddleware):
    """Assemble the system message for each model call.

    Block order is chosen for prefix caching:
        0  base prompt        — static for the channel
        1  date/time          — changes every minute
        2  session context    — profile, CRM record, guards; stable within a session
        3  conversation       — changes every turn
    """

    async def awrap_model_call(self, request: ModelRequest, handler) -> ModelResponse:
        ctx = request.runtime.context
        session_id = getattr(ctx, "session_id", None) if ctx else None
        if not session_id:
            return await handler(request)

        channel = getattr(ctx, "channel", "whatsapp") if ctx else "whatsapp"
        store = SessionStore(session_id, channel=channel)
        session_logger = SessionLogger(session_id)

        static_parts = []
        volatile_parts = []

        # ── Date / time (IST is the source of truth for anything scheduled) ──
        now_ist = _dt.datetime.now(IST)
        current_date_str = now_ist.strftime("%A, %B %d, %Y")
        current_time_str = now_ist.strftime("%I:%M %p IST")
        datetime_context = f"""CURRENT DATE/TIME (IST)
Date: {current_date_str}
Time: {current_time_str}

Use this as the source of truth for every time-based decision.
Never describe a past visit, callback or appointment as upcoming.

PROMPT VARIABLES:
{{current_date}}={current_date_str}
{{current_time}}={current_time_str}
{{current_weekday}}={now_ist.strftime('%A')}
{{date_iso}}={now_ist.strftime('%Y-%m-%d')}
"""

        # ── Customer profile captured by the webhook ──
        user_info = store.get_user_info()
        session_logger.log_user_info(user_info)

        customer_name = ""
        customer_phone = ""
        if user_info:
            customer_name = user_info.get("name", "") or ""
            customer_phone = user_info.get("phone", "") or ""
            sender_id = (user_info.get("sender_id") or "").split(":")[0]
            static_parts.append(f"""CUSTOMER PROFILE ({user_info.get('source', channel).upper()})
Name: {user_info.get('name') or 'Unknown'}
Phone: {customer_phone or 'Unknown'}
Email: {user_info.get('email') or 'Unknown'}
City: {user_info.get('city') or 'Unknown'}
Sender ID: {sender_id or 'Unknown'}

Do NOT ask for anything already listed above.""")

        # ── CRM record, if a tool resolved one ──
        lead_id = store.get_lead_id()
        lead_data = store.get_existing_lead_data()
        if lead_id:
            static_parts.append(
                f"KNOWN CRM RECORD (id: {lead_id})\n"
                f"This customer is already identified. Do NOT look them up again."
            )
            if lead_data:
                history = lead_data.get("context") or lead_data.get("summary")
                if history:
                    static_parts.append(f"Previous CRM context:\n{history}\nDo not re-ask anything covered above.")

        # ── Carry-over summary from an earlier day ──
        carried = store.get("carried_summary")
        if carried:
            static_parts.append(f"EARLIER CONVERSATION (previous day)\n{carried}")

        # ── Tools already used, so the model stops retrying them ──
        config = get_channel_config(channel)
        blocked = [name for name in config.once_per_session if store.exists(f"tool_done:{name}")]
        if blocked:
            static_parts.append(
                f"System: these tools already ran in this session and are blocked: {', '.join(blocked)}. "
                f"Do NOT call them again — answer the customer directly."
            )

        checked_phones = store.smembers("checked_phones")
        if checked_phones:
            static_parts.append(
                "CRM SEARCH HISTORY\nAlready looked up: " + ", ".join(checked_phones) +
                "\nDo not repeat these lookups; use what came back."
            )

        # ── Flow hint: was the last thing we said a question? ──
        transcript = store.get_full_transcript()
        last_assistant = next(
            (m for m in reversed(transcript) if m["role"].lower() == "assistant"), None
        )
        if last_assistant and last_assistant["content"].strip().endswith("?"):
            volatile_parts.append(
                "FLOW HINT: your last message was a question. A short reply is the answer to it — "
                "acknowledge briefly and move on, do not repeat the question or start a new search."
            )

        # ── House rules ──
        static_parts.append(f"""
<Rules>
- Reuse what you already know (name, phone, product, city). Never re-ask.
- Reply ONLY to the latest message. No preamble, no filler.
- When calling a tool, emit ONLY the tool call — no text around it.
- Call each tool at most once per turn. If it returns nothing, say so honestly.
- Keep replies under {config.max_response_chars // 8} words — this is a {channel} chat, not an email.
- Never invent prices, model numbers, warranty terms or appointment slots.
- If you cannot help, hand off to {SUPPORT_NUMBER or 'the support team'}.
</Rules>
""")

        # ── Interpolate variables into the base prompt ──
        if hasattr(request.system_message, "content_blocks"):
            blocks = list(request.system_message.content_blocks)
        elif isinstance(request.system_message.content, list):
            blocks = list(request.system_message.content)
        else:
            blocks = [{"type": "text", "text": str(request.system_message.content)}]

        replacements = {
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "brand_name": BRAND_NAME,
            "agent_name": AGENT_NAME,
            "support_number": SUPPORT_NUMBER,
            # Kept in step with the reminder templates, which quote the same figure.
            "renewal_offer_pct": str(offers.DISCOUNT_PCT),
            # Injected rather than fetched by a tool: the offer is relevant to almost
            # every conversation, and it goes stale on its own end date without anyone
            # editing the prompt.
            "current_offer": _offer_block(),
            "payment_link": _payment_block(),
            "current_date": current_date_str,
            "current_time": current_time_str,
            "channel": channel,
        }

        new_content = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block["text"]
                for key, value in replacements.items():
                    text = text.replace("{{" + key + "}}", value).replace("{" + key + "}", value)
                new_content.append({"type": "text", "text": text})
            else:
                new_content.append(block)

        new_content.append({"type": "text", "text": datetime_context})

        if static_parts:
            static_text = "\n".join(static_parts)
            session_logger.log_system_message(static_text)
            new_content.append({"type": "text", "text": static_text})

        # Anthropic-style caching marks the static prefix; OpenAI caches automatically
        if supports_prompt_caching():
            for block in new_content:
                if isinstance(block, dict):
                    block["cache_control"] = {"type": "ephemeral"}

        # ── Conversation history goes AFTER the cache breakpoint ──
        conversation = store.get_context_for_chat()
        if conversation:
            volatile_parts.append(
                f"CONVERSATION HISTORY (REFERENCE ONLY)\n{conversation}\n\n"
                f"Do NOT re-answer any of it. Respond only to the current message."
            )

        if volatile_parts:
            new_content.append({"type": "text", "text": "\n".join(volatile_parts)})

        # OpenAI-compatible endpoints want a plain string; Anthropic wants blocks
        if supports_prompt_caching():
            new_system_message = SystemMessage(content=new_content)
            new_messages = list(request.messages)
        else:
            flat = "\n\n".join(
                b["text"] if isinstance(b, dict) and "text" in b else str(b) for b in new_content
            )
            new_system_message = SystemMessage(content=flat.strip())
            new_messages = [_stringify(m) for m in request.messages]

        return await handler(
            request.override(system_message=new_system_message, messages=new_messages)
        )


def _stringify(msg):
    """OpenAI-compatible gateways reject list content on tool/assistant messages."""
    if isinstance(msg.content, str):
        return msg
    parts = []
    if isinstance(msg.content, list):
        for block in msg.content:
            if isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            else:
                parts.append(str(block))
    else:
        parts.append(str(msg.content))
    try:
        return msg.copy(update={"content": "\n\n".join(parts)})
    except Exception:
        msg.content = "\n\n".join(parts)
        return msg


class HandleToolErrors(AgentMiddleware):
    """Inject session context into tool args, guard once-per-session tools, and
    turn any tool exception into a message the model can recover from."""

    async def awrap_tool_call(self, request, handler):
        tool_name = request.tool_call.get("name")
        ctx = getattr(request.runtime, "context", None)
        session_id = getattr(ctx, "session_id", None) if ctx else None
        channel = getattr(ctx, "channel", "whatsapp") if ctx else "whatsapp"

        try:
            if session_id:
                store = SessionStore(session_id, channel=channel)
                config = get_channel_config(channel)

                # Hard guard: the model ignores "don't call this again" in the prompt,
                # so block it here instead of letting it loop.
                if tool_name in config.once_per_session and store.exists(f"tool_done:{tool_name}"):
                    logger.info(f"🛡️ Blocked repeat call to {tool_name} for {session_id}")
                    return ToolMessage(
                        content=json.dumps({
                            "status": "already_done",
                            "message": (
                                f"SYSTEM GUARD: {tool_name} already ran in this session and was blocked. "
                                f"Do not call it again — reply to the customer directly."
                            ),
                        }),
                        tool_call_id=request.tool_call.get("id"),
                    )

                # Auto-inject session_id for any tool that declares it
                tool_obj = getattr(request, "tool", None)
                declares_session = True
                if tool_obj is not None and getattr(tool_obj, "args_schema", None) is not None:
                    try:
                        declares_session = "session_id" in tool_obj.args_schema.model_fields
                    except Exception:
                        declares_session = True
                if declares_session:
                    request.tool_call.setdefault("args", {})
                    request.tool_call["args"]["session_id"] = session_id

            return await handler(request)

        except Exception as e:
            logger.warning(f"Tool error in {tool_name}: {e}")
            return ToolMessage(
                content=json.dumps({"status": "error", "message": f"Tool execution failed: {e}"}),
                tool_call_id=request.tool_call.get("id"),
            )


# ==================== AGENT FACTORY ====================

async def get_or_create_agent(channel: str = "whatsapp"):
    """One agent per channel, rebuilt automatically when its prompt file changes."""
    channel = (channel or "whatsapp").lower()
    config = get_channel_config(channel)

    cached = _agent_instances.get(channel)
    if cached is not None and not prompt_changed(config.prompt_id):
        return cached

    async with _agent_lock:
        cached = _agent_instances.get(channel)
        if cached is not None and not prompt_changed(config.prompt_id):
            return cached

        base_prompt = load_prompt(config.prompt_id)
        if not base_prompt.strip():
            raise RuntimeError(
                f"Prompt '{config.prompt_id}' is empty or missing — expected prompts/{config.prompt_id}.md"
            )

        selected_tools = get_tools(config.tools)
        _channel_tools[channel] = selected_tools
        _channel_prompts[channel] = base_prompt

        if supports_prompt_caching():
            system_prompt = SystemMessage(
                content=[{"type": "text", "text": base_prompt, "cache_control": {"type": "ephemeral"}}]
            )
        else:
            system_prompt = base_prompt

        agent = create_agent(
            model=create_chat_llm(),
            tools=selected_tools,
            system_prompt=system_prompt,
            middleware=[InjectSessionContext(), HandleToolErrors()],
            context_schema=AgentContext,
        )

        _agent_instances[channel] = agent
        _agent_fingerprints[channel] = prompt_fingerprint(config.prompt_id)

        logger.info(json.dumps({
            "event": "agent_created",
            "channel": channel,
            "prompt_id": config.prompt_id,
            "prompt_chars": len(base_prompt),
            "tools": [t.name for t in selected_tools],
            "response_format": config.response_format,
        }))
        return agent


def get_channel_tools(channel: str) -> list:
    return _channel_tools.get((channel or "").lower(), [])


# ==================== QUERY PROCESSING ====================

async def process_query(query: str, session_id: str, channel: str = "whatsapp", user_id: str = None):
    """Run one customer message through the agent, yielding stream events.

    Yields dicts: {"type": "token"|"tool_start"|"tool_result"|"error", ...}
    Social channels accumulate the tokens and send one message at the end.
    """
    channel = (channel or "whatsapp").lower()
    user_id = user_id or session_id
    store = SessionStore(session_id, channel=channel)
    session_logger = SessionLogger(session_id)

    agent = await get_or_create_agent(channel)

    session_logger.start_interaction(
        user_query=query,
        system_prompt=_channel_prompts.get(channel, "[prompt not cached]"),
        context=store.get_context_for_chat(),
    )

    await store.add_message("user", query)

    full_response = ""
    yielded_text = ""
    total_input = total_output = total_cache_read = total_cache_creation = 0
    pending_tool_args: Dict[str, dict] = {}

    try:
        async for token, meta in agent.astream(
            {"messages": [{"role": "user", "content": query}]},
            context={"session_id": session_id, "user_id": user_id, "channel": channel},
            stream_mode="messages",
            config={"recursion_limit": 15},  # stops runaway tool loops
        ):
            node = meta.get("langgraph_node") if meta else None
            if node == "__start__":
                continue

            if isinstance(token, AIMessage):
                is_chunk = isinstance(token, AIMessageChunk)

                if getattr(token, "tool_calls", None):
                    # Text emitted before a tool call is filler — drop it
                    full_response = ""
                    yielded_text = ""
                    for tool_call in token.tool_calls:
                        # Streaming emits partial tool_call chunks with no name yet;
                        # only announce the ones that are actually resolved.
                        name = tool_call.get("name")
                        if not name:
                            continue
                        pending_tool_args[tool_call.get("id", "")] = {
                            "name": name,
                            "args": tool_call.get("args", {}),
                        }
                        yield {
                            "type": "tool_start",
                            "tool_name": name,
                            "tool_id": tool_call.get("id", ""),
                        }

                if token.content:
                    text = ""
                    if isinstance(token.content, str):
                        text = token.content
                    elif isinstance(token.content, list):
                        for block in token.content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text += block.get("text", "")
                            elif isinstance(block, str):
                                text += block

                    if text:
                        # LangGraph re-emits the complete AIMessage at node exit —
                        # yield only what the chunks have not already covered.
                        new_text = text if is_chunk else text[len(yielded_text):]
                        if new_text:
                            yielded_text += new_text
                            full_response += new_text
                            yield {"type": "token", "content": new_text, "node": node or "model"}

                usage = getattr(token, "usage_metadata", None)
                if usage:
                    usage = usage if isinstance(usage, dict) else vars(usage)
                    total_input += usage.get("input_tokens", 0) or 0
                    total_output += usage.get("output_tokens", 0) or 0
                    details = usage.get("input_token_details") or {}
                    if isinstance(details, dict):
                        total_cache_read += details.get("cache_read", 0) or details.get("cached_tokens", 0) or 0
                        total_cache_creation += details.get("cache_creation", 0) or 0

            elif isinstance(token, ToolMessage):
                full_response = ""
                yielded_text = ""
                logger.info(f"🛠️ [TOOL EXECUTED] {token.name}")

                call = pending_tool_args.get(getattr(token, "tool_call_id", ""), {})
                args = call.get("args", {})

                # Remember what was looked up, so the model does not repeat it
                phone_arg = args.get("phone") or args.get("phone_number")
                if phone_arg:
                    digits = "".join(c for c in str(phone_arg) if c.isdigit())
                    if len(digits) >= 10:
                        store.sadd("checked_phones", digits[-10:])

                config = get_channel_config(channel)
                if token.name in config.once_per_session:
                    store.set(f"tool_done:{token.name}", "1")

                # Any tool that resolves an identity gets cached on the session
                data = _parse_tool_content(token.content)
                if isinstance(data, dict):
                    record_id = data.get("record_id") or data.get("lead_id") or data.get("customer_id")
                    if record_id and data.get("status") in ("found", "success"):
                        store.set_lead_id(record_id)
                        store.set_existing_lead_data(data)
                        logger.info(f"✅ Cached CRM record {record_id} from {token.name}")

                tool_content = token.content if isinstance(token.content, str) else json.dumps(
                    token.content, default=str
                )
                session_logger.log_tool_call(token.name or "unknown", args, tool_content)

                yield {
                    "type": "tool_result",
                    "tool_name": token.name or "unknown",
                    "content": tool_content,
                    "node": node or "tools",
                }

    except asyncio.CancelledError:
        logger.warning(f"⚠️ Stream cancelled for {session_id}")
        if full_response:
            await store.add_message("assistant", full_response + " [interrupted]")
        return

    except Exception as e:
        logger.error(f"❌ Agent error for {session_id}: {e}", exc_info=True)
        err = str(e)
        if any(p in err for p in ("content_filter", "ResponsibleAIPolicyViolation", "content management policy")):
            err = (
                f"That request falls outside our safety guidelines. "
                f"Please ask about {BRAND_NAME} products and services."
            )
        yield {"type": "error", "error": err}
        return

    await store.add_message(
        "assistant",
        full_response,
        input_tokens=total_input,
        output_tokens=total_output,
        cache_read_tokens=total_cache_read,
        cache_creation_tokens=total_cache_creation,
    )
    session_logger.end_interaction(full_response, total_input, total_output)
    logger.info(
        f"✅ [TURN COMPLETE] session={session_id} | channel={channel} | "
        f"in={total_input} out={total_output} cached={total_cache_read}"
    )


def _parse_tool_content(content) -> Any:
    """Tool results arrive as str, dict, or a one-element list — normalise to dict."""
    data = content
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return data
    if isinstance(data, list) and data:
        item = data[0]
        text = getattr(item, "text", None) or (item.get("text") if isinstance(item, dict) else None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return item
    return data
