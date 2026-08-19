"""Scorer unit tests on synthetic findings and ranges (tasks.md 7.1): exact hit, near miss
inside tolerance, miss outside tolerance, wrong file, multi-range case.
"""
from bench.case import Case, GroundTruthRange
from bench.scorer import applicability, case_hit, is_hit, recall


def _case(ranges, diff_visible=True, case_id="c1", repo="pydantic"):
    return Case(
        id=case_id, repo=repo, fix_sha="fff0000", introducing_sha="bbb1111",
        diff_visible=diff_visible, confirmed_at="2026-08-19T00:00:00Z", note="",
        ground_truth=tuple(ranges), path=None,  # type: ignore[arg-type]
    )


def test_exact_hit():
    assert is_hit({"file": "a.py", "line": 10}, "a.py", 10, 10, tolerance=0)


def test_near_miss_inside_tolerance():
    # finding at line 18, range 10..10, distance 8 -> hit at tolerance 10, miss at tolerance 0
    assert is_hit({"file": "a.py", "line": 18}, "a.py", 10, 10, tolerance=10)
    assert not is_hit({"file": "a.py", "line": 18}, "a.py", 10, 10, tolerance=0)


def test_miss_outside_tolerance():
    assert not is_hit({"file": "a.py", "line": 30}, "a.py", 10, 10, tolerance=10)


def test_wrong_file_is_never_a_hit():
    assert not is_hit({"file": "b.py", "line": 10}, "a.py", 10, 10, tolerance=100)


def test_end_line_span_intersects_range():
    assert is_hit({"file": "a.py", "line": 5, "end_line": 15}, "a.py", 10, 10, tolerance=0)


def test_multi_range_case_hits_if_any_range_hits():
    case = _case([
        GroundTruthRange(file="a.py", start_line=1, end_line=2),
        GroundTruthRange(file="b.py", start_line=50, end_line=52),
    ])
    findings = [{"file": "b.py", "line": 51}]
    assert case_hit(findings, case, tolerance=0)


def test_multi_range_case_misses_if_no_range_hits():
    case = _case([
        GroundTruthRange(file="a.py", start_line=1, end_line=2),
        GroundTruthRange(file="b.py", start_line=50, end_line=52),
    ])
    findings = [{"file": "c.py", "line": 51}]
    assert not case_hit(findings, case, tolerance=0)


def test_recall_counts_only_completed_cases():
    from bench.scorer import CaseRunResult

    case1 = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)], case_id="c1")
    case2 = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)], case_id="c2")
    results = [
        CaseRunResult(case=case1, depth="medium", findings=[{"file": "a.py", "line": 1}],
                      cost_usd=1.0, status="ok"),
        CaseRunResult(case=case2, depth="medium", findings=None, cost_usd=0.0,
                      status="budget_exhausted"),
    ]
    rate, n = recall(results, "medium", tolerance=0)
    assert (rate, n) == (1.0, 1)


def test_recall_diff_visible_subset_excludes_invisible_cases():
    from bench.scorer import CaseRunResult

    visible = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)],
                     diff_visible=True, case_id="v")
    invisible = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)],
                       diff_visible=False, case_id="iv")
    results = [
        CaseRunResult(case=visible, depth="medium", findings=[], cost_usd=1.0, status="ok"),
        CaseRunResult(case=invisible, depth="medium", findings=[], cost_usd=1.0, status="ok"),
    ]
    _, n_all = recall(results, "medium", tolerance=0)
    _, n_dv = recall(results, "medium", tolerance=0, diff_visible_only=True)
    assert n_all == 2
    assert n_dv == 1


def _run_with_patches(tmp_path, case, names):
    from bench.scorer import CaseRunResult

    patches = tmp_path / "patches"
    patches.mkdir(parents=True, exist_ok=True)
    for name in names:
        (patches / name).write_text("diff --git a/a b/a\n")
    return CaseRunResult(
        case=case, depth="medium", findings=[], cost_usd=1.0, status="ok",
        run_dir=tmp_path, repo_dir=tmp_path / "repo", head_sha="deadbeef",
    )


def test_applicability_counts_patch_files_checked_independently(tmp_path):
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    result = _run_with_patches(tmp_path, case, ["all.patch", "auto_fixed.patch"])

    # Only `all.patch` applies -- the checker is injected so the metric is exercised without git.
    def check(repo_dir, head_sha, patch):
        return patch.name == "all.patch"

    assert applicability([result], check=check) == (0.5, 2)


def test_applicability_ignores_findings_status_field(tmp_path):
    """The tool's own `status: applicable` must not influence the metric -- a bench that trusts
    the subject's self-report cannot catch a defect in the subject's verification path."""
    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    result = _run_with_patches(tmp_path, case, ["all.patch"])
    result.findings = [
        {"file": "a.py", "line": 1, "before": "x", "after": "y", "status": "applicable"},
    ]
    assert applicability([result], check=lambda *_: False) == (0.0, 1)


def test_applicability_skips_runs_without_a_run_dir():
    from bench.scorer import CaseRunResult

    case = _case([GroundTruthRange(file="a.py", start_line=1, end_line=1)])
    result = CaseRunResult(
        case=case, depth="medium", findings=None, cost_usd=0.0, status="review_failed",
    )
    assert applicability([result], check=lambda *_: True) == (0.0, 0)
