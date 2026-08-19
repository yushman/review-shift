"""Renders bench results: recall per depth at both tolerances, over-all and diff-visible
subsets, applicability, a per-repository breakdown, and every metric with its case count --
a result below the corpus target is marked unpublishable in the output itself, not only in
prose (spec "Every reported metric carries its case count"). Never emits a precision figure
(design.md D6) -- there is no code path here that could compute one.
"""
from __future__ import annotations

from bench.runner import DEPTHS
from bench.scorer import TOLERANCES, CaseRunResult, applicability, case_hit, recall

__all__ = ["CORPUS_TARGET", "render"]

# ADR-015's "30 PR" minimum, carried over unchanged by design.md's Open Questions.
CORPUS_TARGET = 30


def _fmt(label: str, rate: float, n: int) -> str:
    marker = (
        "" if n >= CORPUS_TARGET
        else f"  [UNPUBLISHABLE: n={n} < corpus target {CORPUS_TARGET}]"
    )
    return f"{label}: {rate * 100:.0f}% (n={n}){marker}"


def render(results: list[CaseRunResult]) -> str:
    lines = ["# bench results", ""]
    total_cases = len({r.case.id for r in results})
    lines.append(f"cases attempted: {total_cases}")
    if total_cases < CORPUS_TARGET:
        lines.append(
            f"[UNPUBLISHABLE: {total_cases} cases is below the corpus target of "
            f"{CORPUS_TARGET} -- no number in this report may be published]"
        )
    lines.append("")

    for tolerance in TOLERANCES:
        lines.append(f"## recall @ tolerance={tolerance}")
        for depth in DEPTHS:
            rate, n = recall(results, depth, tolerance)
            lines.append("  " + _fmt(depth, rate, n))
            rate_dv, n_dv = recall(results, depth, tolerance, diff_visible_only=True)
            lines.append("  " + _fmt(f"{depth} (diff_visible)", rate_dv, n_dv))
        lines.append("")

    app_rate, app_n = applicability([r for r in results if r.status == "ok"])
    lines.append(_fmt("applicability", app_rate, app_n))
    lines.append("")

    lines.append("## per repository (recall @ tolerance=0)")
    repos = sorted({r.case.repo for r in results})
    for repo_id in repos:
        for depth in DEPTHS:
            subset = [
                r for r in results
                if r.status == "ok" and r.case.repo == repo_id and r.depth == depth
            ]
            if not subset:
                continue
            hits = sum(1 for r in subset if case_hit(r.findings or [], r.case, 0))
            lines.append(f"  {repo_id}/{depth}: {hits}/{len(subset)}")

    failed = [r for r in results if r.status != "ok"]
    if failed:
        lines.append("")
        lines.append("## not completed")
        for r in failed:
            lines.append(f"  {r.case.id}/{r.depth}: {r.status} -- {r.reason or ''}")

    return "\n".join(lines)
