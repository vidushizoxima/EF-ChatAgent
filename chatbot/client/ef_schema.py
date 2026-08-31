"""
ef_schema.py — schema-driven access to the Eureka Forbes Dataverse model.

Loads ef_schema_reference.json once so nothing else hard-codes an entity-set name,
a primary key, or a magic option-set integer. `ef_disposition` alone has 19 values;
sprinkling `100000411` through the code makes it unreadable and it breaks silently
when the solution is re-published with different numbers.

    apiset("ef_lead")                      -> "ef_leads"
    pk("ef_customer")                      -> "ef_customerid"
    choice("ef_interaction", "ef_channel", "WhatsApp")  -> 100000000
    label("ef_lead", "ef_status", 100000001)            -> "Working"
    bind("ef_customer", guid)              -> "/ef_customers(<guid>)"
"""

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ef_schema_reference.json",
)


@lru_cache(maxsize=1)
def schema() -> dict:
    path = os.getenv("EF_SCHEMA_FILE", _DEFAULT_PATH)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    logger.info(
        f"📐 EF schema '{data.get('solution')}' loaded — "
        f"{len(data.get('tables', {}))} tables, {len(data.get('globalChoices', {}))} global choices"
    )
    return data


def table(logical_name: str) -> dict:
    t = schema()["tables"].get(logical_name)
    if not t:
        raise KeyError(f"Unknown EF table '{logical_name}'. Known: {sorted(schema()['tables'])}")
    return t


def apiset(logical_name: str) -> str:
    """OData entity-set name, e.g. ef_lead -> ef_leads."""
    return table(logical_name)["apiSet"]


def pk(logical_name: str) -> str:
    return table(logical_name)["primaryId"]


def primary_name(logical_name: str) -> str:
    return table(logical_name)["primaryName"]


def columns(logical_name: str) -> dict:
    return table(logical_name)["columns"]


def has_column(logical_name: str, column: str) -> bool:
    return column in columns(logical_name)


def bind(logical_name: str, guid: str) -> str:
    """@odata.bind target, e.g. '/ef_customers(<guid>)'."""
    return f"/{apiset(logical_name)}({str(guid).strip('{} ')})"


def nav(logical_name: str, attribute: str) -> str:
    """Navigation property name for a lookup column.

    Dataverse binds relationships by navigation property, which is PascalCase and
    NOT the attribute name: ef_interaction.ef_lead binds as 'ef_Lead'. Getting this
    wrong fails with "undeclared property ... only has property annotations".
    """
    for rel in table(logical_name).get("manyToOne", []):
        if rel.get("attribute") == attribute:
            return rel.get("nav") or attribute
    raise KeyError(
        f"'{logical_name}' has no lookup '{attribute}'. "
        f"Lookups: {[r.get('attribute') for r in table(logical_name).get('manyToOne', [])]}"
    )


def bind_key(logical_name: str, attribute: str) -> str:
    """The payload key that binds a lookup, e.g. 'ef_Lead@odata.bind'."""
    return f"{nav(logical_name, attribute)}@odata.bind"


def datetime_value(logical_name: str, column: str, moment) -> str:
    """Format a datetime the way THIS column expects it.

    Dataverse distinguishes DateOnly (Edm.Date, "2026-08-21") from DateAndTime
    (Edm.DateTimeOffset). The same logical field differs between tables — customer
    date fields are DateOnly while the lead's are DateAndTime — so the format is
    read from the schema rather than assumed.
    """
    meta = columns(logical_name).get(column, {})
    if meta.get("format") == "DateOnly":
        return moment.strftime("%Y-%m-%d")
    return moment.replace(microsecond=0).isoformat()


def _options(logical_name: str, column: str) -> Dict[str, str]:
    meta = columns(logical_name).get(column)
    if not meta:
        raise KeyError(f"'{logical_name}' has no column '{column}'")
    opts = meta.get("options")
    if opts:
        return opts
    global_choice = meta.get("globalChoice")
    if global_choice:
        return schema()["globalChoices"][global_choice]["options"]
    raise KeyError(f"'{logical_name}.{column}' is not a choice column")


def choice(logical_name: str, column: str, name: str) -> int:
    """Option-set value by its label, case-insensitively."""
    for value, label_text in _options(logical_name, column).items():
        if str(label_text).lower() == str(name).lower():
            return int(value)
    raise KeyError(
        f"'{name}' is not a valid {logical_name}.{column} option. "
        f"Valid: {sorted(_options(logical_name, column).values())}"
    )


def label(logical_name: str, column: str, value: Any) -> Optional[str]:
    """Human label for a stored option-set integer."""
    if value is None:
        return None
    return _options(logical_name, column).get(str(value))


def labels(logical_name: str, column: str) -> list:
    return sorted(_options(logical_name, column).values())
