"""WEV-3325: branch() must not spawn git for a cwd that cannot be in a repository."""

from __future__ import annotations

import subprocess

import pytest

from tui_gateway import git_probe


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    real = git_probe.bounded_git_probe

    def record(argv, *, timeout):
        calls.append(list(argv))
        return real(argv, timeout=timeout)

    monkeypatch.setattr(git_probe, "bounded_git_probe", record)
    return calls


def test_non_repo_cwd_never_spawns_git(tmp_path, spawns):
    plain = tmp_path / "opt" / "data"
    plain.mkdir(parents=True)

    assert git_probe.branch(str(plain)) == ""
    assert spawns == []


def test_repo_and_nested_cwd_still_report_branch(tmp_path, spawns):
    repo = tmp_path / "repo"
    nested = repo / "pkg" / "deep"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)

    assert git_probe.branch(str(repo)) == "trunk"
    assert git_probe.branch(str(nested)) == "trunk"
    assert spawns


def test_linked_worktree_git_file_still_probes(tmp_path, spawns):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "side", str(linked)], check=True)

    assert (linked / ".git").is_file()
    assert git_probe.branch(str(linked)) == "side"


def test_git_dir_environment_still_probes(tmp_path, spawns, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))

    git_probe.branch(str(plain))

    assert spawns
