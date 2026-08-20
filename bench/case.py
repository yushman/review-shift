"""Bench case format: metadata and pinned shas, never vendored code (design.md D2). A case
records the corpus repository, the fix and introducing commits, ground-truth line ranges, the
`diff_visible` label and the confirmation timestamp (design.md D3). Mechanical derivation
(`bench/derive.py`) produces drafts with `confirmed_at` unset; only a human sets it, and only
before the case's first run (spec "Ground truth is confirmed by a human before the case is
first run", "Label independence").
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

__all__ = [
    "Case", "CaseError", "GroundTruthRange", "StrictLoader", "is_confirmed", "load_case",
    "load_cases",
]

CASES_DIR = Path(__file__).resolve().parent / "cases"

CASE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id", "repo", "fix_sha", "introducing_sha", "diff_visible", "confirmed_at",
        "note", "ground_truth",
    ],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "repo": {"type": "string", "minLength": 1},
        "fix_sha": {"type": "string", "minLength": 7},
        "introducing_sha": {"type": "string", "minLength": 7},
        "diff_visible": {"type": "boolean"},
        # Empty string is how a draft spells "not yet confirmed" in YAML without `null`
        # round-tripping oddly through some editors; both are accepted as unset.
        "confirmed_at": {"type": ["string", "null"]},
        "note": {"type": "string"},
        "ground_truth": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["file", "start_line", "end_line"],
                "properties": {
                    "file": {"type": "string", "minLength": 1},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class GroundTruthRange:
    file: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Case:
    id: str
    repo: str
    fix_sha: str
    introducing_sha: str
    diff_visible: bool
    confirmed_at: str | None
    note: str
    ground_truth: tuple[GroundTruthRange, ...]
    path: Path


class CaseError(RuntimeError):
    pass


def _parse_case(data: dict[str, Any], path: Path) -> Case:
    try:
        jsonschema.validate(data, CASE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise CaseError(f"{path}: {exc.message}") from exc

    ranges = []
    for r in data["ground_truth"]:
        if r["end_line"] < r["start_line"]:
            raise CaseError(
                f"{path}: ground_truth range for {r['file']} has end_line < start_line"
            )
        ranges.append(
            GroundTruthRange(file=r["file"], start_line=r["start_line"], end_line=r["end_line"])
        )

    confirmed_at = data["confirmed_at"] or None
    return Case(
        id=data["id"], repo=data["repo"], fix_sha=data["fix_sha"],
        introducing_sha=data["introducing_sha"], diff_visible=data["diff_visible"],
        confirmed_at=confirmed_at, note=data["note"], ground_truth=tuple(ranges), path=path,
    )


class StrictLoader(yaml.SafeLoader):
    """`yaml.safe_load` resolves a duplicate key by silently keeping the last one. In a case
    file that loses ground truth without a word -- a second `ground_truth:` block overwrites
    the first -- which is exactly the class of quiet failure this bench exists to measure, so
    a duplicate key is an error here rather than a last-one-wins.

    Public because `bench/verdict.py` loads verdict files with the same loader for the same
    reason: a duplicate key there silently drops a human's judgement.
    """


def _no_duplicate_keys(loader: StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            raise CaseError(f"duplicate key {key!r}")
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


def load_case(path: Path) -> Case:
    try:
        data = yaml.load(path.read_text(), Loader=StrictLoader)
    except CaseError as exc:
        raise CaseError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CaseError(f"{path}: case file must be a mapping at the top level")
    return _parse_case(data, path)


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    return [load_case(p) for p in sorted(cases_dir.glob("*.yml"))]


def is_confirmed(case: Case, first_run_at: str | None = None) -> bool:
    """Spec "Label independence": unconfirmed unless `confirmed_at` is set, and — once a first
    run is on record — unless it precedes that run. ISO 8601 timestamps in the `...Z` form sort
    correctly as plain strings, so no datetime parsing is needed for the comparison.
    """
    if not case.confirmed_at:
        return False
    if first_run_at is None:
        return True
    return case.confirmed_at <= first_run_at
