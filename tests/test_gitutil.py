"""gitutil.resolve_base_branch: `base_branch: auto` -> origin/HEAD, explicit value passthrough
(TDR FR-3, batch-execution spec "base_branch: auto resolves to origin/HEAD").
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from review_shift import gitutil


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


# --- trunk-review git plumbing --------------------------------------------------------------


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref], check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_rev_list_direct_excludes_merged_branch_commits(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")
    _git(repo, "branch", "-m", "main")
    anchor = _rev(repo, "main")

    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "f.txt").write_text("feature\n")
    _git(repo, "commit", "-q", "-am", "feature work")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    (repo / "g.txt").write_text("direct\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "direct commit")

    direct_sha = _rev(repo, "main")
    shas = gitutil.rev_list_direct(repo, anchor, "main")
    assert shas == [direct_sha]


def test_rev_list_direct_returns_oldest_first(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")
    _git(repo, "branch", "-m", "main")
    anchor = _rev(repo, "main")

    shas = []
    for i in range(3):
        (repo / "f.txt").write_text(f"v{i}\n")
        _git(repo, "commit", "-q", "-am", f"c{i + 1}")
        shas.append(_rev(repo, "main"))

    assert gitutil.rev_list_direct(repo, anchor, "main") == shas


def test_is_ancestor_true_and_false(branched_repo: Path):
    main_sha = _rev(branched_repo, "main")
    feature_sha = _rev(branched_repo, "feature/x")
    assert gitutil.is_ancestor(branched_repo, main_sha, "feature/x") is True
    assert gitutil.is_ancestor(branched_repo, feature_sha, "main") is False


def test_is_ancestor_false_after_history_rewrite(branched_repo: Path):
    """A watermark invalidated by rebase/amend/reset must read as `False`, not raise -- it's
    the trigger for bootstrap, not a git error."""
    old_feature_sha = _rev(branched_repo, "feature/x")
    _git(branched_repo, "checkout", "-q", "feature/x")
    _git(branched_repo, "commit", "-q", "--amend", "-m", "rewritten")
    _git(branched_repo, "checkout", "-q", "main")
    assert gitutil.is_ancestor(branched_repo, old_feature_sha, "feature/x") is False


def test_is_ancestor_raises_on_unknown_sha(branched_repo: Path):
    with pytest.raises(gitutil.GitError):
        gitutil.is_ancestor(branched_repo, "d" * 40, "main")


def test_commit_diff_matches_branch_diff_flags(fixture_repo: Path):
    (fixture_repo / "src" / "bar.py").write_text("VALUE = 1  # comment\nOTHER = 2\n")
    _git(fixture_repo, "commit", "-q", "-am", "add comment")
    sha = _rev(fixture_repo, "HEAD")
    diff_text = gitutil.commit_diff(fixture_repo, sha)
    assert "src/bar.py" in diff_text
    assert "+VALUE = 1  # comment" in diff_text


def test_commit_diff_handles_root_commit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("a\nb\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "root")
    sha = _rev(repo, "HEAD")
    diff_text = gitutil.commit_diff(repo, sha)
    assert "new file mode" in diff_text
    assert "+a" in diff_text and "+b" in diff_text


def test_commit_diff_handles_rename(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("x = 1\n" * 10)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add a")
    _git(repo, "mv", "src/a.py", "src/b.py")
    _git(repo, "commit", "-q", "-m", "rename")
    sha = _rev(repo, "HEAD")
    diff_text = gitutil.commit_diff(repo, sha)
    assert "rename from src/a.py" in diff_text
    assert "rename to src/b.py" in diff_text


def test_commit_diff_ignores_binary_and_does_not_crash(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")
    (repo / "blob.bin").write_bytes(bytes(range(256)))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add binary")
    sha = _rev(repo, "HEAD")
    diff_text = gitutil.commit_diff(repo, sha)
    assert "Binary files" in diff_text


def test_blame_line_map_maps_lines_to_originating_commit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("a\nb\nc\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c1")
    c1 = _rev(repo, "HEAD")
    (repo / "f.txt").write_text("a\nB\nc\n")
    _git(repo, "commit", "-q", "-am", "c2")
    c2 = _rev(repo, "HEAD")

    line_map, authors = gitutil.blame_line_map(repo, "HEAD", "f.txt")
    assert line_map == {1: c1, 2: c2, 3: c1}
    assert authors[c1] == "test"
    assert authors[c2] == "test"
