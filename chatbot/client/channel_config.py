"""
channel_config.py — what each channel runs with.

One entry per channel: which prompt file to load, which tools the agent may call,
whether the response streams, and which tools are allowed at most once per session.

Tool names must match the names registered in chatbot/tools/__init__.py.
"""

from dataclasses import dataclass, field
from typing import List, Literal


@dataclass
class ChannelConfig:
    prompt_id: str                       # prompts/<prompt_id>.md
    tools: List[str]                     # tool names exposed to the LLM
    response_format: Literal["streaming", "complete"]
    once_per_session: List[str] = field(default_factory=list)  # hard-guarded against repeat calls
    max_response_chars: int = 4096       # platform message limit


CHANNEL_CONFIGS = {
    "whatsapp": ChannelConfig(
        prompt_id="whatsapp",
        tools=[
            "identify_customer",
            "create_lead",
            "update_lead_details",
            "get_renewal_plans",
            "lookup_knowledge",
            "start_amc_renewal",
            "log_renewal_outcome",
            "escalate_to_human",
            "register_purchase_interest",
            "send_offer_brochure",
            "raise_service_request",
            "book_service_visit",
        ],
        response_format="complete",
        # Identity is resolved once; a lead is created once; a renewal is started once.
        # Guarded here as well as in the prompt because the model will retry otherwise.
        once_per_session=["create_lead", "start_amc_renewal", "log_renewal_outcome",
                          "escalate_to_human", "register_purchase_interest", "send_offer_brochure"],
        max_response_chars=4096,
    ),
    "instagram": ChannelConfig(
        prompt_id="instagram",
        tools=[
            "identify_customer",
            "create_lead",
            "update_lead_details",
            "start_amc_renewal",
            "raise_service_request",
            "book_service_visit",
        ],
        response_format="complete",
        once_per_session=["create_lead", "start_amc_renewal"],
        max_response_chars=1000,
    ),
    "facebook": ChannelConfig(
        prompt_id="facebook",
        tools=[
            "identify_customer",
            "create_lead",
            "update_lead_details",
            "lookup_knowledge",
            "register_purchase_interest",
            "send_offer_brochure",
            "start_amc_renewal",
            "raise_service_request",
            "book_service_visit",
        ],
        response_format="complete",
        once_per_session=["create_lead", "start_amc_renewal",
                          "register_purchase_interest", "send_offer_brochure"],
        max_response_chars=2000,
    ),
}

DEFAULT_CHANNEL = "whatsapp"


def get_channel_config(channel: str) -> ChannelConfig:
    """Config for a channel, falling back to WhatsApp."""
    return CHANNEL_CONFIGS.get((channel or "").lower(), CHANNEL_CONFIGS[DEFAULT_CHANNEL])


def get_available_channels() -> List[str]:
    return list(CHANNEL_CONFIGS.keys())
