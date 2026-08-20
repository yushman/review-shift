"""Renders bench results. The headline block is the paired depth comparison: an exact sign
test over discordant pairs, the comparison the product actually turns on and the one that is
tractable at this corpus size (design.md D3, proposal.md). Detection recall, yield and
precision follow, then localization -- the line arithmetic at both tolerances -- demoted to
its own section and labelled for what it measures, which is whether a patch can be anchored,
not whether the review works (design.md D1).

Every rate carries its Wilson 95% interval alongside its point estimate and denominator, and a
rate whose interval is wider than `CLAIM_INTERVAL_WIDTH` is marked as supporting no claim --
by interval width, not by corpus size (design.md D2; spec "A figure whose interval is too wide
to separate the outcomes it compares supports no claim"). `yield` is the deliberate exception:
it is a mean of counts, not a proportion, so it carries no interval and says so explicitly
(design.md D5). A metric with nothing adjudicated prints as `uncomputed` with its outstanding
count: `0%` would read as a measured failure (design.md D4).
"""
from __future__ import annotations

from itertools import combinations

from bench.runner import DEPTHS
from bench.scorer import (
    CLAIM_INTERVAL_WIDTH,
    TOLERANCES,
    CaseRunResult,
    Metric,
    applicability,
    detected,
    localization,
    paired_comparison,
    precision,
    recall,
    undetected_everywhere,
    yield_per_case,
)
from bench.verdict import VerdictIndex

__all__ = ["CLAIM_INTERVAL_WIDTH", "CORPUS_TARGET", "render"]

# The corpus's growth target (ADR-015's "30 PR" minimum). No longer the publishability rule --
# see `CLAIM_INTERVAL_WIDTH` in bench.scorer for what replaced it -- but it stays as the number
# the corpus is grown toward, and ADR-015 and the adjudication scaling limit both cite it.
CORPUS_TARGET = 30


def _coverage(metric: Metric, unit: str) -> str:
    produced = metric.adjudicated + metric.outstanding
    return f"adjudicated {metric.adjudicated}/{produced} {unit}"


def _fmt_interval(metric: Metric) -> str:
    if not metric.has_interval:
        return ""
    assert metric.lower is not None and metric.upper is not None
    conf = metric.confidence if metric.confidence is not None else 0.95
    return f" [{metric.lower * 100:.0f}% .. {metric.upper * 100:.0f}%] ({conf * 100:.0f}% CI)"


def _claim_mark(metric: Metric) -> str:
    """design.md D2: a rate is marked as supporting no claim when its own interval is wider
    than `CLAIM_INTERVAL_WIDTH`, not when the corpus is smaller than `CORPUS_TARGET`. A metric
    with no interval (uncomputed, or `yield`) is never marked here -- there is nothing to
    measure the width of."""
    if not metric.has_interval:
        return ""
    assert metric.lower is not None and metric.upper is not None
    width = metric.upper - metric.lower
    if width <= CLAIM_INTERVAL_WIDTH:
        return ""
    return (
        f"  [UNPUBLISHABLE: interval width {width * 100:.0f}pp > "
        f"{CLAIM_INTERVAL_WIDTH * 100:.0f}pp -- cannot separate adjacent depths]"
    )


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
        f"{label}: {figure}{_fmt_interval(metric)} "
        f"(n={metric.n}, {_coverage(metric, coverage_unit)}){_claim_mark(metric)}"
    )


def _fmt_yield(label: str, metric: Metric) -> str:
    """`yield` reports its numerator and denominator explicitly rather than only their ratio,
    and says plainly that its uncertainty is not quantified -- it is a mean of small,
    heterogeneous counts, not a proportion, so no Wilson interval applies (design.md D5)."""
    if not metric.computed:
        return (
            f"{label}: uncomputed (adjudicated {metric.adjudicated}/"
            f"{metric.adjudicated + metric.outstanding} findings, "
            f"{metric.outstanding} outstanding)"
        )
    assert metric.value is not None and metric.k is not None
    return (
        f"{label}: {metric.value:.2f} per case "
        f"({metric.k} true defects / {metric.n} cases, "
        f"{_coverage(metric, 'findings')}; uncertainty not quantified)"
    )


def _depths(results: list[CaseRunResult]) -> list[str]:
    """The ladder's own levels, plus any level a stored run carries. A pre-relabel depth is
    reported rather than dropped -- a figure that vanished because its level was renamed is
    the kind of silence this harness exists to avoid."""
    extra = sorted({r.depth for r in results if r.depth and r.depth not in DEPTHS})
    return [*DEPTHS, *extra]


def _depths_with_results(results: list[CaseRunResult]) -> list[str]:
    """`_depths` in ladder order, restricted to depths that actually ran. A depth on the
    ladder with no stored run (e.g. a level added since the last bench run) has nothing to
    pair against, so it is excluded from the paired comparison rather than reported as three
    empty comparisons."""
    ran = {r.depth for r in results if r.status == "ok"}
    return [d for d in _depths(results) if d in ran]


def render(results: list[CaseRunResult], verdicts: VerdictIndex) -> str:
    lines = ["# bench results", ""]
    total_cases = len({r.case.id for r in results})
    lines.append(f"cases attempted: {total_cases} (corpus growth target: {CORPUS_TARGET})")

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
    lines.append("### paired comparison across depths (headline)")
    lines.append(
        "  Wins/losses over cases where both depths ran and both are adjudicated; ties carry"
    )
    lines.append(
        "  no directional information and are excluded from the sign test. p is exact and"
    )
    lines.append("  one-sided, in the direction observed.")
    paired_depths = _depths_with_results(results)
    for depth_a, depth_b in combinations(paired_depths, 2):
        pc = paired_comparison(results, verdicts, depth_a, depth_b)
        base = (
            f"  {pc.depth_a} vs {pc.depth_b}: {pc.depth_a} {pc.wins_a}, {pc.depth_b} "
            f"{pc.wins_b}, ties {pc.ties} (n={pc.n})"
        )
        if pc.p_value is None:
            lines.append(f"{base} -- no discordant pairs, no p-value")
        else:
            lines.append(f"{base} -- {pc.direction} ahead, p={pc.p_value:.4f}")
    undetected = undetected_everywhere(results, verdicts)
    lines.append(
        f"  undetected at every depth: {undetected} case(s) -- these can never contribute a "
        f"discordant pair, and they cap recall regardless of what the tool does"
    )
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

    lines.append("### yield -- findings adjudicated as true defects, per case")
    for depth in depths:
        lines.append("  " + _fmt_yield(depth, yield_per_case(results, verdicts, depth=depth)))
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

    app_metric = applicability([r for r in results if r.status == "ok"])
    lines.append(_fmt_metric("applicability", app_metric, coverage_unit="patches"))
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
