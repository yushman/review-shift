"""The adjudication command's answer handling (tasks.md 3.2, 3.3). The parsing is the part
worth testing: a misread answer silently records the wrong human judgement, and there is
nothing downstream that could catch it.
"""
from pathlib import Path

import pytest

from bench.adjudicate import Aborted, Item
from bench.case import Case, GroundTruthRange
from bench.cli import _prompt, _yes_no, cmd_adjudicate, main
from bench.scorer import CaseRunResult
from bench.verdict import finding_key, load_verdict_index

FINDING = {
    "file": "a.py", "line": 7, "severity": "high", "category": "bug",
    "rationale": "a claim about a.py",
}


def _case(case_id: str = "c1") -> Case:
    return Case(
        id=case_id, repo="pydantic", fix_sha="fff0000", introducing_sha="bbb1111",
        diff_visible=True, confirmed_at="2026-08-19T00:00:00Z", note="the note",
        ground_truth=(GroundTruthRange(file="a.py", start_line=10, end_line=10),),
        path=None,  # type: ignore[arg-type]
    )


def _item() -> Item:
    return Item(case=_case(), depth="medium", finding=FINDING, key=finding_key(FINDING))


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda *_: queue.pop(0))


def test_yes_no_reads_yes_and_no(monkeypatch: pytest.MonkeyPatch):
    _answers(monkeypatch, "y")
    assert _yes_no("? ") is True
    _answers(monkeypatch, "no")
    assert _yes_no("? ") is False


def test_yes_no_reprompts_on_nonsense(monkeypatch: pytest.MonkeyPatch):
    _answers(monkeypatch, "maybe", "n")
    assert _yes_no("? ") is False


def test_yes_no_quit_aborts(monkeypatch: pytest.MonkeyPatch):
    _answers(monkeypatch, "q")
    with pytest.raises(Aborted):
        _yes_no("? ")


def test_prompt_records_both_booleans_and_the_reason(monkeypatch: pytest.MonkeyPatch):
    _answers(monkeypatch, "y", "y", "matches the fix commit")
    answer = _prompt(_item(), 1, 1)
    assert answer is not None
    assert (answer.true_defect, answer.case_defect) == (True, True)
    assert answer.reason == "matches the fix commit"


def test_prompt_does_not_ask_about_the_case_defect_for_a_false_finding(
    monkeypatch: pytest.MonkeyPatch,
):
    """A finding that is not a defect cannot be the case's defect -- asking would invite the
    pair the verdict loader rejects."""
    _answers(monkeypatch, "n", "not a defect, the guard is upstream")
    answer = _prompt(_item(), 1, 1)
    assert answer is not None
    assert (answer.true_defect, answer.case_defect) == (False, False)


def test_prompt_skip_leaves_the_finding_unadjudicated(monkeypatch: pytest.MonkeyPatch):
    _answers(monkeypatch, "s")
    assert _prompt(_item(), 1, 1) is None


def test_prompt_unrecognised_answer_leaves_the_finding_unadjudicated(
    monkeypatch: pytest.MonkeyPatch,
):
    _answers(monkeypatch, "probably?")
    assert _prompt(_item(), 1, 1) is None


def test_prompt_quit_aborts(monkeypatch: pytest.MonkeyPatch):
    _answers(monkeypatch, "q")
    with pytest.raises(Aborted):
        _prompt(_item(), 1, 1)


def _stub_loaders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    case = _case()
    monkeypatch.setattr("bench.cli.load_cases", lambda: [case])
    monkeypatch.setattr(
        "bench.cli.runner.load_stored_results",
        lambda cases: [CaseRunResult(
            case=case, depth="medium", findings=[FINDING], cost_usd=0.1, status="ok",
        )],
    )
    monkeypatch.setattr("bench.cli.VERDICTS_DIR", tmp_path)
    monkeypatch.setattr("bench.cli.load_verdict_index", lambda: load_verdict_index(tmp_path))


def test_status_reports_outstanding_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    _stub_loaders(monkeypatch, tmp_path)
    assert main(["adjudicate", "--status"]) == 0
    out = capsys.readouterr().out
    assert "c1/medium: 0/1 adjudicated" in out
    assert "total: 0/1 adjudicated, 1 outstanding" in out


def test_adjudicate_writes_the_answer_it_was_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
):
    _stub_loaders(monkeypatch, tmp_path)
    _answers(monkeypatch, "y", "n", "real, but a different defect")
    assert main(["adjudicate"]) == 0
    verdict = load_verdict_index(tmp_path).resolve("c1", FINDING)
    assert verdict is not None
    assert (verdict.true_defect, verdict.case_defect) == (True, False)
    assert verdict.reason == "real, but a different defect"
    assert "recorded 1 verdict(s)" in capsys.readouterr().out


def test_adjudicate_unknown_case_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    _stub_loaders(monkeypatch, tmp_path)
    args = type("A", (), {"case": "nope", "status": False, "list": False})()
    assert cmd_adjudicate(args) == 1  # type: ignore[arg-type]
