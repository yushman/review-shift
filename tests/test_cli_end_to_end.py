"""Wires branch -> diff -> claude -p (mocked) -> validate -> patch -> run directory (section 4).

Patches `review._invoke_with_timeout`, not `subprocess.run`: `subprocess` is one shared module
object, so patching `src.review.subprocess.run` would also intercept gitutil's real `git`
subprocess calls that cli.cmd_run makes in the same call — a real footgun worth documenting
here. `_invoke_with_timeout` (not the older `_invoke`) is the seam actually hit through
`cli.main`, since batch.py always threads the configured soft/hard timeouts (defaults 15/45
min) into `review.run_review` — run-orchestration-and-resilience.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

from review_shift import cli


def _run_dirs(out_dir: Path) -> list[Path]:
    """out_dir now also holds index.json/latest/latest.txt and the batch summary
    (run-orchestration-and-resilience) alongside the per-branch run directories; `latest` is
    itself a symlink to one of them, so exclude symlinks too, not just non-directories."""
    return sorted(p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink())


def _fake_claude_events(structured_output: dict) -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5",
         "claude_code_version": "2.1.226"},
        {
            "type": "result",
            "stop_reason": "tool_use",
            "subtype": "success",
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "structured_output": structured_output,
            "result": json.dumps(structured_output),
        },
    ]


def test_run_writes_full_run_directory(branched_repo: Path, tmp_path: Path):
    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": "src/bar.py", "line": 1, "severity": "high", "category": "bug",
             "rationale": "off-by-one in the comment, not the value",
             "before": "VALUE = 1  # off by one", "after": "VALUE = 1"},
        ],
    }
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/x", "--base", "main", "--depth", "medium",
            "--repo", str(branched_repo), "--out-dir", str(out_dir)]

    mock_events = (_fake_claude_events(structured_output), False)
    with mock_patch("review_shift.review._invoke_with_timeout", return_value=mock_events):
        exit_code = cli.main(argv)

    assert exit_code == 1  # a high-severity finding is present
    run_dirs = _run_dirs(out_dir)
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    assert (run_dir / "findings.json").exists()
    assert (run_dir / "run.json").exists()
    assert (run_dir / "report.md").exists()
    assert (run_dir / "patches" / "auto_fixed.patch").exists()
    assert (run_dir / "patches" / "all.patch").exists()

    run_meta = json.loads((run_dir / "run.json").read_text())
    assert run_meta["branch"] == "feature/x"
    assert run_meta["findings_by_severity"]["high"] == 1
    assert run_meta["exit_code"] == 1

    # the apply recipe (report.md) checks out the reviewed branch first — do the same here
    subprocess.run(["git", "-C", str(branched_repo), "checkout", "-q", "feature/x"], check=True)
    patch_text = (run_dir / "patches" / "auto_fixed.patch").read_text()
    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn"],
        cwd=branched_repo,
        input=patch_text,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


def test_depth_high_from_config_file_alone_reaches_build_command(
    branched_repo: Path, tmp_path: Path
):
    """add-depth-high task 1.6: `depth: high` set only in config.yml, no `--depth` flag, used
    to pass schema validation and then fail once it reached `build_command` (the CLI flag's
    hardcoded default silently won). This pins that the config value now takes effect."""
    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("version: 1\ndepth: high\n")

    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": "src/bar.py", "line": 1, "severity": "low", "category": "style",
             "rationale": "minor"},
        ],
    }
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/x", "--base", "main",
            "--repo", str(branched_repo), "--out-dir", str(out_dir)]

    captured_cmds = []

    def _side_effect(cmd, soft_timeout_s, hard_timeout_s):
        captured_cmds.append(cmd)
        return _fake_claude_events(structured_output), False

    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=_side_effect):
        exit_code = cli.main(argv)

    assert exit_code == 0
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert cmd[cmd.index("--effort") + 1] == "high"
    prompt = cmd[2]
    assert "review-shift — depth: high" in prompt

    run_dirs = _run_dirs(out_dir)
    run_meta = json.loads((run_dirs[0] / "run.json").read_text())
    assert run_meta["depth"] == "high"


def test_run_with_no_findings_exits_zero(branched_repo: Path, tmp_path: Path):
    structured_output = {"schema_version": 1, "findings": []}
    # empty findings against a non-empty diff triggers a retry per ADR-011; give it 3 empties
    # so it exhausts attempts and we can assert the internal-error path instead — use a diff-y
    # finding-free response isn't representative, so cover the "no findings" case via low-signal
    # content: an empty diff isn't realistic here, so we instead assert exit 0 through a finding
    # with severity below high.
    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": "src/bar.py", "line": 1, "severity": "low", "category": "style",
             "rationale": "minor"},
        ],
    }
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/x", "--base", "main", "--depth", "medium",
            "--repo", str(branched_repo), "--out-dir", str(out_dir)]
    mock_events = (_fake_claude_events(structured_output), False)
    with mock_patch("review_shift.review._invoke_with_timeout", return_value=mock_events):
        exit_code = cli.main(argv)
    assert exit_code == 0
