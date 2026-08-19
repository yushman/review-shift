"""Hand-run entry point for the bench harness (design.md D5, tasks.md 5.4-5.5). Never invoked
from CI -- it costs money and is non-deterministic (ADR-015).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bench import derive, report, runner
from bench.case import Case, is_confirmed, load_cases
from bench.corpus import load_corpus

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
    print(report.render(results))
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
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
