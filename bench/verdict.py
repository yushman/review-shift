"""Per-finding verdicts: the human judgement the scorer cannot make (design.md D2, D3, D4).

A verdict answers two independent questions about one finding -- is it a real defect, and is
it the defect its case is about -- and carries the reasoning that produced the answer. It is
stored as data and never derived at scoring time, so scoring stays deterministic, costs
nothing and can be replayed over findings already on disk.

The two questions are separate rather than one enum because "a real defect the corpus never
labelled" is the common outcome: it raises yield and precision and must leave recall alone.

A verdict is keyed to a digest of the finding's own content, never to its position. Model
output is not deterministic, so a re-run may put a different claim at the same file and line;
a location key would hand that new claim a human's judgement about a claim they never saw.
A finding whose content changed therefore resolves to unadjudicated, which makes
re-adjudication after a prompt change visible work rather than an invisible omission.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from bench.case import CaseError, StrictLoader

__all__ = [
    "DIGESTED_FIELDS", "VERDICTS_DIR", "Finding", "Verdict", "VerdictError", "VerdictIndex",
    "append_verdict", "finding_key", "load_verdict_index", "load_verdicts", "utcnow",
]

Finding = dict[str, Any]

VERDICTS_DIR = Path(__file__).resolve().parent / "verdicts"

# The fields that make a finding a distinct claim. `confidence` and `status` are excluded:
# they describe the tool's own bookkeeping about the claim, not the claim itself.
DIGESTED_FIELDS = (
    "file", "line", "end_line", "severity", "category", "rationale", "before", "after",
)

VERDICT_FILE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "case_id", "verdicts"],
    "properties": {
        "schema_version": {"const": 1},
        "case_id": {"type": "string", "minLength": 1},
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "finding_key", "true_defect", "case_defect", "reason", "recorded_at",
                    "adjudicated_by",
                ],
                "properties": {
                    "finding_key": {"type": "string", "minLength": 1},
                    "true_defect": {"type": "boolean"},
                    "case_defect": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "recorded_at": {"type": "string", "minLength": 1},
                    "adjudicated_by": {"enum": ["human", "model"]},
                },
            },
        },
    },
}


class VerdictError(RuntimeError):
    pass


@dataclass(frozen=True)
class Verdict:
    """`true_defect` and `case_defect` are two judgements, not one scale. The combination
    `case_defect` without `true_defect` is the one the loader rejects: the case's defect is a
    real defect by construction, so that pair can only be a mistake."""

    finding_key: str
    true_defect: bool
    case_defect: bool
    reason: str
    recorded_at: str
    # design.md D2 chose a human judge precisely so the judge would not be the same kind of
    # system as the reviewer. When that is overridden, the record has to say so: a
    # model-adjudicated figure carries the reviewer's blind spots into its own scoring, and a
    # later reader who cannot tell the two apart will quote it as if a person had checked.
    adjudicated_by: str = "human"


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def finding_key(finding: Finding) -> str:
    """A digest over the fields that make the finding a distinct claim, stable across dict
    ordering (`sort_keys`) and across the encoding of non-ASCII rationales (`ensure_ascii`
    off, then UTF-8). A missing field digests as `null`, so a finding that gained an
    `end_line` is a different claim and resolves to unadjudicated rather than inheriting the
    older verdict.
    """
    payload = {name: finding.get(name) for name in DIGESTED_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerdictIndex:
    """Verdicts by case id and finding key. `resolve` returning `None` is the third state --
    unadjudicated -- and every metric that consumes it must keep it out of both the numerator
    and the denominator (design.md D4)."""

    by_case: dict[str, dict[str, Verdict]]

    def resolve(self, case_id: str, finding: Finding) -> Verdict | None:
        return self.by_case.get(case_id, {}).get(finding_key(finding))

    def count(self, case_id: str) -> int:
        return len(self.by_case.get(case_id, {}))


def _parse(data: dict[str, Any], path: Path) -> tuple[str, dict[str, Verdict]]:
    try:
        jsonschema.validate(data, VERDICT_FILE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise VerdictError(f"{path}: {exc.message}") from exc

    case_id = data["case_id"]
    if path.stem != case_id:
        raise VerdictError(f"{path}: case_id {case_id!r} does not match the file name")

    verdicts: dict[str, Verdict] = {}
    for entry in data["verdicts"]:
        verdict = Verdict(
            finding_key=entry["finding_key"], true_defect=entry["true_defect"],
            case_defect=entry["case_defect"], reason=entry["reason"],
            recorded_at=entry["recorded_at"], adjudicated_by=entry["adjudicated_by"],
        )
        if verdict.case_defect and not verdict.true_defect:
            raise VerdictError(
                f"{path}: {verdict.finding_key} is marked as the case's defect but not as a "
                f"true defect"
            )
        if verdict.finding_key in verdicts:
            raise VerdictError(f"{path}: duplicate verdict for {verdict.finding_key}")
        verdicts[verdict.finding_key] = verdict
    return case_id, verdicts


def load_verdicts(path: Path) -> dict[str, Verdict]:
    try:
        data = yaml.load(path.read_text(), Loader=StrictLoader)
    except CaseError as exc:
        raise VerdictError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise VerdictError(f"{path}: verdict file must be a mapping at the top level")
    _, verdicts = _parse(data, path)
    return verdicts


def load_verdict_index(verdicts_dir: Path = VERDICTS_DIR) -> VerdictIndex:
    """Every `<case-id>.yml` under `verdicts_dir`. A missing directory is an empty index --
    nothing adjudicated yet is a state the metrics report, not an error."""
    by_case: dict[str, dict[str, Verdict]] = {}
    if not verdicts_dir.exists():
        return VerdictIndex(by_case=by_case)
    for path in sorted(verdicts_dir.glob("*.yml")):
        by_case[path.stem] = load_verdicts(path)
    return VerdictIndex(by_case=by_case)


def append_verdict(
    verdict: Verdict, case_id: str, verdicts_dir: Path = VERDICTS_DIR
) -> Path:
    """Appends one verdict to the case's file, creating it if needed, and writes atomically so
    an interrupted session leaves a readable file. Re-adjudicating an already-judged finding is
    an error rather than an overwrite: a session that silently replaced verdicts would make the
    record depend on the order in which it was run.
    """
    path = verdicts_dir / f"{case_id}.yml"
    if path.exists():
        existing = load_verdicts(path)
    else:
        existing = {}
    if verdict.finding_key in existing:
        raise VerdictError(f"{path}: {verdict.finding_key} already has a verdict")

    entries = [
        {
            "finding_key": v.finding_key, "true_defect": v.true_defect,
            "case_defect": v.case_defect, "reason": v.reason, "recorded_at": v.recorded_at,
            "adjudicated_by": v.adjudicated_by,
        }
        for v in [*existing.values(), verdict]
    ]
    payload = {"schema_version": 1, "case_id": case_id, "verdicts": entries}
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yml.tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    tmp.replace(path)
    return path
