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
from bench.materialize import WORK_DIR, MaterializeError, ensure_sha, materialize_repo
from bench.scorer import CaseRunResult

__all__ = ["DEPTHS", "RUNS_DIR", "run_case", "run_all"]

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
