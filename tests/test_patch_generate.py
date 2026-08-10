"""Conflict resolution and the git apply --check gate (ADR-013 §3-5), against a real repo."""
from pathlib import Path

from review_shift.patch import NO_FIX, REDACTED, generate_and_verify, resolve


def test_resolve_marks_stale_and_applicable(fixture_repo: Path, head_sha: str):
    findings = [
        {"file": "src/foo.py", "line": 2, "severity": "medium", "category": "style",
         "rationale": "r", "before": "    return a + b", "after": "    return a + b  # sum"},
        {"file": "src/foo.py", "line": 2, "severity": "medium", "category": "style",
         "rationale": "r", "before": "this text is not in the file"},
    ]
    localized = resolve(findings, fixture_repo, head_sha)
    assert localized[0].status == "applicable"
    assert localized[1].status == "stale"


def test_observation_only_finding_gets_no_fix_status(fixture_repo: Path, head_sha: str):
    findings = [{"file": "src/foo.py", "line": 2, "severity": "low", "category": "style",
                 "rationale": "r"}]
    localized = resolve(findings, fixture_repo, head_sha)
    assert localized[0].status == NO_FIX


def test_overlapping_findings_resolve_by_severity(fixture_repo: Path, head_sha: str):
    findings = [
        {"file": "src/foo.py", "line": 2, "severity": "medium", "category": "style",
         "rationale": "r", "before": "    return a + b", "after": "    return a + b  # medium"},
        {"file": "src/foo.py", "line": 2, "severity": "high", "category": "bug",
         "rationale": "r", "before": "    return a + b", "after": "    return a + b  # high"},
    ]
    localized = resolve(findings, fixture_repo, head_sha)
    assert localized[0].status == "conflict"
    assert localized[1].status == "applicable"


def test_non_overlapping_findings_in_same_file_both_apply(fixture_repo: Path, head_sha: str):
    findings = [
        {"file": "src/bar.py", "line": 1, "severity": "low", "category": "style",
         "rationale": "r", "before": "VALUE = 1", "after": "VALUE = 10"},
        {"file": "src/bar.py", "line": 2, "severity": "low", "category": "style",
         "rationale": "r", "before": "OTHER = 2", "after": "OTHER = 20"},
    ]
    localized = resolve(findings, fixture_repo, head_sha)
    assert all(lf.status == "applicable" for lf in localized)


def test_generate_and_verify_produces_checkable_patch(fixture_repo: Path, head_sha: str):
    findings = [
        {"file": "src/bar.py", "line": 1, "severity": "high", "category": "bug",
         "rationale": "r", "before": "VALUE = 1", "after": "VALUE = 10"},
    ]
    localized = resolve(findings, fixture_repo, head_sha)
    diff_text, error = generate_and_verify(localized, fixture_repo, head_sha, "test-run")
    assert error is None
    assert diff_text is not None
    assert "src/bar.py" in diff_text
    assert "-VALUE = 1" in diff_text
    assert "+VALUE = 10" in diff_text

    # the produced diff must actually apply to a real checkout at head_sha, -p1, exactly the
    # way the README's apply recipe uses it
    import subprocess
    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn"],
        cwd=fixture_repo,
        input=diff_text,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stderr


def test_generate_and_verify_returns_none_when_nothing_applicable(
    fixture_repo: Path, head_sha: str
):
    findings = [{"file": "src/foo.py", "line": 2, "severity": "low", "category": "style",
                 "rationale": "r"}]
    localized = resolve(findings, fixture_repo, head_sha)
    diff_text, error = generate_and_verify(localized, fixture_repo, head_sha, "test-run")
    assert diff_text is None
    assert error is None


def test_finding_touching_masked_region_is_redacted_not_localized(
    fixture_repo: Path, head_sha: str
):
    findings = [
        {"file": "src/bar.py", "line": 1, "severity": "critical", "category": "security",
         "rationale": "hardcoded secret",
         "before": 'VALUE = "<<REDACTED:aws_secret_key>>"', "after": 'VALUE = "x"'},
    ]
    localized = resolve(findings, fixture_repo, head_sha)
    assert localized[0].status == REDACTED
    assert localized[0].match_start is None


def test_redacted_finding_excluded_from_patch_even_when_critical(
    fixture_repo: Path, head_sha: str
):
    findings = [
        {"file": "src/bar.py", "line": 1, "severity": "critical", "category": "security",
         "rationale": "hardcoded secret",
         "before": 'VALUE = "<<REDACTED:aws_secret_key>>"', "after": 'VALUE = "x"'},
    ]
    localized = resolve(findings, fixture_repo, head_sha)
    critical = [lf for lf in localized if lf.finding["severity"] in ("critical", "high")]
    diff_text, error = generate_and_verify(critical, fixture_repo, head_sha, "test-run")
    assert diff_text is None
    assert error is None


def test_multiple_files_combine_into_one_diff(fixture_repo: Path, head_sha: str):
    findings = [
        {"file": "src/bar.py", "line": 1, "severity": "high", "category": "bug",
         "rationale": "r", "before": "VALUE = 1", "after": "VALUE = 10"},
        {"file": "src/foo.py", "line": 2, "severity": "high", "category": "bug",
         "rationale": "r", "before": "    return a + b", "after": "    return a + b  # x"},
    ]
    localized = resolve(findings, fixture_repo, head_sha)
    diff_text, error = generate_and_verify(localized, fixture_repo, head_sha, "test-run")
    assert error is None
    assert "src/bar.py" in diff_text
    assert "src/foo.py" in diff_text
