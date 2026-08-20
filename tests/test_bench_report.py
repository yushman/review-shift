"""Report rendering (tasks.md 4): case counts present, unpublishable marking, both tolerances
present, localization labelled for what it measures, precision now emitted, and a metric with
nothing adjudicated printed as uncomputed rather than as `0%`.
"""
from bench.case import Case, GroundTruthRange
from bench.report import CORPUS_TARGET, render
from bench.scorer import CaseRunResult
from bench.verdict import Verdict, VerdictIndex, finding_key

FINDING = {"file": "a.py", "line": 1, "rationale": "the labelled defect"}


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
            case=_case(f"c{i}"), depth="medium", findings=[dict(FINDING)],
            cost_usd=0.5, status="ok",
        )
        for i in range(n)
    ]


def _verdicts(n: int, *, true_defect: bool = True, case_defect: bool = True) -> VerdictIndex:
    key = finding_key(FINDING)
    verdict = Verdict(
        finding_key=key, true_defect=true_defect, case_defect=case_defect,
        reason="fixture", recorded_at="2026-08-19T12:00:00Z",
    )
    return VerdictIndex(by_case={f"c{i}": {key: verdict} for i in range(n)})


def _empty() -> VerdictIndex:
    return VerdictIndex(by_case={})


def _model_verdicts(n: int) -> VerdictIndex:
    key = finding_key(FINDING)
    verdict = Verdict(
        finding_key=key, true_defect=True, case_defect=True,
        reason="fixture", recorded_at="2026-08-19T12:00:00Z", adjudicated_by="model",
    )
    return VerdictIndex(by_case={f"c{i}": {key: verdict} for i in range(n)})


def test_model_adjudicated_verdicts_are_stamped_on_the_report():
    """A model judge shares the reviewer's blind spots, so every figure below the stamp is
    weaker evidence than a human-judged one. A reader who cannot see that will quote it as if
    a person had checked."""
    text = render(_results(3), _model_verdicts(3))
    assert "MODEL-ADJUDICATED: 3/3" in text


def test_human_adjudicated_verdicts_carry_no_stamp():
    text = render(_results(3), _verdicts(3))
    assert "MODEL-ADJUDICATED" not in text


def test_report_shows_case_count():
    text = render(_results(3), _verdicts(3))
    assert "cases attempted: 3" in text


def test_report_below_target_is_marked_unpublishable():
    assert CORPUS_TARGET > 3
    text = render(_results(3), _verdicts(3))
    assert "UNPUBLISHABLE" in text


def test_report_at_target_is_not_marked_unpublishable_for_case_count():
    text = render(_results(CORPUS_TARGET), _verdicts(CORPUS_TARGET))
    assert f"cases attempted: {CORPUS_TARGET}" in text
    lines = text.splitlines()
    header_line = next(line for line in lines if line.startswith("cases attempted"))
    idx = lines.index(header_line)
    assert "UNPUBLISHABLE" not in lines[idx + 1]


def test_report_shows_both_tolerances_under_localization():
    text = render(_results(3), _verdicts(3))
    assert "tolerance=0" in text
    assert "tolerance=10" in text
    assert "## localization -- patch anchoring, not review quality" in text


def test_report_emits_yield_and_precision():
    """The prohibition on precision is removed: with a verdict on every finding, an unmatched
    finding is recorded as true or false rather than inferred as false."""
    text = render(_results(3), _verdicts(3))
    assert "yield" in text
    assert "precision" in text


def test_report_shows_uncomputed_rather_than_zero_when_nothing_is_adjudicated():
    text = render(_results(3), _empty())
    assert "uncomputed" in text
    assert "outstanding" in text
    recall_lines = [line for line in text.splitlines() if line.strip().startswith("medium:")]
    assert recall_lines, text
    assert all("0%" not in line for line in recall_lines), recall_lines


def test_report_shows_adjudication_coverage_beside_the_figure():
    text = render(_results(3), _verdicts(3))
    assert "adjudicated 3/3 findings" in text
    assert "adjudicated 3/3 cases" in text


def test_report_flags_pre_relabel_depths():
    results = _results(1)
    results[0].depth = "high"
    text = render(results, _verdicts(1))
    assert "PRE-RELABEL DEPTHS" in text
    assert "high" in text


def test_report_shows_failed_cases_not_completed():
    results = _results(2)
    results.append(
        CaseRunResult(
            case=_case("c-failed"), depth="medium", findings=None, cost_usd=0.0,
            status="budget_exhausted", reason=None,
        )
    )
    text = render(results, _verdicts(2))
    assert "not completed" in text
    assert "c-failed" in text
