"""Hand-run entry point for the bench harness (design.md D5, tasks.md 5.4-5.5). Never invoked
from CI -- it costs money and is non-deterministic (ADR-015).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bench import adjudicate, derive, report, runner
from bench.adjudicate import Aborted, Answer, Item
from bench.case import Case, is_confirmed, load_cases
from bench.corpus import load_corpus
from bench.verdict import VERDICTS_DIR, load_verdict_index

FAST_SUBSET_SIZE = 8


def _confirmed_cases(cases: list[Case]) -> list[Case]:
    return [c for c in cases if is_confirmed(c)]


def cmd_list_candidates(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    repo = corpus[args.repo]
    repo_dir = Path(args.repo_dir) if args.repo_dir else Path("bench/.work") / repo.id
    for c in derive.list_candidates(repo_dir, repo.language, limit=args.limit):
        print(f"{c.sha}\t{c.subject}\t{','.join(c.files)}")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    repo = corpus[args.repo]
    repo_dir = Path(args.repo_dir) if args.repo_dir else Path("bench/.work") / repo.id
    draft = derive.draft_case(repo_dir, repo.id, args.fix_sha, args.case_id)
    path = derive.write_draft(draft, Path("bench/cases"))
    print(f"wrote {path} -- confirmed_at is unset; a human confirms before the first run")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    cases = _confirmed_cases(load_cases())
    if args.fast:
        cases = cases[:FAST_SUBSET_SIZE]
    if not cases:
        print("no confirmed cases to run", file=sys.stderr)
        return 1
    results = runner.run_all(cases, corpus, budget_usd=args.budget_usd)
    print(report.render(results, load_verdict_index()))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    """Re-scores the runs already on disk. Costs nothing and calls no model, so it is the way
    to see what a fresh batch of verdicts did to the numbers."""
    results = runner.load_stored_results(load_cases())
    if not results:
        print("no stored runs found", file=sys.stderr)
        return 1
    print(report.render(results, load_verdict_index()))
    return 0


def _yes_no(prompt: str) -> bool:
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer in ("q", "quit"):
            raise Aborted
        print("  answer y, n, or q to stop for now")


def _prompt(item: Item, index: int, total: int) -> Answer | None:
    """The interactive judgement. `s` leaves the finding unadjudicated, which is a legitimate
    answer -- a finding the adjudicator cannot decide must stay in the outstanding count
    rather than be pushed into either side."""
    print()
    print(adjudicate.format_item(item, index=index, total=total))
    print()
    print("  [y]es / [n]o / [s]kip (leave unadjudicated) / [q]uit")
    first = input("  is this a true defect? ").strip().lower()
    if first in ("q", "quit"):
        raise Aborted
    if first in ("s", "skip", ""):
        return None
    if first in ("y", "yes"):
        true_defect = True
    elif first in ("n", "no"):
        true_defect = False
    else:
        print("  unrecognised answer -- left unadjudicated")
        return None

    case_defect = (
        _yes_no("  is it the defect this case is about? ") if true_defect else False
    )
    reason = input("  reason: ").strip()
    return Answer(true_defect=true_defect, case_defect=case_defect, reason=reason)


def cmd_adjudicate(args: argparse.Namespace) -> int:
    cases = load_cases()
    if args.case:
        cases = [c for c in cases if c.id == args.case]
        if not cases:
            print(f"no case with id {args.case!r}", file=sys.stderr)
            return 1
    results = runner.load_stored_results(cases)
    verdicts = load_verdict_index()

    if args.status:
        rows = adjudicate.coverage_rows(results, verdicts)
        for case_id, depth, done, produced in rows:
            print(f"{case_id}/{depth}: {done}/{produced} adjudicated")
        total_done = sum(r[2] for r in rows)
        total = sum(r[3] for r in rows)
        print(f"total: {total_done}/{total} adjudicated, {total - total_done} outstanding")
        return 0

    items = adjudicate.outstanding_items(results, verdicts)
    if not items:
        print("nothing outstanding")
        return 0

    if args.list:
        for i, item in enumerate(items, start=1):
            print(adjudicate.format_item(item, index=i, total=len(items)))
            print()
        return 0

    total = len(items)
    seen = 0

    def ask(item: Item) -> Answer | None:
        nonlocal seen
        seen += 1
        return _prompt(item, seen, total)

    recorded = adjudicate.run_session(items, ask, verdicts_dir=VERDICTS_DIR)
    print(f"\nrecorded {recorded} verdict(s); {total - recorded} still outstanding")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bench")
    sub = parser.add_subparsers(dest="command", required=True)

    lc = sub.add_parser("list-candidates", help="list mechanically filtered fix commits")
    lc.add_argument("--repo", required=True, choices=["duckduckgo-android", "pydantic", "cli"])
    lc.add_argument("--repo-dir", default=None)
    lc.add_argument("--limit", type=int, default=500)

    dr = sub.add_parser("draft", help="draft a case file from a chosen fix commit")
    dr.add_argument("--repo", required=True, choices=["duckduckgo-android", "pydantic", "cli"])
    dr.add_argument("--repo-dir", default=None)
    dr.add_argument("--fix-sha", required=True)
    dr.add_argument("--case-id", required=True)

    rn = sub.add_parser("run", help="run confirmed cases through review-shift and score them")
    rn.add_argument("--fast", action="store_true", help=f"first {FAST_SUBSET_SIZE} cases only")
    rn.add_argument("--budget-usd", type=float, required=True)

    sub.add_parser("score", help="re-score the runs already on disk; costs nothing")

    ad = sub.add_parser("adjudicate", help="record a human verdict for each stored finding")
    ad.add_argument("--status", action="store_true", help="print coverage and exit")
    ad.add_argument("--list", action="store_true", help="print outstanding findings and exit")
    ad.add_argument("--case", default=None, help="restrict to one case id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "list-candidates":
        return cmd_list_candidates(args)
    if args.command == "draft":
        return cmd_draft(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "score":
        return cmd_score(args)
    if args.command == "adjudicate":
        return cmd_adjudicate(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
