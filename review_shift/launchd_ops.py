"""launchd plist rendering, `pmset repeat` schedule detection/registration, and
`launchctl bootstrap` job installation, per ADR-005.

Every subprocess call to `pmset`/`launchctl` goes through its own module-level seam
(`_run_pmset`, `_run_launchctl`) so tests can replace it without ever shelling out to the
real, system-wide `pmset repeat` schedule or registering a real launchd job on the machine
running the tests.
"""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "launchd.plist"

PLIST_LABEL = "com.user.review-shift"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "review-shift"

_PMSET_REPEAT_DAYS = "MTWRFSU"
WAKE_LEAD_MINUTES = 5


@dataclass(frozen=True)
class RenderContext:
    install_prefix: str
    node_bin: str
    home: str
    repo_root: str
    hour: int
    minute: int


def render_plist(ctx: RenderContext) -> str:
    text = TEMPLATE_PATH.read_text()
    replacements = {
        "{{INSTALL_PREFIX}}": ctx.install_prefix,
        "{{NODE_BIN}}": ctx.node_bin,
        "{{HOME}}": ctx.home,
        "{{REPO_ROOT}}": ctx.repo_root,
        "{{HOUR}}": str(ctx.hour),
        "{{MINUTE}}": str(ctx.minute),
    }
    for token, value in replacements.items():
        text = text.replace(token, value)
    return text


def read_installed_plist(plist_path: Path) -> dict[str, Any] | None:
    if not plist_path.exists():
        return None
    return plistlib.loads(plist_path.read_bytes())  # type: ignore[no-any-return]


# Real `pmset -g sched` output looks like:
#   Repeating power events:
#     wakeorpoweron at 3:25AM every day
# or, with nothing registered:
#   Repeating power events:
#   None
_REPEAT_HEADER = "Repeating power events:"
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])")


def _run_pmset(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, **kwargs)


def read_pmset_schedule() -> str:
    proc = _run_pmset(["pmset", "-g", "sched"], capture_output=True, text=True, check=False)
    return proc.stdout


def has_existing_repeat_schedule(pmset_output: str) -> bool:
    lines = pmset_output.splitlines()
    try:
        idx = next(i for i, line in enumerate(lines) if _REPEAT_HEADER in line)
    except StopIteration:
        return False
    rest = lines[idx + 1 :]
    return any(line.strip() and line.strip() != "None" for line in rest)


def extract_scheduled_time(pmset_output: str) -> tuple[int, int] | None:
    """The (hour, minute) of an existing `wakeorpoweron` entry in 24h form, or None if there
    is no repeating schedule to parse."""
    if not has_existing_repeat_schedule(pmset_output):
        return None
    match = _TIME_RE.search(pmset_output)
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2)), match.group(3).upper()
    if meridiem == "AM":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return hour, minute


def register_pmset_schedule(*, hour: int, minute: int) -> subprocess.CompletedProcess[str]:
    """`sudo pmset repeat wakeorpoweron MTWRFSU HH:MM:SS`, `_WAKE_LEAD_MINUTES` before the
    scheduled run so the machine is awake by the time `StartCalendarInterval` fires (ADR-005
    -- launchd does not itself wake the machine)."""
    total = (hour * 60 + minute - WAKE_LEAD_MINUTES) % (24 * 60)
    wake_hour, wake_minute = divmod(total, 60)
    time_str = f"{wake_hour:02d}:{wake_minute:02d}:00"
    cmd = ["sudo", "pmset", "repeat", "wakeorpoweron", _PMSET_REPEAT_DAYS, time_str]
    return _run_pmset(cmd, capture_output=True, text=True, check=False)


def _run_launchctl(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, **kwargs)


def install_launchd_job(plist_path: Path) -> subprocess.CompletedProcess[str]:
    """`launchctl bootstrap gui/$(id -u) <plist>` (ADR-005's "Установка и удаление"), the
    step that actually schedules the job -- writing the plist file alone does not register
    it with launchd. A `bootout` of any previously-loaded job with the same label runs first
    and its result is ignored: launchd does not hot-reload a changed plist, and `bootout`
    against a label that was never loaded is expected to fail on a first-ever install."""
    uid = os.getuid()
    gui_domain = f"gui/{uid}"
    _run_launchctl(
        ["launchctl", "bootout", f"{gui_domain}/{PLIST_LABEL}"],
        capture_output=True, text=True, check=False,
    )
    return _run_launchctl(
        ["launchctl", "bootstrap", gui_domain, str(plist_path)],
        capture_output=True, text=True, check=False,
    )
