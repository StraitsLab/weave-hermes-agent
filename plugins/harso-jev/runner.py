"""Process-isolated adapter for jev-ultrafast 1231850 (MIT).

Safety/text/proxy logic adapted from weave-api's jev_browser_agent.py.
Hermes owns backend selection, the harness daemon, Chrome and native Vault.
"""
from __future__ import annotations

import contextlib
import json
import re
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse

PAGE_TEXT_LIMIT = 6000
PAGE_TEXT_JS = "(document.body||{}).innerText||''"
FOCUSED_TEXTBOX_JS = ("(() => { const e=document.activeElement; return !!e && (e.tagName==='TEXTAREA' || e.isContentEditable || "
                      "(e.tagName==='INPUT' && ['text','search','email','url','tel','number',''].includes(e.type))); })()")
FRAME_TEXT_JS = ("[...document.querySelectorAll('iframe')].map(f => { try { const d=f.contentDocument; "
                 "return d && f.getClientRects().length ? (d.body||{}).innerText||'' : ''; } catch (e) { return ''; } }).filter(Boolean)")


def resolve_credentials(env: dict) -> dict:
    """Reuse the cell/attempt bearer for both weave-api proxy routes."""
    base = env.get("WEAVE_API_URL", "").rstrip("/")
    bearer = env.get("WEAVE_API_MCP_BEARER", "")
    resolved = {
        "TYPESAFE_MODEL": env.get("TYPESAFE_MODEL") or "jev-latest",
        "TEXT_MODEL": env.get("TEXT_MODEL") or "google/gemini-2.5-flash",
        "TYPESAFE_BASE_URL": base + "/v1/jev/systemone",
        "TEXT_MODEL_BASE_URL": base + "/v1/jev/text",
    }
    if not base or not bearer:
        raise ValueError("WEAVE_API_URL and WEAVE_API_MCP_BEARER are required")
    if bearer.startswith(("wvc1_", "wvj1_")):
        session = env.get("WEAVE_HERMES_SESSION_ID", "")
        if re.fullmatch(r"[!-~]{1,200}", session) is None:
            raise ValueError("Hermes session context is required and must be 1–200 visible ASCII characters.")
        resolved["WEAVE_HERMES_SESSION_ID"] = session
    resolved.update(TYPESAFE_API_KEY=bearer, TEXT_MODEL_API_KEY=bearer)
    return resolved


def host_allowed(url: str, allow_hosts: list[str]) -> bool:
    """True when the URL's host is the allowlisted host or a subdomain of it."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and any(
        host == a or host.endswith("." + a) for a in allow_hosts if a
    )


def page_excerpt(text: str, limit: int = PAGE_TEXT_LIMIT, offset: int = 0) -> str:
    """Visible page text, whitespace folded, one window of the budget."""
    folded = re.sub(r"[ \t\r\f\v]+", " ", text or "")
    folded = re.sub(r"\s*\n\s*", "\n", folded).strip()
    offset = max(0, min(offset, len(folded)))
    window = folded[offset:offset + limit]
    if offset + limit < len(folded):
        window = window.rstrip() + f"\n[{len(folded) - offset - limit} more characters; call again with text_offset={offset + limit}]"
    return window


def blocked_reason(status: str, ticks: int, max_ticks: int, page_actions: list) -> str:
    if status == "done":
        return ""
    if ticks >= max_ticks:
        return f"stopped at the {max_ticks}-step budget without finishing; the page may need more steps or a narrower goal"
    offered = [a for a in page_actions if a and not a.startswith(("Scroll", "Wait"))][:12]
    if not offered:
        return "the page offered nothing to click or type into (empty, still loading, or a wall such as a CAPTCHA)"
    return "no offered action fits the goal; the page offers: " + "; ".join(offered)


def outcome_verified(title: str, heading: str, url: str, expect: str, text: str = "") -> bool:
    """Independent check against live page state, not the agent's DONE choice."""
    if not expect:
        return False
    haystack = " ".join([title or "", heading or "", url or "", text or ""]).lower()
    return expect.lower() in haystack


class NavigationTimeout(TimeoutError):
    pass


class LeftAllowlist(RuntimeError):
    pass


class NativeLoginRequired(RuntimeError):
    pass


