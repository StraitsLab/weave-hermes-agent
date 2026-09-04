from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "audit_pr_attribution.py"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, text=True, capture_output=True
    )


def commit(repo: Path, name: str, email: str, message: str) -> str:
    marker = repo / message
    marker.write_text(message)
    git(repo, "add", marker.name)
    git(
        repo,
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "commit", "-m", message,
    )
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "contributors" / "emails").mkdir(parents=True)
    (tmp_path / "scripts" / "release.py").write_text("AUTHOR_MAP = {}\n")
    return tmp_path


def audit(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(SCRIPT), "--base-ref", base],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def test_scans_only_mapped_authors_after_base(repo: Path) -> None:
    base = commit(repo, "Old", "ignored@example.com", "base")
    commit(repo, "New", "mapped@example.com", "mapped")
    (repo / "contributors" / "emails" / "mapped@example.com").write_text("mapped\n")

    result = audit(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_rejects_unmapped_author_after_base(repo: Path) -> None:
    base = commit(repo, "Old", "ignored@example.com", "base")
    commit(repo, "New", "unknown@example.com", "unknown")

    result = audit(repo, base)

    assert result.returncode != 0
    assert "unknown@example.com" in result.stdout


@pytest.mark.parametrize("base", ["missing", "side"])
def test_rejects_invalid_or_non_ancestor_base(repo: Path, base: str) -> None:
    ancestor = commit(repo, "Old", "ignored@example.com", "base")
    if base == "side":
        git(repo, "checkout", "--orphan", "side")
        git(repo, "rm", "-rf", ".")
        base = commit(repo, "Side", "side@example.com", "side")
        git(repo, "checkout", ancestor)
    else:
        assert ancestor

    result = audit(repo, base)

    assert result.returncode != 0
    assert "valid ancestor" in (result.stdout + result.stderr)
