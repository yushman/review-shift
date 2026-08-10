"""discover.py: the filter pipeline (ADR-012, TDR FR-2) plus the six ADR-012 edge cases,
each against a real fixture repo — not mocks, since these are exactly the git states that
look fine in a mock and misbehave for real (worktrees, detached HEAD, orphan history,
gitlinks, binary diffs, renames).
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src import discover


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_out(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _commit_dated(repo: Path, message: str, hours_ago: float) -> None:
    import os

    # "@<epoch> +0000" is unambiguous (git's own --date format) — a plain ISO string without
    # an offset gets interpreted in the local timezone by git, silently skewing the fixture.
    when = datetime.now(UTC) - timedelta(hours=hours_ago)
    ts = f"@{int(when.timestamp())} +0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": ts}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message, "--date", ts, "--allow-empty"],
        check=True, capture_output=True, text=True, env=env,
    )


# --- filter pipeline ---------------------------------------------------------------------


def test_pattern_selects_matching_branch(branched_repo: Path):
    result = discover.discover(branched_repo, "main", patterns=["feature/*"])
    assert [c.branch for c in result.selected] == ["feature/x"]
    assert result.skipped == []


def test_exclude_patterns_wins_over_patterns(branched_repo: Path):
    result = discover.discover(
        branched_repo, "main", patterns=["feature/*"], exclude_patterns=["feature/*"]
    )
    assert result.selected == []


def test_no_patterns_and_no_discover_all_selects_nothing(branched_repo: Path):
    result = discover.discover(branched_repo, "main")
    assert result.selected == []


def test_discover_all_selects_every_local_branch_except_base(branched_repo: Path):
    _git(branched_repo, "branch", "another")
    result = discover.discover(branched_repo, "main", discover_all=True)
    assert {c.branch for c in result.selected} == {"feature/x", "another"}


def test_max_age_hours_drops_old_branch(branched_repo: Path):
    _git(branched_repo, "checkout", "-q", "-b", "old-branch")
    _commit_dated(branched_repo, "old work", hours_ago=100)
    _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(
        branched_repo, "main", discover_all=True, max_age_hours=24
    )
    assert "old-branch" not in {c.branch for c in result.selected}
    assert "feature/x" in {c.branch for c in result.selected}


def test_sorted_by_committerdate_desc(branched_repo: Path):
    # feature/x's commit is real "now" from the fixture; force "newer" strictly past that
    # regardless of clock skew between fixture setup and this line.
    _git(branched_repo, "checkout", "-q", "-b", "newer")
    _commit_dated(branched_repo, "newer work", hours_ago=-1)
    _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(branched_repo, "main", discover_all=True)
    branches = [c.branch for c in result.selected]
    assert branches.index("newer") < branches.index("feature/x")


def test_max_branches_per_run_caps_and_records_reason(branched_repo: Path):
    for i in range(3):
        _git(branched_repo, "checkout", "-q", "-b", f"extra-{i}", "main")
        _commit_dated(branched_repo, f"extra {i}", hours_ago=i)
        _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(branched_repo, "main", discover_all=True, max_branches_per_run=2)
    assert len(result.selected) == 2
    capped = [s for s in result.skipped if s["reason"] == "max_branches_per_run_cap"]
    assert len(capped) == 2  # 4 candidates total (feature/x + 3 extras), cap at 2


def test_diff_too_large_is_skipped_with_reason(branched_repo: Path):
    _git(branched_repo, "checkout", "-q", "-b", "huge")
    big = "\n".join(f"line {i}" for i in range(3000))
    (branched_repo / "src" / "big.py").write_text(big)
    _git(branched_repo, "add", ".")
    _git(branched_repo, "commit", "-q", "-m", "huge change")
    _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(
        branched_repo, "main", patterns=["huge"], max_diff_lines=2000
    )
    assert result.selected == []
    assert result.skipped[0]["reason"] == "diff_too_large"
    assert result.skipped[0]["branch"] == "huge"


# --- ADR-012 edge cases -------------------------------------------------------------------


def test_branch_checked_out_in_worktree_is_reviewed_and_annotated(
    branched_repo: Path, tmp_path: Path
):
    """ADR-012: reviewed normally (read-only), but annotated `checked_out_elsewhere` since
    the apply recipe would need the right worktree — not skipped."""
    wt_path = tmp_path / "linked-worktree"
    _git(branched_repo, "worktree", "add", "-q", str(wt_path), "feature/x")
    try:
        result = discover.discover(branched_repo, "main", patterns=["feature/*"])
        assert len(result.selected) == 1
        assert result.selected[0].branch == "feature/x"
        assert result.selected[0].checked_out_elsewhere is True
        assert result.skipped == []
    finally:
        _git(branched_repo, "worktree", "remove", "--force", str(wt_path))


def test_detached_head_does_not_crash_or_appear_as_a_branch(branched_repo: Path):
    """ADR-012: a detached HEAD is not a ref under refs/heads/ and is never returned by
    for-each-ref; discovery must still work normally for real branches while HEAD is
    detached."""
    sha = _git_out(branched_repo, "rev-parse", "feature/x")
    _git(branched_repo, "checkout", "-q", "--detach", sha)
    try:
        result = discover.discover(branched_repo, "main", patterns=["feature/*"])
        assert [c.branch for c in result.selected] == ["feature/x"]
        assert "HEAD" not in {c.branch for c in result.selected}
    finally:
        _git(branched_repo, "checkout", "-q", "main")


def test_orphan_branch_has_no_merge_base_and_is_skipped(branched_repo: Path):
    """ADR-012: no merge-base with base_branch (orphan import, shallow clone) would
    otherwise diff 'everything against everything' — must be skipped, not reviewed."""
    _git(branched_repo, "checkout", "-q", "--orphan", "orphan-branch")
    _git(branched_repo, "rm", "-rf", "-q", "--cached", ".")
    (branched_repo / "unrelated.txt").write_text("nothing to do with main\n")
    _git(branched_repo, "add", ".")
    _git(branched_repo, "commit", "-q", "-m", "orphan root")
    _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(branched_repo, "main", patterns=["orphan-branch"])
    assert result.selected == []
    assert result.skipped == [
        {"branch": "orphan-branch", "reason": "no_merge_base", "base": "main"}
    ]


def test_submodule_gitlink_change_does_not_crash_and_branch_is_reviewed(branched_repo: Path):
    """ADR-012: a gitlink (submodule pointer) change is not code and must not be diffed —
    --ignore-submodules=all keeps it out of the changed-line count entirely, and the branch
    is reviewed normally rather than skipped."""
    fake_sha_a = "a" * 40
    fake_sha_b = "b" * 40
    _git(branched_repo, "update-index", "--add", "--cacheinfo", f"160000,{fake_sha_a},vendor/lib")
    _git(branched_repo, "commit", "-q", "-m", "add gitlink")
    _git(branched_repo, "checkout", "-q", "-b", "bump-submodule")
    _git(branched_repo, "update-index", "--cacheinfo", f"160000,{fake_sha_b},vendor/lib")
    _git(branched_repo, "commit", "-q", "-m", "bump submodule pointer")
    _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(branched_repo, "main", patterns=["bump-submodule"])
    assert [c.branch for c in result.selected] == ["bump-submodule"]
    assert result.skipped == []


def test_binary_file_diff_does_not_crash_and_branch_is_reviewed(branched_repo: Path):
    """ADR-012: `git diff --numstat` reports `-\t-` for a binary file; that must not crash
    int() parsing and must not be counted toward the diff-size gate."""
    _git(branched_repo, "checkout", "-q", "-b", "add-binary")
    (branched_repo / "blob.bin").write_bytes(bytes(range(256)) * 4)
    _git(branched_repo, "add", ".")
    _git(branched_repo, "commit", "-q", "-m", "add binary blob")
    _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(branched_repo, "main", patterns=["add-binary"], max_diff_lines=10)
    assert [c.branch for c in result.selected] == ["add-binary"]
    assert result.skipped == []


def test_rename_is_handled_correctly_not_skipped(branched_repo: Path):
    """ADR-012: `git diff -M` handles renames; a pure rename must not be miscounted as a
    huge diff or crash the size gate."""
    _git(branched_repo, "checkout", "-q", "-b", "rename-file")
    _git(branched_repo, "mv", "src/foo.py", "src/foo_renamed.py")
    _git(branched_repo, "commit", "-q", "-m", "rename foo.py")
    _git(branched_repo, "checkout", "-q", "main")

    result = discover.discover(branched_repo, "main", patterns=["rename-file"], max_diff_lines=10)
    assert [c.branch for c in result.selected] == ["rename-file"]
    assert result.skipped == []


def test_unrelated_git_error_is_not_swallowed(tmp_path: Path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(discover.DiscoverError):
        discover.discover(not_a_repo, "main", discover_all=True)
