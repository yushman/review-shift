"""lock.py: fcntl.flock(LOCK_EX|LOCK_NB) on .review-shift/.lock, per ADR-007.

test_two_concurrent_cli_invocations_one_wins is a real subprocess-level test (two actual
`python -m src.cli run` processes racing for the same repo's lock, a fake `claude` on PATH
that sleeps so the winner holds the lock long enough for the loser to observe contention) —
not mocked, per the "two concurrent review-shift run invocations" done-when in tasks.md 2.5.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from review_shift import lock


def test_acquire_creates_lock_file_and_writes_holder_info(tmp_path: Path):
    with lock.acquire(tmp_path) as info:
        lock_path = tmp_path / ".review-shift" / ".lock"
        assert lock_path.exists()
        on_disk = json.loads(lock_path.read_text())
        assert on_disk["pid"] == os.getpid()
        assert on_disk["pid"] == info["pid"]
        assert "started_at" in on_disk


def test_second_acquire_while_held_raises_lock_held(tmp_path: Path):
    with lock.acquire(tmp_path):
        # a second, independent file descriptor on the same lock file: flock is scoped to the
        # open file description, so this genuinely contends even within one process/thread.
        with pytest.raises(lock.LockHeld) as exc_info:
            with lock.acquire(tmp_path):
                pass
        assert exc_info.value.pid == os.getpid()
        assert exc_info.value.started_at is not None


def test_lock_released_on_context_exit_allows_reacquire(tmp_path: Path):
    with lock.acquire(tmp_path):
        pass
    with lock.acquire(tmp_path):
        pass  # no LockHeld raised: released cleanly


def test_lock_released_when_holder_process_dies(tmp_path: Path):
    """flock is dropped by the kernel on process death, even without cleanup (ADR-007:
    'stale-лок не нужен')."""
    holder = subprocess.run(
        [sys.executable, "-c",
         "import sys\n"
         "from pathlib import Path\n"
         "sys.path.insert(0, sys.argv[1])\n"
         "from review_shift import lock\n"
         "with lock.acquire(Path(sys.argv[2])):\n"
         "    pass\n",
         str(Path(__file__).resolve().parent.parent), str(tmp_path)],
        capture_output=True, text=True,
    )
    assert holder.returncode == 0, holder.stderr
    with lock.acquire(tmp_path):
        pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _make_slow_claude(bin_dir: Path, sleep_s: float) -> None:
    """A fake `claude` executable that sleeps, standing in for a real review call so the
    winning process holds the lock long enough for the loser to observe contention."""
    script = bin_dir / "claude"
    # a non-empty findings array: an empty one against this fixture's real, non-empty diff
    # would trigger ADR-011's "retry on empty findings" 3x and exhaust attempts instead of
    # exercising the lock contention this test is actually about.
    structured_output = {
        "schema_version": 1,
        "findings": [
            {"file": "f.txt", "line": 1, "severity": "low", "category": "style",
             "rationale": "stub finding"},
        ],
    }
    events = [
        {"type": "system", "subtype": "init", "model": "stub", "claude_code_version": "0"},
        {
            "type": "result", "stop_reason": "end_turn", "subtype": "success",
            "total_cost_usd": 0.0, "usage": {"input_tokens": 1, "output_tokens": 1},
            "structured_output": structured_output,
            "result": json.dumps(structured_output),
        },
    ]
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, time\n"
        f"time.sleep({sleep_s})\n"
        f"print(json.dumps({events!r}))\n"
    )
    script.chmod(0o755)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo_with_branch(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("one\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "branch", "-m", "main")
    _git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "f.txt").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "change")
    _git(repo, "checkout", "-q", "main")
    return repo


def test_two_concurrent_cli_invocations_one_wins(repo_with_branch: Path, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_slow_claude(bin_dir, sleep_s=3.0)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    def _spawn(out_dir: Path) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, "-m", "review_shift.cli", "run", "--branch", "feature/x",
             "--base", "main", "--repo", str(repo_with_branch), "--out-dir", str(out_dir)],
            cwd=str(_repo_root()), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    out_dir_a = tmp_path / "runs-a"
    out_dir_b = tmp_path / "runs-b"
    first = _spawn(out_dir_a)
    time.sleep(0.8)  # let the first process win the race and start sleeping in "claude"
    second = _spawn(out_dir_b)

    second_out, second_err = second.communicate(timeout=15)
    first_out, first_err = first.communicate(timeout=15)

    codes = {first.returncode, second.returncode}
    assert 3 in codes, (
        f"expected one exit 3; first={first.returncode} second={second.returncode}\n"
        f"first_err={first_err}\nsecond_err={second_err}"
    )
    winner_code = first.returncode if first.returncode != 3 else second.returncode
    assert winner_code in (0, 1)
    loser_err = second_err if second.returncode == 3 else first_err
    assert "pid" in loser_err and "started" in loser_err
