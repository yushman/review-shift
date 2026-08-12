"""Trunk-review's survival filter and finding attribution, both from one blame pass per
touched file at the base head (design.md D5). A commit whose added lines have no survivor at
the head never reaches the model; a finding that does survive carries its originating commit
and author, read from the same index.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from review_shift import gitutil


@dataclass
class BlameIndex:
    line_maps: dict[str, dict[int, str]] = field(default_factory=dict)  # file -> {line: sha}
    authors: dict[str, str] = field(default_factory=dict)  # commit sha -> author name
    missing_files: set[str] = field(default_factory=set)  # touched files gone by head_sha

    def surviving_shas(self, path: str) -> set[str]:
        return set(self.line_maps.get(path, {}).values())

    def attribute(self, path: str, line: int) -> tuple[str | None, str | None]:
        sha = self.line_maps.get(path, {}).get(line)
        if sha is None:
            return None, None
        return sha, self.authors.get(sha)


def touched_files(diff_text: str) -> set[str]:
    """Post-image paths from a unified diff's `+++ b/<path>` headers — what survival and
    attribution both care about; a pure deletion's `+++ /dev/null` is not a path anything can
    localize against, so it is not included."""
    files: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            files.add(line[len("+++ b/"):])
    return files


def build_blame_index(repo_root: Path, head_sha: str, files: set[str]) -> BlameIndex:
    """One `git blame --line-porcelain` per file (design.md D5's "per file, not per unit" —
    cost does not grow with the number of reviewed commits). A file already deleted by
    `head_sha` has no blame output at all; it contributes zero survivors and is recorded in
    `missing_files` so `commit_outcome` can tell "file removed" apart from "content replaced"
    instead of collapsing both into the same skip reason."""
    index = BlameIndex()
    for path in sorted(files):
        try:
            line_map, authors = gitutil.blame_line_map(repo_root, head_sha, path)
        except gitutil.GitError:
            index.missing_files.add(path)
            continue
        index.line_maps[path] = line_map
        index.authors.update(authors)
    return index


def commit_outcome(index: BlameIndex, files: set[str], sha: str) -> str:
    """`"survives"` if at least one line in any of `files` still blames to `sha` at the head —
    the direct mitigation for reviewing a commit whose content has since been fully replaced.
    Otherwise `"removed"` when every touched file is gone at the head (deleted, not rewritten
    — a distinct situation a reader should be told apart from `"superseded"`, which means a
    later commit overwrote the same lines while the file itself still exists)."""
    if any(sha in index.surviving_shas(path) for path in files):
        return "survives"
    if files and files <= index.missing_files:
        return "removed"
    return "superseded"
