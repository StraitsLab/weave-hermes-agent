"""Memory-trim and cache-expiry coverage for gateway housekeeping."""

import os
import time

import pytest

import gateway.run as gateway_run


class _OneTickStopEvent:
    """Run one housekeeping tick without a sleep or background thread."""

    def __init__(self):
        self.waited = False

    def is_set(self):
        return self.waited

    def wait(self, timeout=None):
        self.waited = True
        return True


class _HourlyPassStopEvent:
    """Advance the real ticker to one hourly pass without sleeping."""

    def __init__(self):
        self.ticks = 0

    def is_set(self):
        return self.ticks >= 60

    def wait(self, timeout=None):
        self.ticks += 1
        return self.is_set()


def test_gateway_housekeeping_calls_periodic_memory_trim(monkeypatch):
    import hermes_cli.mem_trim as mem_trim

    calls = []
    monkeypatch.setattr(
        mem_trim,
        "trim_memory",
        lambda **kwargs: calls.append(kwargs) or True,
    )

    gateway_run._start_gateway_housekeeping(_OneTickStopEvent(), interval=0)

    assert calls == [{"reason": "messaging gateway housekeeping"}]


@pytest.mark.parametrize("enabled", [True, False, None], ids=["on", "off", "default-off"])
def test_hourly_cache_expiry_reaches_route_homes_only_when_enabled(
    tmp_path, monkeypatch, enabled
):
    from hermes_constants import get_hermes_home, get_hermes_home_override

    root_home = tmp_path / ".hermes"
    route_home = root_home / "profiles" / "general"
    excluded_home = root_home / "profiles" / "excluded"
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", root_home)

    files = {}
    now = time.time()
    for home in (root_home, route_home, excluded_home):
        for cache in ("spillover", "documents", "images", "audio"):
            directory = home / "cache" / cache
            directory.mkdir(parents=True)
            for name, age_hours in (("old", 25), ("fresh", 1)):
                path = directory / f"{name}.txt"
                path.write_text(f"{cache} {name}", encoding="utf-8")
                timestamp = now - age_hours * 3600
                os.utime(path, (timestamp, timestamp))
                files[home, cache, name] = path

    setting = "" if enabled is None else f"  housekeeping_all_profile_homes: {str(enabled).lower()}\n"
    (root_home / "config.yaml").write_text(
        "gateway:\n"
        "  multiplex_profiles: true\n"
        "  multiplex_profile_allowlist: [general]\n"
        + setting,
        encoding="utf-8",
    )

    # Only cache cleanup is under test. Keep unrelated hourly network and
    # maintenance jobs inert; the real profile resolver and cleaners run.
    monkeypatch.setattr("hermes_cli.mem_trim.trim_memory", lambda **kwargs: False)
    monkeypatch.setattr("agent.curator.maybe_run_curator", lambda **kwargs: None)
    monkeypatch.setattr("tools.skills_sync_client.maybe_pull_skills", lambda: None)
    monkeypatch.setattr("tools.skills_sync_client.maybe_pull_org_skills", lambda: None)
    monkeypatch.setattr("hermes_cli.debug._sweep_expired_pastes", lambda: (0, 0))

    previous_override = get_hermes_home_override()
    gateway_run._start_gateway_housekeeping(_HourlyPassStopEvent(), interval=0)

    for home in (root_home, route_home, excluded_home):
        for cache in ("spillover", "documents", "images", "audio"):
            should_expire = home == root_home or (home == route_home and enabled is True)
            assert files[home, cache, "old"].exists() is not should_expire, (home, cache)
            assert files[home, cache, "fresh"].read_text(encoding="utf-8") == f"{cache} fresh"
    assert get_hermes_home_override() == previous_override
    assert get_hermes_home() == root_home

    if enabled is None:
        # Enabling the setting on disk takes effect on the next hourly pass,
        # without rebuilding or starting a gateway.
        config_path = root_home / "config.yaml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + "  housekeeping_all_profile_homes: true\n",
            encoding="utf-8",
        )
        timestamp = config_path.stat().st_mtime + 1
        os.utime(config_path, (timestamp, timestamp))
        gateway_run._start_gateway_housekeeping(_HourlyPassStopEvent(), interval=0)
        for cache in ("spillover", "documents", "images", "audio"):
            assert not files[route_home, cache, "old"].exists()
            assert files[route_home, cache, "fresh"].exists()
            assert files[excluded_home, cache, "old"].exists()
        assert get_hermes_home_override() == previous_override
