"""Runs each case at each depth through the shipped `review-shift` CLI -- the harness is a
consumer of the existing CLI and never reimplements review logic (proposal.md). Materializes
the case's repository, then invokes `review-shift run --branch <introducing_sha> --base
<introducing_sha>^ --force` -- the same shape as the manual depth comparisons of 2026-08-18
(design.md D3), with `--force` so a case is genuinely re-reviewed rather than served from
`index.json` on an unchanged idempotency key (design.md D7, spec "Re-running an unchanged
case").
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bench.case import Case
from bench.corpus import CorpusRepo
from bench.materialize import WORK_DIR, MaterializeError, clone_dir, ensure_sha, materialize_repo
from bench.scorer import CaseRunResult

__all__ = ["DEPTHS", "RUNS_DIR", "load_stored_results", "run_case", "run_all"]

DEPTHS = ("smoke", "low", "medium")
RUNS_DIR = WORK_DIR / "runs"


def run_case(
    case: Case, repo: CorpusRepo, depth: str, *,
    work_dir: Path = WORK_DIR, runs_dir: Path = RUNS_DIR, review_shift_bin: str = "review-shift",
) -> CaseRunResult:
    try:
        repo_dir = materialize_repo(repo.id, repo.url, work_dir)
        ensure_sha(repo_dir, case.introducing_sha)
    except MaterializeError as exc:
        return CaseRunResult(
            case=case, depth=depth, findings=None, cost_usd=0.0,
            status="materialize_failed", reason=str(exc),
        )

    out_dir = runs_dir / repo.id
    proc = subprocess.run(
        [
            review_shift_bin, "run",
            "--repo", str(repo_dir),
            "--branch", case.introducing_sha,
            "--base", f"{case.introducing_sha}^",
            "--depth", depth,
            "--out-dir", str(out_dir),
            "--force",
            "--exit-zero-on-findings",
        ],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return CaseRunResult(
            case=case, depth=depth, findings=None, cost_usd=0.0, status="review_failed",
            reason=(proc.stderr.strip() or f"exit {proc.returncode}"),
        )

    stdout_lines = [line for line in proc.stdout.strip().splitlines() if line]
    run_dir = Path(stdout_lines[-1]) if stdout_lines else None
    if run_dir is None or not (run_dir / "run.json").exists():
        return CaseRunResult(
            case=case, depth=depth, findings=None, cost_usd=0.0, status="review_failed",
            reason="review-shift did not print a run directory with run.json",
        )

    run_meta = json.loads((run_dir / "run.json").read_text())
    findings = json.loads((run_dir / "findings.json").read_text())["findings"]
    return CaseRunResult(
        case=case, depth=depth, findings=findings,
        cost_usd=run_meta.get("cost_usd", 0.0) or 0.0, status="ok", reason=None,
        run_dir=run_dir, repo_dir=repo_dir, head_sha=run_meta.get("head_sha"),
    )


def _matches(case: Case, branch: str) -> bool:
    """A run records the branch it reviewed, which the harness always sets to the case's
    introducing sha. Either side may be abbreviated, so compare by prefix in both directions.
    """
    if not branch:
        return False
    return case.introducing_sha.startswith(branch) or branch.startswith(case.introducing_sha)


def load_stored_results(
    cases: list[Case], *, runs_dir: Path = RUNS_DIR, work_dir: Path = WORK_DIR,
) -> list[CaseRunResult]:
    """Reconstructs `CaseRunResult`s from runs already on disk, so stored findings can be
    adjudicated and re-scored without paying for a review again.

    When a case/depth pair was run more than once, the newest run wins -- run ids are
    timestamp-prefixed, so directory order is chronological. Older runs are dropped rather
    than double-counted, which would inflate yield with the same finding twice. A run
    directory missing `findings.json` is reported as incomplete rather than skipped silently.
    """
    latest: dict[tuple[str, str], tuple[str, CaseRunResult]] = {}
    for run_json in sorted(runs_dir.glob("*/*/run.json")):
        run_dir = run_json.parent
        try:
            meta = json.loads(run_json.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        case = next((c for c in cases if _matches(c, meta.get("branch", ""))), None)
        if case is None:
            continue
        depth = meta.get("depth", "")
        run_id = meta.get("run_id", run_dir.name)
        findings_path = run_dir / "findings.json"
        if not findings_path.exists():
            result = CaseRunResult(
                case=case, depth=depth, findings=None, cost_usd=0.0, status="incomplete_run",
                reason=f"{run_dir} has run.json but no findings.json",
            )
        else:
            result = CaseRunResult(
                case=case, depth=depth,
                findings=json.loads(findings_path.read_text())["findings"],
                cost_usd=meta.get("cost_usd", 0.0) or 0.0, status="ok", reason=None,
                run_dir=run_dir, repo_dir=clone_dir(case.repo, work_dir),
                head_sha=meta.get("head_sha"),
            )
        key = (case.id, depth)
        previous = latest.get(key)
        if previous is None or previous[0] <= run_id:
            latest[key] = (run_id, result)
    return [result for _, (_, result) in sorted(latest.items())]


def run_all(
    cases: list[Case], repos: dict[str, CorpusRepo], depths: tuple[str, ...] = DEPTHS,
    *, budget_usd: float, work_dir: Path = WORK_DIR, runs_dir: Path = RUNS_DIR,
    review_shift_bin: str = "review-shift",
) -> list[CaseRunResult]:
    """Iterates cases x depths, accumulating spend from each run's `cost_usd`, and stops
    attempting new runs once `budget_usd` is reached -- cases and depths not attempted are
    recorded as `budget_exhausted` rather than omitted (spec "Budget exhausted mid-run").
    """
    results: list[CaseRunResult] = []
    spent = 0.0
    for case in cases:
        repo = repos.get(case.repo)
        for depth in depths:
            if repo is None:
                results.append(CaseRunResult(
                    case=case, depth=depth, findings=None, cost_usd=0.0,
                    status="unknown_repo", reason=f"corpus has no repo {case.repo!r}",
                ))
                continue
            if spent >= budget_usd:
                results.append(CaseRunResult(
                    case=case, depth=depth, findings=None, cost_usd=0.0,
                    status="budget_exhausted", reason=None,
                ))
                continue
            result = run_case(
                case, repo, depth, work_dir=work_dir, runs_dir=runs_dir,
                review_shift_bin=review_shift_bin,
            )
            results.append(result)
            spent += result.cost_usd
    return results
