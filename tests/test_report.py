from review_shift import report
from review_shift.patch import LocalizedFinding


def _lf(index, file, line, severity, status, before=None, after=None):
    finding = {
        "file": file, "line": line, "severity": severity, "category": "bug",
        "rationale": "because", "confidence": "high",
    }
    if before is not None:
        finding["before"] = before
    if after is not None:
        finding["after"] = after
    return LocalizedFinding(index=index, finding=finding, status=status)


RUN = {
    "branch": "feature/x", "head_sha": "abc123", "base": "main", "depth": "medium",
    "started_at": "2026-08-09T03:30:00Z", "duration_ms": 1234, "cost_usd": 0.42,
    "findings_by_severity": {"critical": 0, "high": 1, "medium": 1, "low": 0, "info": 0},
    "findings_without_patch": 1,
    "auto_fix_min_severity": "high",
    "auto_fix_patch_path": ".review-shift/runs/x/patches/auto_fixed.patch",
    "skipped": [],
}


def test_all_six_sections_present_in_order():
    localized = [
        _lf(0, "src/foo.py", 10, "high", "applicable", before="x", after="y"),
        _lf(1, "src/foo.py", 20, "medium", "stale", before="z"),
    ]
    text = report.render(RUN, localized)
    headers = ["## Header", "## Summary", "## Findings by severity",
               "## Findings without patch", "## Skipped branches", "## Apply recipe"]
    positions = [text.index(h) for h in headers]
    assert positions == sorted(positions)


def test_empty_skipped_branches_still_renders():
    localized = [_lf(0, "src/foo.py", 10, "high", "applicable", before="x", after="y")]
    text = report.render(RUN, localized)
    assert "## Skipped branches" in text
    assert "None were skipped" in text


def test_findings_without_patch_renders_full_text_not_just_a_count():
    localized = [_lf(0, "src/foo.py", 20, "medium", "stale", before="z")]
    text = report.render(RUN, localized)
    assert "Findings without patch (1)" in text
    assert "src/foo.py:20" in text
    assert "stale" in text
    assert "because" in text  # rationale text present, not just a counter


def test_apply_recipe_carries_head_sha_and_branch():
    localized = [_lf(0, "src/foo.py", 10, "high", "applicable", before="x", after="y")]
    text = report.render(RUN, localized)
    assert "feature/x" in text.split("## Apply recipe")[1]
    assert "abc123" in text.split("## Apply recipe")[1]


def test_no_findings_without_patch_still_renders_with_zero():
    run = dict(RUN, findings_without_patch=0)
    localized = [_lf(0, "src/foo.py", 10, "high", "applicable", before="x", after="y")]
    text = report.render(run, localized)
    assert "Findings without patch (0)" in text


def test_redacted_finding_shows_secret_specific_reason():
    localized = [
        _lf(0, "src/foo.py", 10, "critical", "redacted",
            before='KEY = "<<REDACTED:aws_secret_key>>"'),
    ]
    text = report.render(RUN, localized)
    assert "**redacted**" in text
    assert report.STATUS_REASON["redacted"] in text


def test_summary_states_the_configured_auto_fix_threshold():
    localized = [_lf(0, "src/foo.py", 10, "high", "applicable", before="x", after="y")]
    run = dict(RUN, auto_fix_min_severity="medium")
    text = report.render(run, localized)
    assert "medium" in text.split("## Summary")[1].split("## Findings")[0]


def test_apply_recipe_names_auto_fixed_patch():
    localized = [_lf(0, "src/foo.py", 10, "high", "applicable", before="x", after="y")]
    text = report.render(RUN, localized)
    assert "auto_fixed.patch" in text.split("## Apply recipe")[1]
    assert "critical.patch" not in text