def configure_runtime(args, creds, allow):
    """Adapt the pinned upstream at its browser and HTTP seams, not its action loop."""
    from browser_harness.admin import ensure_daemon
    from browser_harness.helpers import cdp, switch_tab
    from jev_ultrafast import agent as agent_module, model
    from jev_ultrafast.browser import Browser, StalePage

    post_json = model.post_json

    def session_header(request):
        request.headers.pop("Weave-Hermes-Session", None)
        if (str(request.url) == creds["TEXT_MODEL_BASE_URL"] + "/chat/completions"
                and creds["TYPESAFE_API_KEY"].startswith(("wvc1_", "wvj1_"))):
            request.headers["Weave-Hermes-Session"] = creds["WEAVE_HERMES_SESSION_ID"]

    model.CLIENT.event_hooks["request"].append(session_header)

    def proxy_post(url, key, body):
        route = creds["TEXT_MODEL_BASE_URL"] + "/chat/completions" if url.endswith("/chat/completions") else creds["TYPESAFE_BASE_URL"]
        return post_json(route, creds["TYPESAFE_API_KEY"], body)

    model.post_json = proxy_post
    # Keep the bearer out of the environment inherited by the harness daemon.
    model.os = SimpleNamespace(environ=dict(creds))

    class HermesBrowser(Browser):
        def __init__(self, url):
            self.session = None
            self.target = None
            ensure_daemon()
            try:
                if args.target:
                    pages = cdp("Target.getTargets")["targetInfos"]
                    if not any(p["targetId"] == args.target and p["type"] == "page" for p in pages):
                        raise ValueError("Requested browser tab is unavailable")
                    self.target = args.target
                else:
                    # Never select the first shared page: it may belong to a sibling or the user.
                    self.target = cdp("Target.createTarget", url="about:blank", newWindow=True)["targetId"]
                switch_tab(self.target, activate=True)
                self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
                if args.target:
                    self.check_host()
                    return
                navigation = self.call("Page.navigate", url=url)
                if navigation.get("errorText"):
                    raise RuntimeError("Browser navigation failed; inspect the current page before retrying")
                deadline = time.monotonic() + args.navigation_timeout
                while time.monotonic() < deadline:
                    if self.evaluate("document.readyState") == "complete" and self.evaluate("location.href") != "about:blank":
                        break
                    time.sleep(0.02)
                else:
                    self.check_host()
                    self.close()
                    raise NavigationTimeout("Browser navigation timed out; inspect the current page before retrying")
            except Exception:
                self.close()
                raise

        def check_host(self):
            if not self.session:
                raise RuntimeError("Browser session is detached")
            current = self.evaluate("location.href")
            if not host_allowed(current, allow):
                raise LeftAllowlist("Page left the host allowlist")

        def observe(self, screenshot=True):
            try:
                return self.observe_page(screenshot)
            except Exception:
                self.close()
                raise

        def observe_page(self, screenshot):
            self.check_host()
            if self.evaluate("!![...document.querySelectorAll('input[type=password]')].find(e=>!e.disabled && e.getClientRects().length)"):
                error = NativeLoginRequired("Use browser_vault tools directly, then resume this target after login")
                error.target = self.target
                error.url = self.evaluate("location.href") or ""
                error.title = self.evaluate("document.title") or ""
                self.close()
                raise error
            page = super().observe(screenshot=screenshot)
            if not host_allowed(page["url"], allow):
                raise LeftAllowlist("Page left the host allowlist")
            if self.evaluate(FOCUSED_TEXTBOX_JS) is True:
                page["actions"].append({"id": "press_enter", "kind": "enter", "label": "Press Enter to submit the focused field"})
            with contextlib.suppress(Exception):
                for frame_text in self.evaluate(FRAME_TEXT_JS) or []:
                    if isinstance(frame_text, str):
                        page["text"] = (page["text"] + "\n" + frame_text)[:12000]
            return page

        def act(self, action, page, text=None):
            self.check_host()
            if action["kind"] == "enter":
                if not self.fresh(page):
                    raise StalePage("Page changed before Enter")
                self.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r")
                self.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)
                self.after_input = None
                return {"executed": action["id"]}
            before = {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}
            result = super().act(action, page, text=text)
            if action["kind"] == "click":
                self.follow_new_tab(before)
            return result

        def follow_new_tab(self, before):
            time.sleep(0.15)
            opened = [t for t in cdp("Target.getTargets")["targetInfos"]
                      if t.get("type") == "page" and t["targetId"] not in before and t.get("openerId") == self.target]
            if not opened:
                return
            if not host_allowed(opened[0].get("url", ""), allow):
                cdp("Target.closeTarget", targetId=opened[0]["targetId"])
                raise LeftAllowlist("New tab left the host allowlist")
            cdp("Target.detachFromTarget", sessionId=self.session)
            self.target = opened[0]["targetId"]
            switch_tab(self.target, activate=True)
            self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and self.evaluate("document.readyState") != "complete":
                time.sleep(0.05)

        def close(self):
            # Detach only: the page belongs to the user and remains available to native Vault/browser tools.
            if self.session:
                with contextlib.suppress(Exception):
                    cdp("Target.detachFromTarget", sessionId=self.session)
                self.session = None

    agent_module.Browser = HermesBrowser


