"""Build a depth-blinded, shuffled worklist for an independent judge (ADR-015 D2 / Day 22).

Pass `--anchors` to mix in every finding an earlier judge already ruled on (Day 24's method):
judge disagreement is this project's least stable axis, so a new judge's verdicts are only
comparable with the stored ones if the drift between them is measured rather than assumed. The
anchors are indistinguishable from the new items in the worklist; only the new verdicts are
recorded, and the anchor answers are scored against the stored ones.
"""
import json
import random
import sys
from pathlib import Path

import bench.runner as runner
from bench.adjudicate import Item, outstanding_items
from bench.case import is_confirmed, load_cases
from bench.verdict import finding_key, load_verdict_index

anchors_wanted = "--anchors" in sys.argv[1:]
# Anchors measure drift; they do not need to be the whole archive. Anchoring off every stored
# run pulled 118 of them, which buries the new findings and makes one judging pass unwieldy.
# Cap the pool and fill it with the depths under test first -- those are the verdicts a repeat
# is actually asking a second judge to reproduce.
anchor_limit = next(
    (int(a.split("=", 1)[1]) for a in sys.argv[1:] if a.startswith("--anchor-limit=")), 50
)
out = Path([a for a in sys.argv[1:] if not a.startswith("--")][0])
# Confirmed cases only, for the same reason `bench.cli score` filters: a verdict on an
# unconfirmed case is a label written after seeing the output (ADR-015 label independence).
cases = [c for c in load_cases() if is_confirmed(c)]
# Every stored run, on both sides. `latest_only=True` keeps one run per (case, depth), so a
# second repeat hides the first: round four judged only the newer of two `low`/`medium` repeats
# and left 12 findings unadjudicated with no sign that anything was missing. Scoring still
# de-duplicates; a judge's worklist must not.
results = runner.load_stored_results(cases, latest_only=False)
verdicts = load_verdict_index()
items = outstanding_items(results, verdicts)
new_keys = {(it.case.id, it.key) for it in items}
if anchors_wanted:
    # Every stored run, not just the newest per (case, depth): anchoring off the newest drops
    # the earlier repeats of the depth being re-run, which are exactly the findings a repeat
    # design needs a second judge to rule on (devlog Day 26).
    depths_under_test = {it.depth for it in items}
    same, other = [], []
    for result in results:
        for finding in result.findings or []:
            if verdicts.resolve(result.case.id, finding) is None:
                continue
            anchor = Item(
                case=result.case, depth=result.depth, finding=finding,
                key=finding_key(finding),
            )
            (same if result.depth in depths_under_test else other).append(anchor)
    rng = random.Random(20260826)
    rng.shuffle(same)
    rng.shuffle(other)
    items.extend((same + other)[:anchor_limit])

random.Random(20260826).shuffle(items)   # fixed seed: reproducible, still order-blind

work, keymap = [], {}
for i, it in enumerate(items, 1):
    ident = f"F{i:03d}"
    keymap[ident] = {
        "key": it.key, "case_id": it.case.id, "depth": it.depth,
        "anchor": (it.case.id, it.key) not in new_keys,
    }
    f = it.finding
    work.append({
        "id": ident,
        "repo": it.case.repo,
        "commit_under_review": it.case.introducing_sha,
        "known_defect_in_this_commit": it.case.note,
        "known_defect_location": [
            f"{g.file}:{g.start_line}-{g.end_line}" for g in it.case.ground_truth
        ],
        "finding": {
            "file": f.get("file"), "line": f.get("line"), "end_line": f.get("end_line"),
            "severity": f.get("severity"), "category": f.get("category"),
            "rationale": f.get("rationale"),
        },
    })

out.write_text(json.dumps(work, indent=2, ensure_ascii=False))
(out.parent / "keymap.json").write_text(json.dumps(keymap, indent=2))
anchors = sum(1 for v in keymap.values() if v["anchor"])
print(f"{len(work)} findings -> {out} ({len(work) - anchors} new, {anchors} anchors)")
print("repos:", sorted({w['repo'] for w in work}))

# Kept in-tree because the blinding is the method, not a one-off: ADR-015 D2 only tolerates a
# model judge when it cannot see which depth produced a finding (devlog Day 22, Day 24). Doing
# this by hand is exactly how the blinding quietly stops happening.
