"""The adjudication session (tasks.md 3): what is outstanding, what the adjudicator is shown,
that an answer is written as it is given so a sitting can be resumed, and that a skipped
finding stays unadjudicated instead of being pushed into either side.

The answers here come from a stub `ask`. Nothing in `bench/` may produce a verdict on its own:
the judgement is the human's contribution and the only part of the metric a machine cannot
supply.
"""
from pathlib import Path

from bench.adjudicate import (
    Aborted,
    Answer,
    Item,
    coverage_rows,
    format_item,
    outstanding_items,
    run_session,
)
from bench.case import Case, GroundTruthRange
from bench.scorer import CaseRunResult
from bench.verdict import Verdict, VerdictIndex, finding_key, load_verdict_index

FINDING = {
    "file": "pkg/a.go", "line": 219, "severity": "high", "category": "bug",
    "rationale": "the branch check is missing, so a dirty worktree is overwritten",
}
OTHER = dict(FINDING, line=400, rationale="a second claim entirely")


def _case(case_id: str = "c1") -> Case:
    return Case(
        id=case_id, repo="cli", fix_sha="fff0000", introducing_sha="bbb1111",
        diff_visible=True, confirmed_at="2026-08-19T00:00:00Z",
        note="checkout --worktree rewrites the current checkout",
        ground_truth=(GroundTruthRange(file="pkg/a.go", start_line=222, end_line=224),),
        path=None,  # type: ignore[arg-type]
    )


def _result(findings, depth="medium", case=None):
    return CaseRunResult(
        case=case or _case(), depth=depth, findings=findings, cost_usd=1.0, status="ok",
    )


def _index(finding, *, true_defect=True, case_defect=True, case_id="c1") -> VerdictIndex:
    key = finding_key(finding)
    return VerdictIndex(by_case={case_id: {key: Verdict(
        finding_key=key, true_defect=true_defect, case_defect=case_defect,
        reason="fixture", recorded_at="2026-08-19T12:00:00Z",
    )}})


def test_outstanding_lists_only_unadjudicated_findings():
    results = [_result([FINDING, OTHER])]
    items = outstanding_items(results, _index(FINDING))
    assert [i.key for i in items] == [finding_key(OTHER)]


def test_format_item_shows_the_full_rationale_and_the_case_for_comparison():
    item = Item(case=_case(), depth="medium", finding=FINDING, key=finding_key(FINDING))
    text = format_item(item, index=1, total=3)
    assert "[1/3] c1 @ medium" in text
    assert FINDING["rationale"] in text, "a truncated rationale invites a guessed verdict"
    assert "pkg/a.go:219" in text
    assert "checkout --worktree rewrites the current checkout" in text
    assert "pkg/a.go:222-224" in text


def test_session_records_each_answer_as_it_is_given(tmp_path: Path):
    results = [_result([FINDING, OTHER])]
    items = outstanding_items(results, VerdictIndex(by_case={}))

    def ask(item: Item) -> Answer:
        return Answer(true_defect=True, case_defect=item.key == finding_key(FINDING),
                      reason="because")

    assert run_session(items, ask, verdicts_dir=tmp_path) == 2
    index = load_verdict_index(tmp_path)
    assert index.resolve("c1", FINDING) is not None
    assert index.resolve("c1", FINDING).case_defect is True  # type: ignore[union-attr]
    assert index.resolve("c1", OTHER).case_defect is False  # type: ignore[union-attr]


def test_session_is_resumable_after_an_abort(tmp_path: Path):
    """An aborted sitting keeps what came before it, and the next sitting sees only what is
    left -- 26 findings is one sitting, 150 is not."""
    results = [_result([FINDING, OTHER])]

    def ask_first_then_stop(item: Item) -> Answer:
        if item.key == finding_key(FINDING):
            return Answer(true_defect=True, case_defect=True, reason="the labelled one")
        raise Aborted

    items = outstanding_items(results, VerdictIndex(by_case={}))
    assert run_session(items, ask_first_then_stop, verdicts_dir=tmp_path) == 1

    remaining = outstanding_items(results, load_verdict_index(tmp_path))
    assert [i.key for i in remaining] == [finding_key(OTHER)]


def test_a_skipped_finding_stays_unadjudicated(tmp_path: Path):
    results = [_result([FINDING])]
    items = outstanding_items(results, VerdictIndex(by_case={}))
    assert run_session(items, lambda _item: None, verdicts_dir=tmp_path) == 0
    assert load_verdict_index(tmp_path).resolve("c1", FINDING) is None


def test_coverage_rows_report_per_case_and_depth():
    results = [_result([FINDING, OTHER], depth="low"), _result([FINDING], depth="medium")]
    rows = coverage_rows(results, _index(FINDING))
    assert rows == [("c1", "low", 1, 2), ("c1", "medium", 1, 1)]
