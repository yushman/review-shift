"""launchd_ops.py: plist rendering, pmset schedule parsing/registration, per ADR-005.

Every test here that would touch the real system's `pmset`/`launchctl` state instead calls
the render/parse functions directly or stubs the `_run_pmset` seam -- this module must never
shell out to the real `pmset` during tests (orchestrator directive: `pmset repeat` is a real,
system-wide macOS setting shared with everything else on the machine).
"""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

from src import launchd_ops


def _ctx(**overrides: object) -> launchd_ops.RenderContext:
    base = dict(
        install_prefix="/Users/test/.local/pipx/venvs/review-shift",
        node_bin="/Users/test/.nvm/versions/node/v20/bin",
        home="/Users/test",
        repo_root="/Users/test/proj/some-repo",
        hour=3,
        minute=30,
    )
    base.update(overrides)
    return launchd_ops.RenderContext(**base)  # type: ignore[arg-type]


def test_render_plist_substitutes_all_placeholders():
    rendered = launchd_ops.render_plist(_ctx())
    assert "{{" not in rendered
    assert "/Users/test/.local/pipx/venvs/review-shift/bin/review-shift" in rendered
    assert "/Users/test/.nvm/versions/node/v20/bin" in rendered
    assert "/Users/test/proj/some-repo" in rendered


def test_render_plist_has_no_run_at_load_or_keep_alive():
    rendered = launchd_ops.render_plist(_ctx())
    assert "RunAtLoad" not in rendered
    assert "KeepAlive" not in rendered


def test_render_plist_uses_caffeinate_wrapper():
    rendered = launchd_ops.render_plist(_ctx())
    assert "/usr/bin/caffeinate" in rendered
    assert "-i" in rendered
    assert "-m" in rendered


def test_render_plist_is_valid_plist_xml_with_expected_schedule():
    rendered = launchd_ops.render_plist(_ctx(hour=4, minute=15))
    parsed = plistlib.loads(rendered.encode())
    assert parsed["StartCalendarInterval"]["Hour"] == 4
    assert parsed["StartCalendarInterval"]["Minute"] == 15
    assert parsed["Label"] == "com.user.review-shift"
    assert "RunAtLoad" not in parsed
    assert "KeepAlive" not in parsed


def test_has_existing_repeat_schedule_false_when_none():
    output = "Repeating power events:\nNone\n"
    assert launchd_ops.has_existing_repeat_schedule(output) is False


def test_has_existing_repeat_schedule_true_when_present():
    output = "Repeating power events:\n  wakeorpoweron at 3:25AM every day \n"
    assert launchd_ops.has_existing_repeat_schedule(output) is True


def test_has_existing_repeat_schedule_false_on_unparseable_output():
    assert launchd_ops.has_existing_repeat_schedule("") is False


def test_register_pmset_schedule_builds_wake_command_five_minutes_early(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd_ops, "_run_pmset", fake_run)
    launchd_ops.register_pmset_schedule(hour=3, minute=30)

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:4] == ["sudo", "pmset", "repeat", "wakeorpoweron"]
    assert "03:25:00" in cmd


def test_register_pmset_schedule_wraps_past_midnight(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(launchd_ops, "_run_pmset", fake_run)
    launchd_ops.register_pmset_schedule(hour=0, minute=2)

    assert "23:57:00" in calls[0]


def test_read_pmset_schedule_uses_injected_seam_not_real_pmset(monkeypatch):
    """Confirms the read path goes through `_run_pmset`, the mockable seam -- never a bare
    `subprocess.run(["pmset", ...])` that a test could accidentally leave unmocked."""
    sentinel = subprocess.CompletedProcess(["pmset"], 0, stdout="Repeating power events:\nNone\n",
                                            stderr="")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return sentinel

    monkeypatch.setattr(launchd_ops, "_run_pmset", fake_run)
    output = launchd_ops.read_pmset_schedule()

    assert output == sentinel.stdout
    assert calls == [["pmset", "-g", "sched"]]


def test_extract_scheduled_time_parses_am_pm():
    output = "Repeating power events:\n  wakeorpoweron at 3:25AM every day \n"
    assert launchd_ops.extract_scheduled_time(output) == (3, 25)


def test_extract_scheduled_time_parses_pm():
    output = "Repeating power events:\n  wakeorpoweron at 11:05PM every day \n"
    assert launchd_ops.extract_scheduled_time(output) == (23, 5)


def test_extract_scheduled_time_none_when_absent():
    assert launchd_ops.extract_scheduled_time("Repeating power events:\nNone\n") is None


def test_read_installed_plist_returns_none_when_missing(tmp_path: Path):
    assert launchd_ops.read_installed_plist(tmp_path / "nope.plist") is None


def test_read_installed_plist_parses_schedule(tmp_path: Path):
    rendered = launchd_ops.render_plist(_ctx(hour=5, minute=45))
    plist_path = tmp_path / "com.user.review-shift.plist"
    plist_path.write_text(rendered)

    parsed = launchd_ops.read_installed_plist(plist_path)

    assert parsed is not None
    assert parsed["StartCalendarInterval"]["Hour"] == 5
    assert parsed["StartCalendarInterval"]["Minute"] == 45


def test_install_launchd_job_calls_bootout_then_bootstrap_via_the_seam(
    tmp_path: Path, monkeypatch
):
    """`install_launchd_job` must never call the real `launchctl` -- every test of it goes
    through `_run_launchctl`, the same mockable seam `_run_pmset` established for `pmset`."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd_ops, "_run_launchctl", fake_run)
    plist_path = tmp_path / "com.user.review-shift.plist"

    result = launchd_ops.install_launchd_job(plist_path)

    assert result.returncode == 0
    assert calls[0][:2] == ["launchctl", "bootout"]
    assert calls[1][:2] == ["launchctl", "bootstrap"]
    assert calls[1][-1] == str(plist_path)


def test_install_launchd_job_ignores_bootout_failure_on_first_install(
    tmp_path: Path, monkeypatch
):
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "bootout" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Could not find service")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd_ops, "_run_launchctl", fake_run)
    plist_path = tmp_path / "com.user.review-shift.plist"

    result = launchd_ops.install_launchd_job(plist_path)

    assert result.returncode == 0
