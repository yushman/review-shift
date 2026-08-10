"""`review-shift doctor` -- eight independent environment checks (environment-setup spec,
ADR-005). Every check runs and reports on its own; doctor never stops at the first failure,
because system-analysis.md §8 already flagged this area as "worse than estimated" precisely
because a single silent gap here reads as a quiet night, not a failure.

Checks 6 and 7 (plist / pmset agree with config) treat "nothing installed yet" as a pass, not
a failure: before the first `review-shift init launchd`, there is nothing to disagree with,
and `init launchd` itself gates on `doctor` passing (per ADR-005) -- if "not installed" were
a failure, the very first install could never happen. A real drift is only reported once a
plist (or a pmset schedule, for check 7) actually exists and disagrees with config.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from review_shift import config as config_module
from review_shift import gitutil, launchd_ops, review
from review_shift.config.schema import DEFAULTS

MIN_CLAUDE_VERSION = (2, 1, 0)
RUNS_PATHSPEC = ".review-shift/runs"


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


def _version_tuple(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        return None
    groups = match.groups()
    return int(groups[0]), int(groups[1]), int(groups[2])


def _run_version_cmd() -> subprocess.CompletedProcess[str]:
    return subprocess.run(["claude", "--version"], capture_output=True, text=True, check=False)


def check_claude_version() -> DoctorCheck:
    try:
        proc = _run_version_cmd()
    except FileNotFoundError:
        return DoctorCheck("claude_version", False, "claude CLI not found on PATH")
    version = _version_tuple(proc.stdout or proc.stderr)
    if version is None:
        return DoctorCheck("claude_version", False,
                            f"could not parse a version from {(proc.stdout or proc.stderr)!r}")
    if version < MIN_CLAUDE_VERSION:
        got = ".".join(str(p) for p in version)
        return DoctorCheck("claude_version", False, f"claude {got} < 2.1.0 required")
    return DoctorCheck("claude_version", True, f"claude {'.'.join(str(p) for p in version)}")


def check_auth_liveness(model: str = "sonnet") -> DoctorCheck:
    try:
        review.check_auth(model)
    except (review.AuthError, review.QuotaError, review.AuthPreflightError) as exc:
        return DoctorCheck("auth", False, str(exc))
    return DoctorCheck("auth", True, "claude -p preflight succeeded")


def check_config_version(repo_root: Path) -> DoctorCheck:
    try:
        config_module.load_config(repo_root)
    except config_module.ConfigError as exc:
        return DoctorCheck("config_version", False, str(exc))
    return DoctorCheck("config_version", True, "config version is valid")


def check_absolute_paths() -> DoctorCheck:
    """The paths `init launchd` bakes into the plist as `{{INSTALL_PREFIX}}`/`{{NODE_BIN}}`
    must be absolute -- launchd resolves `ProgramArguments[0]` through its own minimal
    `_PATH_STDPATH` when no `Program` key is set, where `claude`/`node` are absent for an
    nvm/homebrew install (ADR-005)."""
    resolved = {name: shutil.which(name) for name in ("review-shift", "claude")}
    missing = [name for name, path in resolved.items() if path is None]
    if missing:
        return DoctorCheck("absolute_paths", False, f"not found on PATH: {', '.join(missing)}")
    non_absolute = [path for path in resolved.values() if path and not Path(path).is_absolute()]
    if non_absolute:
        return DoctorCheck("absolute_paths", False,
                            f"non-absolute path(s) on PATH: {', '.join(non_absolute)}")
    detail = ", ".join(f"{name}={path}" for name, path in resolved.items())
    return DoctorCheck("absolute_paths", True, detail)


def check_log_directory(log_dir: Path, *, plist_installed: bool) -> DoctorCheck:
    if log_dir.is_dir():
        return DoctorCheck("log_directory", True, f"{log_dir} exists")
    if not plist_installed:
        return DoctorCheck("log_directory", True,
                            f"{log_dir} does not exist yet; created by init launchd")
    return DoctorCheck("log_directory", False,
                        f"{log_dir} does not exist, but a plist is installed and expects it")


def check_plist_matches_config(*, plist_path: Path, expected_hour: int,
                                expected_minute: int) -> DoctorCheck:
    installed = launchd_ops.read_installed_plist(plist_path)
    if installed is None:
        return DoctorCheck("plist_matches_config", True, "no plist installed yet")
    interval = installed.get("StartCalendarInterval", {})
    hour, minute = interval.get("Hour"), interval.get("Minute")
    if (hour, minute) != (expected_hour, expected_minute):
        return DoctorCheck(
            "plist_matches_config", False,
            f"installed plist is scheduled for {hour:02d}:{minute:02d}, "
            f"config says {expected_hour:02d}:{expected_minute:02d}",
        )
    return DoctorCheck("plist_matches_config", True,
                        f"plist matches config ({expected_hour:02d}:{expected_minute:02d})")


def check_pmset_matches_config(*, expected_hour: int, expected_minute: int,
                                wake_machine: bool, plist_installed: bool) -> DoctorCheck:
    """Only compares an *existing* schedule once our own plist is installed. Before that, any
    schedule already on the machine is someone/something else's, and `init launchd`'s own
    "don't overwrite a foreign schedule, warn instead" handling (a separate, non-blocking
    step, not this gate) is what deals with it -- this check must not treat a foreign
    schedule found on a never-before-configured machine as a doctor failure, or the very
    first `init launchd` (which doctor gates) could never run at all."""
    if not wake_machine:
        return DoctorCheck("pmset_matches_config", True, "wake_machine disabled in config")
    if not plist_installed:
        return DoctorCheck("pmset_matches_config", True, "no plist installed yet")
    output = launchd_ops.read_pmset_schedule()
    scheduled = launchd_ops.extract_scheduled_time(output)
    if scheduled is None:
        return DoctorCheck("pmset_matches_config", True, "no pmset repeat schedule registered yet")
    total = (expected_hour * 60 + expected_minute - launchd_ops.WAKE_LEAD_MINUTES) % (24 * 60)
    want_hour, want_minute = divmod(total, 60)
    if scheduled != (want_hour, want_minute):
        got_hour, got_minute = scheduled
        return DoctorCheck(
            "pmset_matches_config", False,
            f"pmset is set to wake at {got_hour:02d}:{got_minute:02d}, "
            f"config implies {want_hour:02d}:{want_minute:02d}",
        )
    return DoctorCheck("pmset_matches_config", True,
                        f"pmset wake time matches config ({want_hour:02d}:{want_minute:02d})")


def check_runs_not_staged(repo_root: Path) -> DoctorCheck:
    staged = gitutil.ls_files_staged(repo_root, RUNS_PATHSPEC)
    if staged:
        return DoctorCheck("runs_not_staged", False,
                            f"{len(staged)} path(s) under {RUNS_PATHSPEC} are staged in git")
    return DoctorCheck("runs_not_staged", True, f"nothing under {RUNS_PATHSPEC} is staged")


def run_doctor(
    repo_root: Path,
    *,
    plist_path: Path | None = None,
    log_dir: Path | None = None,
    model: str = "sonnet",
) -> list[DoctorCheck]:
    """Run all eight checks and return every result -- never short-circuits on a failure."""
    plist_path = plist_path or launchd_ops.PLIST_PATH
    log_dir = log_dir or launchd_ops.LOG_DIR

    # An unrecognized config version must surface as a single failed check (below), not crash
    # the whole doctor run before the other seven checks get a chance to report -- so this
    # load is independent of check_config_version()'s own load, and falls back to schema
    # defaults for the two fields (hour/minute/wake_machine) the later checks need.
    try:
        launchd_cfg = config_module.load_config(repo_root).data["launchd"]
    except config_module.ConfigError:
        launchd_cfg = DEFAULTS["launchd"]
    plist_installed = plist_path.exists()

    return [
        check_claude_version(),
        check_auth_liveness(model),
        check_config_version(repo_root),
        check_absolute_paths(),
        check_log_directory(log_dir, plist_installed=plist_installed),
        check_plist_matches_config(
            plist_path=plist_path,
            expected_hour=launchd_cfg["hour"],
            expected_minute=launchd_cfg["minute"],
        ),
        check_pmset_matches_config(
            expected_hour=launchd_cfg["hour"],
            expected_minute=launchd_cfg["minute"],
            wake_machine=launchd_cfg["wake_machine"],
            plist_installed=plist_installed,
        ),
        check_runs_not_staged(repo_root),
    ]
