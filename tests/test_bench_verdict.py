"""The verdict record (tasks.md 1.4): a verdict round-trips, an identical finding resolves to
it, a finding differing in any digested field is unadjudicated, and a duplicate key in a
verdict file is an error rather than a last-one-wins.

The digested-field test is the load-bearing one: model output is not deterministic, so a
re-run may put a different claim at the same file and line. A verdict keyed to a location
would hand that claim a human's judgement about a claim they never saw (design.md D3).
"""
from pathlib import Path

import pytest
import yaml

from bench.verdict import (
    DIGESTED_FIELDS,
    Verdict,
    VerdictError,
    VerdictIndex,
    append_verdict,
    finding_key,
    load_verdict_index,
    load_verdicts,
)

FINDING = {
    "file": "pkg/a.go", "line": 219, "end_line": 221, "severity": "high", "category": "bug",
    "rationale": "the branch check is missing, so a dirty tree is overwritten",
    "before": "x := 1", "after": "x := 2",
    # Not digested: the tool's own bookkeeping about the claim, not the claim itself.
    "confidence": "medium", "status": "stale",
}


def _verdict(**kwargs: object) -> Verdict:
    base = {
        "finding_key": finding_key(FINDING), "true_defect": True, "case_defect": True,
        "reason": "matches the fix commit", "recorded_at": "2026-08-19T12:00:00Z",
    }
    base.update(kwargs)
    return Verdict(**base)  # type: ignore[arg-type]


def test_verdict_round_trips(tmp_path: Path):
    append_verdict(_verdict(), "case-1", tmp_path)
    loaded = load_verdicts(tmp_path / "case-1.yml")
    assert loaded[finding_key(FINDING)] == _verdict()


def test_adjudicated_by_round_trips_and_defaults_to_human(tmp_path: Path):
    """Provenance is the difference between a figure a person checked and one a model checked,
    and a model judge shares the reviewer's blind spots. If it does not survive the file it is
    not evidence of anything."""
    append_verdict(_verdict(adjudicated_by="model"), "case-1", tmp_path)
    loaded = load_verdicts(tmp_path / "case-1.yml")
    assert loaded[finding_key(FINDING)].adjudicated_by == "model"
    assert _verdict().adjudicated_by == "human"


def test_an_unknown_adjudicator_is_rejected(tmp_path: Path):
    append_verdict(_verdict(adjudicated_by="model"), "case-1", tmp_path)
    path = tmp_path / "case-1.yml"
    data = yaml.safe_load(path.read_text())
    data["verdicts"][0]["adjudicated_by"] = "committee"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(VerdictError):
        load_verdicts(path)


def test_identical_finding_resolves_to_its_verdict(tmp_path: Path):
    append_verdict(_verdict(), "case-1", tmp_path)
    index = load_verdict_index(tmp_path)
    # A copy with the keys in a different order is the same claim.
    reordered = dict(reversed(list(FINDING.items())))
    resolved = index.resolve("case-1", reordered)
    assert resolved is not None
    assert resolved.case_defect is True


@pytest.mark.parametrize("field", DIGESTED_FIELDS)
def test_finding_differing_in_any_digested_field_is_unadjudicated(tmp_path: Path, field: str):
    append_verdict(_verdict(), "case-1", tmp_path)
    index = load_verdict_index(tmp_path)
    changed = dict(FINDING)
    changed[field] = 999 if isinstance(FINDING[field], int) else "something else entirely"
    assert index.resolve("case-1", changed) is None


def test_a_dropped_digested_field_is_unadjudicated(tmp_path: Path):
    """A finding that lost its `end_line` is a different claim, not the same one with less
    detail -- it resolves to unadjudicated rather than inheriting the older verdict."""
    append_verdict(_verdict(), "case-1", tmp_path)
    index = load_verdict_index(tmp_path)
    without = {k: v for k, v in FINDING.items() if k != "end_line"}
    assert index.resolve("case-1", without) is None


def test_bookkeeping_fields_do_not_change_the_key():
    other = dict(FINDING, confidence="high", status="applicable")
    assert finding_key(other) == finding_key(FINDING)


def test_duplicate_key_in_a_verdict_file_is_an_error(tmp_path: Path):
    path = tmp_path / "case-1.yml"
    path.write_text(
        "schema_version: 1\n"
        "case_id: case-1\n"
        "verdicts: []\n"
        "verdicts: []\n"
    )
    with pytest.raises(VerdictError):
        load_verdicts(path)


def test_duplicate_verdict_for_the_same_finding_is_an_error(tmp_path: Path):
    entry = {
        "finding_key": finding_key(FINDING), "true_defect": True, "case_defect": True,
        "reason": "r", "recorded_at": "2026-08-19T12:00:00Z",
    }
    path = tmp_path / "case-1.yml"
    path.write_text(yaml.safe_dump(
        {"schema_version": 1, "case_id": "case-1", "verdicts": [entry, dict(entry)]}
    ))
    with pytest.raises(VerdictError):
        load_verdicts(path)


def test_appending_over_an_existing_verdict_is_an_error(tmp_path: Path):
    append_verdict(_verdict(), "case-1", tmp_path)
    with pytest.raises(VerdictError):
        append_verdict(_verdict(reason="second thoughts"), "case-1", tmp_path)


def test_case_defect_without_true_defect_is_rejected(tmp_path: Path):
    path = tmp_path / "case-1.yml"
    path.write_text(yaml.safe_dump({
        "schema_version": 1, "case_id": "case-1",
        "verdicts": [{
            "finding_key": "sha256:abc", "true_defect": False, "case_defect": True,
            "reason": "r", "recorded_at": "2026-08-19T12:00:00Z",
        }],
    }))
    with pytest.raises(VerdictError):
        load_verdicts(path)


def test_case_id_must_match_the_file_name(tmp_path: Path):
    path = tmp_path / "case-1.yml"
    path.write_text(yaml.safe_dump(
        {"schema_version": 1, "case_id": "case-other", "verdicts": []}
    ))
    with pytest.raises(VerdictError):
        load_verdicts(path)


def test_missing_verdicts_dir_is_an_empty_index(tmp_path: Path):
    index = load_verdict_index(tmp_path / "nope")
    assert index == VerdictIndex(by_case={})
    assert index.resolve("case-1", FINDING) is None


def test_appending_is_atomic_enough_to_resume(tmp_path: Path):
    """Two findings adjudicated in separate sittings both survive -- the session appends after
    every answer rather than at the end."""
    append_verdict(_verdict(), "case-1", tmp_path)
    other = dict(FINDING, line=400)
    append_verdict(
        _verdict(finding_key=finding_key(other), case_defect=False), "case-1", tmp_path,
    )
    loaded = load_verdicts(tmp_path / "case-1.yml")
    assert set(loaded) == {finding_key(FINDING), finding_key(other)}
