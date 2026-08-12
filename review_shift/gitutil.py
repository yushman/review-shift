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


def rev_list_direct(repo_root: Path, anchor: str, base: str) -> list[str]:
    """Commits put directly on `base` since `anchor`, oldest first (design.md D3/D4):
    `--first-parent --no-merges` excludes both a merge commit's own diff and every commit that
    arrived through a merged branch, so branch work already reviewed via merge-base diffing is
    never selected again here."""
    out = _run(
        repo_root, ["rev-list", "--reverse", "--first-parent", "--no-merges", f"{anchor}..{base}"]
    )
    return [line for line in out.splitlines() if line]


def is_ancestor(repo_root: Path, sha: str, ref: str) -> bool:
    """`git merge-base --is-ancestor sha ref` — exit 0/1 are the yes/no answer, not errors
    (used both to validate the trunk watermark and to classify a finding's commit as still
    local or already pushed)."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", sha, ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise GitError(f"git merge-base --is-ancestor failed: {proc.stderr.strip()}")
    return proc.returncode == 0


def commit_diff(repo_root: Path, sha: str) -> str:
    """One trunk unit's own diff against its parent (or the empty tree for a root commit),
    with the same flags the branch path uses (`merge_base_diff`): `-M
    --ignore-submodules=all`. `git show` (rather than `diff sha^ sha`) handles a parentless
    root commit without a special case."""
    return _run(
        repo_root, ["show", "-M", "--no-color", "--ignore-submodules=all", "--format=", sha]
    )


_BLAME_METADATA_PREFIXES = (
    "author", "committer", "summary", "previous", "filename", "boundary",
)


def blame_line_map(
    repo_root: Path, head_sha: str, path: str
) -> tuple[dict[int, str], dict[str, str]]:
    """One `git blame --line-porcelain` pass (design.md D5): returns `(line -> commit sha at
    head_sha, commit sha -> author name)`. `--line-porcelain` repeats full metadata for every
    line (unlike `--porcelain`, which dedups), so every content line's header is directly
    readable without carrying state from an earlier block."""
    out = _run(repo_root, ["blame", "--line-porcelain", head_sha, "--", path])
    line_map: dict[int, str] = {}
    authors: dict[str, str] = {}
    current_sha: str | None = None
    for line in out.splitlines():
        if line.startswith("\t"):
            continue
        if line.startswith(_BLAME_METADATA_PREFIXES):
            if line.startswith("author ") and current_sha is not None:
                authors.setdefault(current_sha, line[len("author "):])
            continue
        parts = line.split()
        sha_token = parts[0] if parts else ""
        if len(sha_token) == 40 and all(c in "0123456789abcdef" for c in sha_token):
            current_sha = sha_token
            final_line = int(parts[2])
            line_map[final_line] = current_sha
    return line_map, authors


def resolve_base_branch(repo_root: Path, base_branch: str) -> str:
    """`base_branch: auto` resolves to `origin/HEAD`'s target branch name; any other value
    passes through unresolved (TDR FR-3)."""
    if base_branch != "auto":
        return base_branch
    ref = _run(repo_root, ["symbolic-ref", "refs/remotes/origin/HEAD"]).strip()
    return ref.rsplit("/", 1)[-1]
