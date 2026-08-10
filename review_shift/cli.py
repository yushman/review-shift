"""argparse entry point. `run` has two branch-selection paths (explicit `--branch`, or
discovery driven by `.review-shift/config.yml`) that both feed into `batch.run_batch` — per
design.md's decision, a single `--branch` run is a batch of one, so both paths go through the
same lock/index/budget/timeout machinery (run-orchestration-and-resilience).

`init`, `init launchd`, `init skill` and `doctor` are the environment-setup capability
(operability-doctor-init-skill): the commands a human runs once (or checks before trusting a
scheduled night to them) rather than the nightly review path itself.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import shutil
import sys
from pathlib import Path
from typing import Any

from review_shift import batch, discover, doctor, gitutil, launchd_ops, redact
from review_shift import config as config_module
from review_shift.exitcodes import EXIT_INTERNAL_ERROR, EXIT_OK

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
PLUGIN_SKILL_PATH = (
    Path(__file__).resolve().parent / "plugin" / "skills" / "review-shift" / "SKILL.md"
)
GIT_EXCLUDE_ENTRY = ".review-shift/runs/"


def _discover_branches(
    args: argparse.Namespace, repo_root: Path, base: str, loaded: config_module.LoadedConfig
) -> tuple[list[str], list[dict[str, Any]]]:
    """The no-`--branch` path: run the fixed discovery pipeline (ADR-012, TDR FR-2) against
    the already-loaded config, return the selected branch names and every rejected branch's
    reason."""
    disc = loaded.data["discovery"]
    result = discover.discover(
        repo_root,
        base,
        patterns=disc["patterns"] or None,
        exclude_patterns=disc["exclude_patterns"] or None,
        discover_all=disc["discover_all"],
        max_age_hours=disc["max_age_hours"],
        max_branches_per_run=disc["max_branches_per_run"],
    )
    return [c.branch for c in result.selected], result.skipped


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    out_dir = Path(args.out_dir) if args.out_dir else repo_root / ".review-shift" / "runs"

    try:
        config_path = Path(args.config).resolve() if args.config else None
        loaded = config_module.load_config(repo_root, config_path=config_path)
    except config_module.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    exclude_paths = redact.merge_exclude_paths(loaded.data["scope"]["exclude_paths"])

    # `--base` is an explicit override; omitted, the effective base_branch comes from config
    # (default "auto"), which resolves to origin/HEAD (batch-execution spec "base_branch:
    # auto resolves to origin/HEAD", TDR FR-3). An explicit "auto" (from either source) is
    # resolved the same way; anything else passes through unchanged.
    base_branch_value = args.base if args.base is not None else loaded.data["base_branch"]
    try:
        base = gitutil.resolve_base_branch(repo_root, base_branch_value)
    except gitutil.GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    exit_zero_on_findings = (
        args.exit_zero_on_findings or loaded.data["runtime"]["exit_zero_on_findings"]
    )

    if args.branch:
        branches = [args.branch]
        discovery_skipped: list[dict[str, Any]] = []
    else:
        try:
            branches, discovery_skipped = _discover_branches(args, repo_root, base, loaded)
        except discover.DiscoverError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_INTERNAL_ERROR

        if not branches:
            for s in discovery_skipped:
                print(f"skipped {s['branch']}: {s['reason']}", file=sys.stderr)
            print("no branches discovered", file=sys.stderr)
            return EXIT_OK

    return batch.run_batch(
        repo_root=repo_root,
        out_dir=out_dir,
        branches=branches,
        base=base,
        depth=args.depth,
        model=args.model,
        loaded=loaded,
        exclude_paths=exclude_paths,
        discovery_skipped=discovery_skipped,
        force=args.force,
        exit_zero_on_findings=exit_zero_on_findings,
    )


def _append_git_exclude(repo_root: Path) -> None:
    """Append `.review-shift/runs/` to `.git/info/exclude`, never to `.gitignore` (ADR-007,
    environment-setup spec "init writes config and excludes runs from git" — `config.yml`
    itself is meant to be committed, only the run artifacts are local-only)."""
    exclude_path = repo_root / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    if GIT_EXCLUDE_ENTRY in existing.splitlines():
        return
    with exclude_path.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(GIT_EXCLUDE_ENTRY + "\n")
    print(f"added {GIT_EXCLUDE_ENTRY} to {exclude_path}")


def cmd_init(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    rs_dir = repo_root / ".review-shift"
    rs_dir.mkdir(parents=True, exist_ok=True)
    config_path = rs_dir / "config.yml"
    if config_path.exists() and not args.force:
        print(f"{config_path} already exists; use --force to overwrite", file=sys.stderr)
    else:
        config_path.write_text((TEMPLATES_DIR / "config.yml").read_text())
        print(f"wrote {config_path}")
    _append_git_exclude(repo_root)
    return EXIT_OK


def cmd_init_skill(args: argparse.Namespace) -> int:
    """Copy the canonical `SKILL.md` (`environment-setup` spec) into the consumer repo's
    `.claude/skills/review-shift/SKILL.md`, overwriting on re-run -- the same
    marketplace-independent path this dev repo already uses for its own copy, just invoked by
    the CLI instead of done by hand (design.md's "init gains init skill" decision)."""
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    skill_dir = repo_root / ".claude" / "skills" / "review-shift"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(PLUGIN_SKILL_PATH.read_text())
    print(f"wrote {skill_path}")
    return EXIT_OK


def _print_doctor_checks(checks: list[doctor.DoctorCheck]) -> None:
    for check in checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"[{status}] {check.name}: {check.detail}")


