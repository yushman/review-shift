"""batch.py: lock + index + budget/timeout orchestration end to end through `cli.main` —
batch-execution and budget-and-resilience "done when" scenarios from tasks.md sections 2/3/4:
a middle branch failing doesn't stop the batch, exit-code aggregation, the idempotency cache
(hit/miss/--force) at the batch level, budget exhaustion as a normal skip, auth/quota preflight
failure before any branch runs, and a real-subprocess hard-timeout that still leaves the batch
able to finish other branches.
"""
from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from review_shift import cli


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _events(findings: list[dict], cost_usd: float = 0.05) -> list[dict]:
    structured_output = {"schema_version": 1, "findings": findings}
    return [
        {"type": "system", "subtype": "init", "model": "stub", "claude_code_version": "0"},
        {
            "type": "result", "stop_reason": "end_turn", "subtype": "success",
            "total_cost_usd": cost_usd, "usage": {"input_tokens": 10, "output_tokens": 5},
            "structured_output": structured_output, "result": json.dumps(structured_output),
        },
    ]


def _low_finding_events(cost_usd: float = 0.05) -> list[dict]:
    return _events(
        [{"file": "f.txt", "line": 1, "severity": "low", "category": "style",
          "rationale": "minor"}],
        cost_usd=cost_usd,
    )


def _critical_finding_events(cost_usd: float = 0.05) -> list[dict]:
    return _events(
        [{"file": "f.txt", "line": 1, "severity": "critical", "category": "security",
          "rationale": "bad"}],
        cost_usd=cost_usd,
    )


def _bad_json_events() -> list[dict]:
    return [
        {"type": "system", "subtype": "init"},
        {"type": "result", "stop_reason": "tool_use", "subtype": "success",
         "total_cost_usd": 0.0, "usage": {}, "result": "not json"},
    ]


