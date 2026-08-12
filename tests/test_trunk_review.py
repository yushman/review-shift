"""End-to-end trunk-review tests through `cli.main --trunk`: bootstrap, per-commit review,
watermark advancement rules (design.md D4), localization against the base head (D7), and the
blame-based survival filter (D5) -- the scenarios that only show up once discovery, the
blame pass, the per-unit loop and patch localization are wired together, as opposed to the
unit-level tests in test_discover.py / test_blame.py / test_index_store.py.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from review_shift import cli


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _run_dirs(out_dir: Path) -> list[Path]:
    return sorted(p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink())


@pytest.fixture
def trunk_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")
    _git(repo, "branch", "-m", "main")

    config_dir = repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("version: 2\ntrunk:\n  enabled: true\n")
    return repo


def _direct_commit(repo: Path, content: str, message: str) -> str:
    """Overwrites the shared `f.txt` -- for tests that specifically want one commit's line to
    supersede another's."""
    (repo / "f.txt").write_text(content)
    _git(repo, "commit", "-q", "-am", message)
    return _rev(repo, "HEAD")


def _new_file_commit(repo: Path, filename: str, content: str, message: str) -> str:
    """Adds a brand-new file -- for tests that need several independent, non-superseding
    direct commits."""
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _rev(repo, "HEAD")


def _no_finding_events(cost_usd: float = 0.01) -> list[dict]:
    structured_output = {"schema_version": 1, "findings": []}
    return [
        {"type": "system", "subtype": "init", "model": "stub", "claude_code_version": "0"},
        {
            "type": "result", "stop_reason": "end_turn", "subtype": "success",
            "total_cost_usd": cost_usd, "usage": {"input_tokens": 5, "output_tokens": 5},
            "structured_output": structured_output, "result": json.dumps(structured_output),
        },
    ]


def _finding_events(
    file: str, before: str, after: str, severity: str = "high", cost_usd: float = 0.01
) -> list[dict]:
    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": file, "line": 1, "severity": severity, "category": "bug",
             "rationale": "r", "before": before, "after": after},
        ],
    }
    return [
        {"type": "system", "subtype": "init", "model": "stub", "claude_code_version": "0"},
        {
            "type": "result", "stop_reason": "end_turn", "subtype": "success",
            "total_cost_usd": cost_usd, "usage": {"input_tokens": 5, "output_tokens": 5},
            "structured_output": structured_output, "result": json.dumps(structured_output),
        },
    ]


def test_first_run_bootstraps_and_reviews_nothing(trunk_repo: Path, tmp_path: Path):
    _direct_commit(trunk_repo, "v1\n", "c1")  # landed before the tool ever ran
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    with mock_patch("review_shift.review._invoke_with_timeout") as mock_invoke:
        exit_code = cli.main(argv)
    assert exit_code == 0
    mock_invoke.assert_not_called()

    run_dirs = _run_dirs(out_dir)
    assert len(run_dirs) == 1
    run_meta = json.loads((run_dirs[0] / "run.json").read_text())
    assert run_meta["mode"] == "trunk"
    assert run_meta["trunk_outcome"] == "bootstrap"
    assert run_meta["findings_count"] == 0

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == _rev(trunk_repo, "main")


