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


def test_platform_non_root_owner_refused(home, platform_root, monkeypatch):
    import cron.scheduler as scheduler

    _platform_script(platform_root)
    _config(home, platform_script_root=str(platform_root))
    # Production owner uid; this test process is not uid 0.
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
