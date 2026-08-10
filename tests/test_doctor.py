"""doctor.py: the eight independent checks behind `review-shift doctor`, per the
environment-setup spec ("doctor checks eight independent conditions") and ADR-005.

Each check is tested in isolation so a change to one can't silently stop covering another --
this is exactly the area system-analysis.md §8 flagged as "worse than estimated."

`claude --version` and auth are the two checks that must go through the real seams
(`doctor._run_version_cmd`, `review.check_auth`/`review._run_preflight`) rather than being
faked in a way that could hide a real regression in those checks themselves; every other
check here is pure/file-based and needs no subprocess seam at all except pmset, which is
`launchd_ops._run_pmset` (never real `pmset`).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

from review_shift import doctor, launchd_ops


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _ok_version_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["claude", "--version"], 0, stdout="2.1.226 (Claude Code)\n",
                                        stderr="")


def _no_pmset_schedule_proc() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "Repeating power events:\nNone\n", "")


# --- claude --version ---------------------------------------------------------------------


def test_check_claude_version_passes_for_current_version(monkeypatch):
    monkeypatch.setattr(doctor, "_run_version_cmd", lambda: _ok_version_proc())
    result = doctor.check_claude_version()
    assert result.ok is True


def test_check_claude_version_fails_for_old_version(monkeypatch):
    proc = subprocess.CompletedProcess(["claude", "--version"], 0, stdout="1.9.0 (Claude Code)\n",
                                        stderr="")
    monkeypatch.setattr(doctor, "_run_version_cmd", lambda: proc)
    result = doctor.check_claude_version()
    assert result.ok is False
    assert "1.9.0" in result.detail


def test_check_claude_version_fails_when_cli_missing(monkeypatch):
    def raise_not_found() -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(doctor, "_run_version_cmd", raise_not_found)
    result = doctor.check_claude_version()
    assert result.ok is False
    assert "not found" in result.detail


# --- auth liveness (reuses review.check_auth) ----------------------------------------------


def test_check_auth_passes_by_default_stubbed_preflight():
    # conftest.py's autouse fixture stubs src.review._run_preflight to a passing result.
    result = doctor.check_auth_liveness()
    assert result.ok is True


def test_check_auth_fails_when_preflight_reports_auth_error():
    failing = subprocess.CompletedProcess(["claude"], 1, stdout="", stderr="not logged in")
    with mock_patch("review_shift.review._run_preflight", return_value=failing):
        result = doctor.check_auth_liveness()
    assert result.ok is False


# --- config version -------------------------------------------------------------------------


def test_check_config_version_passes_with_no_config_file(tmp_path: Path):
    repo = _repo(tmp_path)
    result = doctor.check_config_version(repo)
    assert result.ok is True


def test_check_config_version_passes_with_current_version(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".review-shift").mkdir()
    (repo / ".review-shift" / "config.yml").write_text("version: 1\n")
    result = doctor.check_config_version(repo)
    assert result.ok is True


def test_check_config_version_fails_for_unrecognized_version(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / ".review-shift").mkdir()
    (repo / ".review-shift" / "config.yml").write_text("version: 99\n")
    result = doctor.check_config_version(repo)
    assert result.ok is False


# --- absolute paths --------------------------------------------------------------------------


def test_check_absolute_paths_passes_when_both_resolve_absolute(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which",
                         lambda name: f"/usr/local/bin/{name}")
    result = doctor.check_absolute_paths()
    assert result.ok is True


def test_check_absolute_paths_fails_when_claude_missing(monkeypatch):
    def fake_which(name: str) -> str | None:
        return None if name == "claude" else f"/usr/local/bin/{name}"

    monkeypatch.setattr(doctor.shutil, "which", fake_which)
    result = doctor.check_absolute_paths()
    assert result.ok is False
    assert "claude" in result.detail


# --- log directory ---------------------------------------------------------------------------


def test_check_log_directory_passes_when_no_plist_installed_yet(tmp_path: Path):
    result = doctor.check_log_directory(log_dir=tmp_path / "nope", plist_installed=False)
    assert result.ok is True


def test_check_log_directory_fails_when_plist_installed_but_dir_missing(tmp_path: Path):
    result = doctor.check_log_directory(log_dir=tmp_path / "nope", plist_installed=True)
    assert result.ok is False


def test_check_log_directory_passes_when_dir_exists(tmp_path: Path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    result = doctor.check_log_directory(log_dir=log_dir, plist_installed=True)
    assert result.ok is True


# --- plist agrees with config -----------------------------------------------------------------


def test_check_plist_matches_config_passes_when_not_installed(tmp_path: Path):
    result = doctor.check_plist_matches_config(
        plist_path=tmp_path / "nope.plist", expected_hour=3, expected_minute=30
    )
    assert result.ok is True


def test_check_plist_matches_config_passes_on_agreement(tmp_path: Path):
    ctx = launchd_ops.RenderContext(
        install_prefix="/x", node_bin="/y", home="/z", repo_root="/r", hour=3, minute=30,
    )
    plist_path = tmp_path / "com.user.review-shift.plist"
    plist_path.write_text(launchd_ops.render_plist(ctx))
    result = doctor.check_plist_matches_config(
        plist_path=plist_path, expected_hour=3, expected_minute=30
    )
    assert result.ok is True


def test_check_plist_matches_config_fails_on_drift(tmp_path: Path):
    ctx = launchd_ops.RenderContext(
        install_prefix="/x", node_bin="/y", home="/z", repo_root="/r", hour=3, minute=30,
    )
    plist_path = tmp_path / "com.user.review-shift.plist"
    plist_path.write_text(launchd_ops.render_plist(ctx))
    result = doctor.check_plist_matches_config(
        plist_path=plist_path, expected_hour=4, expected_minute=0
    )
    assert result.ok is False


# --- pmset agrees with config ------------------------------------------------------------------


def test_check_pmset_matches_config_passes_when_wake_machine_disabled(monkeypatch):
    monkeypatch.setattr(launchd_ops, "_run_pmset",
                         lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    result = doctor.check_pmset_matches_config(expected_hour=3, expected_minute=30,
                                                wake_machine=False, plist_installed=True)
    assert result.ok is True


def test_check_pmset_matches_config_passes_when_nothing_registered_yet(monkeypatch):
    proc = subprocess.CompletedProcess([], 0, "Repeating power events:\nNone\n", "")
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: proc)
    result = doctor.check_pmset_matches_config(expected_hour=3, expected_minute=30,
                                                wake_machine=True, plist_installed=True)
    assert result.ok is True


def test_check_pmset_matches_config_passes_on_agreement(monkeypatch):
    proc = subprocess.CompletedProcess(
        [], 0, "Repeating power events:\n  wakeorpoweron at 3:25AM every day \n", ""
    )
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: proc)
    result = doctor.check_pmset_matches_config(expected_hour=3, expected_minute=30,
                                                wake_machine=True, plist_installed=True)
    assert result.ok is True


def test_check_pmset_matches_config_fails_on_drift(monkeypatch):
    proc = subprocess.CompletedProcess(
        [], 0, "Repeating power events:\n  wakeorpoweron at 6:00AM every day \n", ""
    )
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: proc)
    result = doctor.check_pmset_matches_config(expected_hour=3, expected_minute=30,
                                                wake_machine=True, plist_installed=True)
    assert result.ok is False


def test_check_pmset_matches_config_passes_when_not_installed_even_if_a_foreign_schedule_exists(
    monkeypatch,
):
    """The fix for a real deadlock: `init launchd` gates on doctor passing, and its own
    "don't overwrite a foreign pmset schedule" handling is a separate, non-blocking warning
    -- so on a machine that already has someone else's pmset schedule but no review-shift
    plist yet, this check must not block the very first `init launchd`."""
    proc = subprocess.CompletedProcess(
        [], 0, "Repeating power events:\n  wakeorpoweron at 6:00AM every day \n", ""
    )
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: proc)
    result = doctor.check_pmset_matches_config(expected_hour=3, expected_minute=30,
                                                wake_machine=True, plist_installed=False)
    assert result.ok is True


# --- runs/ not staged ----------------------------------------------------------------------


def test_check_runs_not_staged_passes_when_clean(tmp_path: Path):
    repo = _repo(tmp_path)
    result = doctor.check_runs_not_staged(repo)
    assert result.ok is True


def test_check_runs_not_staged_fails_when_staged(tmp_path: Path):
    repo = _repo(tmp_path)
    runs = repo / ".review-shift" / "runs"
    runs.mkdir(parents=True)
    (runs / "index.json").write_text("{}")
    _git(repo, "add", ".review-shift/runs/index.json")
    result = doctor.check_runs_not_staged(repo)
    assert result.ok is False


# --- run_doctor: all eight, independent, doesn't stop at first failure ----------------------


def test_run_doctor_returns_eight_checks_and_all_pass_on_clean_repo(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(doctor, "_run_version_cmd", lambda: _ok_version_proc())
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _no_pmset_schedule_proc())

    checks = doctor.run_doctor(repo, plist_path=tmp_path / "nope.plist",
                                log_dir=tmp_path / "nope-log")

    assert len(checks) == 8
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_run_doctor_reports_every_check_even_when_several_fail(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)

    def raise_not_found() -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    monkeypatch.setattr(doctor, "_run_version_cmd", raise_not_found)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    failing_auth = subprocess.CompletedProcess(["claude"], 1, stdout="", stderr="not logged in")
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _no_pmset_schedule_proc())
    with mock_patch("review_shift.review._run_preflight", return_value=failing_auth):
        checks = doctor.run_doctor(repo, plist_path=tmp_path / "nope.plist",
                                    log_dir=tmp_path / "nope-log")

    assert len(checks) == 8
    failed_names = {c.name for c in checks if not c.ok}
    assert "claude_version" in failed_names
    assert "auth" in failed_names
    assert "absolute_paths" in failed_names
    # config_version, log_directory (not installed => pass), plist/pmset (not installed =>
    # pass) and runs_not_staged still evaluated independently, not short-circuited.
    passed_names = {c.name for c in checks if c.ok}
    assert "config_version" in passed_names
    assert "runs_not_staged" in passed_names


def test_run_doctor_does_not_crash_on_unrecognized_config_version(tmp_path: Path, monkeypatch):
    """A broken config must show up as one failed check among eight, not take down the whole
    doctor run before the other seven get a chance to report (system-analysis.md §8)."""
    repo = _repo(tmp_path)
    (repo / ".review-shift").mkdir()
    (repo / ".review-shift" / "config.yml").write_text("version: 99\n")
    monkeypatch.setattr(doctor, "_run_version_cmd", lambda: _ok_version_proc())
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _no_pmset_schedule_proc())

    checks = doctor.run_doctor(repo, plist_path=tmp_path / "nope.plist",
                                log_dir=tmp_path / "nope-log")

    assert len(checks) == 8
    by_name = {c.name: c for c in checks}
    assert by_name["config_version"].ok is False
    assert by_name["claude_version"].ok is True
    assert by_name["runs_not_staged"].ok is True
