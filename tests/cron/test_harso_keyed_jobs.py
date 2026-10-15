"""Harso keyed job admission (ported from the v0.21 fork)."""
import os
import subprocess
import sys
import threading
from pathlib import Path
import pytest
from cron.jobs import create_job, load_jobs

@pytest.fixture
def tmp_cron_dir(tmp_path, monkeypatch):
    from cron import jobs
    home = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(jobs, "HERMES_DIR", home)
    monkeypatch.setattr(jobs, "CRON_DIR", home / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", home / "cron" / "output")
    return home

class TestDedup:
    def test_create_dedup_key_returns_existing_job(self, tmp_cron_dir):
        from cron import jobs as jobs_mod
        first = create_job(prompt="one", schedule="30m", dedup_key="routine-1", attach_to_session=True)
        second = create_job(prompt="one", schedule="30m", dedup_key="routine-1", attach_to_session=True)
        assert second["id"] == first["id"]
        assert "_dedup_fingerprint" not in second
        for changed in ({"prompt": "changed", "schedule": "30m"},
                        {"prompt": "one", "schedule": "31m"},
                        {"prompt": "one", "schedule": "30m", "attach_to_session": False}):
            with pytest.raises(jobs_mod.CronDedupConflict) as conflict:
                create_job(**changed, dedup_key="routine-1")
            assert conflict.value.job_id == first["id"]
        assert len(load_jobs()) == 1

    @pytest.mark.parametrize("key", ["", "space key", "\n", "x" * 129, 7])
    def test_create_rejects_invalid_dedup_key(self, tmp_cron_dir, key):
        from cron import jobs as jobs_mod
        with pytest.raises(jobs_mod.CronDedupKeyInvalid):
            create_job(prompt="one", schedule="30m", dedup_key=key)

    def test_create_dedup_key_is_atomic_across_threads(self, tmp_cron_dir):
        results = []

        def create():
            results.append(create_job(prompt="one", schedule="30m", dedup_key="routine-race"))

        threads = [threading.Thread(target=create) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len({job["id"] for job in results}) == 1
        assert len(load_jobs()) == 1

    def test_keyed_create_fails_closed_without_durable_lock(self, tmp_cron_dir, monkeypatch):
        """A keyed retry must not mint a process-local duplicate."""
        from cron import jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "fcntl", None)
        monkeypatch.setattr(jobs_mod, "msvcrt", None)
        with pytest.raises(jobs_mod.CronStoreLockUnavailable):
            create_job(prompt="one", schedule="30m", dedup_key="no-lock")
        assert load_jobs() == []

    def test_create_dedup_key_replays_across_processes_and_profiles(self, tmp_path):
        repo = Path(__file__).resolve().parents[2]
        code = (
            "from cron.jobs import create_job; "
            "print(create_job(prompt='one', schedule='every 5m', "
            "dedup_key='process-key')['id'])"
        )

        def child(home, *, capture=True):
            env = os.environ.copy()
            env["HERMES_HOME"] = str(home)
            env["PYTHONPATH"] = str(repo)
            return subprocess.run(
                [sys.executable, "-c", code],
                cwd=repo,
                env=env,
                text=True,
                capture_output=capture,
                check=True,
            )

        first_home = tmp_path / "first"
        second_home = tmp_path / "second"
        first = child(first_home).stdout.strip()
        # Simulate a successful create whose response was lost before the
        # caller observed it; the retry must return the durable same ID.
        child(first_home, capture=False)
        replay = child(first_home).stdout.strip()
        other_profile = child(second_home).stdout.strip()
        assert replay == first
        assert other_profile != first
        assert len(__import__("json").loads((first_home / "cron" / "jobs.json").read_text())["jobs"]) == 1
        assert len(__import__("json").loads((second_home / "cron" / "jobs.json").read_text())["jobs"]) == 1