def cmd_doctor(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    checks = doctor.run_doctor(repo_root, model=args.model)
    _print_doctor_checks(checks)
    return EXIT_OK if all(c.ok for c in checks) else EXIT_INTERNAL_ERROR


def cmd_init_launchd(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    try:
        loaded = config_module.load_config(repo_root)
    except config_module.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR
    launchd_cfg = loaded.data["launchd"]
    hour = args.hour if args.hour is not None else launchd_cfg["hour"]
    minute = args.minute if args.minute is not None else launchd_cfg["minute"]

    # Same checks as `doctor` (ADR-005): "not installed yet" reads as a pass for the
    # plist/pmset-agreement checks (doctor.py's module docstring), so this gate holds even
    # for the very first install — there is no separate, narrower precondition list.
    checks = doctor.run_doctor(
        repo_root, plist_path=launchd_ops.PLIST_PATH, log_dir=launchd_ops.LOG_DIR,
        model=args.model,
    )
    if not all(c.ok for c in checks):
        _print_doctor_checks(checks)
        print("doctor checks failed; refusing to install the launchd job", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    review_shift_bin = shutil.which("review-shift")
    assert review_shift_bin is not None  # guaranteed by the doctor gate above
    install_prefix = str(Path(review_shift_bin).parent.parent)
    node_bin_path = shutil.which("node")
    node_bin = str(Path(node_bin_path).parent) if node_bin_path else ""

    ctx = launchd_ops.RenderContext(
        install_prefix=install_prefix, node_bin=node_bin, home=str(Path.home()),
        repo_root=str(repo_root), hour=hour, minute=minute,
    )
    launchd_ops.PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    launchd_ops.PLIST_PATH.write_text(launchd_ops.render_plist(ctx))
    launchd_ops.LOG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"wrote {launchd_ops.PLIST_PATH}")

    if launchd_cfg["wake_machine"]:
        schedule = launchd_ops.read_pmset_schedule()
        if launchd_ops.has_existing_repeat_schedule(schedule) and not args.force_pmset:
            print(
                "warning: an existing `pmset repeat` schedule was found; not overwriting it "
                "(pass --force-pmset to replace it, or run "
                "`sudo pmset repeat wakeorpoweron MTWRFSU HH:MM:SS` yourself)",
                file=sys.stderr,
            )
        else:
            launchd_ops.register_pmset_schedule(hour=hour, minute=minute)
            print("registered pmset repeat wakeorpoweron schedule")

    result = launchd_ops.install_launchd_job(launchd_ops.PLIST_PATH)
    if result.returncode != 0:
        print(
            f"error: launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}",
            file=sys.stderr,
        )
        return EXIT_INTERNAL_ERROR
    print(f"job installed and scheduled for {hour:02d}:{minute:02d}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review-shift")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="review one branch, or discover branches to review")
    run_p.add_argument(
        "--branch", default=None,
        help="review this branch only; omit to run discovery against .review-shift/config.yml",
    )
    run_p.add_argument(
        "--base", default=None,
        help="base branch (default: config's base_branch, itself defaulting to auto -> "
        "origin/HEAD)",
    )
    run_p.add_argument("--depth", choices=["low", "medium"], default="medium")
    run_p.add_argument("--model", default="sonnet")
    run_p.add_argument("--repo", default=None, help="repo root (default: cwd)")
    run_p.add_argument(
        "--out-dir", default=None, help="run directory root (default: <repo>/.review-shift/runs)"
    )
    run_p.add_argument(
        "--config", default=None,
        help="config file path (default: <repo>/.review-shift/config.{yml,json})",
    )
    run_p.add_argument(
        "--force", action="store_true",
        help="bypass the idempotency cache for every branch in this run",
    )
    run_p.add_argument(
        "--exit-zero-on-findings", action="store_true",
        help="collapse exit 1 (critical/high findings present) to exit 0",
    )

    init_p = sub.add_parser("init", help="write config.yml and .git/info/exclude")
    init_p.add_argument("--repo", default=None, help="repo root (default: cwd)")
    init_p.add_argument(
        "--force", action="store_true", help="overwrite an existing .review-shift/config.yml",
    )
    init_sub = init_p.add_subparsers(dest="init_command")
    launchd_p = init_sub.add_parser(
        "launchd", help="install the launchd plist and pmset wake schedule",
    )
    launchd_p.add_argument("--repo", default=None, help="repo root (default: cwd)")
    launchd_p.add_argument("--hour", type=int, default=None, help="default: config's launchd.hour")
    launchd_p.add_argument(
        "--minute", type=int, default=None, help="default: config's launchd.minute",
    )
    launchd_p.add_argument(
        "--model", default="sonnet", help="model used for the doctor gate's auth check",
    )
    launchd_p.add_argument(
        "--force-pmset", action="store_true",
        help="overwrite an existing `pmset repeat` schedule instead of warning and leaving it",
    )

    skill_p = init_sub.add_parser(
        "skill", help="copy SKILL.md into .claude/skills/review-shift/ for /review-shift",
    )
    # SUPPRESS, not None: `init --repo X skill` must not have this default clobber the
    # `--repo` already parsed by the parent `init` subparser into the shared namespace (a
    # latent nested-subparsers argparse gotcha -- see cmd_init_skill's tests).
    skill_p.add_argument(
        "--repo", default=argparse.SUPPRESS, help="repo root (default: cwd, or init's --repo)",
    )

    doctor_p = sub.add_parser("doctor", help="check the environment before a scheduled run")
    doctor_p.add_argument("--repo", default=None, help="repo root (default: cwd)")
    doctor_p.add_argument("--model", default="sonnet", help="model used for the auth check")

    parser.add_argument(
        "--version", action="version",
        version=f"review-shift {importlib.metadata.version('review-shift')}",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "init":
        if getattr(args, "init_command", None) == "launchd":
            return cmd_init_launchd(args)
        if getattr(args, "init_command", None) == "skill":
            return cmd_init_skill(args)
        return cmd_init(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    parser.error(f"unknown command: {args.command}")
    return EXIT_INTERNAL_ERROR


if __name__ == "__main__":
    sys.exit(main())
