"""Mechanical half of ground-truth derivation (design.md D3, tasks.md section 3): lists
candidates and drafts case files. Never sets `confirmed_at` -- that is a human decision made
by reading the draft, not something this module is trusted to assert (spec "Ground truth is
confirmed by a human before the case is first run").
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Candidate", "IntroducingResult", "SOURCE_EXTENSIONS", "list_candidates",
    "resolve_introducing_commit", "draft_case",
]

SOURCE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "kotlin": (".kt", ".kts"),
    "python": (".py",),
    "go": (".go",),
}

# Subjects that describe tidying rather than a defect (design.md D3, proposal.md's
# nowinandroid measurement: six of fifteen were typo fixes, three were formatter runs).
EXCLUDE_SUBJECT = re.compile(
    r"\b(typo|lint|format(ting)?|spotless|rename|whitespace|ktlint|gofmt|black|"
    r"style|comment)\b",
    re.IGNORECASE,
)

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))?")
BLAME_HEADER_RE = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)")


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass(frozen=True)
class Candidate:
    sha: str
    subject: str
    files: tuple[str, ...]


def list_candidates(repo_dir: Path, language: str, limit: int = 500) -> list[Candidate]:
    """Fix-like commits, no merges, at most three changed files, at least one source file of
    `language`, excluding subjects matching typo/lint/format/spotless (task 3.1)."""
    log = _run(
        [
            "git", "log", "--no-merges", "--grep=^fix", "-i", "--pretty=format:%H%x1f%s",
            "-n", str(limit),
        ],
        repo_dir,
    )
    exts = SOURCE_EXTENSIONS[language]
    candidates: list[Candidate] = []
    for line in log.splitlines():
        if not line:
            continue
        sha, _, subject = line.partition("\x1f")
        if EXCLUDE_SUBJECT.search(subject):
            continue
        files_out = _run(
            ["git", "show", "--name-only", "--pretty=format:", sha], repo_dir,
        ).strip()
        files = tuple(f for f in files_out.splitlines() if f)
        if not files or len(files) > 3:
            continue
        if not any(f.endswith(exts) for f in files):
            continue
        candidates.append(Candidate(sha=sha, subject=subject, files=files))
    return candidates


def _removed_ranges(repo_dir: Path, fix_sha: str, file: str) -> list[tuple[int, int]]:
    """Old-side (`-a,b`) hunk ranges from the fix's diff for `file` -- the lines the fix
    deleted or replaced, in the fix's parent's coordinate space. A pure addition (no removed
    lines) yields nothing; there is nothing to blame."""
    diff = _run(["git", "diff", "-U0", f"{fix_sha}^", fix_sha, "--", file], repo_dir)
    ranges = []
    for line in diff.splitlines():
        m = HUNK_RE.match(line)
        if not m:
            continue
        count = int(m.group(2)) if m.group(2) is not None else 1
        if count == 0:
            continue
        start = int(m.group(1))
        ranges.append((start, start + count - 1))
    return ranges


def _blame_orig_lines(
    repo_dir: Path, rev: str, file: str, start: int, end: int
) -> list[tuple[str, int]]:
    """[(sha, orig_lineno)] for each blamed line in `start..end` of `file` at `rev`, via
    `--line-porcelain` so every physical line repeats its own header (simpler to parse than
    the metadata-once-per-commit default). `orig_lineno` is the line's number in the commit
    that introduced it -- the coordinate space `review-shift run --branch I --base I^` (D3)
    numbers its findings against, not `rev`'s own line numbers."""
    out = _run(
        ["git", "blame", "--line-porcelain", f"-L{start},{end}", rev, "--", file], repo_dir,
    )
    results = []
    for line in out.splitlines():
        m = BLAME_HEADER_RE.match(line)
        if m:
            results.append((m.group(1), int(m.group(2))))
    return results


@dataclass(frozen=True)
class IntroducingResult:
    file: str
    introducing_sha: str
    start_line: int
    end_line: int
    agreement: bool  # False when other files in the same fix blame to a different commit


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.endswith(("_test.go", "_test.py")) or name.startswith("test_")
        or "Test.kt" in name
    )


def _contiguous_runs(lines: list[int], gap: int = 3) -> list[tuple[int, int]]:
    """Splits blamed line numbers into contiguous runs, merging gaps of at most `gap` lines --
    two one-line matches forty lines apart that happen to share an introducing commit (a large
    refactor blames many unrelated spots to itself) are not one defect location, and collapsing
    them to a single min-max span produces a misleadingly wide, near-whole-file ground-truth
    range (found while drafting cli-003: an import removal and a `return` fourteen lines later
    both blamed to the same commit, 285 lines apart in its own coordinate space)."""
    ordered = sorted(set(lines))
    if not ordered:
        return []
    runs = []
    start = prev = ordered[0]
    for ln in ordered[1:]:
        if ln - prev <= gap:
            prev = ln
            continue
        runs.append((start, prev))
        start = prev = ln
    runs.append((start, prev))
    return runs


