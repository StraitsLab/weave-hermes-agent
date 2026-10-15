"""Harso (StraitsLab fork) HTTP routes on the API server.

Upstream plugins cannot add HTTP routes, so the fork adds exactly one call in
``APIServerAdapter._http_route_table()`` that extends the table with these.
Fork code lives in its own modules; core files keep only small call-site hooks.
Each work stream adds its import + extend lines directly below its own marker.
"""
from __future__ import annotations

from typing import Any


def _http_routes(adapter: Any) -> list[tuple[str, str, Any]]:
    routes: list[tuple[str, str, Any]] = []
    # seam:s1:routes (native sessions: submit/events/approvals/clarify/append/credential/fork)
    # seam:s2:routes (cron: /api/jobs/{job_id}/runs)
    from gateway.platforms import api_server_harso_cron as cron
    routes.extend(route for route in cron._http_routes(adapter) if route[1].endswith("/runs"))
    # seam:s5:routes (MCP: /v1/mcp/reload, /v1/mcp/refresh)
    return routes
