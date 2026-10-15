"""Allowlisted answer-card projection for the native event contract."""
import json
from typing import Any, Optional, Dict

HUB = "mcp__weave_connectors__"  # the cell's name prefix for Harso hub tools
CODE_CARDS = {HUB + "weather_now", HUB + "page_read"}  # hub tools whose own code builds a card


def hub_card(tool_name: Any, result: Any) -> Optional[dict[str, Any]]:
    """WEV-3395: the answer card a Harso card tool returned (MCP structuredContent or result ``card``), else None.

    Only Harso's card tools count: ``show_<card>`` (weave-api fills and checks it) and CODE_CARDS. Every other hub
    tool can carry third-party output (Exa results, forwarded ``<provider>__<tool>`` connectors), so it never does.
    ``result`` is the raw MCP JSON string (a guardrail note may follow it) or that JSON already parsed.
    """
    name = str(tool_name or "")
    if name not in CODE_CARDS and not (name.startswith(HUB + "show_") and "__" not in name[len(HUB):]):
        return None
    try:
        payload = json.JSONDecoder().raw_decode(result)[0] if isinstance(result, str) else result
        content = payload.get("structuredContent") or payload.get("result")
        if isinstance(content, str):
            content = json.JSONDecoder().raw_decode(content)[0]
        card = content.get("card")
    except (ValueError, AttributeError):
        return None
    return card if isinstance(card, dict) and card.get("kind") == "output_blocks" else None
