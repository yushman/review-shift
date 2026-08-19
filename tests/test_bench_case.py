"""Case-file parsing and validation, including the unconfirmed-case rule (tasks.md 7.2, spec
"Label independence")."""
from pathlib import Path

import pytest

from bench.case import CaseError, is_confirmed, load_case, load_cases

VALID = {
    "id": "case-001",
    "repo": "pydantic",
    "fix_sha": "abc1234",
    "introducing_sha": "def5678",
    "diff_visible": True,
    "confirmed_at": None,
    "note": "off-by-one in validator bound",
    "ground_truth": [{"file": "pydantic/validators.py", "start_line": 10, "end_line": 12}],
}


def _write(tmp_path: Path, data: dict, name: str = "case.yml") -> Path:
    import yaml

    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def test_valid_case_loads(tmp_path: Path):
    path = _write(tmp_path, VALID)
    case = load_case(path)
    assert case.id == "case-001"
    assert case.confirmed_at is None
    assert case.ground_truth[0].file == "pydantic/validators.py"


def test_missing_required_field_is_rejected(tmp_path: Path):
    data = dict(VALID)
    del data["fix_sha"]
    path = _write(tmp_path, data)
    with pytest.raises(CaseError):
        load_case(path)


def test_unknown_field_is_rejected(tmp_path: Path):
    data = dict(VALID)
    data["extra"] = "nope"
    path = _write(tmp_path, data)
    with pytest.raises(CaseError):
        load_case(path)


def test_end_line_before_start_line_is_rejected(tmp_path: Path):
    data = dict(VALID)
    data["ground_truth"] = [{"file": "f.py", "start_line": 10, "end_line": 5}]
    path = _write(tmp_path, data)
    with pytest.raises(CaseError):
        load_case(path)


def test_non_mapping_top_level_is_rejected(tmp_path: Path):
    path = tmp_path / "case.yml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(CaseError):
        load_case(path)


def test_load_cases_reads_every_yml_in_dir_sorted(tmp_path: Path):
    a = dict(VALID)
    a["id"] = "aaa"
    b = dict(VALID)
    b["id"] = "bbb"
    _write(tmp_path, a, "aaa.yml")
    _write(tmp_path, b, "bbb.yml")
    cases = load_cases(tmp_path)
    assert [c.id for c in cases] == ["aaa", "bbb"]


def test_unconfirmed_when_confirmed_at_absent(tmp_path: Path):
    case = load_case(_write(tmp_path, VALID))
    assert is_confirmed(case) is False


def test_confirmed_when_confirmed_at_set_and_no_run_yet(tmp_path: Path):
    data = dict(VALID)
    data["confirmed_at"] = "2026-08-19T00:00:00Z"
    case = load_case(_write(tmp_path, data))
    assert is_confirmed(case) is True


def test_confirmed_when_confirmation_precedes_first_run(tmp_path: Path):
    data = dict(VALID)
    data["confirmed_at"] = "2026-08-19T00:00:00Z"
    case = load_case(_write(tmp_path, data))
    assert is_confirmed(case, first_run_at="2026-08-19T01:00:00Z") is True


def test_unconfirmed_when_confirmation_follows_first_run(tmp_path: Path):
    """Spec "Label independence": a confirmation timestamp later than the first recorded run
    means the label cannot be trusted as independent of the findings it labels."""
    data = dict(VALID)
    data["confirmed_at"] = "2026-08-19T02:00:00Z"
    case = load_case(_write(tmp_path, data))
    assert is_confirmed(case, first_run_at="2026-08-19T01:00:00Z") is False
