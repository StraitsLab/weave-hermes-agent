"""Tests for Chromium-presence detection in browser_tool.

Regression guard for the "browser tool advertised but Chromium missing"
class of bug — where ``agent-browser`` CLI is discoverable but no
Chromium build is on disk, causing every browser_* tool call to hang
for the full command timeout before surfacing a useless error.
"""

import os

import pytest

from tools import browser_tool as bt


@pytest.fixture(autouse=True)
def _reset_chromium_cache():
    bt._cached_chromium_installed = None
    yield
    bt._cached_chromium_installed = None


class TestChromiumSearchRoots:
    def test_respects_playwright_browsers_path_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        roots = bt._chromium_search_roots()
        assert str(tmp_path) == roots[0]


    def test_always_includes_default_ms_playwright_cache(self, monkeypatch):
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        roots = bt._chromium_search_roots()
        home = os.path.expanduser("~")
        assert any(r == os.path.join(home, ".cache", "ms-playwright") for r in roots)


class TestChromiumInstalled:
    def test_true_when_plain_chromium_on_path(self, monkeypatch):
        monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
        monkeypatch.setattr(
            bt.shutil,
            "which",
            lambda name, path=None: "/usr/bin/chromium" if name == "chromium" else None,
        )

        assert bt._chromium_installed() is True


    def test_result_cached(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium-1208").mkdir()
        assert bt._chromium_installed() is True
        # Delete after first call — cached True should still return True.
        (tmp_path / "chromium-1208").rmdir()
        assert bt._chromium_installed() is True


class TestCheckBrowserRequirementsChromium:

    def test_local_mode_with_chromium_returns_true(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda **_kw: "/usr/local/bin/agent-browser")
        monkeypatch.setattr(bt, "_requires_real_termux_browser_install", lambda _: False)
        monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium-1208").mkdir()

        assert bt.check_browser_requirements() is True


    def test_camofox_mode_does_not_require_chromium(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
        # Even with no chromium on disk, camofox drives its own backend.
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "fakehome"))

        assert bt.check_browser_requirements() is True


def test_tool_definitions_probe_npx_once(monkeypatch):
    import model_tools
    from tools import registry as tool_registry

    # Other tests leave check_fn / tool-definition snapshots behind; this test
    # measures a cold first build, so start from an empty cache.
    tool_registry.invalidate_check_fn_cache()
    model_tools._clear_tool_defs_cache()

    monkeypatch.setattr(bt, "_agent_browser_resolved", False)
    monkeypatch.setattr(bt, "_cached_agent_browser", None)
    monkeypatch.setattr(bt, "_agent_browser_probe_resolved", False, raising=False)
    monkeypatch.setattr(bt, "_cached_agent_browser_probe", None, raising=False)
    monkeypatch.setattr(bt, "_is_browser_use_cli_mode", lambda: False)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_get_cdp_override_raw", lambda: None)
    monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(bt, "_chromium_installed", lambda: True)
    monkeypatch.setattr(bt, "_requires_real_termux_browser_install", lambda _: False)
    monkeypatch.setattr(bt, "_merge_browser_path", lambda _: "/test/bin")
    monkeypatch.setattr(
        bt.shutil, "which",
        lambda name, path=None: "/test/bin/npx" if name == "npx" else None,
    )
    calls = []
    monkeypatch.setattr(bt, "node_tool_runnable", lambda path: calls.append(path) or True)

    for _ in range(2):
        tools = model_tools.get_tool_definitions(enabled_toolsets=["hermes-acp"], quiet_mode=True)
        assert any(t["function"]["name"] == "browser_navigate" for t in tools)
    assert calls, "The real npx resolution path must be exercised"
    assert len(calls) <= 1
    # A cheap availability probe must not populate the execution-validation cache.
    assert bt._agent_browser_resolved is False
    assert bt._cached_agent_browser is None


class TestRunBrowserCommandChromiumGuard:
    """Verify _run_browser_command fails fast (no timeout hang) when
    Chromium is missing in local mode.
    """


