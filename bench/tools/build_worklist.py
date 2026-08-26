"""Build a depth-blinded, shuffled worklist for an independent judge (ADR-015 D2 / Day 22)."""
import json
import random
import sys
from pathlib import Path

import bench.runner as runner
from bench.adjudicate import outstanding_items
from bench.case import load_cases
from bench.verdict import load_verdict_index

out = Path(sys.argv[1])
cases = load_cases()
results = runner.load_stored_results(cases)
items = outstanding_items(results, load_verdict_index())

random.Random(20260826).shuffle(items)   # fixed seed: reproducible, still order-blind

work, keymap = [], {}
for i, it in enumerate(items, 1):
    ident = f"F{i:03d}"
    keymap[ident] = {"key": it.key, "case_id": it.case.id, "depth": it.depth}
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
print(f"{len(work)} findings -> {out}")
print("repos:", sorted({w['repo'] for w in work}))

# Kept in-tree because the blinding is the method, not a one-off: ADR-015 D2 only tolerates a
# model judge when it cannot see which depth produced a finding (devlog Day 22, Day 24). Doing
# this by hand is exactly how the blinding quietly stops happening.
