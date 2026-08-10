"""The run pipeline redacts the diff before it reaches the model (diff -> redact -> review,
system-analysis.md §4), and records the activity in run.json (ADR-008 point 4)."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

from src import cli


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


def test_secret_in_diff_never_reaches_the_model_prompt(branched_repo: Path, tmp_path: Path):
    subprocess.run(
        ["git", "-C", str(branched_repo), "checkout", "-q", "feature/x"], check=True
    )
    (branched_repo / "src" / "bar.py").write_text(
        'VALUE = 1  # off by one\nAWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
    )
    subprocess.run(
        ["git", "-C", str(branched_repo), "commit", "-q", "-am", "add key"], check=True
    )
    subprocess.run(["git", "-C", str(branched_repo), "checkout", "-q", "main"], check=True)

    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": "src/bar.py", "line": 1, "severity": "low", "category": "style",
             "rationale": "minor"},
        ],
    }
    captured: dict = {}

    def fake_invoke(cmd: list[str], soft_timeout_s: float, hard_timeout_s: float):
        captured["prompt"] = cmd[2]
        return _fake_claude_events(structured_output), False

    out_dir = tmp_path / "runs"
    argv = ["run", "--branch", "feature/x", "--base", "main", "--depth", "medium",
            "--repo", str(branched_repo), "--out-dir", str(out_dir)]

    with mock_patch("src.review._invoke_with_timeout", side_effect=fake_invoke):
        exit_code = cli.main(argv)

    assert exit_code == 0
    assert "AKIAABCDEFGHIJKLMNOP" not in captured["prompt"]
    assert "<<REDACTED:" in captured["prompt"]

    run_dirs = [p for p in out_dir.iterdir() if p.is_dir() and not p.is_symlink()]
    run_meta = json.loads((run_dirs[0] / "run.json").read_text())
    assert run_meta["secrets_redacted"] >= 1
    assert "src/bar.py" in run_meta["secrets_redacted_files"]
