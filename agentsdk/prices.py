"""FR-69 (M17): the dated price table that ships with the package.

It is the default source of `ModelPricing`: a caller-supplied price for a model wins,
and a model absent from the table stays unpriced rather than guessed. The table names
its own date, which travels with every price it supplies and is recorded in the
`ExecutionManifest`, so nobody reads a stale price as current fact.

It holds `openai.gpt-4o-mini` alone (DECISION-727f3a42). Anthropic's Haiku 4.5 is left
out although this project uses it through the gateway: it runs on Bedrock, which sets
its own prices and charges more on regional endpoints than global ones, and neither the
Bedrock rate nor the gateway's endpoint type could be established (KNOWLEDGE-b40dd9de).
A USD budget on it therefore needs a caller-supplied `ModelPricing`, exactly as for any
other model outside the table.

These are list prices. A gateway or reseller may bill something else.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from agentsdk.registry import ModelPricing

__all__ = [
    "PRICE_TABLE",
    "PRICE_TABLE_DATE",
    "PRICE_TABLE_PATH",
    "PRICE_TABLE_SOURCES",
    "shipped_pricing",
]

PRICE_TABLE_PATH = Path(__file__).resolve().parent / "prices.json"
_DOCUMENT = json.loads(PRICE_TABLE_PATH.read_text(encoding="utf-8"))

PRICE_TABLE_DATE: str = _DOCUMENT["date"]


def _price(value: str | None) -> Decimal | None:
    """A price as written in the table, as a Decimal. Absent means unpriced."""
    return None if value is None else Decimal(value)


PRICE_TABLE: Mapping[str, ModelPricing] = MappingProxyType({
    model_id: ModelPricing(
        input=_price(entry.get("input")),
        output=_price(entry.get("output")),
        cache_read=_price(entry.get("cache_read")),
        cache_write=_price(entry.get("cache_write")),
    )
    for model_id, entry in _DOCUMENT["models"].items()
})
PRICE_TABLE_SOURCES: Mapping[str, str] = MappingProxyType({
    model_id: entry["source"] for model_id, entry in _DOCUMENT["models"].items()
})


def shipped_pricing(model_id: str | None) -> ModelPricing | None:
    """The table's price for `model_id`, or None when it does not list it.

    None means "no price here", never "free": the caller must supply one, and a USD
    budget on an unpriced model is refused at the call site (FR-69).
    """
    return PRICE_TABLE.get(model_id) if isinstance(model_id, str) else None
