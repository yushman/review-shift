"""Scorer unit tests on synthetic findings, ranges and verdicts (tasks.md 2.6, 2.7).

Two groups matter most. The first reproduces the three anomalies the split exists for: a
correct finding four lines off the ground truth, a wrong finding three lines inside it, and a
true defect the corpus never labelled. The second asserts, rather than assumes, that an
unadjudicated finding stays out of both the numerator and the denominator of every metric --
folding it into either side is the quiet failure this change is about.

The verdicts here are invented for obviously synthetic findings. Real verdicts about real
stored findings are the adjudicator's work and live in `bench/verdicts/`.
"""
from pathlib import Path

from bench.case import Case, GroundTruthRange
from bench.scorer import (
    CaseRunResult,
    applicability,
    detected,
    is_localized,
    localization,
    precision,
    recall,
    yield_per_case,
)
from bench.verdict import Verdict, VerdictIndex, finding_key


def _case(ranges, diff_visible=True, case_id="c1", repo="pydantic", note=""):
    return Case(
        id=case_id, repo=repo, fix_sha="fff0000", introducing_sha="bbb1111",
        diff_visible=diff_visible, confirmed_at="2026-08-19T00:00:00Z", note=note,
        ground_truth=tuple(ranges), path=None,  # type: ignore[arg-type]
    )


def _index(*pairs):
    """`(case_id, finding, true_defect, case_defect)` tuples into a VerdictIndex."""
    by_case: dict[str, dict[str, Verdict]] = {}
    for case_id, finding, true_defect, case_defect in pairs:
        by_case.setdefault(case_id, {})[finding_key(finding)] = Verdict(
            finding_key=finding_key(finding), true_defect=true_defect,
            case_defect=case_defect, reason="fixture", recorded_at="2026-08-19T12:00:00Z",
        )
    return VerdictIndex(by_case=by_case)


def _result(case, findings, depth="medium", status="ok"):
    return CaseRunResult(
        case=case, depth=depth, findings=findings, cost_usd=1.0, status=status,
    )


# --- localization arithmetic, unchanged apart from its name and its scope ---------------

def test_exact_localization():
    assert is_localized({"file": "a.py", "line": 10}, "a.py", 10, 10, tolerance=0)


def test_near_miss_inside_tolerance():
    assert is_localized({"file": "a.py", "line": 18}, "a.py", 10, 10, tolerance=10)
    assert not is_localized({"file": "a.py", "line": 18}, "a.py", 10, 10, tolerance=0)


def test_miss_outside_tolerance():
    assert not is_localized({"file": "a.py", "line": 30}, "a.py", 10, 10, tolerance=10)


def test_wrong_file_is_never_localized():
    assert not is_localized({"file": "b.py", "line": 10}, "a.py", 10, 10, tolerance=100)


def test_end_line_span_intersects_range():
    assert is_localized({"file": "a.py", "line": 5, "end_line": 15}, "a.py", 10, 10, tolerance=0)


# --- the three anomalies from the proposal -----------------------------------------------

def test_correct_finding_four_lines_off_is_detected_and_a_localization_miss():
    """pydantic-001 shape: ground truth 1891, a finding at 1887 that describes the same defect
    with a repro. Line arithmetic scored it a miss at tolerance 0; the verdict decides now."""
    case = _case([GroundTruthRange(file="p.py", start_line=1891, end_line=1891)])
    finding = {"file": "p.py", "line": 1887, "rationale": "compare_digest on non-ASCII"}
    verdicts = _index((case.id, finding, True, True))
    results = [_result(case, [finding])]

    assert detected([finding], verdicts, case) is True
    assert recall(results, verdicts, "medium").value == 1.0
    assert localization(results, verdicts, 0).value == 0.0
    assert localization(results, verdicts, 10).value == 1.0


