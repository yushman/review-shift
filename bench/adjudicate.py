"""The adjudication session: which findings still need a human verdict, what the human needs
to see to give one, and appending the answer (tasks.md 3).

Adjudication is the one step of the bench a machine must not take for the human. This module
therefore only assembles and records; the judgement itself arrives through `ask`, and nothing
here infers a verdict from a case note, a line number or any other proxy.

The session is resumable by construction: every answer is appended to the case's verdict file
as it is given, and the outstanding list is recomputed from the files each time, so an
interrupted sitting loses nothing but the finding being answered.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from bench.case import Case
from bench.scorer import CaseRunResult
from bench.verdict import (
    VERDICTS_DIR,
    Finding,
    Verdict,
    VerdictIndex,
    append_verdict,
    finding_key,
    utcnow,
)

__all__ = [
    "Answer", "Aborted", "Item", "coverage_rows", "format_item", "outstanding_items",
    "run_session",
]


class Aborted(RuntimeError):
    """Raised by `ask` when the human stops the sitting. Everything answered so far is already
    on disk."""


@dataclass(frozen=True)
class Answer:
    true_defect: bool
    case_defect: bool
    reason: str


@dataclass(frozen=True)
class Item:
    case: Case
    depth: str
    finding: Finding
    key: str


# Returns `None` to leave a finding unadjudicated -- which is a real third state, not a "no".
Ask = Callable[[Item], Answer | None]


def outstanding_items(results: list[CaseRunResult], verdicts: VerdictIndex) -> list[Item]:
    items: list[Item] = []
    for result in results:
        for finding in result.findings or []:
            if verdicts.resolve(result.case.id, finding) is not None:
                continue
            items.append(Item(
                case=result.case, depth=result.depth, finding=finding,
                key=finding_key(finding),
            ))
    return items


def format_item(item: Item, *, index: int | None = None, total: int | None = None) -> str:
    """Everything the adjudicator needs in one block: what the finding claims, and the case's
    own note and ground truth to compare it against. The full rationale is printed, never
    truncated -- a verdict given on half a claim is worse than no verdict."""
    finding = item.finding
    header = f"{item.case.id} @ {item.depth}"
    if index is not None and total is not None:
        header = f"[{index}/{total}] {header}"
    span = str(finding.get("line"))
    if finding.get("end_line") is not None and finding.get("end_line") != finding.get("line"):
        span = f"{finding.get('line')}-{finding.get('end_line')}"
    ground_truth = ", ".join(
        f"{gt.file}:{gt.start_line}-{gt.end_line}" for gt in item.case.ground_truth
    )
    lines = [
        header,
        f"  key:       {item.key}",
        f"  finding:   {finding.get('file')}:{span} "
        f"[{finding.get('severity')}/{finding.get('category')}]",
        f"  rationale: {finding.get('rationale', '')}",
        "",
        f"  case note:    {item.case.note}",
        f"  ground truth: {ground_truth}",
    ]
    return "\n".join(lines)


def coverage_rows(
    results: list[CaseRunResult], verdicts: VerdictIndex
) -> list[tuple[str, str, int, int]]:
    """`(case_id, depth, adjudicated, produced)` per run, in case/depth order -- the
    outstanding work made visible without starting a sitting."""
    rows: list[tuple[str, str, int, int]] = []
    for result in sorted(results, key=lambda r: (r.case.id, r.depth)):
        findings = result.findings or []
        adjudicated = sum(
            1 for f in findings if verdicts.resolve(result.case.id, f) is not None
        )
        rows.append((result.case.id, result.depth, adjudicated, len(findings)))
    return rows


def run_session(
    items: Iterable[Item], ask: Ask, *, verdicts_dir: Path = VERDICTS_DIR,
    now: Callable[[], str] = utcnow,
) -> int:
    """Asks for each item in turn and appends every answer immediately. Returns how many
    verdicts were recorded. `Aborted` stops the sitting and keeps what came before it."""
    recorded = 0
    for item in items:
        try:
            answer = ask(item)
        except Aborted:
            break
        if answer is None:
            continue
        append_verdict(
            Verdict(
                finding_key=item.key, true_defect=answer.true_defect,
                case_defect=answer.case_defect, reason=answer.reason, recorded_at=now(),
            ),
            item.case.id, verdicts_dir,
        )
        recorded += 1
    return recorded
