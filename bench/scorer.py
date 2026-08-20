"""Scores findings against ground truth and stored verdicts (design.md D1, D4, D6).
Deterministic and free: every judgement it needs is already on disk, so it makes no model
call, which is why it runs in normal `pytest` (tasks.md 7.4) even though producing the
findings does not.

Detection -- did the review find *this* defect -- comes from the verdict a human recorded
against the finding's content. Localization -- how precisely did the finding point -- is the
line arithmetic that used to answer both questions, now applied only to findings already
adjudicated as the case's defect and read as fitness for patch generation, not as review
quality.
"""
from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bench.case import Case
from bench.verdict import Finding, VerdictIndex

__all__ = [
    "TOLERANCES", "CaseRunResult", "Finding", "Metric", "applicability", "detected",
    "git_apply_check", "is_localized", "localization", "precision", "recall",
    "yield_per_case",
]

PatchCheck = Callable[[Path, str, Path], bool]

# Both tolerances are always computed and reported, never one chosen and buried.
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


@dataclass(frozen=True)
class Metric:
    """A figure with its denominator and its adjudication coverage. `value is None` means
    uncomputed -- there was nothing to compute it over -- and is deliberately not `0.0`:
    a metric over an unjudged set that printed `0%` would read as a measured failure
    (design.md D4).
    """

    value: float | None
    n: int
    adjudicated: int
    outstanding: int

    @property
    def computed(self) -> bool:
        return self.value is not None


def _finding_span(finding: Finding) -> tuple[int, int]:
    line = int(finding["line"])
    end_line = int(finding.get("end_line", line))
    return line, end_line


def is_localized(
    finding: Finding, gt_file: str, gt_start: int, gt_end: int, tolerance: int
) -> bool:
    """Same file, and the finding's line span intersects the ground-truth range after both are
    expanded by `tolerance`. This was `is_hit` and decided detection; it now only decides how
    well an already-detected finding anchors.
    """
    if finding.get("file") != gt_file:
        return False
    f_start, f_end = _finding_span(finding)
    lo, hi = gt_start - tolerance, gt_end + tolerance
    return f_start <= hi and lo <= f_end


def _localizes_case(finding: Finding, case: Case, tolerance: int) -> bool:
    return any(
        is_localized(finding, gt.file, gt.start_line, gt.end_line, tolerance)
        for gt in case.ground_truth
    )


def detected(findings: list[Finding], verdicts: VerdictIndex, case: Case) -> bool | None:
    """Tri-state, and the third state is the point: `True` when some finding carries a verdict
    marking it as the case's defect, `False` when every finding is adjudicated and none is,
    and `None` when a finding is still unadjudicated and could yet be the one. `None` is
    excluded from both sides of recall rather than folded into a miss.

    A run with no findings at all is `False`, not `None`: there is nothing left to judge.
    """
    resolved = [verdicts.resolve(case.id, f) for f in findings]
    if any(v is not None and v.case_defect for v in resolved):
        return True
    if any(v is None for v in resolved):
        return None
    return False


def _ok_results(
    results: list[CaseRunResult], depth: str | None, *, diff_visible_only: bool = False
) -> list[CaseRunResult]:
    return [
        r for r in results
        if r.status == "ok"
        and (depth is None or r.depth == depth)
        and (not diff_visible_only or r.case.diff_visible)
    ]


def _coverage(results: list[CaseRunResult], verdicts: VerdictIndex) -> tuple[int, int]:
    """(adjudicated, outstanding) findings over `results`."""
    adjudicated = 0
    outstanding = 0
    for r in results:
        for f in r.findings or []:
            if verdicts.resolve(r.case.id, f) is None:
                outstanding += 1
            else:
                adjudicated += 1
    return adjudicated, outstanding


def recall(
    results: list[CaseRunResult], verdicts: VerdictIndex, depth: str, *,
    diff_visible_only: bool = False,
) -> Metric:
    """Of the defects the corpus labelled, how many the review found -- computed from
    detection verdicts, not line arithmetic (design.md D6).

    A case that never ran contributes to neither the numerator nor the denominator, so an
    exhausted budget shrinks `n` rather than silently counting as a miss. A case whose
    findings are not yet adjudicated is excluded the same way and reported as outstanding.
    """
    subset = _ok_results(results, depth, diff_visible_only=diff_visible_only)
    states = [detected(r.findings or [], verdicts, r.case) for r in subset]
    decided = [s for s in states if s is not None]
    outstanding_cases = len(states) - len(decided)
    n = len(decided)
    value = (sum(1 for s in decided if s) / n) if n else None
    return Metric(value=value, n=n, adjudicated=n, outstanding=outstanding_cases)


def localization(
    results: list[CaseRunResult], verdicts: VerdictIndex, tolerance: int, *,
    depth: str | None = None,
) -> Metric:
    """Among findings whose verdict marks them as the case's defect, the fraction whose line
    span intersects ground truth at `tolerance`. Measures patch anchoring, not whether the
    review works: a finding adjudicated as *not* the case's defect contributes nothing here
    regardless of where it points.
    """
    subset = _ok_results(results, depth)
    adjudicated, outstanding = _coverage(subset, verdicts)
    n = 0
    hits = 0
    for r in subset:
        for f in r.findings or []:
            verdict = verdicts.resolve(r.case.id, f)
            if verdict is None or not verdict.case_defect:
                continue
            n += 1
            if _localizes_case(f, r.case, tolerance):
                hits += 1
    value = (hits / n) if n else None
    return Metric(value=value, n=n, adjudicated=adjudicated, outstanding=outstanding)


def yield_per_case(
    results: list[CaseRunResult], verdicts: VerdictIndex, *, depth: str | None = None
) -> Metric:
    """Findings adjudicated as true defects per case (design.md D5). The headline: it has no
    denominator problem, and it is the number that credits a run for the true defects the
    corpus never labelled.

    `value` is a count per case rather than a rate, so it is not a percentage.
    """
    subset = _ok_results(results, depth)
    adjudicated, outstanding = _coverage(subset, verdicts)
    n = len(subset)
    true_defects = sum(
        1
        for r in subset
        for f in (r.findings or [])
        if (v := verdicts.resolve(r.case.id, f)) is not None and v.true_defect
    )
    if n == 0 or (adjudicated == 0 and outstanding > 0):
        return Metric(value=None, n=n, adjudicated=adjudicated, outstanding=outstanding)
    return Metric(
        value=true_defects / n, n=n, adjudicated=adjudicated, outstanding=outstanding
    )


def precision(
    results: list[CaseRunResult], verdicts: VerdictIndex, *, depth: str | None = None
) -> Metric:
    """The fraction of adjudicated findings judged true defects. Reported beside yield because
    a depth that finds more by reporting everything is a real failure mode yield cannot see
    (design.md D5). Unadjudicated findings are in neither the numerator nor the denominator.
    """
    subset = _ok_results(results, depth)
    adjudicated, outstanding = _coverage(subset, verdicts)
    true_defects = sum(
        1
        for r in subset
        for f in (r.findings or [])
        if (v := verdicts.resolve(r.case.id, f)) is not None and v.true_defect
    )
    value = (true_defects / adjudicated) if adjudicated else None
    return Metric(
        value=value, n=adjudicated, adjudicated=adjudicated, outstanding=outstanding
    )


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
    -- product-analysis.md §5 metric 1, which is stated per patch file. Unchanged by the
    detection/localization split: it needs no verdict, because whether a patch applies is not
    a matter of judgement.

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
