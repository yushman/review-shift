"""`index.json` read-modify-write, the atomic `latest`/`latest.txt` swap, and the idempotency
key/cache-lookup (NFR-1, system-analysis.md F4) — all ADR-007. Every call here happens under
`lock.acquire()`'s exclusive lock; this module does not lock anything itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

INDEX_SCHEMA_VERSION = 1


def compute_idempotency_key(
    *, head_sha: str, base_sha: str, depth: str, config_hash: str, prompt_hash: str
) -> str:
    """`idempotency_key = f(head_sha, base_sha, depth, config_hash, prompt_hash)` — NFR-1.
    Every component is part of the digest input; changing any one changes the key."""
    canonical = "\x1f".join([head_sha, base_sha, depth, config_hash, prompt_hash])
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_index(out_dir: Path) -> dict[str, Any]:
    path = out_dir / "index.json"
    if not path.exists():
        return {"schema_version": INDEX_SCHEMA_VERSION, "runs": []}
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def write_index_atomic(out_dir: Path, data: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "index.json"
    tmp = out_dir / f".index.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def find_cache_hit(index: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
    """The most recent successful (`status: ok`) entry for this key, or None. Only `ok`
    entries are cache-eligible — an errored/timed-out/refused prior attempt must not be
    reported as a hit (system-analysis.md F4)."""
    match = None
    for entry in index.get("runs", []):
        if entry.get("idempotency_key") == idempotency_key and entry.get("status") == "ok":
            match = entry
    return match


def swap_latest(out_dir: Path, run_dir_name: str) -> None:
    """Point `latest` (symlink) and `latest.txt` (fallback, ADR-007 — symlinks don't survive
    every sync tool/filesystem) at `run_dir_name`, both via tmp + `os.replace` for atomicity."""
    out_dir.mkdir(parents=True, exist_ok=True)

    tmp_link = out_dir / f".latest.{os.getpid()}.tmp"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(run_dir_name, tmp_link)
    os.replace(tmp_link, out_dir / "latest")

    tmp_txt = out_dir / f".latest.{os.getpid()}.txt.tmp"
    tmp_txt.write_text(run_dir_name + "\n")
    os.replace(tmp_txt, out_dir / "latest.txt")