def resolve_introducing_commit(
    repo_dir: Path, fix_sha: str, files: tuple[str, ...]
) -> list[IntroducingResult]:
    """Blames the fix's removed lines against its parent, per file (task 3.2). A file with no
    removed lines (pure addition) contributes nothing -- left for the human confirming the
    draft to resolve by hand. Test-file blame does not vote for the overall introducing commit:
    a test file's history is noisier and does not tell you where a defect lives, and weighting
    it equally let a large test-file edit outvote a small, correct source-file match in practice
    (found while drafting cli-002 and cli-003 -- both mechanically picked the test file's
    commit until this exclusion was added). When files still disagree after that, every result
    is returned with `agreement=False` so the draft surfaces the disagreement instead of
    silently picking a side.
    """
    per_file: dict[str, dict[str, list[int]]] = {}
    for file in files:
        removed = _removed_ranges(repo_dir, fix_sha, file)
        if not removed:
            continue
        by_sha: dict[str, list[int]] = {}
        for start, end in removed:
            for sha, orig_line in _blame_orig_lines(repo_dir, f"{fix_sha}^", file, start, end):
                by_sha.setdefault(sha, []).append(orig_line)
        if by_sha:
            per_file[file] = by_sha

    if not per_file:
        return []

    overall: dict[str, int] = {}
    for file, by_sha in per_file.items():
        if _is_test_file(file):
            continue
        for sha, lines in by_sha.items():
            overall[sha] = overall.get(sha, 0) + len(lines)
    if not overall:  # every touched file was a test file
        for by_sha in per_file.values():
            for sha, lines in by_sha.items():
                overall[sha] = overall.get(sha, 0) + len(lines)
    dominant_sha = max(overall, key=lambda s: overall[s])

    results = []
    for file, by_sha in per_file.items():
        file_dominant = max(by_sha, key=lambda s: len(by_sha[s]))
        agreement = file_dominant == dominant_sha
        for start, end in _contiguous_runs(by_sha[file_dominant]):
            results.append(
                IntroducingResult(
                    file=file, introducing_sha=file_dominant,
                    start_line=start, end_line=end, agreement=agreement,
                )
            )
    return results


def draft_case(repo_dir: Path, repo_id: str, fix_sha: str, case_id: str) -> dict[str, Any]:
    """Builds a case-file draft dict for `fix_sha` (task 3.3). `confirmed_at` and `note` are
    left for a human to fill in; `introducing_sha` is the dominant commit across all touched
    files, and `ground_truth` covers only the files that agree with it -- files that disagree
    are omitted from `ground_truth` and named in `note` instead, for the human to resolve.
    """
    subject = _run(["git", "show", "-s", "--format=%s", fix_sha], repo_dir).strip()
    files_out = _run(
        ["git", "show", "--name-only", "--pretty=format:", fix_sha], repo_dir,
    ).strip()
    files = tuple(f for f in files_out.splitlines() if f)

    results = resolve_introducing_commit(repo_dir, fix_sha, files)
    agreeing = [r for r in results if r.agreement]
    disagreeing = [r for r in results if not r.agreement]

    introducing_sha = agreeing[0].introducing_sha if agreeing else ""
    ground_truth = [
        {"file": r.file, "start_line": r.start_line, "end_line": r.end_line} for r in agreeing
    ]

    note_lines = [f"fix subject: {subject}"]
    if disagreeing:
        disagree_desc = ", ".join(f"{r.file}->{r.introducing_sha[:12]}" for r in disagreeing)
        note_lines.append(f"DISAGREEMENT -- files blaming to a different commit: {disagree_desc}")
    if not results:
        note_lines.append("NO REMOVED LINES -- fix was a pure addition, blame found nothing")

    return {
        "id": case_id,
        "repo": repo_id,
        "fix_sha": fix_sha,
        "introducing_sha": introducing_sha,
        "diff_visible": True,
        "confirmed_at": None,
        "note": "\n".join(note_lines),
        "ground_truth": ground_truth,
    }


def write_draft(draft: dict[str, Any], cases_dir: Path) -> Path:
    cases_dir.mkdir(parents=True, exist_ok=True)
    path = cases_dir / f"{draft['id']}.yml"
    path.write_text(yaml.safe_dump(draft, sort_keys=False, allow_unicode=True))
    return path
