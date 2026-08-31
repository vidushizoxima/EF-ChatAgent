"""
tools — the agent's tool registry.

Tools are plain LangChain tools in this process: no MCP server, no network hop.

Adding a tool:
    1. write it in ef_tools.py, decorated with @tool
    2. import it below and add it to REGISTRY
    3. list its name in the channels that may call it (client/channel_config.py)

A tool a channel does not list is invisible to the model on that channel.
"""

import logging
from typing import Dict, List

from langchain_core.tools import BaseTool

from tools.ef_tools import (
    book_service_visit,
    create_lead,
    escalate_to_human,
    get_renewal_plans,
    identify_customer,
    lookup_knowledge,
    log_renewal_outcome,
    raise_service_request,
    register_purchase_interest,
    send_offer_brochure,
    start_amc_renewal,
    update_lead_details,
)

logger = logging.getLogger(__name__)

REGISTRY: Dict[str, BaseTool] = {
    t.name: t
    for t in [
        identify_customer,
        create_lead,
        update_lead_details,
        get_renewal_plans,
        lookup_knowledge,
        start_amc_renewal,
        log_renewal_outcome,
        escalate_to_human,
        register_purchase_interest,
        send_offer_brochure,
        raise_service_request,
        book_service_visit,
    ]
}


def get_tools(names: List[str]) -> List[BaseTool]:
    """Resolve tool names to instances, warning loudly about typos."""
    selected = []
    for name in names:
        tool = REGISTRY.get(name)
        if tool is None:
            logger.error(f"❌ Unknown tool '{name}' — not in the registry: {sorted(REGISTRY)}")
            continue
        selected.append(tool)
    return selected


def available_tools() -> List[str]:
    return sorted(REGISTRY)
