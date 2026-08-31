"""
config.py — environment + LLM factory.

Single tenant: every knob comes from the environment. The provider switch is kept
(azure_openai today, anthropic/gemini wired) so swapping models later is a two-line
env change rather than a code change.
"""

import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ==================== RUNTIME ====================

APP_ENV = os.getenv("APP_ENV", "production").lower()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
PORT = int(os.getenv("PORT", "8000"))


def is_dev_mode() -> bool:
    return APP_ENV == "development"


# ==================== BRAND ====================

BRAND_NAME = os.getenv("BRAND_NAME", "Eureka Forbes")
AGENT_NAME = os.getenv("AGENT_NAME", "Asha")
SUPPORT_NUMBER = os.getenv("SUPPORT_NUMBER", "")


# ==================== LLM ====================

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "azure_openai").lower()
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "4096"))


def _temperature() -> Optional[float]:
    """gpt-5.x reasoning models reject any temperature but the default.

    Empty LLM_TEMPERATURE means 'do not send the parameter at all'.
    """
    raw = (os.getenv("LLM_TEMPERATURE") or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning(f"⚠️ Invalid LLM_TEMPERATURE={raw!r} — omitting the parameter")
        return None


def _require(keys: list, context: str):
    missing = [k for k in keys if not os.getenv(k)]
    if missing:
        raise ValueError(f"[{context}] Missing required environment variables: {missing}")


def create_chat_llm(model: Optional[str] = None, streaming: bool = True, max_retries: int = 5):
    """Build the chat LLM for the agent.

    azure_openai talks to the v1-compatible surface
    (https://<resource>.cognitiveservices.azure.com/openai/v1), so it is driven
    through ChatOpenAI with a base_url — no deployment name or api-version needed.
    """
    provider = LLM_PROVIDER
    temperature = _temperature()

    if provider == "azure_openai":
        _require(["AZURE_LLM_ENDPOINT", "AZURE_LLM_API_KEY"], "Azure OpenAI chat")
        from langchain_openai import ChatOpenAI

        model_name = model or os.getenv("AZURE_LLM_MODEL", "gpt-5.4-mini")
        kwargs = dict(
            model=model_name,
            base_url=os.getenv("AZURE_LLM_ENDPOINT"),
            api_key=os.getenv("AZURE_LLM_API_KEY"),
            max_tokens=MAX_OUTPUT_TOKENS,
            streaming=streaming,
            # Without this, a streamed response carries no usage metadata and every
            # token count in the logs and /session/*/stats reads zero.
            stream_usage=streaming,
            max_retries=max_retries,
            timeout=90.0,
        )
        if temperature is not None:
            kwargs["temperature"] = temperature
        logger.info(f"🤖 Azure OpenAI | model={model_name} | temperature={temperature if temperature is not None else 'default'}")
        return ChatOpenAI(**kwargs)

    if provider == "anthropic":
        _require(["ANTHROPIC_API_KEY"], "Anthropic chat")
        from langchain_anthropic import ChatAnthropic

        model_name = model or os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
        logger.info(f"🤖 Anthropic | model={model_name}")
        return ChatAnthropic(
            model=model_name,
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            temperature=temperature if temperature is not None else 0.0,
            max_tokens=MAX_OUTPUT_TOKENS,
            streaming=streaming,
            max_retries=max_retries,
            default_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )

    if provider in ("googlegemini", "gemini"):
        _require(["GEMINI_API_KEY"], "Gemini chat")
        from langchain_openai import ChatOpenAI

        model_name = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        logger.info(f"🤖 Google Gemini | model={model_name}")
        return ChatOpenAI(
            model=model_name,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=os.getenv("GEMINI_API_KEY"),
            temperature=temperature if temperature is not None else 0.0,
            max_tokens=MAX_OUTPUT_TOKENS,
            streaming=streaming,
            max_retries=max_retries,
        )

    raise ValueError(
        f"Unknown LLM_PROVIDER: '{provider}'. Use 'azure_openai', 'anthropic', or 'gemini'."
    )


_summary_llm = None


def create_summary_llm():
    """Cheap non-streaming model for the background rolling summary.

    One retry only: a summary is a nicety, and a long retry chain would hold the
    session's summary lock while the customer waits to send the next message.
    """
    global _summary_llm
    if _summary_llm is None:
        _summary_llm = create_chat_llm(
            model=os.getenv("SUMMARY_MODEL") or None,
            streaming=False,
            max_retries=1,
        )
    return _summary_llm


def supports_prompt_caching() -> bool:
    """Anthropic-style explicit cache_control blocks."""
    return LLM_PROVIDER == "anthropic"