def test_second_run_reviews_commits_since_bootstrap(trunk_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap; watermark now at c0
    # run_id has second-granularity (ADR-007) -- avoid colliding with bootstrap's directory
    time.sleep(1.1)

    c1 = _new_file_commit(trunk_repo, "c1.txt", "v1\n", "c1")
    c2 = _new_file_commit(trunk_repo, "c2.txt", "v2\n", "c2")

    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_no_finding_events(), False)
    ) as mock_invoke:
        exit_code = cli.main(argv)
    assert exit_code == 0
    assert mock_invoke.call_count == 2  # both c1 and c2 reviewed, no cache hits

    run_dirs = _run_dirs(out_dir)
    assert len(run_dirs) == 2  # bootstrap run + this one
    run_dir = sorted(run_dirs)[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["trunk_outcome"] == "units"
    assert [u["sha"] for u in run_meta["units"]] == [c1, c2]
    assert all(u["status"] == "ok" for u in run_meta["units"])

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == c2


def test_patch_across_three_commits_applies_against_recorded_head(
    trunk_repo: Path, tmp_path: Path
):
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    # an `origin/main` that has not seen any of these commits yet -- the remedy for a finding
    # in one of them must read "still_local" (trunk-review report spec).
    origin_path = tmp_path / "origin.git"
    _git(trunk_repo, "clone", "-q", "--bare", str(trunk_repo), str(origin_path))
    _git(trunk_repo, "remote", "add", "origin", str(origin_path))
    _git(trunk_repo, "fetch", "-q", "origin")

    c1 = _new_file_commit(trunk_repo, "a.txt", "alpha\n", "add a.txt")
    _new_file_commit(trunk_repo, "b.txt", "bravo\n", "add b.txt")
    _new_file_commit(trunk_repo, "c.txt", "charlie\n", "add c.txt")

    def side_effect(cmd, soft_timeout_s, hard_timeout_s):
        prompt = cmd[2]
        if f"head_sha: {c1}\n" in prompt:
            return _finding_events("a.txt", "alpha", "ALPHA"), False
        return _no_finding_events(), False

    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=side_effect):
        exit_code = cli.main(argv)
    assert exit_code == 1  # a high-severity finding made it into the patch

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["head_sha"] == _rev(trunk_repo, "main")

    patch_text = (run_dir / "patches" / "auto_fixed.patch").read_text()
    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn"],
        cwd=trunk_repo, input=patch_text, capture_output=True, text=True,
    )
    assert check.returncode == 0, check.stderr

    findings = json.loads((run_dir / "findings.json").read_text())["findings"]
    assert findings[0]["commit"] == c1
    assert findings[0]["author"] == "test"
    assert findings[0]["remedy"] == "still_local"


def test_stale_finding_from_earlier_commit_excluded_from_patch(trunk_repo: Path, tmp_path: Path):
    """c1 touches two files; c2 only overwrites one of them, so c1 still has a surviving line
    (reviewed normally, not `superseded`) but the specific finding the model reports about the
    overwritten file no longer matches anything at the base head."""
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    (trunk_repo / "keep.txt").write_text("KEEP\n")
    (trunk_repo / "change.txt").write_text("v1\n")
    _git(trunk_repo, "add", "keep.txt", "change.txt")
    _git(trunk_repo, "commit", "-q", "-m", "c1: two files")
    c1 = _rev(trunk_repo, "HEAD")

    (trunk_repo / "change.txt").write_text("v2\n")
    _git(trunk_repo, "commit", "-q", "-am", "c2: overwrite change.txt")

    def side_effect(cmd, soft_timeout_s, hard_timeout_s):
        prompt = cmd[2]
        if f"head_sha: {c1}\n" in prompt:
            # what change.txt looked like right after c1 -- c2 has since overwritten it, so
            # this `before` text no longer matches anything at the base head.
            return _finding_events("change.txt", "v1", "V1"), False
        return _no_finding_events(), False

    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=side_effect):
        cli.main(argv)

    run_dir = sorted(_run_dirs(out_dir))[-1]
    findings = json.loads((run_dir / "findings.json").read_text())["findings"]
    assert findings[0]["status"] == "stale"
    assert not (run_dir / "patches" / "auto_fixed.patch").exists()
    assert not (run_dir / "patches" / "all.patch").exists()


def test_unit_3_of_5_times_out_watermark_stops_and_resumes_next_run(
    trunk_repo: Path, tmp_path: Path
):
    from review_shift import review as review_module

    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    shas = [_new_file_commit(trunk_repo, f"c{i}.txt", f"v{i}\n", f"c{i}") for i in range(1, 6)]

    def side_effect(cmd, soft_timeout_s, hard_timeout_s):
        prompt = cmd[2]
        if f"head_sha: {shas[2]}\n" in prompt:
            raise review_module.ReviewTimeout(attempts=0)
        return _no_finding_events(), False

    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=side_effect):
        cli.main(argv)

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    statuses = {u["sha"]: u["status"] for u in run_meta["units"]}
    assert statuses[shas[0]] == "ok"
    assert statuses[shas[1]] == "ok"
    assert statuses[shas[2]] == "timeout"
    assert shas[3] not in statuses  # never attempted this run

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == shas[1]

    # the next run resumes at the failed commit, not past it
    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_no_finding_events(), False)
    ) as mock_invoke:
        cli.main(argv)
    assert mock_invoke.call_count == 3  # shas[2], shas[3], shas[4]

    run_dir2 = sorted(_run_dirs(out_dir))[-1]
    run_meta2 = json.loads((run_dir2 / "run.json").read_text())
    assert [u["sha"] for u in run_meta2["units"]] == shas[2:]


