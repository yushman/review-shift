"""`review-shift init`, `init launchd`, `init skill`, `doctor` at the CLI layer --
environment-setup spec.

`init launchd` never touches the real `pmset`/`launchctl` state here: every test stubs
`src.launchd_ops._run_pmset` before calling `cli.main`, per the orchestrator directive that
this is a real, system-wide macOS setting outside the sandbox of any one repo.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from review_shift import cli, launchd_ops


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "test")
    (r / "f.txt").write_text("one\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "initial")
    return r


_NO_PMSET_SCHEDULE = subprocess.CompletedProcess([], 0, "Repeating power events:\nNone\n", "")
_OK_VERSION = subprocess.CompletedProcess(["claude", "--version"], 0, "2.1.226 (Claude Code)\n", "")


# --- init -------------------------------------------------------------------------------------


def test_init_writes_config_and_git_exclude(repo: Path):
    rc = cli.main(["init", "--repo", str(repo)])

    assert rc == 0
    config_path = repo / ".review-shift" / "config.yml"
    assert config_path.exists()
    loaded = yaml.safe_load(config_path.read_text())
    assert loaded["version"] == 1

    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert ".review-shift/runs/" in exclude


def test_init_does_not_touch_gitignore(repo: Path):
    gitignore = repo / ".gitignore"
    gitignore.write_text("node_modules/\n")

    cli.main(["init", "--repo", str(repo)])

    assert gitignore.read_text() == "node_modules/\n"


def test_init_is_idempotent_without_force(repo: Path):
    cli.main(["init", "--repo", str(repo)])
    config_path = repo / ".review-shift" / "config.yml"
    config_path.write_text("version: 1\ndepth: low\n")

    rc = cli.main(["init", "--repo", str(repo)])

    assert rc == 0
    assert "depth: low" in config_path.read_text()


def test_init_force_overwrites_existing_config(repo: Path):
    cli.main(["init", "--repo", str(repo)])
    config_path = repo / ".review-shift" / "config.yml"
    config_path.write_text("version: 1\ndepth: low\n")

    rc = cli.main(["init", "--repo", str(repo), "--force"])

    assert rc == 0
    assert "depth: low" not in config_path.read_text()


def test_init_running_twice_does_not_duplicate_exclude_entry(repo: Path):
    cli.main(["init", "--repo", str(repo)])
    cli.main(["init", "--repo", str(repo)])

    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".review-shift/runs/") == 1


# --- init skill ---------------------------------------------------------------------------------


def test_init_skill_writes_canonical_content_on_fresh_repo(repo: Path):
    skill_path = repo / ".claude" / "skills" / "review-shift" / "SKILL.md"
    assert not skill_path.exists()

    rc = cli.main(["init", "--repo", str(repo), "skill"])

    assert rc == 0
    assert skill_path.read_text() == cli.PLUGIN_SKILL_PATH.read_text()


def test_init_skill_overwrites_stale_content_on_rerun(repo: Path):
    skill_path = repo / ".claude" / "skills" / "review-shift" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("stale content from a previous review-shift version\n")

    rc = cli.main(["init", "--repo", str(repo), "skill"])

    assert rc == 0
    content = skill_path.read_text()
    assert content == cli.PLUGIN_SKILL_PATH.read_text()
    assert "stale content" not in content


# --- doctor -----------------------------------------------------------------------------------


def test_doctor_exits_zero_on_clean_repo(repo: Path, monkeypatch):
    monkeypatch.setattr("review_shift.doctor._run_version_cmd", lambda: _OK_VERSION)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _NO_PMSET_SCHEDULE)

    rc = cli.main(["doctor", "--repo", str(repo)])

    assert rc == 0


def test_doctor_exits_nonzero_when_a_check_fails(repo: Path, monkeypatch):
    def raise_not_found() -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    monkeypatch.setattr("review_shift.doctor._run_version_cmd", raise_not_found)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _NO_PMSET_SCHEDULE)

    rc = cli.main(["doctor", "--repo", str(repo)])

    assert rc == 2


def test_doctor_prints_every_check_not_just_the_first_failure(repo: Path, monkeypatch, capsys):
    def raise_not_found() -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    monkeypatch.setattr("review_shift.doctor._run_version_cmd", raise_not_found)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _NO_PMSET_SCHEDULE)

    cli.main(["doctor", "--repo", str(repo)])

    out = capsys.readouterr().out
    for name in ("claude_version", "auth", "config_version", "absolute_paths",
                  "log_directory", "plist_matches_config", "pmset_matches_config",
                  "runs_not_staged"):
        assert name in out


# --- init launchd -------------------------------------------------------------------------------


def test_init_launchd_refuses_when_doctor_fails(repo: Path, tmp_path: Path, monkeypatch):
    def raise_not_found() -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    monkeypatch.setattr("review_shift.doctor._run_version_cmd", raise_not_found)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: None)
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _NO_PMSET_SCHEDULE)
    plist_path = tmp_path / "com.user.review-shift.plist"
    monkeypatch.setattr(launchd_ops, "PLIST_PATH", plist_path)

    cli.main(["init", "--repo", str(repo)])
    rc = cli.main(["init", "--repo", str(repo), "launchd"])

    assert rc == 2
    assert not plist_path.exists()


def _fake_ok_launchctl(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_init_launchd_writes_plist_and_creates_log_dir_and_registers_pmset(
    repo: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("review_shift.doctor._run_version_cmd", lambda: _OK_VERSION)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: f"/usr/local/bin/{name}")
    pmset_calls: list[list[str]] = []

    def fake_pmset(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        pmset_calls.append(cmd)
        if cmd[:2] == ["pmset", "-g"]:
            return _NO_PMSET_SCHEDULE
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd_ops, "_run_pmset", fake_pmset)
    monkeypatch.setattr(launchd_ops, "_run_launchctl", _fake_ok_launchctl)
    plist_path = tmp_path / "com.user.review-shift.plist"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(launchd_ops, "PLIST_PATH", plist_path)
    monkeypatch.setattr(launchd_ops, "LOG_DIR", log_dir)

    cli.main(["init", "--repo", str(repo)])
    rc = cli.main(["init", "--repo", str(repo), "launchd"])

    assert rc == 0
    assert plist_path.exists()
    assert log_dir.is_dir()
    assert any(c[:4] == ["sudo", "pmset", "repeat", "wakeorpoweron"] for c in pmset_calls)


def test_init_launchd_never_overwrites_existing_pmset_schedule(
    repo: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("review_shift.doctor._run_version_cmd", lambda: _OK_VERSION)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: f"/usr/local/bin/{name}")
    existing_schedule = subprocess.CompletedProcess(
        [], 0, "Repeating power events:\n  wakeorpoweron at 6:00AM every day \n", ""
    )
    register_calls: list[list[str]] = []

    def fake_pmset(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["pmset", "-g"]:
            return existing_schedule
        register_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd_ops, "_run_pmset", fake_pmset)
    monkeypatch.setattr(launchd_ops, "_run_launchctl", _fake_ok_launchctl)
    plist_path = tmp_path / "com.user.review-shift.plist"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(launchd_ops, "PLIST_PATH", plist_path)
    monkeypatch.setattr(launchd_ops, "LOG_DIR", log_dir)

    cli.main(["init", "--repo", str(repo)])
    rc = cli.main(["init", "--repo", str(repo), "launchd"])

    assert rc == 0
    assert register_calls == []  # never called pmset repeat -- refused, warned instead
    assert plist_path.exists()  # the plist itself still gets written


def test_init_launchd_with_force_overwrites_existing_pmset_schedule(
    repo: Path, tmp_path: Path, monkeypatch
):
    """A judgment call this change makes explicit: the spec only requires *not silently*
    overwriting a foreign schedule, so an opt-in `--force-pmset` escape hatch is offered for
    the case where the existing schedule genuinely is review-shift's own from a previous
    install (re-running `init launchd` after changing `launchd.hour` in config, say)."""
    monkeypatch.setattr("review_shift.doctor._run_version_cmd", lambda: _OK_VERSION)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: f"/usr/local/bin/{name}")
    existing_schedule = subprocess.CompletedProcess(
        [], 0, "Repeating power events:\n  wakeorpoweron at 6:00AM every day \n", ""
    )
    register_calls: list[list[str]] = []

    def fake_pmset(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["pmset", "-g"]:
            return existing_schedule
        register_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd_ops, "_run_pmset", fake_pmset)
    monkeypatch.setattr(launchd_ops, "_run_launchctl", _fake_ok_launchctl)
    plist_path = tmp_path / "com.user.review-shift.plist"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(launchd_ops, "PLIST_PATH", plist_path)
    monkeypatch.setattr(launchd_ops, "LOG_DIR", log_dir)

    cli.main(["init", "--repo", str(repo)])
    rc = cli.main(["init", "--repo", str(repo), "launchd", "--force-pmset"])

    assert rc == 0
    assert len(register_calls) == 1


def test_init_launchd_bootstraps_the_job_via_the_launchctl_seam(
    repo: Path, tmp_path: Path, monkeypatch, capsys
):
    """`init launchd` must actually schedule the job (design.md's feature-freeze checkpoint:
    "the launchd job is installed and scheduled for tonight"), not just write the plist file
    -- but it must go through the mockable `_run_launchctl` seam, never a real
    `subprocess.run(["launchctl", ...])`, so this test (and every other `init launchd` test)
    never registers a real job on the machine running the tests."""
    monkeypatch.setattr("review_shift.doctor._run_version_cmd", lambda: _OK_VERSION)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _NO_PMSET_SCHEDULE)
    launchctl_calls: list[list[str]] = []

    def fake_launchctl(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        launchctl_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd_ops, "_run_launchctl", fake_launchctl)
    plist_path = tmp_path / "com.user.review-shift.plist"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(launchd_ops, "PLIST_PATH", plist_path)
    monkeypatch.setattr(launchd_ops, "LOG_DIR", log_dir)

    cli.main(["init", "--repo", str(repo)])
    rc = cli.main(["init", "--repo", str(repo), "launchd"])

    assert rc == 0
    bootstrap_calls = [c for c in launchctl_calls if "bootstrap" in c]
    assert len(bootstrap_calls) == 1
    assert bootstrap_calls[0][-1] == str(plist_path)
    assert "job installed and scheduled" in capsys.readouterr().out


def test_init_launchd_reports_failure_when_launchctl_bootstrap_fails(
    repo: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("review_shift.doctor._run_version_cmd", lambda: _OK_VERSION)
    monkeypatch.setattr("review_shift.doctor.shutil.which", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(launchd_ops, "_run_pmset", lambda *a, **k: _NO_PMSET_SCHEDULE)

    def fake_failing_launchctl(
        cmd: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "bootstrap" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "Bootstrap failed: 5: I/O error")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(launchd_ops, "_run_launchctl", fake_failing_launchctl)
    plist_path = tmp_path / "com.user.review-shift.plist"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(launchd_ops, "PLIST_PATH", plist_path)
    monkeypatch.setattr(launchd_ops, "LOG_DIR", log_dir)

    cli.main(["init", "--repo", str(repo)])
    rc = cli.main(["init", "--repo", str(repo), "launchd"])

    assert rc == 2