def test_wrong_finding_three_lines_off_is_neither_detected_nor_localized():
    """cli-001 shape: ground truth 222, a finding at 219 about a different defect. Tolerance 10
    scored it a hit; it must now count for neither detection nor localization."""
    case = _case([GroundTruthRange(file="c.go", start_line=222, end_line=222)])
    finding = {"file": "c.go", "line": 219, "rationale": "a missing branch check, elsewhere"}
    verdicts = _index((case.id, finding, True, False))
    results = [_result(case, [finding])]

    assert detected([finding], verdicts, case) is False
    assert recall(results, verdicts, "medium").value == 0.0
    loc = localization(results, verdicts, 10)
    assert loc.value is None
    assert loc.n == 0


def test_true_unlabelled_defect_raises_yield_and_leaves_recall_alone():
    """The structural failure: a run reporting several true defects was credited with at most
    one, because the corpus labels one defect per case."""
    case = _case([GroundTruthRange(file="c.go", start_line=222, end_line=222)])
    labelled = {"file": "c.go", "line": 222, "rationale": "the labelled defect"}
    unlabelled = {"file": "c.go", "line": 40, "rationale": "--worktree . rewrites the checkout"}
    verdicts = _index(
        (case.id, labelled, True, True),
        (case.id, unlabelled, True, False),
    )
    with_extra = [_result(case, [labelled, unlabelled])]
    without_extra = [_result(case, [labelled])]

    assert recall(with_extra, verdicts, "medium").value == 1.0
    assert recall(without_extra, verdicts, "medium").value == 1.0
    assert yield_per_case(with_extra, verdicts).value == 2.0
    assert yield_per_case(without_extra, verdicts).value == 1.0
    assert precision(with_extra, verdicts).value == 1.0


# --- unadjudicated is a third state, in every metric ---------------------------------------

def test_detection_is_tri_state():
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    judged_no = {"file": "a.py", "line": 1, "rationale": "not the case's defect"}
    unjudged = {"file": "a.py", "line": 9, "rationale": "nobody has looked at this yet"}
    verdicts = _index((case.id, judged_no, True, False))

    assert detected([judged_no], verdicts, case) is False
    assert detected([judged_no, unjudged], verdicts, case) is None
    assert detected([], verdicts, case) is False, "an empty run is a miss, not a gap"


def test_recall_excludes_unadjudicated_cases_from_both_sides():
    case_hit = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)], case_id="hit")
    case_open = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)], case_id="open")
    found = {"file": "a.py", "line": 1, "rationale": "found it"}
    unjudged = {"file": "a.py", "line": 1, "rationale": "unjudged"}
    verdicts = _index((case_hit.id, found, True, True))
    results = [_result(case_hit, [found]), _result(case_open, [unjudged])]

    metric = recall(results, verdicts, "medium")
    assert metric.value == 1.0, "the unjudged case must not be a miss"
    assert metric.n == 1, "nor may it sit in the denominator"
    assert metric.outstanding == 1


def test_metrics_with_nothing_adjudicated_are_uncomputed_not_zero():
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    finding = {"file": "a.py", "line": 1, "rationale": "unjudged"}
    verdicts = VerdictIndex(by_case={})
    results = [_result(case, [finding])]

    for metric in (
        recall(results, verdicts, "medium"),
        yield_per_case(results, verdicts),
        precision(results, verdicts),
        localization(results, verdicts, 0),
    ):
        assert metric.value is None, "0% would read as a measured failure"
        assert metric.computed is False
        assert metric.outstanding >= 1


def test_precision_ignores_unadjudicated_findings():
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    good = {"file": "a.py", "line": 1, "rationale": "real"}
    bad = {"file": "a.py", "line": 2, "rationale": "noise"}
    unjudged = {"file": "a.py", "line": 3, "rationale": "unjudged"}
    verdicts = _index((case.id, good, True, True), (case.id, bad, False, False))
    results = [_result(case, [good, bad, unjudged])]

    metric = precision(results, verdicts)
    assert metric.value == 0.5
    assert metric.n == 2
    assert metric.adjudicated == 2
    assert metric.outstanding == 1


