from dataclasses import dataclass


@dataclass
class AgentContext:
    """Runtime context handed to agent.astream() — immutable per invocation.

    Reaches middleware as request.runtime.context.
    """
    session_id: str       # SQLite session key
    user_id: str          # phone number / platform sender id
    channel: str = "whatsapp"
