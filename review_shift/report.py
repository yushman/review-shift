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

# review-report spec "Trunk findings state which remedy is still available".
REMEDY_TEXT = {
    "still_local": "still local — amend or rebase is available",
    "pushed": "already pushed — fix forward only",
    "unknown": "pushed state unknown (no `origin/<base>`)",
}

# Unit statuses that represent "reviewed" for the trunk header's counts, vs. skipped/gapped.
_TRUNK_REVIEWED_STATUSES = {"ok", "cache_hit"}
_TRUNK_GAP_STATUSES = {"diff_too_large"}


def _finding_line(lf: LocalizedFinding) -> str:
    f = lf.finding
    loc = f"{f['file']}:{f['line']}"
    line = (
        f"- **{loc}** [{f['severity']}/{f['category']}] {f['rationale']}"
        + (f" (confidence: {f['confidence']})" if "confidence" in f else "")
    )
    if f.get("commit"):
        author = f.get("author") or "unknown author"
        remedy = REMEDY_TEXT.get(f.get("remedy", "unknown"), REMEDY_TEXT["unknown"])
        line += f" — commit `{f['commit']}` by {author}; {remedy}"
    return line


def _header(run: RunMeta) -> str:
    lines = [
        "## Header\n\n",
        f"- branch: `{run['branch']}`\n",
        f"- head_sha: `{run['head_sha']}`\n",
        f"- base: `{run['base']}`\n",
        f"- depth: `{run['depth']}`\n",
        f"- started_at: {run['started_at']}\n",
        f"- duration_ms: {run['duration_ms']}\n",
        f"- cost_usd: {run['cost_usd']:.4f}\n",
    ]
    if run.get("mode") == "trunk":
        lines.append("- mode: `trunk`\n")
        lines.append(f"- anchor_sha: `{run.get('anchor_sha') or 'none (bootstrap)'}`\n")
        lines.append(
            f"- commits reviewed: {run.get('reviewed_count', 0)}, "
            f"skipped: {run.get('skipped_count', 0)}, gapped: {run.get('gapped_count', 0)}\n"
        )
    return "".join(lines)


def _trunk_reason_line(run: RunMeta) -> str | None:
    """review-report spec "A trunk run that reviewed nothing says why": bootstrap, nothing_new,
    and budget exhaustion from the very first unit must never read as a completed, clean
    review just because `findings_count` is 0."""
    outcome = run.get("trunk_outcome")
    if outcome == "bootstrap":
        return (
            "- **bootstrap**: no watermark existed yet for this branch; it was initialized to "
            "the current head and no commit was reviewed this run\n"
        )
    if outcome == "nothing_new":
        return (
            "- **nothing_new**: no commit has landed directly on the base branch since "
            "the anchor\n"
        )
    if outcome == "units":
        units = run.get("units", [])
        reviewed = [u for u in units if u["status"] in _TRUNK_REVIEWED_STATUSES]
        exhausted = [u for u in units if u["status"] == "budget_exhausted"]
        if not reviewed and exhausted:
            return (
                "- **budget exhausted**: total_budget_usd was exhausted before any unit "
                "completed this run\n"
            )
    return None


def _summary(run: RunMeta, localized: list[LocalizedFinding]) -> str:
    total = len(localized)
    by_sev = run["findings_by_severity"]
    applicable = sum(1 for lf in localized if lf.status == "applicable")
    without_patch = run["findings_without_patch"]
    lines = ["## Summary\n"]
    if run.get("mode") == "trunk":
        reason = _trunk_reason_line(run)
        if reason:
            lines.append(reason)
    lines.append(
        f"- {total} finding(s): "
        + ", ".join(f"{sev} {by_sev.get(sev, 0)}" for sev in SEVERITY_ORDER)
        + "\n"
    )
    lines.append(
        f"- {applicable} applicable (in a `.patch` file), {without_patch} without a patch\n"
    )
    lines.append(
        f"- auto-fix threshold: `{run['auto_fix_min_severity']}` "
        "(severity that gates `auto_fixed.patch` and exit code 1)\n"
    )
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
    if run.get("mode") == "trunk":
        # review-report spec "Trunk run with skipped commits": every unit that isn't a
        # completed review (superseded, diff_too_large, max_commits_per_run_cap,
        # budget_exhausted, or an outright failure) renders here, same heading, same position.
        skipped = [
            u for u in run.get("units", []) if u["status"] not in _TRUNK_REVIEWED_STATUSES
        ]
        if not skipped:
            return "## Skipped branches\n\nNone were skipped.\n"
        out = [f"## Skipped branches ({len(skipped)})\n\n"]
        for u in skipped:
            out.append(f"- `{u['sha']}`: {u['status']}\n")
        return "".join(out)

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
