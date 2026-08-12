"""Which local branches get reviewed in a run, and why one is skipped when it isn't.

Filter order is fixed by TDR FR-2 and is not configurable (design.md "Filter order is fixed,
not configurable"): `discover_all`/`patterns` -> `exclude_patterns` -> `max_age_hours` ->
merge-base check -> diff-size gate -> sort by `committerdate desc` -> `max_branches_per_run`.

The six ADR-012 edge cases (worktree, detached HEAD, orphan branch, submodule gitlink,
binary file, rename) are each handled explicitly below; only two of them are actual skips
(`no_merge_base`, and the diff-size gate above it) — the rest are "reviewed normally, handled
correctly" per ADR-012's own text, not skips.
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from review_shift import gitutil


class DiscoverError(RuntimeError):
    pass


def _run(repo_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DiscoverError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class Candidate:
    branch: str
    head_sha: str
    committerdate: int
    checked_out_elsewhere: bool = False


@dataclass
class DiscoveryResult:
    selected: list[Candidate]
    skipped: list[dict[str, object]]


def _list_branch_refs(repo_root: Path) -> list[tuple[str, str, int]]:
    out = _run(
        repo_root,
        [
            "for-each-ref",
            "refs/heads/",
            "--format=%(refname:short)%09%(objectname)%09%(committerdate:unix)",
        ],
    )
    refs = []
    for line in out.splitlines():
        if not line:
            continue
        name, sha, ts = line.split("\t")
        refs.append((name, sha, int(ts)))
    return refs


def _worktree_branches_elsewhere(repo_root: Path) -> set[str]:
    """Branches checked out in a linked worktree other than the one we're running in
    (ADR-012: read-only review is fine, but the apply recipe needs the right worktree)."""
    out = _run(repo_root, ["worktree", "list", "--porcelain"])
    blocks: list[list[str]] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)

    here = repo_root.resolve()
    branches: set[str] = set()
    for block in blocks:
        wt_path = Path(block[0][len("worktree "):]).resolve()
        if wt_path == here:
            continue
        for line in block:
            if line.startswith("branch refs/heads/"):
                branches.add(line[len("branch refs/heads/"):])
    return branches


def _has_merge_base(repo_root: Path, base: str, branch: str) -> bool:
    # No `--quiet`: real git merge-base has no such flag (ADR-012's shell snippet is
    # slightly off) — exit 1 with empty stdout/stderr already means "no common ancestor".
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", base, branch],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise DiscoverError(f"git merge-base failed: {proc.stderr.strip()}")
    return proc.returncode == 0


def _changed_lines(repo_root: Path, base: str, branch: str) -> int:
    """Changed-line count for the diff-size gate. `--ignore-submodules=all` keeps gitlink
    changes out entirely (ADR-012); a binary file reports `-\t-` in numstat, which counts as
    zero rather than crashing int()."""
    out = _run(
        repo_root,
        ["diff", "--merge-base", "--ignore-submodules=all", "-M", "--numstat", base, branch],
    )
    total = 0
    for line in out.splitlines():
        if not line:
            continue
        added, deleted, _path = line.split("\t", 2)
        if added == "-" or deleted == "-":
            continue
        total += int(added) + int(deleted)
    return total


def _matches(name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("re:"):
            if re.search(pattern[3:], name):
                return True
        elif fnmatch.fnmatch(name, pattern):
            return True
    return False


def discover(
    repo_root: Path,
    base: str,
    *,
    patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    discover_all: bool = False,
    max_age_hours: float = 24,
    max_branches_per_run: int = 10,
    max_diff_lines: int = 2000,
    now: datetime | None = None,
) -> DiscoveryResult:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=max_age_hours)
    elsewhere = _worktree_branches_elsewhere(repo_root)

    refs = _list_branch_refs(repo_root)
    candidates = [ref for ref in refs if ref[0] != base]

    if discover_all:
        pass
    elif patterns:
        candidates = [c for c in candidates if _matches(c[0], patterns)]
    else:
        candidates = []

    if exclude_patterns:
        candidates = [c for c in candidates if not _matches(c[0], exclude_patterns)]

    candidates = [
        c for c in candidates if datetime.fromtimestamp(c[2], tz=UTC) >= cutoff
    ]

    selected: list[Candidate] = []
    skipped: list[dict[str, object]] = []
    for name, sha, ts in candidates:
        if not _has_merge_base(repo_root, base, name):
            skipped.append({"branch": name, "reason": "no_merge_base", "base": base})
            continue
        changed = _changed_lines(repo_root, base, name)
        if changed > max_diff_lines:
            skipped.append(
                {"branch": name, "reason": "diff_too_large", "changed_lines": changed}
            )
            continue
        selected.append(
            Candidate(
                branch=name,
                head_sha=sha,
                committerdate=ts,
                checked_out_elsewhere=name in elsewhere,
            )
        )

    selected.sort(key=lambda c: c.committerdate, reverse=True)
    if len(selected) > max_branches_per_run:
        for dropped in selected[max_branches_per_run:]:
            skipped.append({"branch": dropped.branch, "reason": "max_branches_per_run_cap"})
        selected = selected[:max_branches_per_run]

    return DiscoveryResult(selected=selected, skipped=skipped)


# --- trunk-review: commits landed directly on the base branch ----------------------------


@dataclass
class TrunkUnit:
    sha: str
    diff_too_large: bool = False
    changed_lines: int | None = None  # informational, set when diff_too_large is True


@dataclass
class TrunkDiscoveryResult:
    # "units": a valid (possibly empty-after-cap) range was found; "bootstrap": no valid
    # watermark, nothing reviewed this run; "nothing_new": a valid watermark already equals
    # (or has no direct commits ahead of) the base head.
    outcome: str
    base_head_sha: str
    anchor_sha: str | None  # the watermark actually used to select `units`; None for bootstrap
    # Ordered oldest-first, within `max_commits_per_run` -- includes `diff_too_large` entries
    # in place so batch.py can advance the watermark through them in the right sequence
    # (design.md D4: the watermark is a single sha and must stay contiguous).
    units: list[TrunkUnit] = field(default_factory=list)
    # Commits beyond the cap boundary: never attempted this run, so they never touch the
    # watermark, distinct from the interleaved `diff_too_large` entries in `units`.
    skipped: list[dict[str, object]] = field(default_factory=list)


def _changed_lines_for_commit(repo_root: Path, sha: str) -> int:
    """Same counting rule as `_changed_lines`, against the commit's own parent instead of a
    branch's merge-base (`--ignore-submodules=all` keeps gitlink bumps out; a binary file's
    `-\t-` counts as zero rather than crashing int())."""
    out = _run(
        repo_root,
        ["show", "--numstat", "--format=", "-M", "--ignore-submodules=all", sha],
    )
    total = 0
    for line in out.splitlines():
        if not line:
            continue
        added, deleted, _path = line.split("\t", 2)
        if added == "-" or deleted == "-":
            continue
        total += int(added) + int(deleted)
    return total


def discover_trunk(
    repo_root: Path,
    base: str,
    watermark: str | None,
    *,
    max_commits_per_run: int = 10,
    max_diff_lines: int = 2000,
) -> TrunkDiscoveryResult:
    """Selects the trunk review units for one run (trunk-review spec). The anchor is the
    persisted watermark, validated with `is_ancestor` before use; an absent or invalidated
    watermark (history rewritten under it) bootstraps instead of guessing a range
    (design.md D1/D2)."""
    base_head_sha = gitutil.rev_parse(repo_root, base)

    anchor_valid = False
    if watermark:
        try:
            anchor_valid = gitutil.is_ancestor(repo_root, watermark, base)
        except gitutil.GitError:
            anchor_valid = False  # e.g. the watermark sha no longer resolves at all

    if not anchor_valid:
        return TrunkDiscoveryResult(
            outcome="bootstrap", base_head_sha=base_head_sha, anchor_sha=None,
        )
    assert watermark is not None  # anchor_valid is only ever True inside `if watermark:` above

    candidates = gitutil.rev_list_direct(repo_root, watermark, base)
    if not candidates:
        return TrunkDiscoveryResult(
            outcome="nothing_new", base_head_sha=base_head_sha, anchor_sha=watermark,
        )

    skipped: list[dict[str, object]] = []
    if len(candidates) > max_commits_per_run:
        for dropped in candidates[max_commits_per_run:]:
            skipped.append({"sha": dropped, "reason": "max_commits_per_run_cap"})
        candidates = candidates[:max_commits_per_run]

    units: list[TrunkUnit] = []
    for sha in candidates:
        changed = _changed_lines_for_commit(repo_root, sha)
        if changed > max_diff_lines:
            units.append(TrunkUnit(sha=sha, diff_too_large=True, changed_lines=changed))
        else:
            units.append(TrunkUnit(sha=sha))

    return TrunkDiscoveryResult(
        outcome="units", base_head_sha=base_head_sha, anchor_sha=watermark,
        units=units, skipped=skipped,
    )
