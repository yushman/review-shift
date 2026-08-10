"""Soft/hard timeout handling around the `claude` subprocess (TDR NFR-3, budget-and-resilience
spec "Soft and hard timeouts"). Uses a real stub `claude` process, not a mock: a fake
executable that either honors SIGTERM (graceful, soft-timeout path) or ignores it (must be
SIGKILLed, hard-timeout path) — the point is to exercise the actual Popen/terminate/kill
mechanics, since a mocked subprocess.run can't distinguish "handled gracefully" from
"forcibly killed."
"""
from __future__ import annotations

import json
import stat
import sys
import time
from pathlib import Path

import pytest

from src import review

STUB_SOURCE = '''#!/usr/bin/env python3
import argparse, json, os, signal, sys, time

parser = argparse.ArgumentParser()
parser.add_argument("--sleep", type=float, default=10.0)
parser.add_argument("--honor-sigterm", action="store_true")
args = parser.parse_args()

EVENTS = [
    {"type": "system", "subtype": "init", "model": "stub", "claude_code_version": "0"},
    {
        "type": "result", "stop_reason": "end_turn", "subtype": "success",
        "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1},
        "structured_output": {"schema_version": 1, "findings": []},
        "result": json.dumps({"schema_version": 1, "findings": []}),
    },
]


def _on_term(signum, frame):
    if args.honor_sigterm:
        sys.stdout.write(json.dumps(EVENTS))
        sys.stdout.flush()
        os._exit(0)
    # else: ignore SIGTERM, keep sleeping until SIGKILL


signal.signal(signal.SIGTERM, _on_term)
time.sleep(args.sleep)
sys.stdout.write(json.dumps(EVENTS))
sys.stdout.flush()
'''


@pytest.fixture
def stub_claude(tmp_path: Path) -> Path:
    script = tmp_path / "stub_claude.py"
    script.write_text(STUB_SOURCE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _cmd(stub: Path, *, sleep: float, honor_sigterm: bool) -> list[str]:
    cmd = [sys.executable, str(stub), "--sleep", str(sleep)]
    if honor_sigterm:
        cmd.append("--honor-sigterm")
    return cmd


def test_completes_within_soft_budget_is_not_partial(stub_claude: Path):
    cmd = _cmd(stub_claude, sleep=0.05, honor_sigterm=False)
    events, partial = review._invoke_with_timeout(cmd, soft_timeout_s=5.0, hard_timeout_s=10.0)
    assert partial is False
    assert events[-1]["type"] == "result"


def test_soft_timeout_sends_sigterm_and_accepts_graceful_partial(stub_claude: Path):
    cmd = _cmd(stub_claude, sleep=30.0, honor_sigterm=True)
    start = time.monotonic()
    events, partial = review._invoke_with_timeout(cmd, soft_timeout_s=0.3, hard_timeout_s=5.0)
    elapsed = time.monotonic() - start
    assert partial is True
    assert events[-1]["type"] == "result"
    # proves the process was actually signaled and exited early, not that we waited it out
    assert elapsed < 5.0


def test_hard_timeout_sigkills_a_process_that_ignores_sigterm(stub_claude: Path):
    cmd = _cmd(stub_claude, sleep=30.0, honor_sigterm=False)
    start = time.monotonic()
    with pytest.raises(review.ReviewTimeout):
        review._invoke_with_timeout(cmd, soft_timeout_s=0.2, hard_timeout_s=0.6)
    elapsed = time.monotonic() - start
    # real kill, not waiting out the 30s sleep
    assert elapsed < 5.0


def test_run_review_marks_partial_true_on_soft_timeout(tmp_path: Path, stub_claude: Path,
                                                          monkeypatch):
    monkeypatch.setattr(
        review, "build_command",
        lambda *a, **kw: _cmd(stub_claude, sleep=30.0, honor_sigterm=True),
    )
    result = review.run_review(
        # empty diff -> an empty findings response is valid and doesn't trigger ADR-011's
        # separate "retry on empty findings for a non-empty diff" path, which would eat the
        # remaining soft-timeout budget before this test's own mechanics get to run.
        branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
        diff_text="", head_sha="abc123", repo_files={"src/foo.py"},
        soft_timeout_minutes=0.3 / 60, hard_timeout_minutes=5.0 / 60,
    )
    assert result.partial is True


def test_run_review_raises_timeout_on_hard_deadline(tmp_path: Path, stub_claude: Path,
                                                       monkeypatch):
    monkeypatch.setattr(
        review, "build_command",
        lambda *a, **kw: _cmd(stub_claude, sleep=30.0, honor_sigterm=False),
    )
    with pytest.raises(review.ReviewTimeout) as exc_info:
        review.run_review(
            branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
            diff_text="", head_sha="abc123", repo_files={"src/foo.py"},
            soft_timeout_minutes=0.2 / 60, hard_timeout_minutes=0.6 / 60,
        )
    assert exc_info.value.attempts == 1


def test_run_review_without_timeouts_uses_original_untimed_path(tmp_path: Path):
    """No soft/hard timeout configured -> behaves exactly as before this change (no Popen
    wrapping, no signals) -- backward compatibility for every existing caller/test."""
    import subprocess
    from unittest.mock import patch

    events = [{"type": "system"},
              {"type": "result", "stop_reason": "tool_use", "subtype": "success",
               "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 1},
               "structured_output": {"schema_version": 1, "findings": []},
               "result": json.dumps({"schema_version": 1, "findings": []})}]
    completed = subprocess.CompletedProcess(args=["claude"], returncode=0,
                                             stdout=json.dumps(events), stderr="")
    with patch("src.review.subprocess.run", return_value=completed) as mock_run:
        result = review.run_review(
            branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
            diff_text="", head_sha="abc123", repo_files={"src/foo.py"},
        )
    assert mock_run.called
    assert result.partial is False