def test_oversized_commit_advances_watermark_as_coverage_gap(trunk_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    big = "\n".join(f"line {i}" for i in range(3000))
    (trunk_repo / "big.txt").write_text(big)
    _git(trunk_repo, "add", ".")
    _git(trunk_repo, "commit", "-q", "-m", "huge change")
    huge_sha = _rev(trunk_repo, "HEAD")
    small_sha = _direct_commit(trunk_repo, "v1\n", "small change")

    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_no_finding_events(), False)
    ) as mock_invoke:
        cli.main(argv)
    mock_invoke.assert_called_once()  # only the small commit reaches the model

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    statuses = {u["sha"]: u["status"] for u in run_meta["units"]}
    assert statuses[huge_sha] == "diff_too_large"
    assert statuses[small_sha] == "ok"

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == small_sha  # advanced past the gap

    # a second run does not stop on the same oversized commit again
    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_no_finding_events(), False)
    ) as mock_invoke2:
        cli.main(argv)
    mock_invoke2.assert_not_called()
    run_dir2 = sorted(_run_dirs(out_dir))[-1]
    run_meta2 = json.loads((run_dir2 / "run.json").read_text())
    assert run_meta2["trunk_outcome"] == "nothing_new"


def test_fully_superseded_commit_is_skipped_with_zero_model_calls(
    trunk_repo: Path, tmp_path: Path
):
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    c1 = _direct_commit(trunk_repo, "ORIGINAL\n", "c1: add ORIGINAL")
    c2 = _direct_commit(trunk_repo, "REPLACED\n", "c2: fully overwrite c1's line")

    def side_effect(cmd, soft_timeout_s, hard_timeout_s):
        prompt = cmd[2]
        if f"head_sha: {c1}\n" in prompt:
            raise AssertionError("c1 is fully superseded and must not reach the model")
        return _no_finding_events(), False

    with mock_patch(
        "review_shift.review._invoke_with_timeout", side_effect=side_effect
    ) as mock_invoke:
        cli.main(argv)
    assert mock_invoke.call_count == 1  # only c2

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    statuses = {u["sha"]: u["status"] for u in run_meta["units"]}
    assert statuses[c1] == "superseded"
    assert statuses[c2] == "ok"

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == c2  # superseded still advances the watermark


def test_commit_whose_only_file_is_later_deleted_is_removed_not_superseded(
    trunk_repo: Path, tmp_path: Path
):
    """A commit whose only touched file is deleted by a later commit is a different situation
    from one whose lines were overwritten -- blame's `removed` outcome (not `superseded`) must
    reach `run.json`, and it must cost zero model calls just like `superseded` does."""
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    c1 = _new_file_commit(trunk_repo, "temp.txt", "x\n", "c1: add temp.txt")
    _git(trunk_repo, "rm", "-q", "temp.txt")
    _git(trunk_repo, "commit", "-q", "-m", "c2: delete temp.txt")
    c2 = _rev(trunk_repo, "HEAD")

    def side_effect(cmd, soft_timeout_s, hard_timeout_s):
        prompt = cmd[2]
        if f"head_sha: {c1}\n" in prompt:
            raise AssertionError("c1's only file is gone at head and must not reach the model")
        return _no_finding_events(), False

    with mock_patch(
        "review_shift.review._invoke_with_timeout", side_effect=side_effect
    ) as mock_invoke:
        cli.main(argv)
    assert mock_invoke.call_count == 1  # only c2

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    statuses = {u["sha"]: u["status"] for u in run_meta["units"]}
    assert statuses[c1] == "removed"
    assert statuses[c2] == "ok"

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == c2  # removed still advances the watermark


