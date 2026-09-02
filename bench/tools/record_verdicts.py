"""Record an external judge's answers, and measure its drift from the stored verdicts.

The counterpart to `build_worklist --anchors`. Answers on anchor findings are never written --
they exist only to score this judge against the one whose verdicts are already on disk (Day 24
method: judge disagreement is this project's least stable axis, so cross-judge numbers are
comparable only with the drift attached). Kept in-tree for the same reason as the worklist
builder: done by hand, the anchor check is the step that quietly stops happening.

    python -m bench.tools.record_verdicts <verdicts.json> <keymap.json> [--dry-run]
"""
import json
import sys
from pathlib import Path

from bench.verdict import Verdict, VerdictError, append_verdict, load_verdict_index, utcnow

args = [a for a in sys.argv[1:] if not a.startswith("--")]
dry_run = "--dry-run" in sys.argv[1:]
answers = {a["id"]: a for a in json.loads(Path(args[0]).read_text())}
keymap = json.loads(Path(args[1]).read_text())

missing = sorted(set(keymap) - set(answers))
if missing:
    print(f"judge did not answer {len(missing)} item(s): {', '.join(missing[:10])}")

index = load_verdict_index()
agree_both = disagree_true = disagree_case = 0
undecided, written, invalid = [], 0, []

for ident, meta in sorted(keymap.items()):
    answer = answers.get(ident)
    if answer is None:
        continue
    true_defect, case_defect = bool(answer["true_defect"]), bool(answer["case_defect"])
    reason = (answer.get("reason") or "").strip()
    if case_defect and not true_defect:
        invalid.append(ident)   # the loader rejects this pair; do not launder it into a write
        continue
    if reason.upper().startswith("UNDECIDED"):
        undecided.append(ident)
        continue

    if meta["anchor"]:
        stored = index.by_case.get(meta["case_id"], {}).get(meta["key"])
        if stored is None:
            continue
        if stored.true_defect == true_defect and stored.case_defect == case_defect:
            agree_both += 1
        else:
            if stored.true_defect != true_defect:
                disagree_true += 1
            if stored.case_defect != case_defect:
                disagree_case += 1
            print(
                f"  anchor {ident} ({meta['case_id']}): stored "
                f"true={stored.true_defect} case={stored.case_defect} -> judge "
                f"true={true_defect} case={case_defect}"
            )
        continue

    if dry_run:
        written += 1
        continue
    try:
        append_verdict(
            Verdict(
                finding_key=meta["key"], true_defect=true_defect, case_defect=case_defect,
                reason=reason, recorded_at=utcnow(), adjudicated_by="model",
            ),
            meta["case_id"],
        )
        written += 1
    except VerdictError as exc:
        print(f"  skipped {ident}: {exc}")

anchors = agree_both + max(disagree_true, disagree_case)
print()
print(f"anchors: {agree_both}/{anchors} agreed on both booleans "
      f"({disagree_true} differ on true_defect, {disagree_case} on case_defect)")
print(f"new verdicts {'that would be' if dry_run else ''} recorded: {written}")
if undecided:
    print(f"left unadjudicated (judge undecided): {len(undecided)} -- {', '.join(undecided)}")
if invalid:
    print(f"REJECTED as case_defect without true_defect: {', '.join(invalid)}")
