"""gitutil.resolve_base_branch: `base_branch: auto` -> origin/HEAD, explicit value passthrough
(TDR FR-3, batch-execution spec "base_branch: auto resolves to origin/HEAD").
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src import gitutil


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_origin(tmp_path: Path) -> Path:
    """A local repo whose `origin` remote is another local repo, with origin/HEAD set to
    `main` — mirrors a real `git clone`'s default without touching the network."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "test")
    (upstream / "f.txt").write_text("one\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-q", "-m", "initial")
    _git(upstream, "branch", "-m", "main")

    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(upstream), str(repo))
    return repo


def test_resolve_base_branch_explicit_value_passes_through(tmp_path: Path):
    assert gitutil.resolve_base_branch(tmp_path, "develop") == "develop"


def test_resolve_base_branch_auto_resolves_origin_head(repo_with_origin: Path):
    assert gitutil.resolve_base_branch(repo_with_origin, "auto") == "main"


def test_resolve_base_branch_auto_without_origin_raises(tmp_path: Path):
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    with pytest.raises(gitutil.GitError):
        gitutil.resolve_base_branch(repo, "auto")
