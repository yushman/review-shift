"""Thin wrappers around the read-only git calls this project makes.

Every write path in review-shift is forbidden (ADR-004, ADR-012): no checkout, no commit, no
`git apply` outside `--check`. Everything here is `rev-parse`/`show`/`diff`/`ls-tree`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _run(repo_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def rev_parse(repo_root: Path, ref: str) -> str:
    return _run(repo_root, ["rev-parse", ref]).strip()


def merge_base(repo_root: Path, base: str, branch: str) -> str:
    return _run(repo_root, ["merge-base", base, branch]).strip()


def merge_base_diff(repo_root: Path, base: str, branch: str) -> str:
    """`git diff --merge-base --ignore-submodules=all -M base branch` (TDR FR-3)."""
    return _run(
        repo_root,
        ["diff", "--merge-base", "--ignore-submodules=all", "-M", base, branch],
    )


def show_file(repo_root: Path, sha: str, path: str) -> str:
    """Read `path` as it existed at `sha`, never through the working tree (ADR-012)."""
    return _run(repo_root, ["show", f"{sha}:{path}"])


def ls_tree_files(repo_root: Path, sha: str) -> set[str]:
    out = _run(repo_root, ["ls-tree", "-r", "--name-only", sha])
    return {line for line in out.splitlines() if line}


def ls_files_staged(repo_root: Path, pathspec: str) -> list[str]:
    """Paths under `pathspec` currently in the index (staged), read via `git ls-files
    --cached` rather than `git diff --cached` so it works with zero commits too (doctor's
    "runs/ not staged" check must not require a HEAD to exist)."""
    out = _run(repo_root, ["ls-files", "--cached", "--", pathspec])
    return [line for line in out.splitlines() if line]


def resolve_base_branch(repo_root: Path, base_branch: str) -> str:
    """`base_branch: auto` resolves to `origin/HEAD`'s target branch name; any other value
    passes through unresolved (TDR FR-3)."""
    if base_branch != "auto":
        return base_branch
    ref = _run(repo_root, ["symbolic-ref", "refs/remotes/origin/HEAD"]).strip()
    return ref.rsplit("/", 1)[-1]
