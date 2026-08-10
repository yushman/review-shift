"""review.py: invocation flags, schema/semantic validation, retry policy (ADR-001/011/016)."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest

from review_shift import review


def _result_event(structured_output=None, stop_reason="tool_use", subtype="success", result=None,
                   cost=0.01):
    payload = {
        "type": "result",
        "stop_reason": stop_reason,
        "subtype": subtype,
        "total_cost_usd": cost,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    if structured_output is not None:
        payload["structured_output"] = structured_output
        payload["result"] = json.dumps(structured_output)
    if result is not None:
        payload["result"] = result
    return payload


def _completed(events, returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                        stdout=json.dumps(events), stderr="")


VALID_PAYLOAD = {
    "schema_version": 1,
    "findings": [
        {"file": "src/foo.py", "line": 1, "severity": "medium", "category": "style",
         "rationale": "r"}
    ],
}


def test_build_command_flags_and_no_dangerous_bypass(tmp_path: Path):
    cmd = review.build_command("prompt text", "medium", tmp_path, "session-1")
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "plan"
    assert "--allowedTools" in cmd
    for tool in review.ALLOWED_TOOLS:
        assert tool in cmd
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert "--max-budget-usd" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.0"
    assert "--dangerously-skip-permissions" not in cmd
    assert "--allow-dangerously-skip-permissions" not in cmd


def test_build_command_rejects_unsupported_depth(tmp_path: Path):
    with pytest.raises(review.ReviewConfigError):
        review.build_command("prompt", "high", tmp_path, "session-1")


def test_validate_findings_accepts_valid_payload():
    findings = review.validate_findings(VALID_PAYLOAD, repo_files={"src/foo.py"})
    assert len(findings) == 1


def test_validate_findings_rejects_unknown_file():
    with pytest.raises(jsonschema.ValidationError):
        review.validate_findings(VALID_PAYLOAD, repo_files={"src/other.py"})


def test_validate_findings_rejects_end_line_before_line():
    payload = {
        "schema_version": 1,
        "findings": [
            {"file": "src/foo.py", "line": 5, "end_line": 3, "severity": "low",
             "category": "style", "rationale": "r"}
        ],
    }
    with pytest.raises(jsonschema.ValidationError):
        review.validate_findings(payload, repo_files={"src/foo.py"})


def test_run_review_succeeds_first_attempt(tmp_path: Path):
    events = [{"type": "system"}, _result_event(structured_output=VALID_PAYLOAD)]
    with patch("review_shift.review.subprocess.run", return_value=_completed(events)):
        result = review.run_review(
            branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
            diff_text="diff --git a/src/foo.py b/src/foo.py\n", head_sha="abc123",
            repo_files={"src/foo.py"},
        )
    assert result.attempts == 1
    assert len(result.findings) == 1


def test_run_review_retries_on_invalid_then_succeeds(tmp_path: Path):
    bad = [{"type": "system"}, _result_event(result="not json")]
    good = [{"type": "system"}, _result_event(structured_output=VALID_PAYLOAD)]
    with patch(
        "review_shift.review.subprocess.run", side_effect=[_completed(bad), _completed(good)]
    ):
        result = review.run_review(
            branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
            diff_text="some diff", head_sha="abc123", repo_files={"src/foo.py"},
        )
    assert result.attempts == 2


def test_run_review_refusal_does_not_retry(tmp_path: Path):
    events = [{"type": "system"}, _result_event(stop_reason="refusal", subtype="refusal")]
    with patch("review_shift.review.subprocess.run", return_value=_completed(events)) as mock_run:
        with pytest.raises(review.ReviewRefused):
            review.run_review(
                branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
                diff_text="some diff", head_sha="abc123", repo_files={"src/foo.py"},
            )
    assert mock_run.call_count == 1


def test_run_review_gives_up_after_three_invalid_attempts(tmp_path: Path):
    bad = [{"type": "system"}, _result_event(result="not json")]
    with patch("review_shift.review.subprocess.run", return_value=_completed(bad)) as mock_run:
        with pytest.raises(review.ReviewInvalid) as exc_info:
            review.run_review(
                branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
                diff_text="some diff", head_sha="abc123", repo_files={"src/foo.py"},
            )
    assert mock_run.call_count == 3
    assert exc_info.value.attempts == 3
    assert len(exc_info.value.raw_responses) == 3


def test_prompt_template_hash_is_stable(tmp_path: Path):
    assert review.prompt_template_hash("medium") == review.prompt_template_hash("medium")


def test_prompt_template_hash_differs_by_depth(tmp_path: Path):
    assert review.prompt_template_hash("medium") != review.prompt_template_hash("low")


def test_prompt_template_hash_changes_when_template_edited(tmp_path: Path, monkeypatch):
    fake_prompts = tmp_path / "prompts"
    fake_prompts.mkdir()
    (fake_prompts / "medium.md").write_text("v1")
    monkeypatch.setattr(review, "PROMPTS_DIR", fake_prompts)
    h1 = review.prompt_template_hash("medium")
    (fake_prompts / "medium.md").write_text("v2")
    h2 = review.prompt_template_hash("medium")
    assert h1 != h2


def _preflight_ok():
    return subprocess.CompletedProcess(args=["claude"], returncode=0, stdout='{"type": "result"}',
                                        stderr="")


def _preflight_auth_failure():
    return subprocess.CompletedProcess(
        args=["claude"], returncode=1, stdout="",
        stderr="Error: not logged in. Please run `claude login`.",
    )


def _preflight_quota_failure():
    return subprocess.CompletedProcess(
        args=["claude"], returncode=1, stdout="",
        stderr="Error: rate limit exceeded, quota exhausted",
    )


def test_check_auth_succeeds_on_clean_response():
    with patch("review_shift.review._run_preflight", return_value=_preflight_ok()):
        review.check_auth()  # does not raise


def test_check_auth_raises_auth_error_on_login_failure():
    with patch("review_shift.review._run_preflight", return_value=_preflight_auth_failure()):
        with pytest.raises(review.AuthError):
            review.check_auth()


def test_check_auth_raises_quota_error_on_rate_limit():
    with patch("review_shift.review._run_preflight", return_value=_preflight_quota_failure()):
        with pytest.raises(review.QuotaError):
            review.check_auth()


def test_run_review_succeeds_immediately_on_empty_findings_for_nonempty_diff(tmp_path: Path):
    """A schema-valid empty findings array is a correct "nothing to report" outcome per
    src/prompts/low.md's explicit instruction, not a sign of invalid model output — it must
    not trigger a retry (ADR-011's original retry line predates that prompt contract)."""
    empty_payload = {"schema_version": 1, "findings": []}
    empty = [{"type": "system"}, _result_event(structured_output=empty_payload)]
    with patch("review_shift.review.subprocess.run", return_value=_completed(empty)) as mock_run:
        result = review.run_review(
            branch="feature/x", base="main", depth="medium", repo_root=tmp_path,
            diff_text="a real non-empty diff", head_sha="abc123", repo_files={"src/foo.py"},
        )
    assert result.attempts == 1
    assert result.findings == []
    assert mock_run.call_count == 1
