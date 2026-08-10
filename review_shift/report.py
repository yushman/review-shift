"""Renders report.md: six sections, fixed order, never omitted."""
from __future__ import annotations

from typing import Any

from review_shift.patch import LocalizedFinding

RunMeta = dict[str, Any]

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]

STATUS_REASON = {
    "stale": "the `before` text no longer matches anything in the file at head_sha",
    "ambiguous": "the `before` text matches more than once and could not be resolved to one",
    "conflict": "overlaps a higher (or earlier, on a tie) severity finding in the same range",
    "no_fix": "the model reported this as an observation without a concrete before/after fix",
    "redacted": "affects a line masked by secret-redaction before the model saw it — "
    "review manually",
}


def _finding_line(lf: LocalizedFinding) -> str:
    f = lf.finding
    loc = f"{f['file']}:{f['line']}"
    return (
        f"- **{loc}** [{f['severity']}/{f['category']}] {f['rationale']}"
        + (f" (confidence: {f['confidence']})" if "confidence" in f else "")
    )


def _header(run: RunMeta) -> str:
    return (
        "## Header\n\n"
        f"- branch: `{run['branch']}`\n"
        f"- head_sha: `{run['head_sha']}`\n"
        f"- base: `{run['base']}`\n"
        f"- depth: `{run['depth']}`\n"
        f"- started_at: {run['started_at']}\n"
        f"- duration_ms: {run['duration_ms']}\n"
        f"- cost_usd: {run['cost_usd']:.4f}\n"
    )


def _summary(run: RunMeta, localized: list[LocalizedFinding]) -> str:
    total = len(localized)
    by_sev = run["findings_by_severity"]
    applicable = sum(1 for lf in localized if lf.status == "applicable")
    without_patch = run["findings_without_patch"]
    lines = [
        "## Summary\n",
        f"- {total} finding(s): "
        + ", ".join(f"{sev} {by_sev.get(sev, 0)}" for sev in SEVERITY_ORDER)
        + "\n",
        f"- {applicable} applicable (in a `.patch` file), {without_patch} without a patch\n",
        f"- auto-fix threshold: `{run['auto_fix_min_severity']}` "
        "(severity that gates `auto_fixed.patch` and exit code 1)\n",
    ]
    if run.get("patch_error"):
        lines.append(f"- **patch generation failed:** {run['patch_error']}\n")
    return "".join(lines)


def _findings_by_severity(localized: list[LocalizedFinding]) -> str:
    out = ["## Findings by severity\n"]
    for sev in SEVERITY_ORDER:
        group = [lf for lf in localized if lf.finding["severity"] == sev]
        if not group:
            continue
        out.append(f"\n### {sev} ({len(group)})\n\n")
        for lf in group:
            marker = " — in patch" if lf.status == "applicable" else f" — {lf.status}"
            out.append(_finding_line(lf) + marker + "\n")
    if len(out) == 1:
        out.append("\nNo findings.\n")
    return "".join(out)


def _findings_without_patch(localized: list[LocalizedFinding]) -> str:
    unpatched = [lf for lf in localized if lf.status != "applicable"]
    out = [f"## Findings without patch ({len(unpatched)})\n\n"]
    if not unpatched:
        out.append("None — every finding with a fix made it into a patch.\n")
        return "".join(out)
    for lf in unpatched:
        reason = STATUS_REASON.get(lf.status, lf.status)
        out.append(_finding_line(lf) + f" — **{lf.status}**: {reason}\n")
    return "".join(out)


def _skipped_branches(run: RunMeta) -> str:
    skipped = run.get("skipped", [])
    if not skipped:
        return "## Skipped branches\n\nNone were skipped.\n"
    out = [f"## Skipped branches ({len(skipped)})\n\n"]
    for s in skipped:
        out.append(f"- `{s['branch']}`: {s['reason']}\n")
    return "".join(out)


def _apply_recipe(run: RunMeta) -> str:
    branch = run["branch"]
    head_sha = run["head_sha"]
    patch_path = run.get("auto_fix_patch_path")
    if not patch_path:
        return (
            "## Apply recipe\n\n"
            "No `auto_fixed.patch` was produced for this run — nothing to apply.\n"
        )
    return (
        "## Apply recipe\n\n"
        "```bash\n"
        f"# 1. the sha this patch was built against\n"
        f"git rev-parse {branch}  # expect {head_sha}\n\n"
        f"# 2. check applicability\n"
        f"git switch {branch}\n"
        f"git apply --check {patch_path}\n\n"
        f"# 3. apply\n"
        f"git apply {patch_path}\n"
        "```\n"
    )


def render(run: RunMeta, localized: list[LocalizedFinding]) -> str:
    sections = [
        _header(run),
        _summary(run, localized),
        _findings_by_severity(localized),
        _findings_without_patch(localized),
        _skipped_branches(run),
        _apply_recipe(run),
    ]
    return "\n".join(sections)