def test_yield_counts_only_adjudicated_true_defects_and_reports_coverage():
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    good = {"file": "a.py", "line": 1, "rationale": "real"}
    unjudged = {"file": "a.py", "line": 3, "rationale": "unjudged"}
    verdicts = _index((case.id, good, True, True))
    results = [_result(case, [good, unjudged])]

    metric = yield_per_case(results, verdicts)
    assert metric.value == 1.0
    assert metric.n == 1
    assert (metric.adjudicated, metric.outstanding) == (1, 1)


def test_localization_ignores_findings_that_are_not_the_cases_defect():
    case = _case([GroundTruthRange(file="a.py", start_line=10, end_line=10)])
    elsewhere = {"file": "a.py", "line": 10, "rationale": "a different defect, same line"}
    verdicts = _index((case.id, elsewhere, True, False))
    results = [_result(case, [elsewhere])]

    assert localization(results, verdicts, 0).n == 0


# --- carried over from the line-based scorer ----------------------------------------------

def test_recall_counts_only_completed_cases():
    case1 = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)], case_id="c1")
    case2 = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)], case_id="c2")
    found = {"file": "a.py", "line": 1, "rationale": "found"}
    verdicts = _index((case1.id, found, True, True))
    results = [
        _result(case1, [found]),
        CaseRunResult(case=case2, depth="medium", findings=None, cost_usd=0.0,
                      status="budget_exhausted"),
    ]
    metric = recall(results, verdicts, "medium")
    assert (metric.value, metric.n) == (1.0, 1)


def test_recall_diff_visible_subset_excludes_invisible_cases():
    visible = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)],
                    diff_visible=True, case_id="v")
    invisible = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)],
                      diff_visible=False, case_id="iv")
    verdicts = VerdictIndex(by_case={})
    results = [_result(visible, []), _result(invisible, [])]

    assert recall(results, verdicts, "medium").n == 2
    assert recall(results, verdicts, "medium", diff_visible_only=True).n == 1


def test_multi_range_case_localizes_if_any_range_matches():
    case = _case([
        GroundTruthRange(file="a.py", start_line=1, end_line=2),
        GroundTruthRange(file="b.py", start_line=50, end_line=52),
    ])
    finding = {"file": "b.py", "line": 51, "rationale": "the second range"}
    verdicts = _index((case.id, finding, True, True))
    assert localization([_result(case, [finding])], verdicts, 0).value == 1.0


def _run_with_patches(tmp_path, case, names):
    patches = tmp_path / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    for name in names:
        (patches / name).write_text("diff --git a/a b/a\n")
    return CaseRunResult(
        case=case, depth="medium", findings=[], cost_usd=1.0, status="ok",
        run_dir=tmp_path, repo_dir=tmp_path / "repo", head_sha="deadbeef",
    )


def test_applicability_counts_patch_files_checked_independently(tmp_path: Path):
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    result = _run_with_patches(tmp_path, case, ["all.patch", "auto_fixed.patch"])

    # Only `all.patch` applies -- the checker is injected so the metric is exercised without git.
    def check(repo_dir, head_sha, patch):
        return patch.name == "all.patch"

    assert applicability([result], check=check) == (0.5, 2)


def test_applicability_ignores_findings_status_field(tmp_path: Path):
    """The tool's own `status: applicable` must not influence the metric -- a bench that trusts
    the subject's self-report cannot catch a defect in the subject's verification path."""
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    result = _run_with_patches(tmp_path, case, ["all.patch"])
    result.findings = [
        {"file": "a.py", "line": 1, "before": "x", "after": "y", "status": "applicable"},
    ]
    assert applicability([result], check=lambda *_: False) == (0.0, 1)


def test_applicability_skips_runs_without_a_run_dir():
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    result = CaseRunResult(
        case=case, depth="medium", findings=None, cost_usd=0.0, status="review_failed",
    )
    assert applicability([result], check=lambda *_: True) == (0.0, 0)
