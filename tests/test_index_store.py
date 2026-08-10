"""index_store.py: idempotency key, index.json read-modify-write, atomic latest swap — all
under the batch lock per ADR-007. NFR-1 / system-analysis.md F4 requires one cache-miss test
per idempotency-key component plus one cache-hit test; those live here since the key
computation and the cache lookup against index.json are both index_store's job.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from review_shift import index_store

BASE_KEY_ARGS = dict(
    head_sha="a" * 40, base_sha="b" * 40, depth="medium",
    config_hash="c" * 64, prompt_hash="d" * 64,
)


def test_idempotency_key_is_stable_for_same_inputs():
    k1 = index_store.compute_idempotency_key(**BASE_KEY_ARGS)
    k2 = index_store.compute_idempotency_key(**BASE_KEY_ARGS)
    assert k1 == k2


def test_idempotency_key_changes_when_head_sha_changes():
    k1 = index_store.compute_idempotency_key(**BASE_KEY_ARGS)
    changed = dict(BASE_KEY_ARGS, head_sha="e" * 40)
    assert index_store.compute_idempotency_key(**changed) != k1


def test_idempotency_key_changes_when_base_sha_changes():
    k1 = index_store.compute_idempotency_key(**BASE_KEY_ARGS)
    changed = dict(BASE_KEY_ARGS, base_sha="e" * 40)
    assert index_store.compute_idempotency_key(**changed) != k1


def test_idempotency_key_changes_when_depth_changes():
    k1 = index_store.compute_idempotency_key(**BASE_KEY_ARGS)
    changed = dict(BASE_KEY_ARGS, depth="low")
    assert index_store.compute_idempotency_key(**changed) != k1


def test_idempotency_key_changes_when_config_hash_changes():
    k1 = index_store.compute_idempotency_key(**BASE_KEY_ARGS)
    changed = dict(BASE_KEY_ARGS, config_hash="f" * 64)
    assert index_store.compute_idempotency_key(**changed) != k1


def test_idempotency_key_changes_when_prompt_hash_changes():
    k1 = index_store.compute_idempotency_key(**BASE_KEY_ARGS)
    changed = dict(BASE_KEY_ARGS, prompt_hash="f" * 64)
    assert index_store.compute_idempotency_key(**changed) != k1


def test_load_index_on_missing_file_returns_empty_index(tmp_path: Path):
    idx = index_store.load_index(tmp_path)
    assert idx == {"schema_version": 1, "runs": []}


def test_write_index_atomic_then_load_roundtrips(tmp_path: Path):
    data = {"schema_version": 1, "runs": [{"run_id": "x", "idempotency_key": "k"}]}
    index_store.write_index_atomic(tmp_path, data)
    assert index_store.load_index(tmp_path) == data
    # no leftover tmp file
    assert not list(tmp_path.glob("*.tmp"))


def test_find_cache_hit_returns_none_when_no_match(tmp_path: Path):
    idx = {"schema_version": 1, "runs": [
        {"idempotency_key": "other", "status": "ok", "run_id": "r1"},
    ]}
    assert index_store.find_cache_hit(idx, "k") is None


def test_find_cache_hit_ignores_non_ok_status(tmp_path: Path):
    idx = {"schema_version": 1, "runs": [
        {"idempotency_key": "k", "status": "error", "run_id": "r1"},
    ]}
    assert index_store.find_cache_hit(idx, "k") is None


def test_find_cache_hit_returns_matching_ok_entry(tmp_path: Path):
    idx = {"schema_version": 1, "runs": [
        {"idempotency_key": "k", "status": "ok", "run_id": "r1"},
        {"idempotency_key": "k", "status": "ok", "run_id": "r2"},
    ]}
    hit = index_store.find_cache_hit(idx, "k")
    assert hit is not None
    assert hit["run_id"] == "r2"  # most recent match wins


def test_swap_latest_writes_symlink_and_fallback_text(tmp_path: Path):
    (tmp_path / "2026-01-01T00-00-00Z-feature-x").mkdir()
    index_store.swap_latest(tmp_path, "2026-01-01T00-00-00Z-feature-x")
    latest = tmp_path / "latest"
    assert latest.is_symlink()
    assert os.readlink(latest) == "2026-01-01T00-00-00Z-feature-x"
    assert (tmp_path / "latest.txt").read_text().strip() == "2026-01-01T00-00-00Z-feature-x"


def test_swap_latest_replaces_existing_symlink_atomically(tmp_path: Path):
    (tmp_path / "run-a").mkdir()
    (tmp_path / "run-b").mkdir()
    index_store.swap_latest(tmp_path, "run-a")
    index_store.swap_latest(tmp_path, "run-b")
    assert os.readlink(tmp_path / "latest") == "run-b"
    assert (tmp_path / "latest.txt").read_text().strip() == "run-b"
    # no leftover tmp symlink/file
    assert not list(tmp_path.glob(".latest.*.tmp"))


def test_write_index_atomic_leaves_valid_json_even_if_called_repeatedly(tmp_path: Path):
    for i in range(5):
        data = {"schema_version": 1, "runs": [{"run_id": str(i)}]}
        index_store.write_index_atomic(tmp_path, data)
    final = json.loads((tmp_path / "index.json").read_text())
    assert final["runs"] == [{"run_id": "4"}]
