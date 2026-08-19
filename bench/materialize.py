"""Blobless clone materialization (design.md D2). A case records a repository and pinned
shas, never vendored code; content is fetched on demand into `bench/.work/` (gitignored) via
`git clone --filter=blob:none`, reusing an existing clone and fetching pinned shas as needed.
A repository or sha that cannot be materialized fails with its reason rather than being
silently skipped (spec "Upstream repository unavailable").
"""
from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["MaterializeError", "clone_dir", "ensure_sha", "materialize_repo"]

WORK_DIR = Path(__file__).resolve().parent / ".work"


class MaterializeError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def clone_dir(repo_id: str, work_dir: Path = WORK_DIR) -> Path:
    return work_dir / repo_id


def materialize_repo(repo_id: str, url: str, work_dir: Path = WORK_DIR) -> Path:
    """Clones `url` blobless into `work_dir/repo_id` if not already present; reuses the
    existing clone otherwise (task 2.3). Never touches a clone the harness didn't make itself
    -- `duckduckgo-android` and friends are cloned fresh here, never read from another
    project's clone.
    """
    dest = clone_dir(repo_id, work_dir)
    if dest.exists():
        return dest
    work_dir.mkdir(parents=True, exist_ok=True)
    proc = _run(["git", "clone", "--filter=blob:none", "--quiet", url, str(dest)])
    if proc.returncode != 0:
        raise MaterializeError(f"cannot clone {url}: {proc.stderr.strip()}")
    return dest


def ensure_sha(repo_dir: Path, sha: str) -> None:
    """Fetches `sha` if it is not already present locally -- a pinned sha may be reachable
    only via a branch the initial clone's default refspec did not cover.
    """
    check = _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=repo_dir)
    if check.returncode == 0:
        return
    proc = _run(["git", "fetch", "--quiet", "origin", sha], cwd=repo_dir)
    if proc.returncode != 0:
        raise MaterializeError(f"cannot fetch {sha} in {repo_dir}: {proc.stderr.strip()}")