@pytest.fixture
def three_branch_repo(tmp_path: Path) -> Path:
    """main + three branches (a, b, c), each with a distinct real commit against main, plus a
    `.review-shift/config.yml` that discovers all three."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "branch", "-m", "main")

    for name, content in (("a", "aaa\n"), ("b", "bbb\n"), ("c", "ccc\n")):
        _git(repo, "checkout", "-q", "-b", f"feature/{name}", "main")
        (repo / "f.txt").write_text(content)
        _git(repo, "commit", "-q", "-am", f"change {name}")
        _git(repo, "checkout", "-q", "main")

    config_dir = repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "version: 1\ndiscovery:\n  patterns: [\"feature/*\"]\n"
    )
    return repo


def _side_effect_fail_branch(fail_branch: str):
    def _fn(cmd, soft_timeout_s, hard_timeout_s):
        prompt = cmd[2]
        if f"branch: feature/{fail_branch}\n" in prompt:
            return _bad_json_events(), False
        return _low_finding_events(), False
    return _fn


def test_middle_branch_failing_does_not_stop_the_batch(three_branch_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir)]
    with mock_patch(
        "review_shift.review._invoke_with_timeout", side_effect=_side_effect_fail_branch("b")
    ):
        exit_code = cli.main(argv)

    # a and c succeeded (low-severity only) -> no critical among successes -> exit 0
    assert exit_code == 0

    batch_files = list(out_dir.glob("*-batch.json"))
    assert len(batch_files) == 1
    summary = json.loads(batch_files[0].read_text())
    statuses = {b["branch"]: b["status"] for b in summary["branches"]}
    assert statuses["feature/a"] == "ok"
    assert statuses["feature/b"] == "invalid"
    assert statuses["feature/c"] == "ok"

    run_dirs = [p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink()]
    # only a and c get a full run.json with findings; b's run dir still exists (raw responses)
    ok_run_dirs = [p for p in run_dirs if (p / "findings.json").exists()]
    assert len(ok_run_dirs) == 2


def test_middle_branch_patch_resolve_failure_does_not_stop_the_batch(
    three_branch_repo: Path, tmp_path: Path
):
    """`_review_branch`'s own docstring and the batch-execution spec both promise that one
    branch failing must not raise past `_review_branch` and stop the batch loop. The review
    step itself is already guarded (see test_middle_branch_failing_does_not_stop_the_batch);
    this covers the downstream localization step (`patch.resolve`, which can raise
    `gitutil.GitError` via `gitutil.show_file`) which was previously left unguarded."""
    from review_shift import gitutil
    from review_shift.patch import resolve as real_resolve

    fail_head = gitutil.rev_parse(three_branch_repo, "feature/b")

    def _resolve_side_effect(findings, repo_root, head_sha):
        if head_sha == fail_head:
            raise gitutil.GitError("simulated git failure during localization")
        return real_resolve(findings, repo_root, head_sha)

    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir)]
    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_low_finding_events(), False)
    ):
        with mock_patch("review_shift.batch.patch.resolve", side_effect=_resolve_side_effect):
            exit_code = cli.main(argv)

    # a and c succeeded (low-severity only) -> no critical among successes -> exit 0
    assert exit_code == 0

    batch_files = list(out_dir.glob("*-batch.json"))
    assert len(batch_files) == 1
    summary = json.loads(batch_files[0].read_text())
    statuses = {b["branch"]: b["status"] for b in summary["branches"]}
    assert statuses["feature/a"] == "ok"
    assert statuses["feature/b"] == "error"
    assert statuses["feature/c"] == "ok"


def test_all_branches_failing_exits_2(three_branch_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir)]
    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_bad_json_events(), False)
    ):
        exit_code = cli.main(argv)
    assert exit_code == 2


def test_one_critical_among_successes_exits_1_unless_exit_zero_flag(
    three_branch_repo: Path, tmp_path: Path
):
    def side_effect(cmd, soft_timeout_s, hard_timeout_s):
        prompt = cmd[2]
        if "branch: feature/a\n" in prompt:
            return _critical_finding_events(), False
        return _low_finding_events(), False

    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir)]
    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=side_effect):
        exit_code = cli.main(argv)
    assert exit_code == 1

    out_dir2 = tmp_path / "runs2"
    argv2 = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir2),
             "--exit-zero-on-findings"]
    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=side_effect):
        exit_code2 = cli.main(argv2)
    assert exit_code2 == 0


# --- add-autofix-severity-config: patch.auto_fix_min_severity gates both the auto-fix patch
# and exit code 1 from a single config value (design.md D2) ------------------------------


def _finding_events(severity: str, before: str, after: str, cost_usd: float = 0.05) -> list[dict]:
    return _events(
        [{"file": "f.txt", "line": 1, "severity": severity, "category": "bug",
          "rationale": "r", "before": before, "after": after}],
        cost_usd=cost_usd,
    )


def _write_config(repo: Path, extra: str = "") -> None:
    (repo / ".review-shift" / "config.yml").write_text(
        "version: 1\ndiscovery:\n  patterns: [\"feature/*\"]\n" + extra
    )


def test_default_threshold_reproduces_old_behavior(three_branch_repo: Path, tmp_path: Path):
    """Regression check for design.md D3: an unset `patch` config produces the same
    `auto_fixed.patch` composition (high-severity finding included) and exit code (1) as the
    hardcoded `critical`/`high` behavior before this change."""
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/a", "--base", "main", "--repo", str(three_branch_repo),
            "--out-dir", str(out_dir)]
    with mock_patch("review_shift.review._invoke_with_timeout",
                     return_value=(_finding_events("high", "aaa", "AAA"), False)):
        exit_code = cli.main(argv)
    assert exit_code == 1

    run_dir = next(p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink())
    auto_fixed = run_dir / "patches" / "auto_fixed.patch"
    assert auto_fixed.exists()
    assert "AAA" in auto_fixed.read_text()

    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["auto_fix_min_severity"] == "high"
    assert run_meta["auto_fix_patch_path"] == str(auto_fixed)


def test_lowered_threshold_includes_medium_finding_and_flips_exit_code(
    three_branch_repo: Path, tmp_path: Path
):
    _write_config(three_branch_repo, "patch:\n  auto_fix_min_severity: medium\n")
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/a", "--base", "main", "--repo", str(three_branch_repo),
            "--out-dir", str(out_dir)]
    with mock_patch("review_shift.review._invoke_with_timeout",
                     return_value=(_finding_events("medium", "aaa", "AAA"), False)):
        exit_code = cli.main(argv)
    assert exit_code == 1

    run_dir = next(p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink())
    auto_fixed = run_dir / "patches" / "auto_fixed.patch"
    assert auto_fixed.exists()
    assert "AAA" in auto_fixed.read_text()


def test_raised_threshold_excludes_high_finding_and_exit_stays_zero(
    three_branch_repo: Path, tmp_path: Path
):
    _write_config(three_branch_repo, "patch:\n  auto_fix_min_severity: critical\n")
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/a", "--base", "main", "--repo", str(three_branch_repo),
            "--out-dir", str(out_dir)]
    with mock_patch("review_shift.review._invoke_with_timeout",
                     return_value=(_finding_events("high", "aaa", "AAA"), False)):
        exit_code = cli.main(argv)
    assert exit_code == 0

    run_dir = next(p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink())
    assert not (run_dir / "patches" / "auto_fixed.patch").exists()


def test_redacted_finding_excluded_from_auto_fixed_patch_regardless_of_threshold(
    three_branch_repo: Path, tmp_path: Path
):
    _write_config(three_branch_repo, "patch:\n  auto_fix_min_severity: info\n")
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/a", "--base", "main", "--repo", str(three_branch_repo),
            "--out-dir", str(out_dir)]
    redacted_events = _finding_events("critical", 'X = "<<REDACTED:token>>"', 'X = "y"')
    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(redacted_events, False)
    ):
        cli.main(argv)

    run_dir = next(p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink())
    assert not (run_dir / "patches" / "auto_fixed.patch").exists()


def test_rerun_of_unchanged_branch_is_a_cache_hit(three_branch_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/a", "--base", "main", "--repo", str(three_branch_repo),
            "--out-dir", str(out_dir)]

    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_low_finding_events(), False)
    ):
        first_exit = cli.main(argv)
    assert first_exit == 0
    run_dirs_after_first = [p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink()]
    assert len(run_dirs_after_first) == 1

    # second invocation: nothing about the branch/base/config/prompt changed -> cache hit, no
    # new run directory, and the mocked review call must not even be invoked.
    with mock_patch("review_shift.review._invoke_with_timeout") as mock_invoke:
        second_exit = cli.main(argv)
    assert second_exit == 0
    mock_invoke.assert_not_called()
    run_dirs_after_second = [p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink()]
    assert len(run_dirs_after_second) == 1  # still just the one real run directory

    batch_files = sorted(out_dir.glob("*-batch.json"))
    second_summary = json.loads(batch_files[-1].read_text())
    assert second_summary["branches"][0]["status"] == "cache_hit"


def test_force_bypasses_the_cache(three_branch_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/a", "--base", "main", "--repo", str(three_branch_repo),
            "--out-dir", str(out_dir)]
    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_low_finding_events(), False)
    ):
        cli.main(argv)

    # run_id has second-granularity (ADR-007's fixed format); wait past the second boundary
    # so the forced re-run gets a distinct run directory instead of colliding with the first.
    time.sleep(1.1)

    argv_force = [*argv, "--force"]
    with mock_patch("review_shift.review._invoke_with_timeout",
                     return_value=(_low_finding_events(), False)) as mock_invoke:
        exit_code = cli.main(argv_force)
    assert exit_code == 0
    mock_invoke.assert_called()  # cache bypassed -> a real review call happened again
    run_dirs = [p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink()]
    assert len(run_dirs) == 2  # a brand-new run directory was created


def test_total_budget_exhausted_skips_remaining_branches(three_branch_repo: Path, tmp_path: Path):
    config_path = three_branch_repo / ".review-shift" / "config.yml"
    config_path.write_text(
        "version: 1\n"
        "discovery:\n  patterns: [\"feature/*\"]\n"
        "runtime:\n  total_budget_usd: 0.06\n"
    )
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir)]
    with mock_patch("review_shift.review._invoke_with_timeout",
                     return_value=(_low_finding_events(cost_usd=0.05), False)):
        exit_code = cli.main(argv)
    assert exit_code == 0

    batch_files = list(out_dir.glob("*-batch.json"))
    summary = json.loads(batch_files[0].read_text())
    statuses = [b["status"] for b in summary["branches"]]
    assert statuses.count("ok") == 2
    assert statuses.count("budget_exhausted") == 1
    assert summary["exit_code"] == 0  # a normal skip, not a failure


def test_auth_failure_exits_4_before_any_branch_runs(three_branch_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir)]
    failing = subprocess.CompletedProcess(
        args=["claude"], returncode=1, stdout="",
        stderr="Error: not logged in. Please run `claude login`.",
    )
    with mock_patch("review_shift.review._run_preflight", return_value=failing):
        with mock_patch("review_shift.review._invoke_with_timeout") as mock_invoke:
            exit_code = cli.main(argv)
    assert exit_code == 4
    mock_invoke.assert_not_called()

    batch_files = list(out_dir.glob("*-batch.json"))
    summary = json.loads(batch_files[0].read_text())
    assert summary["auth_status"] == "auth_failed"
    assert summary["branches"] == []


def test_quota_exhaustion_exits_4(three_branch_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(three_branch_repo), "--out-dir", str(out_dir)]
    failing = subprocess.CompletedProcess(args=["claude"], returncode=1, stdout="",
                                           stderr="Error: rate limit exceeded, quota exhausted")
    with mock_patch("review_shift.review._run_preflight", return_value=failing):
        exit_code = cli.main(argv)
    assert exit_code == 4
    batch_files = list(out_dir.glob("*-batch.json"))
    summary = json.loads(batch_files[0].read_text())
    assert summary["auth_status"] == "quota_exhausted"


# --- real-subprocess hard timeout, exercised through the full batch/CLI stack -------------

STUB_SOURCE = '''#!/usr/bin/env python3
import json, os, signal, sys, time

argv_text = " ".join(sys.argv)
IS_TIMEOUT_BRANCH = "branch: feature/hang\\n" in argv_text

EVENTS = [
    {"type": "system", "subtype": "init", "model": "stub", "claude_code_version": "0"},
    {
        "type": "result", "stop_reason": "end_turn", "subtype": "success",
        "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 1},
        "structured_output": {"schema_version": 1, "findings": [
            {"file": "f.txt", "line": 1, "severity": "low", "category": "style",
             "rationale": "stub finding"},
        ]},
        "result": "",
    },
]
EVENTS[-1]["result"] = json.dumps(EVENTS[-1]["structured_output"])


def _ignore(signum, frame):
    pass  # a hung claude process that doesn't honor SIGTERM -> must be SIGKILLed


if IS_TIMEOUT_BRANCH:
    signal.signal(signal.SIGTERM, _ignore)
    time.sleep(300)
else:
    time.sleep(0.05)
    sys.stdout.write(json.dumps(EVENTS))
    sys.stdout.flush()
'''


def test_hard_timeout_kills_one_branch_but_batch_still_finishes_the_other(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "branch", "-m", "main")
    for name, content in (("hang", "hang\n"), ("ok", "ok\n")):
        _git(repo, "checkout", "-q", "-b", f"feature/{name}", "main")
        (repo / "f.txt").write_text(content)
        _git(repo, "commit", "-q", "-am", f"change {name}")
        _git(repo, "checkout", "-q", "main")

    config_dir = repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "version: 1\n"
        "discovery:\n  patterns: [\"feature/*\"]\n"
        "runtime:\n  soft_timeout_minutes: 0.01\n  hard_timeout_minutes: 0.02\n"
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude"
    stub.write_text(STUB_SOURCE)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    import os
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    out_dir = tmp_path / "runs"
    proc = subprocess.run(
        [sys.executable, "-m", "review_shift.cli", "run", "--base", "main", "--repo", str(repo),
         "--out-dir", str(out_dir)],
        cwd=str(Path(__file__).resolve().parent.parent), env=env,
        capture_output=True, text=True, timeout=30,
    )

    batch_files = list(out_dir.glob("*-batch.json"))
    assert len(batch_files) == 1, proc.stderr
    summary = json.loads(batch_files[0].read_text())
    statuses = {b["branch"]: b["status"] for b in summary["branches"]}
    assert statuses["feature/hang"] == "timeout"
    assert statuses["feature/ok"] == "ok"

    # the timed-out branch's run directory still exists and is readable (F5's "still leaves a
    # readable run directory") even though the claude process was SIGKILLed mid-flight.
    hang_run_dir = next(p for p in out_dir.iterdir() if p.is_dir() and "feature-hang" in p.name)
    run_meta = json.loads((hang_run_dir / "run.json").read_text())
    assert run_meta["error"]["type"] == "timeout"
    assert run_meta["exit_code"] == 2

    ok_run_dir = next(p for p in out_dir.iterdir() if p.is_dir() and "feature-ok" in p.name)
    assert (ok_run_dir / "findings.json").exists()

    assert proc.returncode == 0  # one branch ok with no critical findings, one timed out
