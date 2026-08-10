"""Exclusive advisory lock for a whole batch, per ADR-007.

`fcntl.flock(LOCK_EX | LOCK_NB)` on `.review-shift/.lock`: held for the entire batch,
including `index.json` and `latest` writes (batch.py acquires it once, not per branch). No
stale-lock handling is needed — flock is released by the kernel when the holding process
dies, so a fresh acquire always reflects a live holder.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class LockHeld(RuntimeError):
    """Another run holds `.review-shift/.lock`; the caller should exit 3 (ADR-007/ADR-010)."""

    def __init__(self, pid: int | None, started_at: str | None):
        self.pid = pid
        self.started_at = started_at
        super().__init__(f"another run in progress (pid {pid}, started at {started_at})")


@contextmanager
def acquire(repo_root: Path) -> Iterator[dict[str, Any]]:
    """Acquire the batch lock at `<repo_root>/.review-shift/.lock`.

    Raises LockHeld immediately (LOCK_NB) if another live process holds it. On success,
    yields `{"pid": ..., "started_at": ...}` — the same info this process just wrote to the
    lock file — and releases on context exit (including on exception).
    """
    lock_dir = repo_root / ".review-shift"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".lock"

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            pid, started_at = _read_holder(lock_path)
            raise LockHeld(pid, started_at) from exc

        info: dict[str, Any] = {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps(info).encode())
        os.fsync(fd)
        try:
            yield info
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_holder(lock_path: Path) -> tuple[int | None, str | None]:
    try:
        data = json.loads(lock_path.read_text())
        return data.get("pid"), data.get("started_at")
    except (OSError, ValueError):
        return None, None
