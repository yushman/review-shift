"""findings -> unified diff, per ADR-013.

Localization matches `before` as a line sequence in a bounded window, resolves ties and
overlaps, then lets `git diff --no-index` compute headers/hunks/offsets instead of us
formatting them by hand (ADR-013 §4 — that hand-rolled path is the class of bug this avoids).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src import gitutil
from src.redact import MARKER_PREFIX

Finding = dict[str, Any]

WINDOW = 20

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

# Findings without `before`/`after` are valid observations (ADR-003) that never enter
# localization at all, so they need a label outside ADR-013 §7's closed status list
# (stale/ambiguous/conflict/redacted/outdated), which only covers findings that *attempted*
# to become a patch and failed.
NO_FIX = "no_fix"

# A `before` containing the secret-redaction marker means the model saw masked text at this
# location (ADR-008 point 3) — its proposed `after` is never trustworthy against the real
# file content, so this is decided before localization even runs, not by a failed match.
REDACTED = "redacted"


@dataclass
class LocalizedFinding:
    index: int
    finding: Finding
    status: str  # applicable | stale | ambiguous | conflict | no_fix | redacted
    match_start: int | None = None  # 0-based line index, inclusive
    match_end: int | None = None  # 0-based line index, inclusive


def _normalize_lines(text: str) -> list[str]:
    if text.startswith("﻿"):
        text = text[1:]
    return [line.rstrip() for line in text.splitlines()]


def _find_all(haystack: list[str], needle: list[str], lo: int, hi: int) -> list[int]:
    if not needle or not haystack:
        return []
    n = len(needle)
    last_start = min(hi, len(haystack) - n)
    return [i for i in range(lo, last_start + 1) if haystack[i : i + n] == needle]


def localize(before: str, file_text: str, line: int) -> tuple[str, int | None, int | None]:
    """Returns (status, match_start, match_end) — 0-based inclusive line indices."""
    before_lines = _normalize_lines(before)
    file_lines = _normalize_lines(file_text)
    if not before_lines:
        return "stale", None, None

    win_lo = max(0, line - 1 - WINDOW)
    win_hi = min(len(file_lines) - 1, line - 1 + WINDOW)
    in_window = _find_all(file_lines, before_lines, win_lo, win_hi)

    def span(start: int) -> tuple[int, int]:
        return start, start + len(before_lines) - 1

    if in_window:
        if len(in_window) == 1:
            return ("applicable", *span(in_window[0]))
        distances = [abs((s + 1) - line) for s in in_window]
        nearest = min(distances)
        nearest_matches = [s for s, d in zip(in_window, distances, strict=True) if d == nearest]
        if len(nearest_matches) == 1:
            return ("applicable", *span(nearest_matches[0]))
        return "ambiguous", None, None

    whole = _find_all(file_lines, before_lines, 0, len(file_lines) - 1)
    if not whole:
        return "stale", None, None
    if len(whole) == 1:
        return ("applicable", *span(whole[0]))
    return "ambiguous", None, None


def _span(lf: LocalizedFinding) -> tuple[int, int]:
    """match_start/match_end are always set once status is 'applicable' (see localize()) —
    this makes that invariant checkable instead of sprinkling `# type: ignore` around it."""
    assert lf.match_start is not None and lf.match_end is not None
    return lf.match_start, lf.match_end


def resolve(findings: list[Finding], repo_root: Path, head_sha: str) -> list[LocalizedFinding]:
    """Localize every finding, then resolve overlaps by severity (ADR-013 §2-3)."""
    file_cache: dict[str, str] = {}
    localized: list[LocalizedFinding] = []
    for i, finding in enumerate(findings):
        before = finding.get("before")
        if not before:
            localized.append(LocalizedFinding(index=i, finding=finding, status=NO_FIX))
            continue
        if MARKER_PREFIX in before:
            localized.append(LocalizedFinding(index=i, finding=finding, status=REDACTED))
            continue
        content = file_cache.get(finding["file"])
        if content is None:
            content = gitutil.show_file(repo_root, head_sha, finding["file"])
            file_cache[finding["file"]] = content
        status, start, end = localize(before, content, finding["line"])
        localized.append(
            LocalizedFinding(
                index=i, finding=finding, status=status, match_start=start, match_end=end
            )
        )

    applicable = [lf for lf in localized if lf.status == "applicable"]
    # Load-bearing for the bucket loop below: ascending (file, start) means whatever is being
    # processed always has start >= every entry already in the bucket, which is what makes
    # checking only the *first* overlap in the bucket equivalent to checking all of them (two
    # already-kept, mutually non-overlapping entries can never both be overlapped by one later
    # entry — proof in devlog.md "Day 12"). Changing this sort key without re-checking that
    # argument can silently reintroduce overlapping "applicable" entries.
    applicable.sort(key=lambda lf: (lf.finding["file"], _span(lf)[0], lf.index))
    kept_by_file: dict[str, list[LocalizedFinding]] = {}
    for lf in applicable:
        lf_start, lf_end = _span(lf)
        bucket = kept_by_file.setdefault(lf.finding["file"], [])
        overlap = next(
            (k for k in bucket if lf_start <= _span(k)[1] and _span(k)[0] <= lf_end),
            None,
        )
        if overlap is None:
            bucket.append(lf)
            continue
        if SEVERITY_RANK[lf.finding["severity"]] > SEVERITY_RANK[overlap.finding["severity"]]:
            overlap.status = "conflict"
            bucket.remove(overlap)
            bucket.append(lf)
        else:
            lf.status = "conflict"
    return localized


def _split_preserving_eol(text: str) -> tuple[list[str], str, bool]:
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.splitlines(), eol, text.endswith(("\n", "\r\n"))


def _apply_edits(orig_text: str, edits: list[tuple[int, int, str]]) -> str:
    """`edits` is a list of (match_start, match_end, after), 0-based inclusive, non-overlapping,
    sorted ascending."""
    lines, eol, ends_with_newline = _split_preserving_eol(orig_text)
    out: list[str] = []
    cursor = 0
    for start, end, after in edits:
        out.extend(lines[cursor:start])
        out.extend(after.splitlines() if after else [])
        cursor = end + 1
    out.extend(lines[cursor:])
    result = eol.join(out)
    if ends_with_newline and out:
        result += eol
    return result


class PatchError(RuntimeError):
    pass


def generate_and_verify(
    localized: list[LocalizedFinding],
    repo_root: Path,
    head_sha: str,
    run_id: str,
    statuses: tuple[str, ...] = ("applicable",),
) -> tuple[str | None, str | None]:
    """Builds a unified diff from findings whose status is in `statuses`, verifies it with
    `git apply --check`, and only then returns it. Returns (diff_text, error) — diff_text is
    None (and never written to disk by the caller) when there is nothing to patch or the check
    fails, per ADR-013 §5.
    """
    by_file: dict[str, list[LocalizedFinding]] = {}
    for lf in localized:
        if lf.status in statuses and lf.match_start is not None:
            by_file.setdefault(lf.finding["file"], []).append(lf)
    if not by_file:
        return None, None

    tmp_root = Path(tempfile.mkdtemp(prefix=f"review-shift-{run_id}-"))
    try:
        diff_parts: list[str] = []
        for relpath, lfs in sorted(by_file.items()):
            orig_text = gitutil.show_file(repo_root, head_sha, relpath)
            lfs_sorted = sorted(lfs, key=lambda lf: _span(lf)[0])
            edits = [(*_span(lf), lf.finding.get("after") or "") for lf in lfs_sorted]
            modified_text = _apply_edits(orig_text, edits)

            a_path = tmp_root / "a" / relpath
            b_path = tmp_root / "b" / relpath
            check_path = tmp_root / "check" / relpath
            copies = ((a_path, orig_text), (b_path, modified_text), (check_path, orig_text))
            for p, content in copies:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)

            proc = subprocess.run(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--no-color",
                    "--no-prefix",
                    "-U3",
                    "--",
                    f"a/{relpath}",
                    f"b/{relpath}",
                ],
                cwd=tmp_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode not in (0, 1):
                raise PatchError(f"git diff --no-index failed for {relpath}: {proc.stderr}")
            if proc.returncode == 1:
                diff_parts.append(proc.stdout)

        diff_text = "".join(diff_parts)
        if not diff_text:
            return None, None

        check_dir = tmp_root / "check"
        check = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", "-"],
            cwd=check_dir,
            input=diff_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0:
            return None, check.stderr.strip()
        return diff_text, None
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
