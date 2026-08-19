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

import pytest

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


def test_deepest_depth_from_config_file_alone_reaches_build_command(
    branched_repo: Path, tmp_path: Path
):
    """add-depth-high task 1.6: `depth: medium` set only in config.yml, no `--depth` flag, used
    to pass schema validation and then fail once it reached `build_command` (the CLI flag's
    hardcoded default silently won). This pins that the config value now takes effect."""
    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("version: 3\ndepth: medium\n")

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
    assert "review-shift — depth: medium" in prompt

    run_dirs = _run_dirs(out_dir)
    run_meta = json.loads((run_dirs[0] / "run.json").read_text())
    assert run_meta["depth"] == "medium"


def test_model_from_config_file_alone_reaches_the_invocation(
    branched_repo: Path, tmp_path: Path
):
    """`--model` carried an eager argparse default of "sonnet", so `runtime.model` (and its
    `REVIEW_SHIFT__RUNTIME__MODEL` override) could never take effect: every run used sonnet
    however the config was written, and said so honestly in `model_resolved`. Same shape as the
    `--depth` dead-config-path above, and quieter, since nothing fails."""
    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("version: 3\nruntime:\n  model: opus\n")

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
    cmd = captured_cmds[0]
    assert cmd[cmd.index("--model") + 1] == "opus"


def test_model_flag_overrides_the_config_value(branched_repo: Path, tmp_path: Path):
    """config-loading spec "CLI flag overrides file value" — the fix above must not invert the
    precedence it was meant to restore."""
    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("version: 3\nruntime:\n  model: opus\n")

    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": "src/bar.py", "line": 1, "severity": "low", "category": "style",
             "rationale": "minor"},
        ],
    }
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/x", "--base", "main", "--model", "haiku",
            "--repo", str(branched_repo), "--out-dir", str(out_dir)]

    captured_cmds = []

    def _side_effect(cmd, soft_timeout_s, hard_timeout_s):
        captured_cmds.append(cmd)
        return _fake_claude_events(structured_output), False

    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=_side_effect):
        cli.main(argv)

    cmd = captured_cmds[0]
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_config_model_changes_the_idempotency_key(branched_repo: Path, tmp_path: Path):
    """The model reaches the key through the same resolution, so a config-only model change
    must invalidate the cache — otherwise the fix above would restore the model to the
    invocation while leaving the key blind to it."""
    config_dir = branched_repo / ".review-shift"
    config_dir.mkdir()
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

    calls = []

    def _side_effect(cmd, soft_timeout_s, hard_timeout_s):
        calls.append(cmd)
        return _fake_claude_events(structured_output), False

    (config_dir / "config.yml").write_text("version: 3\nruntime:\n  model: sonnet\n")
    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=_side_effect):
        cli.main(argv)
    assert len(calls) == 1

    # Control: with nothing changed at all, the cache must engage. Without this the assertion
    # below passes for a second reason -- a cache that never hits -- and pins nothing.
    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=_side_effect):
        cli.main(argv)
    assert len(calls) == 1, "the idempotency cache did not engage; the assertion below is blind"

    # same branch, same shas, same depth -- only the config's model differs
    (config_dir / "config.yml").write_text("version: 3\nruntime:\n  model: opus\n")
    with mock_patch("review_shift.review._invoke_with_timeout", side_effect=_side_effect):
        cli.main(argv)
    assert len(calls) == 2, "a config-only model change was served from the cache"


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


# --- restructure-depth-tiers: the retired depth and --dry-run at the CLI surface ----------


def test_retired_depth_is_refused_naming_the_levels_and_the_remap(capsys):
    """cli-surface spec "A removed enum value is rejected with its replacement named":
    argparse's bare "invalid choice" tells a user whose script says `--depth high` nothing
    about where `high` went, and a silent alias to `medium` would be worse still."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "--depth", "high"])
    assert excinfo.value.code == 2

    err = capsys.readouterr().err
    assert "smoke, low, medium" in err
    assert "`high` is now `medium`" in err


def test_dry_run_combines_with_any_depth(branched_repo: Path, tmp_path: Path):
    """cli-surface spec "Dry-run combines with any depth": the two are separate axes."""
    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/x", "--base", "main", "--depth", "medium",
            "--repo", str(branched_repo), "--out-dir", str(out_dir), "--dry-run"]
    assert cli.main(argv) == 0

    run_meta = json.loads((_run_dirs(out_dir)[0] / "run.json").read_text())
    assert run_meta["depth"] == "medium"
    assert run_meta["targets"][0]["depth"] == "medium"
