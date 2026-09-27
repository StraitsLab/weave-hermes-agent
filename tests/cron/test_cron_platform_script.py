"""Weave platform script seam: ``platform:<name>.py`` cron scripts.

A platform script runs only from ``cron.platform_script_root`` when the root
and the file are platform-owned (uid 0, not group/other-writable), the file is
regular (never a symlink) and, when digests are pinned, its sha256 matches.
``cron.allow_scripts: false`` refuses every other script at create and fire.
uid 0 is unavailable in tests, so the owner uid is patched to the test uid
except in the non-root-owner case, which leaves the production value.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

# uid ownership, mode bits and symlinks are POSIX semantics.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership checks")


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", home / "cron" / "output")
    return home


@pytest.fixture
def platform_root(tmp_path, monkeypatch):
    import cron.scheduler as scheduler

    root = tmp_path / "platform-scripts"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    monkeypatch.setattr(scheduler, "_PLATFORM_SCRIPT_OWNER_UID", os.getuid())  # windows-footgun: ok
    return root


def _config(home, **cron):
    (home / "config.yaml").write_text(yaml.safe_dump({"cron": cron}), encoding="utf-8")


def _platform_script(root, name="tick.py", body='print("platform ran")\n'):
    path = root / name
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o555)
    return path


def _tenant_script(home, marker):
    path = home / "scripts" / "tenant.py"
    path.write_text(f"open({str(marker)!r}, 'w').close()\nprint('tenant ran')\n", encoding="utf-8")
    return path


# --- admitted ---------------------------------------------------------------


def test_platform_ref_runs_when_allow_scripts_false(home, platform_root):
    from cron.scheduler import _run_job_script

    _platform_script(platform_root)
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))

    assert _run_job_script("platform:tick.py") == (True, "platform ran")


def test_platform_ref_runs_with_matching_digest(home, platform_root):
    from cron.scheduler import _run_job_script

    path = _platform_script(platform_root)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _config(
        home,
        allow_scripts=False,
        platform_script_root=str(platform_root),
        platform_script_digests={"tick.py": digest},
    )

    assert _run_job_script("platform:tick.py") == (True, "platform ran")


def test_no_agent_platform_job_runs_end_to_end(home, platform_root):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    _platform_script(platform_root, body='print("watch tick")\n')
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))
    job = create_job(
        prompt=None, schedule="every 5m", script="platform:tick.py",
        no_agent=True, deliver="local",
    )

    success, _doc, final, error = run_job(job)
    assert (success, error) == (True, None)
    assert "watch tick" in final


# --- refused at fire --------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    ["platform:../x.py", "platform:a/b.py", "platform:X.py", "platform:tick.sh", "platform:"],
)
def test_malformed_platform_name_refused(home, platform_root, ref):
    from cron.scheduler import _run_job_script

    (platform_root / "x.py").write_text("print(1)\n", encoding="utf-8")
    _config(home, platform_script_root=str(platform_root))

    ok, out = _run_job_script(ref)
    assert ok is False
    assert "invalid platform script name" in out


def test_platform_symlink_refused(home, platform_root, tmp_path):
    from cron.scheduler import _run_job_script

    target = _platform_script(platform_root, name="real.py")
    (platform_root / "tick.py").symlink_to(target)
    _config(home, platform_script_root=str(platform_root))

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "not a regular file" in out


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_platform_non_regular_file_refused(home, platform_root, kind):
    from cron.scheduler import _run_job_script

    target = platform_root / "tick.py"
    if kind == "fifo":
        os.mkfifo(target)
    else:
        target.mkdir()
    _config(home, platform_script_root=str(platform_root))

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "not a regular file" in out


def test_root_that_is_a_file_refused(home, platform_root, tmp_path):
    from cron.scheduler import _run_job_script

    # platform_root patches the owner uid, so owner and ancestor custody pass
    # and ENOTDIR on the open is the only reason left to refuse.
    root = tmp_path / "root-file"
    root.write_text("", encoding="utf-8")
    os.chmod(root, 0o555)
    _config(home, platform_script_root=str(root))

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert out.startswith("Blocked: platform script cannot be read") and "Not a directory" in out


def _fake_owner(monkeypatch, target, uid):
    """Report ``uid`` as the owner of exactly one object (matched by inode),
    whichever stat call the resolver uses, so one owner guard is tested alone."""
    import cron.scheduler as scheduler

    ident = (os.stat(target).st_dev, os.stat(target).st_ino)

    def wrap(real):
        def fake(*args, **kwargs):
            st = real(*args, **kwargs)
            if (st.st_dev, st.st_ino) == ident:
                values = list(st)
                values[4] = uid
                return os.stat_result(values)
            return st
        return fake

    monkeypatch.setattr(scheduler.os, "lstat", wrap(os.lstat))
    monkeypatch.setattr(scheduler.os, "fstat", wrap(os.fstat))


@pytest.mark.parametrize("which", ["file", "root", "ancestor"])
def test_platform_single_wrong_owner_refused(home, platform_root, monkeypatch, which):
    import cron.scheduler as scheduler

    path = _platform_script(platform_root)
    _config(home, platform_script_root=str(platform_root))
    target = {"file": path, "root": platform_root, "ancestor": platform_root.parent}[which]
    _fake_owner(monkeypatch, target, os.getuid() + 1)  # windows-footgun: ok

    ok, out = scheduler._run_job_script("platform:tick.py")
    assert ok is False
    assert {
        "file": "platform script is not platform-owned",
        "root": "root is not platform-owned",
        "ancestor": "untrusted ancestor",
    }[which] in out


def test_platform_production_owner_uid_refuses_test_owned_tree(home, platform_root, monkeypatch):
    import cron.scheduler as scheduler

    _platform_script(platform_root)
    _config(home, platform_script_root=str(platform_root))
    monkeypatch.setattr(scheduler, "_PLATFORM_SCRIPT_OWNER_UID", 0)
    assert os.getuid() != 0  # windows-footgun: ok

    ok, out = scheduler._run_job_script("platform:tick.py")
    assert ok is False
    assert "not platform-owned" in out


def test_platform_group_writable_file_refused(home, platform_root):
    from cron.scheduler import _run_job_script

    path = _platform_script(platform_root)
    os.chmod(path, 0o575)
    _config(home, platform_script_root=str(platform_root))

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "platform script is not platform-owned" in out


def test_platform_group_writable_root_refused(home, platform_root):
    from cron.scheduler import _run_job_script

    _platform_script(platform_root)
    os.chmod(platform_root, 0o775)
    _config(home, platform_script_root=str(platform_root))

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "root is not platform-owned" in out


@pytest.mark.parametrize("pinned", [{"tick.py": "0" * 64}, {"other.py": "0" * 64}])
def test_platform_digest_mismatch_refused(home, platform_root, pinned):
    from cron.scheduler import _run_job_script

    _platform_script(platform_root)
    _config(
        home, platform_script_root=str(platform_root), platform_script_digests=pinned,
    )

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "digest is not pinned" in out


def test_platform_ref_refused_without_root(home):
    from cron.scheduler import _run_job_script

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "platform_script_root is not configured" in out


# --- allow_scripts: false refuses tenant scripts at both fire call sites ----


def test_tenant_script_refused_on_no_agent_path(home, tmp_path):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    # Created under stock defaults, then the face is locked down.
    job = create_job(
        prompt=None, schedule="every 5m", script="tenant.py", no_agent=True, deliver="local",
    )
    _config(home, allow_scripts=False)

    success, _doc, final, _error = run_job(job)
    assert success is False
    assert "cron.allow_scripts is false" in final
    assert not marker.exists()


def test_tenant_script_refused_on_prerun_path(home, tmp_path):
    from unittest.mock import MagicMock, patch

    import cron.scheduler as scheduler

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _config(home, allow_scripts=False)
    job = {
        "id": "job_prerun", "name": "prerun", "prompt": "summarize",
        "schedule": "*/5 * * * *", "script": "tenant.py",
    }
    agent = MagicMock()
    agent.run_conversation = MagicMock(return_value={"final_response": "ok", "messages": []})
    runtime = {
        "provider": "openrouter", "api_mode": "chat_completions",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "test-key",
        "source": "stub", "requested_provider": None,
    }
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime), \
         patch("run_agent.AIAgent", return_value=agent):
        scheduler.run_job(job)

    prompt = agent.run_conversation.call_args.args[0]
    assert "## Script Error" in prompt
    assert "cron.allow_scripts is false" in prompt
    assert not marker.exists()


def test_tenant_monitor_script_refused_when_allow_scripts_false(home, tmp_path):
    from cron.monitor import _run_monitor_source

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _config(home, allow_scripts=False)

    ok, out = _run_monitor_source({"monitor_script": "tenant.py"})
    assert ok is False
    assert "cron.allow_scripts is false" in out
    assert not marker.exists()


# --- stock defaults ---------------------------------------------------------


def test_stock_defaults_run_tenant_script(home, tmp_path):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    job = create_job(
        prompt=None, schedule="every 5m", script="tenant.py", no_agent=True, deliver="local",
    )

    success, _doc, final, error = run_job(job)
    assert (success, error) == (True, None)
    assert "tenant ran" in final
    assert marker.exists()


def test_stock_defaults_are_declared():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    cron = DEFAULT_CONFIG["cron"]
    assert cron["allow_scripts"] is True
    assert cron["platform_script_root"] == ""
    assert cron["platform_script_digests"] == {}


# --- create time: cronjob tool ----------------------------------------------


def test_cronjob_tool_refuses_tenant_script_when_allow_scripts_false(home):
    from tools.cronjob_tools import cronjob

    (home / "scripts" / "tenant.py").write_text("print(1)\n", encoding="utf-8")
    _config(home, allow_scripts=False)

    result = json.loads(cronjob(
        action="create", schedule="every 5m", script="tenant.py",
        no_agent=True, deliver="local",
    ))
    assert result.get("success") is False
    assert "cron.allow_scripts is false" in result["error"]


def test_cronjob_tool_refuses_tenant_script_update_when_allow_scripts_false(home):
    from cron.jobs import create_job
    from tools.cronjob_tools import cronjob

    (home / "scripts" / "tenant.py").write_text("print(1)\n", encoding="utf-8")
    job = create_job(prompt="hello", schedule="every 5m", deliver="local")
    _config(home, allow_scripts=False)

    result = json.loads(cronjob(action="update", job_id=job["id"], script="tenant.py"))
    assert result.get("success") is False
    assert "cron.allow_scripts is false" in result["error"]


def test_cronjob_tool_admits_platform_ref(home, platform_root):
    from tools.cronjob_tools import cronjob

    _platform_script(platform_root)
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))

    result = json.loads(cronjob(
        action="create", schedule="every 5m", script="platform:tick.py",
        no_agent=True, deliver="local",
    ))
    assert result.get("success") is True, result


# --- F2: the script policy is positively valid or nothing runs -------------


def _both_refused(home, platform_root, marker):
    from cron.scheduler import _run_job_script
    from tools.cronjob_tools import _validate_cron_script_path

    tenant = _run_job_script("tenant.py")
    platform = _run_job_script("platform:tick.py")
    assert tenant[0] is False and platform[0] is False, (tenant, platform)
    assert not marker.exists()
    assert _validate_cron_script_path("tenant.py")
    assert _validate_cron_script_path("platform:tick.py")


def test_cold_malformed_config_refuses_every_script(home, platform_root, tmp_path):
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _platform_script(platform_root)
    (home / "config.yaml").write_text("cron: [broken\n", encoding="utf-8")

    _both_refused(home, platform_root, marker)


def test_warm_malformed_config_refuses_every_script(home, platform_root, tmp_path):
    from cron.scheduler import _run_job_script

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _platform_script(platform_root)
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))
    assert _run_job_script("platform:tick.py") == (True, "platform ran")  # warms last-known-good
    (home / "config.yaml").write_text(
        f"cron: {{platform_script_root: {platform_root}, allow_scripts: [\n", encoding="utf-8",
    )

    _both_refused(home, platform_root, marker)


def test_unreadable_config_refuses_every_script(home, platform_root, tmp_path):
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _platform_script(platform_root)
    _config(home, platform_script_root=str(platform_root))
    os.chmod(home / "config.yaml", 0)
    try:
        _both_refused(home, platform_root, marker)
    finally:
        os.chmod(home / "config.yaml", 0o600)


def test_malformed_managed_config_refuses_every_script(home, platform_root, tmp_path, monkeypatch):
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _platform_script(platform_root)
    _config(home, platform_script_root=str(platform_root))
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text("cron: {allow_scripts: [\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))

    _both_refused(home, platform_root, marker)


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None, [], {}])
def test_non_bool_allow_scripts_refuses_tenant_script(home, tmp_path, value):
    from cron.scheduler import _run_job_script
    from tools.cronjob_tools import _validate_cron_script_path

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _config(home, allow_scripts=value)

    ok, out = _run_job_script("tenant.py")
    assert ok is False and "cron.allow_scripts is false or invalid" in out
    assert not marker.exists()
    assert _validate_cron_script_path("tenant.py")


@pytest.mark.parametrize(
    "bad",
    [
        {"platform_script_digests": ["malformed"]},
        {"platform_script_digests": {"tick.py": 7}},
        {"platform_script_digests": "tick.py"},
        {"platform_script_root": 7},
    ],
    ids=["digests-list", "digest-int", "digests-str", "root-int"],
)
def test_malformed_platform_policy_refuses_platform_script(home, platform_root, bad):
    from cron.scheduler import _run_job_script

    _platform_script(platform_root)
    _config(home, allow_scripts=False, **{"platform_script_root": str(platform_root), **bad})

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "policy is invalid" in out


def test_cron_section_not_a_mapping_refuses_tenant_script(home, tmp_path):
    from cron.scheduler import _run_job_script

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    (home / "config.yaml").write_text("cron: [1, 2]\n", encoding="utf-8")

    assert _run_job_script("tenant.py")[0] is False
    assert not marker.exists()


# --- F2 round 3: the policy comes from one strict read of each file --------


def _write_yaml(path, data):
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _tenant_refused(marker):
    from cron.scheduler import _run_job_script
    from tools.cronjob_tools import _validate_cron_script_path

    ok, out = _run_job_script("tenant.py")
    assert ok is False and out.startswith("Blocked:"), out
    assert not marker.exists()
    assert _validate_cron_script_path("tenant.py")


@pytest.mark.parametrize("agent", [1, "x", ["x"]], ids=["int", "str", "list"])
def test_normalisation_failure_does_not_drop_explicit_false(home, tmp_path, agent):
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _write_yaml(home / "config.yaml", {"cron": {"allow_scripts": False}, "max_turns": 5, "agent": agent})

    _tenant_refused(marker)


def test_warm_normalisation_failure_does_not_serve_permissive_lkg(home, tmp_path):
    from cron.scheduler import _cron_script_policy, _run_job_script
    from hermes_cli.config import load_config

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _write_yaml(home / "config.yaml", {"cron": {"allow_scripts": True}})
    assert _cron_script_policy() == (True, "", {})
    load_config()  # warm the last-known-good cache with the permissive config
    assert _run_job_script("tenant.py")[0] is True
    marker.unlink()
    _write_yaml(home / "config.yaml", {"cron": {"allow_scripts": False}, "max_turns": 5, "agent": 1})

    _tenant_refused(marker)


@pytest.mark.parametrize("managed_text", ["cron: {allow_scripts: [\n", "[1, 2]\n", "[]\n", None],
                         ids=["malformed", "non-mapping", "empty-list", "unreadable"])
def test_cold_bad_managed_config_refuses_tenant(home, tmp_path, monkeypatch, managed_text):
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    managed = tmp_path / "managed"
    managed.mkdir()
    managed_cfg = managed / "config.yaml"
    managed_cfg.write_text(managed_text or "cron: {allow_scripts: true}\n", encoding="utf-8")
    if managed_text is None:
        os.chmod(managed_cfg, 0)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    try:
        _tenant_refused(marker)
    finally:
        os.chmod(managed_cfg, 0o600)


@pytest.mark.parametrize(
    "layer, cron",
    [
        ("user", None),
        ("user", {"allow_scripts": None}),
        ("user", {"platform_script_root": None}),
        ("user", {"platform_script_digests": None}),
        ("managed", None),
        ("managed", {"platform_script_digests": None}),
    ],
    ids=["cron", "allow", "root", "digests", "managed-cron", "managed-digests"],
)
def test_explicit_null_policy_refuses_every_script(home, platform_root, tmp_path, monkeypatch, layer, cron):
    from cron.scheduler import _cron_script_policy, _run_job_script

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    _platform_script(platform_root)
    user = {"cron": {"platform_script_root": str(platform_root)}}
    if layer == "user":
        user["cron"] = None if cron is None else {**user["cron"], **cron}
    else:
        managed = tmp_path / "managed"
        managed.mkdir()
        _write_yaml(managed / "config.yaml", {"cron": cron})
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _write_yaml(home / "config.yaml", user)

    assert _cron_script_policy() is None
    assert _run_job_script("platform:tick.py")[0] is False  # never unpinned
    _tenant_refused(marker)


def test_policy_reads_each_config_file_once_and_ignores_later_replacement(
    home, tmp_path, monkeypatch,
):
    import hermes_cli.config as config_mod
    from cron.scheduler import _cron_script_policy

    managed = tmp_path / "managed"
    managed.mkdir()
    _write_yaml(managed / "config.yaml", {"cron": {"platform_script_root": "/opt/weave"}})
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _write_yaml(home / "config.yaml", {"cron": {"allow_scripts": False}})
    targets = {str(home / "config.yaml"), str(managed / "config.yaml")}
    opened = []
    real_open, real_load = open, config_mod.fast_safe_load

    def spy_open(path, *args, **kwargs):
        if str(path) in targets:
            opened.append(str(path))
        return real_open(path, *args, **kwargs)

    def load_then_replace(stream):
        parsed = real_load(stream)
        # Held replacement: the file just read turns permissive.
        _write_yaml(Path(stream.name), {"cron": {"allow_scripts": True, "platform_script_root": ""}})
        return parsed

    monkeypatch.setattr(config_mod, "open", spy_open, raising=False)
    monkeypatch.setattr(config_mod, "fast_safe_load", load_then_replace)

    assert _cron_script_policy() == (False, "/opt/weave", {})
    assert sorted(opened) == sorted(targets)


@pytest.mark.parametrize("text", [None, "{}\n", "cron: {}\n", "cron: {script_timeout_seconds: 5}\n"],
                         ids=["no-file", "empty-mapping", "empty-cron", "unrelated-cron-key"])
def test_absent_policy_keeps_stock_defaults(home, tmp_path, text):
    from cron.scheduler import _cron_script_policy, _run_job_script

    if text is not None:
        (home / "config.yaml").write_text(text, encoding="utf-8")
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)

    assert _cron_script_policy() == (True, "", {})
    assert _run_job_script("tenant.py")[0] is True and marker.exists()


def test_valid_managed_false_overrides_user_true(home, tmp_path, monkeypatch):
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    managed = tmp_path / "managed"
    managed.mkdir()
    _write_yaml(managed / "config.yaml", {"cron": {"allow_scripts": False}})
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    _write_yaml(home / "config.yaml", {"cron": {"allow_scripts": True}})

    _tenant_refused(marker)


# --- F2 round 4: an explicit null root is not an absent file ---------------

_NULL_ROOTS = ["null\n", "~\n", "!!null null\n", "---\nnull\n...\n", "---\n"]


@pytest.mark.parametrize("layer", ["user", "managed"])
@pytest.mark.parametrize("text", _NULL_ROOTS, ids=["null", "tilde", "tagged", "delimited", "bare-doc"])
def test_null_config_root_refuses_every_script(home, tmp_path, monkeypatch, layer, text):
    from cron.scheduler import _cron_script_policy

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    ((home if layer == "user" else managed) / "config.yaml").write_text(text, encoding="utf-8")

    assert _cron_script_policy() is None
    _tenant_refused(marker)


@pytest.mark.parametrize("layer", ["user", "managed"])
def test_warm_config_turning_null_refuses_tenant_and_platform(home, platform_root, tmp_path, monkeypatch, layer):
    from cron.scheduler import _cron_script_policy, _run_job_script
    from hermes_cli.config import load_config

    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)
    platform_marker = tmp_path / "platform-ran"
    _platform_script(platform_root, body=f"open({str(platform_marker)!r}, 'w').close()\n")
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    restrictive, other = (home, managed) if layer == "user" else (managed, home)
    _write_yaml(restrictive / "config.yaml", {"cron": {"allow_scripts": False}})
    _write_yaml(other / "config.yaml", {"cron": {"platform_script_root": str(platform_root)}})
    assert load_config()["cron"]["allow_scripts"] is False  # warm the caches
    # Control: before the null write the platform script runs, the tenant does not.
    assert _run_job_script("platform:tick.py")[0] is True and platform_marker.exists()
    platform_marker.unlink()
    _tenant_refused(marker)
    (restrictive / "config.yaml").write_text("null\n", encoding="utf-8")

    assert _cron_script_policy() is None
    assert _run_job_script("platform:tick.py")[0] is False
    assert not platform_marker.exists()
    _tenant_refused(marker)


@pytest.mark.parametrize("text", ["", "# comment only\n"], ids=["empty", "comment"])
def test_documentless_config_keeps_stock_defaults(home, tmp_path, text):
    from cron.scheduler import _cron_script_policy, _run_job_script

    (home / "config.yaml").write_text(text, encoding="utf-8")
    marker = tmp_path / "tenant-ran"
    _tenant_script(home, marker)

    assert _cron_script_policy() == (True, "", {})
    assert _run_job_script("tenant.py")[0] is True and marker.exists()


# --- F3: the executed bytes are the verified bytes, from a trusted path ---


def _swap_at_popen(monkeypatch, action):
    import cron.scheduler as scheduler

    real = scheduler.subprocess.Popen

    def swapping(*args, **kwargs):
        action()
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler.subprocess, "Popen", swapping)


def test_root_rename_after_verification_runs_verified_bytes(home, platform_root, monkeypatch, tmp_path):
    from cron.scheduler import _run_job_script

    path = _platform_script(platform_root)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _config(
        home, allow_scripts=False, platform_script_root=str(platform_root),
        platform_script_digests={"tick.py": digest},
    )

    def swap():
        platform_root.rename(tmp_path / "moved")
        platform_root.mkdir()
        (platform_root / "tick.py").write_text('print("SWAPPED")\n', encoding="utf-8")

    _swap_at_popen(monkeypatch, swap)
    assert _run_job_script("platform:tick.py") == (True, "platform ran")


def test_file_replaced_after_verification_runs_verified_bytes(home, platform_root, monkeypatch):
    from cron.scheduler import _run_job_script

    path = _platform_script(platform_root)
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))

    def swap():
        path.unlink()
        path.write_text('print("SWAPPED")\n', encoding="utf-8")

    _swap_at_popen(monkeypatch, swap)
    assert _run_job_script("platform:tick.py") == (True, "platform ran")


@pytest.mark.parametrize("spelling", ["alias", "alias/", "alias/.", "slash/", "dot", "dotdot", "double"])
def test_non_canonical_root_spelling_refused(home, platform_root, tmp_path, spelling):
    from cron.scheduler import _run_job_script

    _platform_script(platform_root)
    alias = tmp_path / "alias"
    alias.symlink_to(platform_root, target_is_directory=True)
    root = {
        "alias": str(alias),
        "alias/": f"{alias}/",
        "alias/.": f"{alias}/.",
        "slash/": f"{platform_root}/",
        "dot": f"{platform_root}/.",
        "dotdot": f"{platform_root}/../{platform_root.name}",
        "double": f"/{platform_root}",
    }[spelling]
    _config(home, allow_scripts=False, platform_script_root=root)

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "not a canonical absolute path" in out


def test_symlinked_ancestor_refused(home, platform_root, tmp_path):
    from cron.scheduler import _run_job_script

    _platform_script(platform_root)
    link = tmp_path / "link-parent"
    link.symlink_to(tmp_path, target_is_directory=True)
    _config(home, allow_scripts=False, platform_script_root=str(link / platform_root.name))

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert "not a canonical absolute path" in out


def test_writable_ancestor_refused_sticky_allowed(home, platform_root):
    from cron.scheduler import _run_job_script

    _platform_script(platform_root)
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))
    parent = platform_root.parent
    mode = os.stat(parent).st_mode & 0o7777
    try:
        os.chmod(parent, 0o777)
        ok, out = _run_job_script("platform:tick.py")
        assert ok is False and "untrusted ancestor" in out
        os.chmod(parent, 0o1777)
        assert _run_job_script("platform:tick.py") == (True, "platform ran")
    finally:
        os.chmod(parent, mode)


# --- F1: nothing but the verified bytes starts in the platform interpreter -


_ENV_PROBE = (
    "import os, sys\n"
    "leaked = sorted(k for k in os.environ if k.startswith(('PYTHON', 'LD_', 'DYLD_')))\n"
    "print(sys.flags.isolated, ','.join(leaked) or '-')\n"
)


def _startup_hook(tmp_path):
    hook = tmp_path / "evil-site"
    hook.mkdir()
    marker = tmp_path / "startup-ran"
    (hook / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'w').close()\n", encoding="utf-8",
    )
    return hook, marker


def test_inherited_python_and_loader_env_do_not_reach_platform_script(
    home, platform_root, tmp_path, monkeypatch,
):
    from cron.scheduler import _run_job_script

    hook, marker = _startup_hook(tmp_path)
    _platform_script(platform_root, body=_ENV_PROBE)
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))
    monkeypatch.setenv("PYTHONPATH", str(hook))
    monkeypatch.setenv("PYTHONSTARTUP", str(hook / "sitecustomize.py"))
    monkeypatch.setenv("PYTHONINSPECT", "1")
    monkeypatch.setenv("LD_PRELOAD", str(tmp_path / "missing.so"))
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", str(tmp_path / "missing.dylib"))

    assert _run_job_script("platform:tick.py") == (True, "1 -")
    assert not marker.exists()


def test_profile_env_pythonpath_does_not_run_on_no_agent_platform_job(
    home, platform_root, tmp_path, monkeypatch,
):
    from cron.jobs import create_job
    from cron.scheduler import run_job

    hook, marker = _startup_hook(tmp_path)
    _platform_script(platform_root, body='print("watch tick")\n')
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))
    (home / ".env").write_text(f"PYTHONPATH={hook}\n", encoding="utf-8")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    job = create_job(
        prompt=None, schedule="every 5m", script="platform:tick.py", no_agent=True, deliver="local",
    )

    try:
        success, _doc, final, error = run_job(job)
    finally:
        os.environ.pop("PYTHONPATH", None)
    assert (success, error) == (True, None)
    assert "watch tick" in final
    assert not marker.exists()


def test_monitor_platform_script_ignores_pythonpath(home, platform_root, tmp_path, monkeypatch):
    from cron.monitor import check_monitor

    hook, marker = _startup_hook(tmp_path)
    _platform_script(platform_root, body='print("snapshot")\n')
    _config(home, allow_scripts=False, platform_script_root=str(platform_root))
    monkeypatch.setenv("PYTHONPATH", str(hook))

    outcome = check_monitor({"id": "m1", "monitor_script": "platform:tick.py"})
    assert outcome.ok is True, outcome
    assert not marker.exists()


def test_stock_tenant_script_keeps_user_pythonpath(home, tmp_path, monkeypatch):
    from cron.scheduler import _run_job_script

    lib = tmp_path / "userlib"
    lib.mkdir()
    (lib / "userhelper.py").write_text("VALUE = 'from user path'\n", encoding="utf-8")
    (home / "scripts" / "uses_lib.py").write_text(
        "import userhelper\nprint(userhelper.VALUE)\n", encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(lib))

    assert _run_job_script("uses_lib.py") == (True, "from user path")


# --- F5: every resolver I/O failure is a Blocked result, never a raise -----


@pytest.fixture
def unreadable_pinned(home, platform_root):
    path = _platform_script(platform_root)
    _config(
        home, allow_scripts=False, platform_script_root=str(platform_root),
        platform_script_digests={"tick.py": hashlib.sha256(path.read_bytes()).hexdigest()},
    )
    os.chmod(path, 0)
    yield path
    os.chmod(path, 0o555)


def test_unreadable_pinned_script_blocked_at_fire(unreadable_pinned):
    from cron.scheduler import _run_job_script

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert out.startswith("Blocked: platform script cannot be read")


def test_unreadable_pinned_script_blocked_on_prerun(unreadable_pinned):
    from unittest.mock import MagicMock, patch

    import cron.scheduler as scheduler

    agent = MagicMock()
    agent.run_conversation = MagicMock(return_value={"final_response": "ok", "messages": []})
    runtime = {
        "provider": "openrouter", "api_mode": "chat_completions",
        "base_url": "https://openrouter.ai/api/v1", "api_key": "test-key",
        "source": "stub", "requested_provider": None,
    }
    job = {"id": "job_prerun", "name": "prerun", "prompt": "summarize",
           "schedule": "*/5 * * * *", "script": "platform:tick.py"}
    with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime), \
         patch("run_agent.AIAgent", return_value=agent):
        scheduler.run_job(job)

    prompt = agent.run_conversation.call_args.args[0]
    assert "Blocked: platform script cannot be read" in prompt


def test_unreadable_pinned_script_blocked_on_monitor(unreadable_pinned):
    from cron.monitor import check_monitor

    outcome = check_monitor({"id": "m1", "monitor_script": "platform:tick.py"})
    assert outcome.ok is False
    assert "Blocked: platform script cannot be read" in outcome.error


def test_unreadable_pinned_script_refused_by_tool_validator(unreadable_pinned):
    from tools.cronjob_tools import _validate_cron_script_path

    assert _validate_cron_script_path("platform:tick.py").startswith(
        "Blocked: platform script cannot be read"
    )


def test_nul_in_configured_root_is_blocked(home):
    from cron.scheduler import _run_job_script

    _config(home, allow_scripts=False, platform_script_root="/opt/x\x00y")

    ok, out = _run_job_script("platform:tick.py")
    assert ok is False
    assert out.startswith("Blocked:")
