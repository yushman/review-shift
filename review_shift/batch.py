"""Batch orchestration: the lock, auth preflight, idempotency cache, per-branch review loop
with budget/timeout enforcement, and the `index.json`/`latest`/batch-summary writes —
`batch-execution` and `budget-and-resilience`. Per design.md's decision, a single `--branch`
run is a batch of one: cli.py always calls `run_batch`, never a per-branch path directly.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from review_shift import config as config_module
from review_shift import gitutil, index_store, lock, patch, redact, report, review
from review_shift.exitcodes import (
    EXIT_AUTH_OR_QUOTA,
    EXIT_FINDINGS,
    EXIT_INTERNAL_ERROR,
    EXIT_LOCK_HELD,
    EXIT_OK,
)

SEVERITIES = ["critical", "high", "medium", "low", "info"]

# "ok"/"cache_hit" are the two outcomes the batch exit-code rule and `latest` treat as
# successful; the rest are all failures for that purpose (batch-execution spec "Batch exit
# codes"). "budget_exhausted" is deliberately in neither set: ADR-014 treats it as a normal
# skip, not a failure, so a batch that is entirely budget-skipped is not "all branches failed".
SUCCESS_STATUSES = {"ok", "cache_hit"}
FAILED_STATUSES = {"error", "timeout", "refused", "invalid"}


def _sanitize(branch: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", branch)


def _make_run_id(branch: str) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{ts}-{_sanitize(branch)}"


def _relative_or_absolute(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


@dataclass
class BranchOutcome:
    branch: str
    status: str  # ok | cache_hit | error | timeout | refused | invalid | budget_exhausted
    run_id: str | None = None
    run_dir: str | None = None
    exit_code: int = EXIT_OK
    cost_usd: float = 0.0
    findings_by_severity: dict[str, int] = field(
        default_factory=lambda: {sev: 0 for sev in SEVERITIES}
    )
    reason: str | None = None
    index_entry: dict[str, Any] | None = None


def _write_error_run(
    run_dir: Path,
    run_id: str,
    branch: str,
    base: str,
    depth: str,
    head_sha: str,
    base_sha: str,
    merge_base_sha: str,
    started_at: datetime,
    *,
    error_type: str,
    message: str,
    attempts: int = 1,
) -> None:
    run_meta = {
        "schema_version": 1,
        "run_id": run_id,
        "branch": branch,
        "base": base,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "merge_base_sha": merge_base_sha,
        "depth": depth,
        "started_at": started_at.isoformat(),
        "attempts": attempts,
        "error": {"type": error_type, "attempts": attempts, "last_error": message},
        "exit_code": EXIT_INTERNAL_ERROR,
    }
    (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2))


def _review_branch(
    *,
    repo_root: Path,
    branch: str,
    base: str,
    depth: str,
    model: str,
    out_dir: Path,
    exclude_paths: list[str],
    skipped_discovery: list[dict[str, Any]],
    config_hash: str,
    budget_override: float | None,
    soft_timeout_minutes: float | None,
    hard_timeout_minutes: float | None,
    auto_fix_min_severity: str,
) -> BranchOutcome:
    """One branch's full diff -> redact -> review -> patch -> report pipeline (walking-skeleton,
    secret-redaction), extended with the idempotency-key fields and timeout/budget wiring this
    change adds. One branch failing must not raise past this function — the batch loop isolates
    it (batch-execution spec "One branch failing does not stop the batch")."""
    started_at = datetime.now(UTC)
    run_id = _make_run_id(branch)
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "patches").mkdir(exist_ok=True)

    try:
        base_sha = gitutil.rev_parse(repo_root, base)
        head_sha = gitutil.rev_parse(repo_root, branch)
        merge_base_sha = gitutil.merge_base(repo_root, base, branch)
        diff_text = gitutil.merge_base_diff(repo_root, base, branch)
        repo_files = gitutil.ls_tree_files(repo_root, head_sha)
    except gitutil.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return BranchOutcome(branch=branch, status="error", run_id=run_id, run_dir=str(run_dir),
                              exit_code=EXIT_INTERNAL_ERROR, reason=str(exc))

    # diff -> redact -> review (system-analysis.md §4): the model never sees an unmasked
    # diff, per ADR-008.
    redaction = redact.redact_diff(diff_text, exclude_paths)
    diff_text = redaction.diff_text

    try:
        result = review.run_review(
            branch=branch,
            base=base,
            depth=depth,
            repo_root=repo_root,
            diff_text=diff_text,
            head_sha=head_sha,
            repo_files=repo_files,
            model=model,
            budget_override=budget_override,
            soft_timeout_minutes=soft_timeout_minutes,
            hard_timeout_minutes=hard_timeout_minutes,
        )
    except review.ReviewTimeout as exc:
        _write_error_run(run_dir, run_id, branch, base, depth, head_sha, base_sha, merge_base_sha,
                          started_at, error_type="timeout", message=str(exc),
                          attempts=exc.attempts)
        print(f"error: {exc}", file=sys.stderr)
        return BranchOutcome(branch=branch, status="timeout", run_id=run_id, run_dir=str(run_dir),
                              exit_code=EXIT_INTERNAL_ERROR, reason=str(exc))
    except review.ReviewRefused as exc:
        _write_error_run(run_dir, run_id, branch, base, depth, head_sha, base_sha, merge_base_sha,
                          started_at, error_type="refused", message=str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return BranchOutcome(branch=branch, status="refused", run_id=run_id, run_dir=str(run_dir),
                              exit_code=EXIT_INTERNAL_ERROR, reason=str(exc))
    except review.ReviewInvalid as exc:
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(exist_ok=True)
        for i, raw in enumerate(exc.raw_responses, start=1):
            (raw_dir / f"attempt-{i}.txt").write_text(raw)
        _write_error_run(run_dir, run_id, branch, base, depth, head_sha, base_sha, merge_base_sha,
                          started_at, error_type="invalid_model_output",
                          message=exc.last_error, attempts=exc.attempts)
        print(f"error: {exc}", file=sys.stderr)
        return BranchOutcome(branch=branch, status="invalid", run_id=run_id, run_dir=str(run_dir),
                              exit_code=EXIT_INTERNAL_ERROR, reason=exc.last_error)

    threshold_rank = patch.SEVERITY_RANK[auto_fix_min_severity]

    try:
        localized = patch.resolve(result.findings, repo_root, head_sha)

        all_applicable = [lf for lf in localized if lf.status == "applicable"]
        auto_fix_applicable = [
            lf for lf in all_applicable
            if patch.SEVERITY_RANK[lf.finding["severity"]] >= threshold_rank
        ]

        all_diff, all_err = patch.generate_and_verify(
            all_applicable, repo_root, head_sha, f"{run_id}-all"
        )
        auto_fix_diff, auto_fix_err = patch.generate_and_verify(
            auto_fix_applicable, repo_root, head_sha, f"{run_id}-auto-fix"
        )
    except (gitutil.GitError, patch.PatchError) as exc:
        _write_error_run(run_dir, run_id, branch, base, depth, head_sha, base_sha, merge_base_sha,
                          started_at, error_type="patch_build_failed", message=str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return BranchOutcome(branch=branch, status="error", run_id=run_id, run_dir=str(run_dir),
                              exit_code=EXIT_INTERNAL_ERROR, reason=str(exc))

    patch_error = all_err or auto_fix_err
    auto_fix_patch_path = None
    if auto_fix_diff:
        auto_fix_patch_path = run_dir / "patches" / "auto_fixed.patch"
        auto_fix_patch_path.write_text(auto_fix_diff)
    if all_diff:
        (run_dir / "patches" / "all.patch").write_text(all_diff)

    findings_by_severity = {sev: 0 for sev in SEVERITIES}
    for lf in localized:
        findings_by_severity[lf.finding["severity"]] += 1
    findings_without_patch = sum(1 for lf in localized if lf.status != "applicable")

    duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)

    prompt_hash = review.prompt_template_hash(depth)
    idempotency_key = index_store.compute_idempotency_key(
        head_sha=head_sha, base_sha=base_sha, depth=depth,
        config_hash=config_hash, prompt_hash=prompt_hash,
    )

    run_meta = {
        "schema_version": 1,
        "run_id": run_id,
        "branch": branch,
        "base": base,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "merge_base_sha": merge_base_sha,
        "depth": depth,
        "model_resolved": result.model_resolved,
        "claude_code_version": result.claude_code_version,
        "prompt_hash": prompt_hash,
        "config_hash": config_hash,
        "idempotency_key": idempotency_key,
        "started_at": started_at.isoformat(),
        "duration_ms": duration_ms,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_usd": result.cost_usd,
        "findings_count": len(localized),
        "findings_by_severity": findings_by_severity,
        "findings_without_patch": findings_without_patch,
        "secrets_redacted": redaction.secrets_redacted,
        "secrets_redacted_files": redaction.secrets_redacted_files,
        "attempts": result.attempts,
        "partial": result.partial,
        "skipped": skipped_discovery,
        "exit_code": EXIT_OK,
        "patch_error": patch_error,
        "auto_fix_min_severity": auto_fix_min_severity,
        "auto_fix_patch_path": (
            _relative_or_absolute(auto_fix_patch_path, repo_root) if auto_fix_patch_path else None
        ),
        "cache_hit": False,
    }

    findings_out = []
    for lf in localized:
        entry = dict(lf.finding)
        entry["status"] = lf.status
        findings_out.append(entry)
    (run_dir / "findings.json").write_text(
        json.dumps({"schema_version": 1, "findings": findings_out}, indent=2)
    )

    has_auto_fix_worthy = any(
        patch.SEVERITY_RANK[lf.finding["severity"]] >= threshold_rank for lf in localized
    )
    exit_code = EXIT_FINDINGS if has_auto_fix_worthy else EXIT_OK
    run_meta["exit_code"] = exit_code
    (run_dir / "run.json").write_text(json.dumps(run_meta, indent=2))

    report_text = report.render(run_meta, localized)
    (run_dir / "report.md").write_text(report_text)

    print(str(run_dir))

    index_entry = {
        "run_id": run_id,
        "branch": branch,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "depth": depth,
        "prompt_hash": prompt_hash,
        "config_hash": config_hash,
        "idempotency_key": idempotency_key,
        "findings_by_severity": findings_by_severity,
        "exit_code": exit_code,
        "status": "ok",
    }

    return BranchOutcome(
        branch=branch, status="ok", run_id=run_id, run_dir=str(run_dir), exit_code=exit_code,
        cost_usd=result.cost_usd, findings_by_severity=findings_by_severity,
        index_entry=index_entry,
    )


def _aggregate_exit_code(outcomes: list[BranchOutcome], exit_zero_on_findings: bool) -> int:
    """batch-execution spec "Batch exit codes": 2 if every attempted branch failed, 1 if any
    successful branch cleared `patch.auto_fix_min_severity` (collapsible via
    --exit-zero-on-findings), 0 otherwise. A batch that is entirely cache hits or entirely
    budget-skipped is not "all failed" — only branches that were actually attempted and errored
    count. Reuses each branch's own `exit_code` (already computed against the threshold in
    `_review_branch`, or carried over from the cached run) rather than re-deriving the decision
    from severity counts here, so the two cannot diverge (design.md D2)."""
    attempted = [o for o in outcomes if o.status in FAILED_STATUSES or o.status in SUCCESS_STATUSES]
    if attempted and all(o.status in FAILED_STATUSES for o in attempted):
        return EXIT_INTERNAL_ERROR

    has_auto_fix_worthy = any(
        o.exit_code == EXIT_FINDINGS for o in outcomes if o.status in SUCCESS_STATUSES
    )
    if has_auto_fix_worthy:
        return EXIT_OK if exit_zero_on_findings else EXIT_FINDINGS
    return EXIT_OK


def _write_batch_summary(
    out_dir: Path,
    base: str,
    *,
    batch_id: str,
    started_at: datetime,
    outcomes: list[BranchOutcome],
    discovery_skipped: list[dict[str, Any]],
    auth_status: str,
    exit_code: int,
    total_cost_usd: float = 0.0,
) -> None:
    summary = {
        "schema_version": 1,
        "batch_id": batch_id,
        "base": base,
        "started_at": started_at.isoformat(),
        "auth_status": auth_status,
        "branches": [
            {
                "branch": o.branch,
                "status": o.status,
                "run_id": o.run_id,
                "run_dir": o.run_dir,
                "exit_code": o.exit_code,
                "cost_usd": o.cost_usd,
                "findings_by_severity": o.findings_by_severity,
                "reason": o.reason,
            }
            for o in outcomes
        ],
        "discovery_skipped": discovery_skipped,
        "total_cost_usd": total_cost_usd,
        "exit_code": exit_code,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{batch_id}.json").write_text(json.dumps(summary, indent=2))


def run_batch(
    *,
    repo_root: Path,
    out_dir: Path,
    branches: list[str],
    base: str,
    depth: str,
    model: str,
    loaded: config_module.LoadedConfig,
    exclude_paths: list[str],
    discovery_skipped: list[dict[str, Any]],
    force: bool,
    exit_zero_on_findings: bool,
) -> int:
    """The whole batch under one lock (ADR-007's "held for the entire batch, including
    index.json and latest writes" — not re-acquired per branch, per design.md's decision).
    A single `--branch` run is a batch of one."""
    runtime = loaded.data["runtime"]
    budget_usd = runtime["budget_usd"]
    total_budget_usd = runtime["total_budget_usd"]
    soft_timeout_minutes = runtime["soft_timeout_minutes"]
    hard_timeout_minutes = runtime["hard_timeout_minutes"]
    auto_fix_min_severity = loaded.data["patch"]["auto_fix_min_severity"]
    config_hash = loaded.config_hash

    batch_started_at = datetime.now(UTC)
    batch_id = f"{batch_started_at.strftime('%Y-%m-%dT%H-%M-%SZ')}-batch"

    try:
        with lock.acquire(repo_root):
            # Auth preflight before any branch starts (budget-and-resilience spec "Auth
            # preflight before the batch") — a broken/expired auth environment must be visible
            # immediately, not discovered mid-batch.
            try:
                review.check_auth(model=model)
            except review.QuotaError as exc:
                print(f"error: {exc}", file=sys.stderr)
                _write_batch_summary(
                    out_dir, base, batch_id=batch_id, started_at=batch_started_at, outcomes=[],
                    discovery_skipped=discovery_skipped, auth_status="quota_exhausted",
                    exit_code=EXIT_AUTH_OR_QUOTA,
                )
                return EXIT_AUTH_OR_QUOTA
            except review.AuthError as exc:
                print(f"error: {exc}", file=sys.stderr)
                _write_batch_summary(
                    out_dir, base, batch_id=batch_id, started_at=batch_started_at, outcomes=[],
                    discovery_skipped=discovery_skipped, auth_status="auth_failed",
                    exit_code=EXIT_AUTH_OR_QUOTA,
                )
                return EXIT_AUTH_OR_QUOTA

            index = index_store.load_index(out_dir)
            prompt_hash = review.prompt_template_hash(depth)
            outcomes: list[BranchOutcome] = []
            total_cost = 0.0

            for branch in branches:
                if total_cost >= total_budget_usd:
                    outcomes.append(BranchOutcome(
                        branch=branch, status="budget_exhausted",
                        reason="total_budget_usd exhausted",
                    ))
                    continue

                if not force:
                    try:
                        head_sha = gitutil.rev_parse(repo_root, branch)
                        base_sha = gitutil.rev_parse(repo_root, base)
                    except gitutil.GitError as exc:
                        outcomes.append(BranchOutcome(
                            branch=branch, status="error", exit_code=EXIT_INTERNAL_ERROR,
                            reason=str(exc),
                        ))
                        continue
                    key = index_store.compute_idempotency_key(
                        head_sha=head_sha, base_sha=base_sha, depth=depth,
                        config_hash=config_hash, prompt_hash=prompt_hash,
                    )
                    hit = index_store.find_cache_hit(index, key)
                    if hit is not None:
                        print(f"{branch}: cache_hit -> {hit['run_id']}")
                        outcomes.append(BranchOutcome(
                            branch=branch, status="cache_hit", run_id=hit["run_id"],
                            run_dir=str(out_dir / hit["run_id"]),
                            exit_code=hit.get("exit_code", EXIT_OK),
                            findings_by_severity=hit.get(
                                "findings_by_severity", {sev: 0 for sev in SEVERITIES}
                            ),
                        ))
                        continue

                outcome = _review_branch(
                    repo_root=repo_root, branch=branch, base=base, depth=depth, model=model,
                    out_dir=out_dir, exclude_paths=exclude_paths,
                    skipped_discovery=discovery_skipped, config_hash=config_hash,
                    budget_override=budget_usd, soft_timeout_minutes=soft_timeout_minutes,
                    hard_timeout_minutes=hard_timeout_minutes,
                    auto_fix_min_severity=auto_fix_min_severity,
                )
                outcomes.append(outcome)
                total_cost += outcome.cost_usd
                if outcome.index_entry is not None:
                    index.setdefault("runs", []).append(outcome.index_entry)

            index_store.write_index_atomic(out_dir, index)

            successful_run_dirs = [
                o.run_dir for o in outcomes if o.status in SUCCESS_STATUSES and o.run_dir
            ]
            if successful_run_dirs:
                index_store.swap_latest(out_dir, Path(successful_run_dirs[-1]).name)

            exit_code = _aggregate_exit_code(outcomes, exit_zero_on_findings)
            _write_batch_summary(
                out_dir, base, batch_id=batch_id, started_at=batch_started_at, outcomes=outcomes,
                discovery_skipped=discovery_skipped, auth_status="ok", exit_code=exit_code,
                total_cost_usd=total_cost,
            )
            return exit_code
    except lock.LockHeld as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_LOCK_HELD
