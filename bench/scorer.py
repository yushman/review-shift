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

import math
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bench.case import Case
from bench.verdict import Finding, VerdictIndex

__all__ = [
    "CLAIM_INTERVAL_WIDTH", "TOLERANCES", "CaseRunResult", "Finding", "Metric",
    "PairedComparison", "applicability", "detected", "git_apply_check", "is_localized",
    "localization", "paired_comparison", "precision", "recall", "sign_test",
    "undetected_everywhere", "wilson", "yield_per_case",
]

PatchCheck = Callable[[Path, str, Path], bool]

# Both tolerances are always computed and reported, never one chosen and buried.
TOLERANCES = (0, 10)

# design.md D2: adjacent depths differ by roughly 17 points on this corpus (low 33%, medium
# 50%, high 67%). An interval wider than 30 points cannot separate a depth from its neighbour,
# which is the only comparison these rates are used for -- so a wider interval supports no
# claim, regardless of how many cases produced it. One named constant, in one place, so a
# later reader can see the reasoning and argue with it rather than a threshold buried in
# report formatting.
CLAIM_INTERVAL_WIDTH = 0.30

# 95% two-sided Wilson z-score, stated once and carried in every Metric that has an interval.
_Z95 = 1.96


def wilson(k: int, n: int, z: float = _Z95) -> tuple[float, float]:
    """Wilson score interval for `k` successes out of `n` trials -- closed-form, stays inside
    `[0, 1]`, and keeps a non-zero margin at the extremes (design.md D1).

    The normal (Wald) interval -- p-hat +/- z*sqrt(p-hat(1-p-hat)/n) -- is the obvious choice
    and is wrong here: at p-hat = 0 or 1 its margin is exactly zero, which reports a rate of
    0/6 as `0% [0% .. 0%]`, a false claim of certainty exactly where the evidence is thinnest,
    and at these single-digit denominators it can also produce bounds outside `[0, 1]`. Wilson
    inverts the normal approximation to the *score* test instead of the estimate, which keeps
    both properties. `wilson(0, 6)` and `wilson(6, 6)` are pinned in tests: both must have
    non-zero width and stay inside `[0, 1]`, which is exactly what a zero-margin Wald interval
    would fail.
    """
    if n == 0:
        raise ValueError("wilson: n must be > 0 -- callers must guard the uncomputed case")
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


