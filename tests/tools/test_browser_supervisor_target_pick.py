"""Initial attach prefers the agent's page over Chrome's internal tabs."""

import asyncio

import pytest

from tools import browser_supervisor as bs


NEWTAB = {"type": "page", "url": "chrome://newtab/", "targetId": "new-tab"}
BLANK = {"type": "page", "url": "about:blank", "targetId": "agent-tab"}
WEB = {"type": "page", "url": "https://example.com/", "targetId": "web-tab"}
UNTRUSTED = {
    "type": "page", "url": "chrome-untrusted://new-tab-page/", "targetId": "untrusted-tab",
}
DEVTOOLS = {"type": "page", "url": "devtools://devtools/bundled/", "targetId": "devtools-tab"}
WORKER = {"type": "service_worker", "url": "https://example.com/worker.js", "targetId": "worker"}


@pytest.mark.parametrize("targets, expected", [
    pytest.param([NEWTAB, BLANK], BLANK, id="newtab-first"),
    pytest.param([BLANK, NEWTAB], BLANK, id="newtab-last"),
    pytest.param([WORKER, NEWTAB], NEWTAB, id="internal-page-fallback"),
    pytest.param([], None, id="no-targets"),
    pytest.param([BLANK, UNTRUSTED], BLANK, id="skip-chrome-untrusted"),
    pytest.param([BLANK, DEVTOOLS], BLANK, id="skip-devtools"),
    pytest.param([BLANK, WEB, NEWTAB], WEB, id="last-eligible-page"),
    pytest.param([NEWTAB, UNTRUSTED, DEVTOOLS], NEWTAB, id="first-page-fallback"),
    pytest.param([WORKER], None, id="no-pages"),
    pytest.param([BLANK, WORKER], BLANK, id="ignore-worker"),
])
def test_initial_page_target_selection(targets, expected):
    assert bs._pick_page_target(targets) == expected


def test_initial_attach_uses_the_agent_tab_not_chromes_new_tab():
    supervisor = bs.CDPSupervisor("target-pick-test", "ws://test-browser")
    attached = []

    async def cdp(method, params=None, **kwargs):
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [NEWTAB, BLANK]}}
        if method == "Target.attachToTarget":
            attached.append(params)
            return {"result": {"sessionId": "agent-session"}}
        return {"result": {}}

    supervisor._cdp = cdp
    asyncio.run(supervisor._attach_initial_page())

    assert attached == [{"targetId": "agent-tab", "flatten": True}]
