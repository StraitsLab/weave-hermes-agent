# Bot Screen — fork port record

Source: upstream `NousResearch/hermes-agent` @ `ee5ee84a345204a3b1d6ef6ba1ab747e602867b9`.
Plan: live-view ENGINEERING-PLAN v6 §2.1 / §2.3 (LV-1a, WEV-1882).

`tests/tools/test_bot_desktop_fork_port.py` hashes every byte-identical file against the table below (T-1a-1).
A re-port overwrites these files from upstream and reapplies only the recorded diffs.

## Files

| file | status | sha256 |
|---|---|---|
| `__init__.py` | byte-identical | `550cd5ecdf8d4966c8e5e9087fd2655509c8022b5ed2d4e4315280015a7b9af8` |
| `lease.py` | byte-identical | `918b5d6072c5c3ed46ea48e23c4c3e12cfd58ea6c4b8fa27bf5a7543df9d03fd` |
| `rfb_filter.py` | byte-identical | `3e7b31584072f82b0a003f7bcc50b30f42cfab3080301e2c4766de384753f1da` |
| `resources.py` | byte-identical | `6f1d9ea8596984ef05ea49802573f25cdb299e603389c3bc5913c39fd08c347c` |
| `thumbnail.py` | byte-identical | `1deffaea19e2c7090f238a905623a5a3946bfe663b6efdbf7df1cc05488c27ff` |
| `launcher.sh` | byte-identical | `09fdb8052012ea589da940e3201c8025c1de199c17df6c22af42bf18e5272216` |
| `wallpaper.png` | byte-identical | `57f8bda750c95bf4804ae73cf4e203b4c200d48b66f6f46da2118b5324276aa2` |
| `runtime.py` | diff 1 | upstream `28e08a7a7232e4dc97a18242c7876d1b9dd7d5a7b291b1c3670c34ff73f983f2` |
| `browser.py` | diff 2 | upstream `d46a49e0a032824d27f046f5b84360dc538db7b8190bacd7fc4d7083d54c0992` |
| `install.py` | not ported | runtime installs are out of scope (packages are host-installed, plan §2.2) |

## Recorded diffs

1. `runtime.py` `state_dir()`: returns `Path($HERMES_BD_STATE_DIR)` when that is set, else upstream
   `<HERMES_HOME>/bot-desktop`. The attempt renders `/tmp/bd`: the socket under the real `HERMES_HOME` is 138 bytes,
   over the 108-byte AF_UNIX limit. `_spawn_and_wait` is unchanged, so the launcher child's `HERMES_BD_SOCKET` is
   `<state_dir>/rfb.sock`. `lease.json` is NOT moved: upstream `lease._path` derives it from `HERMES_HOME`.
2. `browser.py`:
   - `profile_dir()`: `<state_dir>/profile` when `HERMES_BD_STATE_DIR` is set, else upstream `browser-profile`;
   - `dock_argv()`: when `AGENT_BROWSER_ARGS` is set, its comma-split flags are appended instead of the
     sandbox-bypass probe (same flags agent-browser starts the binary with).
3. `tools/browser_tool_session.py` (new, fork-only shim): re-exports `CHROMIUM_SANDBOX_BYPASS_ARGS`,
   `_needs_chromium_sandbox_bypass` and `apparmor_restricts_unprivileged_userns` from `tools/browser_tool.py`. The
   fork has not split `browser_tool.py`; upstream's module of this name holds the policy there. To keep one list,
   `browser_tool.py` gains `CHROMIUM_SANDBOX_BYPASS_ARGS` (its former literal) and `apparmor_restricts_unprivileged_userns`
   (extracted from `_needs_chromium_sandbox_bypass`, same behaviour).
   The fence (`run_fenced`, `_shares_bot_desktop_browser`, upstream `browser_tool_session.py:656-700`) and the
   daemon-idle helper (:166-187) land here in the wiring PR (LV-1a part 2).
4. `tools/browser_tool_install.py` (new, fork-only shim): re-exports `_chromium_search_roots` from
   `tools/browser_tool.py`.

## Tests

Ported from upstream `tests/tools/test_bot_desktop_*.py` @ `ee5ee84a`. Removed or adapted, with the reason:

- `test_bot_desktop_lease.py`: the three `computer_use` fence tests are not ported. The fork's `computer_use` tool
  has no lease fence (and no `_new_backend`); fencing `computer_use` is outside LV-1a.
- `test_bot_desktop_runtime.py::test_the_image_bakes_the_same_apt_packages_the_runtime_would_install`: not ported.
  The fork image has no Bot Screen apt layer; on a Sprite the set is host-installed from the LV-2 lock.
- `test_bot_desktop_browser.py`: the sandbox-policy tests call `tools.browser_tool` (diff 3). Tests that target
  upstream's `browser_tool_session` / `browser_tool_lifecycle` / `browser_tool_cloud` split
  (attach-to-dock `--cdp`, headed-spawn auto-start, janitor, daemon idle) are rewritten against the fork's
  `browser_tool.py` in the wiring PR.
- `test_bot_desktop_install.py`: not ported with `install.py`.
- `test_bot_desktop_resources.py::test_start_refuses_and_status_explains_when_memory_is_short`: marked
  `linux_only`. `runtime.status()` reports no memory blocker off a supported host, so upstream's copy fails on macOS.