def sign_test(wins: int, losses: int) -> float | None:
    """Exact one-sided sign test over discordant pairs, in the direction observed -- the sum of
    binomial coefficients for outcomes at least as extreme as the smaller side, rather than a
    chi-squared approximation that is not valid at these counts (design.md D3).

    Returns `None` when there are no discordant pairs: a p-value computed over an empty set
    would not be a measurement, it would be a made-up number that happens to print.
    """
    m = wins + losses
    if m == 0:
        return None
    s = min(wins, losses)
    tail: int = sum(math.comb(m, i) for i in range(s + 1))
    total: int = 2**m  # int ** non-negative int is always int; typeshed just can't see that
    return tail / total


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

    `k` is the raw numerator (hits, true defects, passed patches) that produced `value`, kept
    alongside it rather than reconstructed from `value * n` at render time. `lower`/`upper`/
    `confidence` are the Wilson interval and its confidence level; they are `None` both when
    the metric is uncomputed and for `yield`, which is a mean of counts, not a proportion, and
    deliberately never receives one (design.md D5) -- a metric with no interval is not a
    metric whose interval was merely omitted.
    """

    value: float | None
    n: int
    adjudicated: int
    outstanding: int
    k: int | None = None
    lower: float | None = None
    upper: float | None = None
    confidence: float | None = None

    @property
    def computed(self) -> bool:
        return self.value is not None

    @property
    def has_interval(self) -> bool:
        return self.lower is not None and self.upper is not None


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
    if n == 0:
        return Metric(value=None, n=0, adjudicated=0, outstanding=outstanding_cases)
    hits = sum(1 for s in decided if s)
    lower, upper = wilson(hits, n)
    return Metric(
        value=hits / n, n=n, adjudicated=n, outstanding=outstanding_cases,
        k=hits, lower=lower, upper=upper, confidence=0.95,
    )


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
    if n == 0:
        return Metric(value=None, n=0, adjudicated=adjudicated, outstanding=outstanding)
    lower, upper = wilson(hits, n)
    return Metric(
        value=hits / n, n=n, adjudicated=adjudicated, outstanding=outstanding,
        k=hits, lower=lower, upper=upper, confidence=0.95,
    )


def yield_per_case(
    results: list[CaseRunResult], verdicts: VerdictIndex, *, depth: str | None = None
) -> Metric:
    """Findings adjudicated as true defects per case (design.md D5). The headline: it has no
    denominator problem, and it is the number that credits a run for the true defects the
    corpus never labelled.

    `value` is a count per case rather than a rate, so it is not a percentage -- and it is a
    mean of small, heterogeneous counts rather than a proportion, so it deliberately carries no
    Wilson interval (design.md D5). `k`, the raw true-defect count, is reported alongside `n`
    so the report can show numerator and denominator explicitly rather than only their ratio.
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
        value=true_defects / n, n=n, adjudicated=adjudicated, outstanding=outstanding,
        k=true_defects,
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
    if adjudicated == 0:
        return Metric(value=None, n=0, adjudicated=0, outstanding=outstanding)
    lower, upper = wilson(true_defects, adjudicated)
    return Metric(
        value=true_defects / adjudicated, n=adjudicated, adjudicated=adjudicated,
        outstanding=outstanding, k=true_defects, lower=lower, upper=upper, confidence=0.95,
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
) -> Metric:
    """The fraction of generated `.patch` files that pass `git apply --check` on the first try
    -- product-analysis.md §5 metric 1, which is stated per patch file. Unchanged by the
    detection/localization split: it needs no verdict, because whether a patch applies is not
    a matter of judgement -- so `adjudicated` is simply every patch checked and `outstanding`
    is always zero, there being no judgement left to record.

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
    if total == 0:
        return Metric(value=None, n=0, adjudicated=0, outstanding=0)
    lower, upper = wilson(passed, total)
    return Metric(
        value=passed / total, n=total, adjudicated=total, outstanding=0,
        k=passed, lower=lower, upper=upper, confidence=0.95,
    )


@dataclass(frozen=True)
class PairedComparison:
    """A sign test over discordant pairs between two depths, restricted to cases where both
    depths ran and both are adjudicated (design.md D3). Ties carry no directional information
    and are excluded from the test -- that exclusion is what makes the test efficient at this
    corpus size: five discordant pairs in one direction already reaches p < 0.05.
    """

    depth_a: str
    depth_b: str
    wins_a: int
    wins_b: int
    ties: int
    n: int
    p_value: float | None
    direction: str | None


def paired_comparison(
    results: list[CaseRunResult], verdicts: VerdictIndex, depth_a: str, depth_b: str,
) -> PairedComparison:
    """Wins, losses, ties and the sign-test p-value between `depth_a` and `depth_b`, over cases
    where both ran (`status == "ok"`) and both are adjudicated. A case with an unresolved
    verdict at either depth cannot be scored as a win, a loss or a tie, so it is dropped from
    `n` rather than guessed at.
    """
    by_a = {r.case.id: r for r in _ok_results(results, depth_a)}
    by_b = {r.case.id: r for r in _ok_results(results, depth_b)}
    wins_a = wins_b = ties = 0
    for case_id in sorted(set(by_a) & set(by_b)):
        ra, rb = by_a[case_id], by_b[case_id]
        state_a = detected(ra.findings or [], verdicts, ra.case)
        state_b = detected(rb.findings or [], verdicts, rb.case)
        if state_a is None or state_b is None:
            continue
        if state_a and not state_b:
            wins_a += 1
        elif state_b and not state_a:
            wins_b += 1
        else:
            ties += 1
    p_value = sign_test(wins_a, wins_b)
    direction = None
    if wins_a != wins_b:
        direction = depth_a if wins_a > wins_b else depth_b
    return PairedComparison(
        depth_a=depth_a, depth_b=depth_b, wins_a=wins_a, wins_b=wins_b, ties=ties,
        n=wins_a + wins_b + ties, p_value=p_value, direction=direction,
    )


def undetected_everywhere(results: list[CaseRunResult], verdicts: VerdictIndex) -> int:
    """Cases adjudicated at every depth they ran and detected at none of them (design.md D4).

    Such a case can never contribute a discordant pair to any paired comparison -- both sides
    agree it was missed -- so it lowers the effective denominator of every comparison while
    still counting toward the case total, and it caps recall regardless of what the tool does.
    A case with any depth still unadjudicated is excluded rather than assumed undetected.
    """
    by_case: dict[str, list[CaseRunResult]] = {}
    for r in _ok_results(results, None):
        by_case.setdefault(r.case.id, []).append(r)
    count = 0
    for case_results in by_case.values():
        states = [detected(r.findings or [], verdicts, r.case) for r in case_results]
        if not states or any(s is None for s in states):
            continue
        if all(s is False for s in states):
            count += 1
    return count
