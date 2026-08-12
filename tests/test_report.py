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


# --- trunk-review: mode-aware header, attribution, remedy, and the "reviewed nothing" cases -


TRUNK_RUN_BASE = {
    "mode": "trunk", "branch": "main", "head_sha": "headsha123", "base": "main",
    "depth": "medium", "started_at": "2026-08-11T03:30:00Z", "duration_ms": 4321,
    "cost_usd": 0.03, "auto_fix_min_severity": "high",
    "auto_fix_patch_path": ".review-shift/runs/x/patches/auto_fixed.patch",
    "anchor_sha": "anchorsha1",
}


def _trunk_lf(index, file, line, severity, status, commit, author, remedy, before=None, after=None):
    finding = {
        "file": file, "line": line, "severity": severity, "category": "bug",
        "rationale": "because", "confidence": "high",
        "commit": commit, "author": author, "remedy": remedy,
    }
    if before is not None:
        finding["before"] = before
    if after is not None:
        finding["after"] = after
    return LocalizedFinding(index=index, finding=finding, status=status)


def test_trunk_header_carries_mode_anchor_and_counts():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="units", reviewed_count=2, skipped_count=1, gapped_count=1,
        findings_by_severity={"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0,
        units=[
            {"sha": "c1", "status": "ok"}, {"sha": "c2", "status": "ok"},
            {"sha": "c3", "status": "superseded"}, {"sha": "c4", "status": "diff_too_large"},
        ],
    )
    localized = [
        _trunk_lf(0, "a.py", 1, "high", "applicable", "c1", "Alice", "still_local",
                  before="x", after="y"),
    ]
    text = report.render(run, localized)
    header = text.split("## Header")[1].split("## Summary")[0]
    assert "mode: `trunk`" in header
    assert "anchor_sha: `anchorsha1`" in header
    assert "reviewed: 2, skipped: 1, gapped: 1" in header


def test_trunk_finding_carries_commit_author_and_remedy():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="units", reviewed_count=1, skipped_count=0, gapped_count=0,
        findings_by_severity={"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0,
        units=[{"sha": "c1", "status": "ok"}],
    )
    localized = [
        _trunk_lf(0, "a.py", 1, "high", "applicable", "c1abc", "Alice", "still_local",
                  before="x", after="y"),
    ]
    text = report.render(run, localized)
    findings_section = (
        text.split("## Findings by severity")[1].split("## Findings without patch")[0]
    )
    assert "c1abc" in findings_section
    assert "Alice" in findings_section
    assert "still local" in findings_section


def test_trunk_finding_already_pushed_states_fix_forward_only():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="units", reviewed_count=1, skipped_count=0, gapped_count=0,
        findings_by_severity={"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0,
        units=[{"sha": "c1", "status": "ok"}],
    )
    localized = [
        _trunk_lf(0, "a.py", 1, "high", "applicable", "c1abc", "Bob", "pushed",
                  before="x", after="y"),
    ]
    text = report.render(run, localized)
    findings_section = text.split("## Findings by severity")[1]
    assert "fix forward only" in findings_section


def test_trunk_finding_unknown_pushed_state_does_not_claim_a_remedy():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="units", reviewed_count=1, skipped_count=0, gapped_count=0,
        findings_by_severity={"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0,
        units=[{"sha": "c1", "status": "ok"}],
    )
    localized = [
        _trunk_lf(0, "a.py", 1, "high", "applicable", "c1abc", "Bob", "unknown",
                  before="x", after="y"),
    ]
    text = report.render(run, localized)
    findings_section = text.split("## Findings by severity")[1]
    assert "pushed state unknown" in findings_section


def test_trunk_skipped_and_gapped_commits_render_in_fifth_section():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="units", reviewed_count=1, skipped_count=1, gapped_count=1,
        findings_by_severity={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0,
        units=[
            {"sha": "c1", "status": "ok"},
            {"sha": "c2", "status": "superseded"},
            {"sha": "c3", "status": "diff_too_large"},
        ],
    )
    text = report.render(run, [])
    headers = ["## Header", "## Summary", "## Findings by severity",
               "## Findings without patch", "## Skipped branches", "## Apply recipe"]
    positions = [text.index(h) for h in headers]
    assert positions == sorted(positions)  # section order/count unchanged (spec requirement)

    skipped_section = text.split("## Skipped branches")[1].split("## Apply recipe")[0]
    assert "c2" in skipped_section and "superseded" in skipped_section
    assert "c3" in skipped_section and "diff_too_large" in skipped_section
    assert "c1" not in skipped_section  # the reviewed unit doesn't belong in the skip section


def test_trunk_bootstrap_run_states_reason_not_a_clean_review():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="bootstrap", anchor_sha=None,
        reviewed_count=0, skipped_count=0, gapped_count=0,
        findings_by_severity={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0, units=[],
    )
    text = report.render(run, [])
    summary = text.split("## Summary")[1].split("## Findings")[0]
    assert "bootstrap" in summary
    assert "no commit was reviewed" in summary


def test_trunk_nothing_new_run_states_reason_distinct_from_no_findings():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="nothing_new",
        reviewed_count=0, skipped_count=0, gapped_count=0,
        findings_by_severity={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0, units=[],
    )
    text = report.render(run, [])
    summary = text.split("## Summary")[1].split("## Findings")[0]
    assert "nothing_new" in summary


def test_trunk_budget_exhausted_from_first_unit_is_stated_in_summary():
    run = dict(
        TRUNK_RUN_BASE, trunk_outcome="units", reviewed_count=0, skipped_count=0, gapped_count=0,
        findings_by_severity={"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        findings_without_patch=0,
        units=[{"sha": "c1", "status": "budget_exhausted"}],
    )
    text = report.render(run, [])
    summary = text.split("## Summary")[1].split("## Findings")[0]
    assert "budget exhausted" in summary
