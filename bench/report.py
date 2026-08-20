"""Renders bench results. The headline block is detection: recall per depth from the stored
verdicts, plus yield and precision (design.md D5, D6). Localization -- the line arithmetic at
both tolerances -- is demoted to its own section and labelled for what it measures, which is
whether a patch can be anchored, not whether the review works (design.md D1).

Every figure carries its case count and its adjudication coverage, and a result below the
corpus target is marked unpublishable in the output itself, not only in prose (spec "Every
reported metric carries its case count"). A metric with nothing adjudicated prints as
`uncomputed` with its outstanding count: `0%` would read as a measured failure (design.md D4).
"""
from __future__ import annotations

from bench.runner import DEPTHS
from bench.scorer import (
    TOLERANCES,
    CaseRunResult,
    Metric,
    applicability,
    detected,
    localization,
    precision,
    recall,
    yield_per_case,
)
from bench.verdict import VerdictIndex

__all__ = ["CORPUS_TARGET", "render"]

# ADR-015's "30 PR" minimum, carried over unchanged by design.md's Open Questions.
CORPUS_TARGET = 30


def _unpublishable(n: int) -> str:
    if n >= CORPUS_TARGET:
        return ""
    return f"  [UNPUBLISHABLE: n={n} < corpus target {CORPUS_TARGET}]"


def _coverage(metric: Metric, unit: str) -> str:
    produced = metric.adjudicated + metric.outstanding
    return f"adjudicated {metric.adjudicated}/{produced} {unit}"


def _fmt_rate(label: str, rate: float, n: int) -> str:
    return f"{label}: {rate * 100:.0f}% (n={n}){_unpublishable(n)}"


def _fmt_metric(
    label: str, metric: Metric, *, unit: str = "%", coverage_unit: str = "findings",
) -> str:
    """Coverage carries its own unit because it is counted in cases for recall and in findings
    everywhere else -- an unlabelled `2/6` next to a rate invites reading one as the other."""
    if not metric.computed:
        return (
            f"{label}: uncomputed ({_coverage(metric, coverage_unit)}, "
            f"{metric.outstanding} outstanding)"
        )
    assert metric.value is not None
    figure = (
        f"{metric.value * 100:.0f}%" if unit == "%" else f"{metric.value:.2f} {unit}"
    )
    return (
        f"{label}: {figure} (n={metric.n}, {_coverage(metric, coverage_unit)})"
        f"{_unpublishable(metric.n)}"
    )


def _depths(results: list[CaseRunResult]) -> list[str]:
    """The ladder's own levels, plus any level a stored run carries. A pre-relabel depth is
    reported rather than dropped -- a figure that vanished because its level was renamed is
    the kind of silence this harness exists to avoid."""
    extra = sorted({r.depth for r in results if r.depth and r.depth not in DEPTHS})
    return [*DEPTHS, *extra]


def render(results: list[CaseRunResult], verdicts: VerdictIndex) -> str:
    lines = ["# bench results", ""]
    total_cases = len({r.case.id for r in results})
    lines.append(f"cases attempted: {total_cases}")
    if total_cases < CORPUS_TARGET:
        lines.append(
            f"[UNPUBLISHABLE: {total_cases} cases is below the corpus target of "
            f"{CORPUS_TARGET} -- no number in this report may be published]"
        )

    depths = _depths(results)
    pre_relabel = [d for d in depths if d not in DEPTHS]
    if pre_relabel:
        lines.append(
            f"[PRE-RELABEL DEPTHS: {', '.join(pre_relabel)} -- recorded before the depth "
            f"ladder was relabelled; these figures are not comparable with the ones above]"
        )

    # design.md D2 picked a human judge so the judge would not be the same kind of system as
    # the reviewer. Whenever that was overridden, the report has to say so on its own face:
    # every verdict-derived number below then carries the reviewer's blind spots into its own
    # scoring, and a reader who cannot tell the two apart will quote it as human-checked.
    model_judged = sum(
        1 for case in verdicts.by_case.values() for v in case.values()
        if v.adjudicated_by == "model"
    )
    if model_judged:
        total_verdicts = sum(len(case) for case in verdicts.by_case.values())
        lines.append(
            f"[MODEL-ADJUDICATED: {model_judged}/{total_verdicts} verdicts were produced by a "
            f"model, not a person -- the judge shares the reviewer's blind spots, so every "
            f"verdict-derived figure below is weaker evidence than a human-judged one]"
        )
    lines.append("")

    lines.append("## detection (verdict-based)")
    lines.append("")
    lines.append("### recall -- of the defects the corpus labelled, how many were found")
    for depth in depths:
        lines.append("  " + _fmt_metric(
            depth, recall(results, verdicts, depth), coverage_unit="cases",
        ))
        lines.append("  " + _fmt_metric(
            f"{depth} (diff_visible)",
            recall(results, verdicts, depth, diff_visible_only=True),
            coverage_unit="cases",
        ))
    lines.append("")

    lines.append("### yield -- findings adjudicated as true defects, per case (headline)")
    for depth in depths:
        lines.append("  " + _fmt_metric(
            depth, yield_per_case(results, verdicts, depth=depth), unit="per case",
        ))
    lines.append("")

    lines.append("### precision -- of adjudicated findings, the fraction judged true defects")
    for depth in depths:
        lines.append("  " + _fmt_metric(depth, precision(results, verdicts, depth=depth)))
    lines.append("")

    lines.append("## localization -- patch anchoring, not review quality")
    lines.append(
        "  Among findings adjudicated as the case's defect, how many point at the "
        "ground-truth"
    )
    lines.append(
        "  range. This decides whether a patch can be generated from a correct finding; it "
        "is not"
    )
    lines.append("  a measure of whether the review found anything.")
    for tolerance in TOLERANCES:
        lines.append("  " + _fmt_metric(
            f"tolerance={tolerance}", localization(results, verdicts, tolerance),
        ))
    lines.append("")

    app_rate, app_n = applicability([r for r in results if r.status == "ok"])
    lines.append(_fmt_rate("applicability", app_rate, app_n))
    lines.append("")

    lines.append("## per repository (detection)")
    repos = sorted({r.case.repo for r in results})
    for repo_id in repos:
        for depth in depths:
            subset = [
                r for r in results
                if r.status == "ok" and r.case.repo == repo_id and r.depth == depth
            ]
            if not subset:
                continue
            states = [detected(r.findings or [], verdicts, r.case) for r in subset]
            decided = [s for s in states if s is not None]
            hits = sum(1 for s in decided if s)
            outstanding = len(states) - len(decided)
            lines.append(
                f"  {repo_id}/{depth}: {hits}/{len(decided)} detected, "
                f"{outstanding} outstanding"
            )

    failed = [r for r in results if r.status != "ok"]
    if failed:
        lines.append("")
        lines.append("## not completed")
        for r in failed:
            lines.append(f"  {r.case.id}/{r.depth}: {r.status} -- {r.reason or ''}")

    return "\n".join(lines)
