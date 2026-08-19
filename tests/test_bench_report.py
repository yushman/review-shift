"""Report rendering (tasks.md 7.3): case counts present, unpublishable marking, both
tolerances present, no precision field (design.md D6)."""
from bench.case import Case, GroundTruthRange
from bench.report import CORPUS_TARGET, render
from bench.scorer import CaseRunResult


def _case(case_id: str, repo: str = "pydantic") -> Case:
    return Case(
        id=case_id, repo=repo, fix_sha="fff0000", introducing_sha="bbb1111",
        diff_visible=True, confirmed_at="2026-08-19T00:00:00Z", note="",
        ground_truth=(GroundTruthRange(file="a.py", start_line=1, end_line=1),),
        path=None,  # type: ignore[arg-type]
    )


def _results(n: int) -> list[CaseRunResult]:
    return [
        CaseRunResult(
            case=_case(f"c{i}"), depth="high", findings=[{"file": "a.py", "line": 1}],
            cost_usd=0.5, status="ok",
        )
        for i in range(n)
    ]


def test_report_shows_case_count():
    text = render(_results(3))
    assert "cases attempted: 3" in text


def test_report_below_target_is_marked_unpublishable():
    assert CORPUS_TARGET > 3
    text = render(_results(3))
    assert "UNPUBLISHABLE" in text


def test_report_at_target_is_not_marked_unpublishable_for_case_count():
    text = render(_results(CORPUS_TARGET))
    assert f"cases attempted: {CORPUS_TARGET}" in text
    lines = text.splitlines()
    header_line = next(line for line in lines if line.startswith("cases attempted"))
    idx = lines.index(header_line)
    assert "UNPUBLISHABLE" not in lines[idx + 1]


def test_report_shows_both_tolerances():
    text = render(_results(3))
    assert "tolerance=0" in text
    assert "tolerance=10" in text


def test_report_never_mentions_precision():
    text = render(_results(3))
    assert "precision" not in text.lower()


def test_report_shows_failed_cases_not_completed():
    results = _results(2)
    results.append(
        CaseRunResult(
            case=_case("c-failed"), depth="high", findings=None, cost_usd=0.0,
            status="budget_exhausted", reason=None,
        )
    )
    text = render(results)
    assert "not completed" in text
    assert "c-failed" in text