def test_merged_branch_work_is_never_reviewed_via_trunk(trunk_repo: Path, tmp_path: Path):
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    _git(trunk_repo, "checkout", "-q", "-b", "feature")
    (trunk_repo / "feature.txt").write_text("x\n")
    _git(trunk_repo, "add", ".")
    _git(trunk_repo, "commit", "-q", "-m", "feature work")
    _git(trunk_repo, "checkout", "-q", "main")
    _git(trunk_repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")

    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_no_finding_events(), False)
    ) as mock_invoke:
        exit_code = cli.main(argv)
    assert exit_code == 0
    mock_invoke.assert_not_called()

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["trunk_outcome"] == "nothing_new"


def test_trunk_enabled_via_config_without_flag(trunk_repo: Path, tmp_path: Path):
    """cli-surface spec "Flag omitted on an unchanged config" / trunk.enabled reproduces the
    same behavior as --trunk when set in config."""
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir)]
    exit_code = cli.main(argv)
    assert exit_code == 0
    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["mode"] == "trunk"


def test_total_budget_exhausted_mid_trunk_run_stops_watermark(trunk_repo: Path, tmp_path: Path):
    """budget-and-resilience spec "Total budget exhausted mid trunk run": 3 of 8 units
    complete, the remaining 5 are `budget_exhausted`, the watermark stops at the third, and the
    run reports exhaustion rather than completion."""
    (trunk_repo / ".review-shift" / "config.yml").write_text(
        "version: 2\ntrunk:\n  enabled: true\nruntime:\n  total_budget_usd: 0.03\n"
    )
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    shas = [_new_file_commit(trunk_repo, f"u{i}.txt", f"v{i}\n", f"u{i}") for i in range(8)]

    with mock_patch(
        "review_shift.review._invoke_with_timeout",
        return_value=(_no_finding_events(cost_usd=0.01), False),
    ) as mock_invoke:
        cli.main(argv)
    assert mock_invoke.call_count == 3  # 0.03 / 0.01 per unit

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    statuses = {u["sha"]: u["status"] for u in run_meta["units"]}
    assert [statuses[s] for s in shas[:3]] == ["ok", "ok", "ok"]
    assert statuses[shas[3]] == "budget_exhausted"
    assert shas[4] not in statuses  # loop stopped, remaining units never attempted this run

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == shas[2]  # stops at the third completed unit


def test_max_commits_per_run_cap_defers_remainder(trunk_repo: Path, tmp_path: Path):
    """trunk-review spec "Trunk unit count per run is capped": with 5 direct commits and a cap
    of 2, the 2 oldest are reviewed, the remaining 3 are `max_commits_per_run_cap` and the
    watermark stops at the second."""
    (trunk_repo / ".review-shift" / "config.yml").write_text(
        "version: 2\ntrunk:\n  enabled: true\n  max_commits_per_run: 2\n"
    )
    out_dir = tmp_path / "runs"
    argv = [
        "run", "--trunk", "--base", "main", "--repo", str(trunk_repo), "--out-dir", str(out_dir),
    ]
    cli.main(argv)  # bootstrap

    shas = [_new_file_commit(trunk_repo, f"u{i}.txt", f"v{i}\n", f"u{i}") for i in range(5)]

    with mock_patch(
        "review_shift.review._invoke_with_timeout", return_value=(_no_finding_events(), False)
    ) as mock_invoke:
        cli.main(argv)
    assert mock_invoke.call_count == 2

    run_dir = sorted(_run_dirs(out_dir))[-1]
    run_meta = json.loads((run_dir / "run.json").read_text())
    statuses = {u["sha"]: u["status"] for u in run_meta["units"]}
    assert statuses[shas[0]] == "ok"
    assert statuses[shas[1]] == "ok"
    for s in shas[2:]:
        assert statuses[s] == "max_commits_per_run_cap"

    index = json.loads((out_dir / "index.json").read_text())
    assert index["watermarks"]["main"]["sha"] == shas[1]