def run(args, creds, max_ticks=10, navigation_timeout=60):
    from jev_ultrafast import Agent

    args = SimpleNamespace(url=args["url"], goal=args.get("goal", "").strip(), expect=args.get("expect", ""),
                           target=args.get("target", ""), text_offset=args.get("text_offset", 0),
                           navigation_timeout=navigation_timeout, allow_hosts=args.get("allow_hosts", ""))
    allow = list(dict.fromkeys([urlparse(args.url).hostname or ""] + [
        h.strip().lower() for h in args.allow_hosts.split(",") if h.strip()
    ]))
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", h) for h in allow):
        raise ValueError("Invalid allow_hosts")
    if not host_allowed(args.url, allow) or max_ticks < 1 or navigation_timeout <= 0:
        raise ValueError("Invalid start URL or execution budget")
    configure_runtime(args, creds, allow)
    ticks, error = 0, None
    try:
        agent = Agent(args.url, args.goal or "Read the page.")
    except NativeLoginRequired as exc:
        return {"success": False, "status": "blocked", "error_type": type(exc).__name__,
                "blocked_reason": str(exc), "browser_page": {"target": exc.target, "url": exc.url, "title": exc.title}}
    with agent:
        try:
            for _state in (agent.run() if args.goal else []):
                ticks += 1
                if not host_allowed(agent.state["page"]["url"], allow):
                    raise LeftAllowlist("Page left the host allowlist")
                if ticks >= max_ticks:
                    break
        except Exception as exc:  # health: allow BLE001 -- isolate model/CDP failures without returning page or request secrets
            error = type(exc).__name__
        try:
            agent.browser.check_host()
            final_url = agent.browser.evaluate("location.href") or ""
            title = agent.browser.evaluate("document.title") or ""
            heading = agent.browser.evaluate("(document.querySelector('h1,h2')||{}).textContent||''") or ""
            text = agent.browser.evaluate(PAGE_TEXT_JS) or ""
        except Exception as exc:  # health: allow BLE001 -- unreadable or off-host final document must return no page text
            error = error or type(exc).__name__
            final_url = title = heading = text = ""
        status = "done" if not args.goal else agent.state["status"]
        if error or (ticks >= max_ticks and status != "done"):
            status = "blocked"
        verified = not error and outcome_verified(title, heading, final_url, args.expect, text)
        return {
            "schema": "hermes.browser_use_jev_run_v1", "goal": args.goal,
            "success": not error and (verified if args.expect else status == "done"),
            "final_url": final_url, "title": title, "heading": heading,
            "text": page_excerpt(text, offset=args.text_offset),
            "steps": len(agent.state["history"]), "text_calls": len(agent.state["text_calls"]), "ticks": ticks,
            "left_allowlist": error == "LeftAllowlist", "expected": args.expect, "verified": verified,
            "browser": "attached", "status": status, "error_type": error,
            "blocked_reason": error or blocked_reason(status, ticks, max_ticks, [
                a.get("label", "") for a in agent.state["page"].get("actions", [])
            ]),
            "browser_page": {"target": agent.browser.target, "url": final_url, "title": title},
        }


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        result = run(payload["args"], resolve_credentials(payload["credentials"]))
    except Exception as exc:  # health: allow BLE001 -- worker protocol boundary; parent applies mandatory output redaction
        result = {"success": False, "error_type": type(exc).__name__, "error": str(exc)}
    print(json.dumps(result))
