"""Scores findings against ground truth (design.md D4, D6). Deterministic -- scoring a fixed
findings list against fixed ranges needs no model call, which is why it runs in normal
`pytest` (tasks.md 7.4) even though producing the findings does not.
"""
from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.case import Case

__all__ = [
    "TOLERANCES", "Finding", "CaseRunResult", "is_hit", "case_hit", "recall",
    "applicability", "git_apply_check",
]

Finding = dict[str, Any]
PatchCheck = Callable[[Path, str, Path], bool]

# D4: both tolerances are always computed and reported, never one chosen and buried.
TOLERANCES = (0, 10)


@dataclass
class CaseRunResult:
    case: Case
    depth: str
    findings: list[Finding] | None  # None when this case/depth never ran
    cost_usd: float
    status: str  # "ok" | "budget_exhausted" | "materialize_failed" | "review_failed" | ...
    reason: str | None = None
    # Needed to verify patches independently rather than trusting findings.json's own status.
    run_dir: Path | None = None
    repo_dir: Path | None = None
    head_sha: str | None = None


def _finding_span(finding: Finding) -> tuple[int, int]:
    line = int(finding["line"])
    end_line = int(finding.get("end_line", line))
    return line, end_line


def is_hit(finding: Finding, gt_file: str, gt_start: int, gt_end: int, tolerance: int) -> bool:
    """D4: same file, and the finding's line span intersects the ground-truth range after
    both are expanded by `tolerance`."""
    if finding.get("file") != gt_file:
        return False
    f_start, f_end = _finding_span(finding)
    lo, hi = gt_start - tolerance, gt_end + tolerance
    return f_start <= hi and lo <= f_end


def case_hit(findings: list[Finding], case: Case, tolerance: int) -> bool:
    return any(
        is_hit(f, gt.file, gt.start_line, gt.end_line, tolerance)
        for f in findings
        for gt in case.ground_truth
    )


def recall(
    results: list[CaseRunResult], depth: str, tolerance: int, *, diff_visible_only: bool = False
) -> tuple[float, int]:
    """Recall at `depth` and `tolerance`, over completed (`status == "ok"`) cases only --
    a case that never ran contributes to neither the numerator nor the denominator, so an
    exhausted budget shrinks `n` rather than silently counting as a miss."""
    subset = [
        r for r in results
        if r.status == "ok" and r.depth == depth and (not diff_visible_only or r.case.diff_visible)
    ]
    n = len(subset)
    if n == 0:
        return 0.0, 0
    hits = sum(1 for r in subset if case_hit(r.findings or [], r.case, tolerance))
    return hits / n, n


def git_apply_check(repo_dir: Path, head_sha: str, patch: Path) -> bool:
    """`git apply --check` against a disposable worktree at `head_sha` -- never the clone's own
    working tree (tasks.md 6.4), which may carry unrelated state we must not disturb."""
    with tempfile.TemporaryDirectory() as tmp:
        wt = Path(tmp) / "wt"
        add = subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(wt), head_sha],
            capture_output=True, text=True, check=False,
        )
        if add.returncode != 0:
            return False
        try:
            proc = subprocess.run(
                ["git", "-C", str(wt), "apply", "--check", str(patch)],
                capture_output=True, text=True, check=False,
            )
            return proc.returncode == 0
        finally:
            subprocess.run(
                ["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(wt)],
                capture_output=True, text=True, check=False,
            )


def applicability(
    results: list[CaseRunResult], *, check: PatchCheck = git_apply_check
) -> tuple[float, int]:
    """The fraction of generated `.patch` files that pass `git apply --check` on the first try
    -- product-analysis.md §5 metric 1, which is stated per patch file and is the only
    quality-adjacent number the README is allowed to make.

    The check is re-run here rather than read from `findings.json`'s `status` field. A bench
    that asks the tool whether its own patches applied cannot detect a defect in the tool's
    verification path: it would report full applicability while patches failed for users. The
    duplicated `git apply --check` is the point, not an oversight.
    """
    total = 0
    passed = 0
    for r in results:
        if r.status != "ok" or r.run_dir is None or r.repo_dir is None or r.head_sha is None:
            continue
        for patch in sorted((r.run_dir / "patches").glob("*.patch")):
            total += 1
            if check(r.repo_dir, r.head_sha, patch):
                passed += 1
    return (passed / total if total else 0.0, total)
