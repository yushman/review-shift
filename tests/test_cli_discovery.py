"""`review-shift run` with no `--branch`: real discovery (src/discover.py) driven by a real
config file, wired into the CLI for the first time (branch-discovery-and-config task 1.3's
"done when" — the CLI's selection must match a hand-written for-each-ref pipeline).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

from src import cli, discover


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _run_dirs(out_dir: Path) -> list[Path]:
    """out_dir now also holds index.json/latest/latest.txt and the batch summary
    (run-orchestration-and-resilience) alongside the per-branch run directories; `latest` is
    itself a symlink to one of them, so exclude symlinks too, not just non-directories."""
    return sorted(p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink())


def _fake_claude_events() -> list[dict]:
    # an empty findings array against a non-empty diff triggers a retry per ADR-011 (see
    # test_cli_end_to_end.py); a low-severity finding keeps exit 0 without that retry loop.
    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": "src/bar.py", "line": 1, "severity": "low", "category": "style",
             "rationale": "minor"},
        ],
    }
    return [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5",
         "claude_code_version": "2.1.226"},
        {
            "type": "result", "stop_reason": "end_turn", "subtype": "success",
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "structured_output": structured_output,
            "result": json.dumps(structured_output),
        },
    ]


def test_run_with_no_branch_reviews_the_discovered_set(branched_repo: Path, tmp_path: Path):
    # a second in-scope branch, and one excluded by config's exclude_patterns
    _git(branched_repo, "checkout", "-q", "-b", "feature/y", "main")
    (branched_repo / "src" / "bar.py").write_text("VALUE = 2\nOTHER = 2\n")
    _git(branched_repo, "commit", "-q", "-am", "second feature branch")
    _git(branched_repo, "checkout", "-q", "-b", "release/1.0", "main")
    _git(branched_repo, "commit", "-q", "--allow-empty", "-m", "release branch")
    _git(branched_repo, "checkout", "-q", "main")

    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "version: 1\n"
        "discovery:\n"
        "  patterns: [\"feature/*\", \"release/*\"]\n"
        "  exclude_patterns: [\"release/*\"]\n"
    )

    # oracle: what a hand-written for-each-ref + merge-base pipeline would select
    expected = discover.discover(
        branched_repo, "main",
        patterns=["feature/*", "release/*"], exclude_patterns=["release/*"],
    )
    assert {c.branch for c in expected.selected} == {"feature/x", "feature/y"}
    assert expected.skipped == []  # release/1.0 is excluded before any skip-reason filter runs

    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(branched_repo), "--out-dir", str(out_dir)]
    mock_events = (_fake_claude_events(), False)
    with mock_patch("src.review._invoke_with_timeout", return_value=mock_events):
        exit_code = cli.main(argv)

    assert exit_code == 0
    run_dirs = _run_dirs(out_dir)
    assert len(run_dirs) == 2
    reviewed_branches = set()
    for run_dir in run_dirs:
        run_meta = json.loads((run_dir / "run.json").read_text())
        reviewed_branches.add(run_meta["branch"])
    assert reviewed_branches == {"feature/x", "feature/y"}
    assert "release/1.0" not in reviewed_branches


def test_run_with_no_branch_and_no_matches_exits_zero_without_crashing(
    branched_repo: Path, tmp_path: Path
):
    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "version: 1\ndiscovery:\n  patterns: [\"nothing-matches/*\"]\n"
    )
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(branched_repo), "--out-dir", str(out_dir)]
    exit_code = cli.main(argv)
    assert exit_code == 0
    assert not out_dir.exists() or list(out_dir.iterdir()) == []


def test_run_with_no_branch_records_skip_reasons_in_reviewed_branch_report(
    branched_repo: Path, tmp_path: Path
):
    _git(branched_repo, "checkout", "-q", "--orphan", "orphan-branch")
    _git(branched_repo, "rm", "-rf", "-q", "--cached", ".")
    (branched_repo / "unrelated.txt").write_text("x\n")
    _git(branched_repo, "add", ".")
    _git(branched_repo, "commit", "-q", "-m", "orphan root")
    _git(branched_repo, "checkout", "-q", "main")

    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text(
        "version: 1\ndiscovery:\n  patterns: [\"feature/*\", \"orphan-branch\"]\n"
    )
    out_dir = tmp_path / "runs"
    argv = ["run", "--base", "main", "--repo", str(branched_repo), "--out-dir", str(out_dir)]
    mock_events = (_fake_claude_events(), False)
    with mock_patch("src.review._invoke_with_timeout", return_value=mock_events):
        exit_code = cli.main(argv)

    assert exit_code == 0
    run_dirs = _run_dirs(out_dir)
    assert len(run_dirs) == 1
    run_meta = json.loads((run_dirs[0] / "run.json").read_text())
    assert run_meta["branch"] == "feature/x"
    assert run_meta["skipped"] == [
        {"branch": "orphan-branch", "reason": "no_merge_base", "base": "main"}
    ]
    report_text = (run_dirs[0] / "report.md").read_text()
    assert "orphan-branch" in report_text
    assert "no_merge_base" in report_text
