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

INDEX_SCHEMA_VERSION = 2  # 2: adds the `watermarks` map (trunk-review capability)


def compute_idempotency_key(
    *, head_sha: str, base_sha: str, depth: str, model: str, config_hash: str, prompt_hash: str
) -> str:
    """`idempotency_key = f(head_sha, base_sha, depth, model, config_hash, prompt_hash)` —
    NFR-1. Every component is part of the digest input; changing any one changes the key.

    `model` is the model *as requested* — the alias or full identifier that came from `--model`
    or `runtime.model` — not the identifier the CLI resolved it to (restructure-depth-tiers
    D6): `model_resolved` only exists after a call returns, and this key has to be computable
    before deciding whether to make the call. Two spellings of one model therefore miss each
    other and cost a redundant review, which is the right direction to be wrong in — a false
    miss is a bill, a false hit is an answer produced under conditions nobody asked for."""
    canonical = "\x1f".join([head_sha, base_sha, depth, model, config_hash, prompt_hash])
    return hashlib.sha256(canonical.encode()).hexdigest()


def compute_trunk_idempotency_key(
    *, commit_sha: str, depth: str, model: str, config_hash: str, prompt_hash: str
) -> str:
    """`f(commit_sha, depth, model, config_hash, prompt_hash)` — no `base_sha` (design.md D6).
    A commit's content is immutable, so this key is stable across the base head advancing,
    which is what makes bootstrap's re-enumeration (D2) resolve as cache hits instead of a
    re-pay. `model` is the requested string, on the same terms as the branch key above."""
    canonical = "\x1f".join([commit_sha, depth, model, config_hash, prompt_hash])
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


def read_watermark(index: dict[str, Any], branch: str) -> dict[str, Any] | None:
    """The trunk anchor for `branch`, or `None` when absent — including an `index.json`
    written before this change, which has no `watermarks` key at all (run-artifacts spec
    "Pre-existing index without watermarks"). `None` routes the caller to bootstrap."""
    watermarks: dict[str, Any] = index.get("watermarks", {})
    return watermarks.get(branch)


def write_watermark(
    index: dict[str, Any], branch: str, *, sha: str, run_id: str, at: str
) -> None:
    """Sets `branch`'s watermark on the in-memory `index` dict; the caller persists it with
    `write_index_atomic` under the same lock as the rest of the run's index writes."""
    index.setdefault("watermarks", {})[branch] = {"sha": sha, "run_id": run_id, "at": at}


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
