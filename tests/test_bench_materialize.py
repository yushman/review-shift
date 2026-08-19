"""Blobless materialization: reuses an existing clone, fails loudly with its reason when a
clone or fetch fails (tasks.md 2.3-2.4, spec "Upstream repository unavailable")."""
import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from bench.materialize import MaterializeError, clone_dir, ensure_sha, materialize_repo


def test_materialize_reuses_existing_clone(tmp_path: Path):
    dest = clone_dir("pydantic", tmp_path)
    dest.mkdir(parents=True)
    with mock_patch("bench.materialize._run") as run_mock:
        result = materialize_repo("pydantic", "https://example.com/x.git", tmp_path)
    assert result == dest
    run_mock.assert_not_called()


def test_materialize_clone_failure_raises_with_reason(tmp_path: Path):
    failure = subprocess.CompletedProcess([], 128, stdout="", stderr="repository not found")
    with mock_patch("bench.materialize._run", return_value=failure):
        with pytest.raises(MaterializeError, match="repository not found"):
            materialize_repo("ghost", "https://example.com/ghost.git", tmp_path)


def test_ensure_sha_present_does_not_fetch(tmp_path: Path):
    present = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    with mock_patch("bench.materialize._run", return_value=present) as run_mock:
        ensure_sha(tmp_path, "abc123")
    run_mock.assert_called_once()


def test_ensure_sha_fetch_failure_raises(tmp_path: Path):
    missing = subprocess.CompletedProcess([], 1, stdout="", stderr="not found")
    fetch_failure = subprocess.CompletedProcess([], 1, stdout="", stderr="fetch failed")
    with mock_patch("bench.materialize._run", side_effect=[missing, fetch_failure]):
        with pytest.raises(MaterializeError, match="fetch failed"):
            ensure_sha(tmp_path, "abc123")
