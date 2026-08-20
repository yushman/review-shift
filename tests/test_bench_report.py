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


def test_report_wide_interval_is_marked_by_width_not_corpus_size():
    """design.md D2: a rate is marked as supporting no claim by its own interval width, and
    the mark names the width -- not the corpus target."""
    text = render(_results(3), _verdicts(3))
    recall_line = next(
        line for line in text.splitlines() if line.strip().startswith("medium:")
    )
    assert "UNPUBLISHABLE" in recall_line
    assert "interval width" in recall_line
    assert "corpus target" not in recall_line


def test_report_narrow_interval_from_a_small_corpus_is_not_marked():
    """design.md D2's second scenario: a rate whose own interval is narrow enough is not
    marked, even though the corpus is far below the growth target of 30 -- corpus size is no
    longer what the rule tests."""
    n = 10
    assert n < CORPUS_TARGET
    text = render(_results(n), _verdicts(n))
    recall_line = next(
        line for line in text.splitlines() if line.strip().startswith("medium:")
    )
    assert "100%" in recall_line
    assert "UNPUBLISHABLE" not in recall_line


def test_report_shows_cases_attempted_without_a_corpus_size_gate():
    """The old behaviour marked the whole report unpublishable below `CORPUS_TARGET`. That
    guard is gone -- each figure now carries its own -- so the case count is informational."""
    text = render(_results(3), _verdicts(3))
    header_line = next(
        line for line in text.splitlines() if line.startswith("cases attempted")
    )
    assert "3" in header_line
    assert "UNPUBLISHABLE" not in header_line


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


def test_report_shows_paired_comparison_as_the_detection_headline():
    text = render(_results(6), _verdicts(6))
    assert "### paired comparison across depths (headline)" in text
    lines = text.splitlines()
    detection_idx = lines.index("## detection (verdict-based)")
    paired_idx = lines.index("### paired comparison across depths (headline)")
    recall_idx = next(
        i for i, line in enumerate(lines)
        if line.startswith("### recall")
    )
    assert detection_idx < paired_idx < recall_idx


def test_report_shows_undetected_everywhere_count_next_to_the_paired_block():
    text = render(_results(3), _verdicts(3))
    assert "undetected at every depth" in text


def test_report_rate_shows_interval_and_states_the_confidence_level():
    text = render(_results(6), _verdicts(6))
    recall_line = next(
        line for line in text.splitlines() if line.strip().startswith("medium:")
    )
    assert "95% CI" in recall_line
    assert ".." in recall_line


def test_report_yield_shows_numerator_denominator_and_uncertainty_note_no_interval():
    text = render(_results(3), _verdicts(3))
    yield_line = next(
        line for line in text.splitlines()
        if line.strip().startswith("medium:") and "per case" in line
    )
    assert "true defects" in yield_line
    assert "uncertainty not quantified" in yield_line
    assert "CI" not in yield_line, "yield must never receive a Wilson interval (design.md D5)"


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
