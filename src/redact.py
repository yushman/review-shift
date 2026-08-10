"""Mask secret values in a diff in place, per ADR-008.

Splitting on `"\\n"` and rejoining the same list guarantees the line-count invariant
(system-analysis.md F1) by construction: masking only ever mutates the *content* of an
existing element, never appends or drops one. No masking rule in this module is allowed to
insert a literal newline into a replacement value.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

# TDR §7 / ADR-008 point 1 — merged into the user's `scope.exclude_paths`, never replaced by
# it, because config-loading's plain dict merge overwrites lists wholesale (see
# `merge_exclude_paths`).
DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "**/.env*",
    "**/secrets/**",
    "**/*.pem",
    "**/*.key",
)

# Public so patch.py can recognize a masked region in a finding's `before` without
# duplicating the literal (ADR-008 point 3).
MARKER_PREFIX = "<<REDACTED:"
_PLACEHOLDER = MARKER_PREFIX + "{kind}>>"

# Named, inline secret shapes (ADR-008 point 2 / design.md's "not gitleaks" non-goal) — a
# fixed, small set, not a pattern-discovery engine. Each pattern's `value` group is what gets
# replaced; everything else on the line survives untouched.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key_id", re.compile(r"(?P<value>AKIA[0-9A-Z]{16})")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)(?:secret|token|password|passwd|api[_-]?key|access[_-]?key|"
            r"private[_-]?key|credential)\w*\s*[:=]\s*(?P<quote>['\"])"
            r"(?P<value>(?:(?!(?P=quote)).)+)(?P=quote)"
        ),
    ),
]


@dataclass
class RedactResult:
    diff_text: str
    secrets_redacted: int
    secrets_redacted_files: list[str]


def merge_exclude_paths(configured: list[str] | None) -> list[str]:
    """Union, never subtraction — no config value can drop a default entry."""
    merged = list(configured or [])
    for default in DEFAULT_EXCLUDE_PATTERNS:
        if default not in merged:
            merged.append(default)
    return merged


def _matches_excluded(path: str, patterns: list[str]) -> bool:
    # Prepend "/" so a bare top-level file (".env", no directory) still satisfies a
    # "**/.env*"-style pattern under plain fnmatch, which has no path-boundary concept.
    candidate = "/" + path
    return any(fnmatch.fnmatch(candidate, pattern) for pattern in patterns)


def _current_file(line: str, current: str | None) -> str | None:
    if line.startswith("+++ "):
        path = line[4:]
        if path == "/dev/null":
            return current
        return path[2:] if path.startswith("b/") else path
    return current


def _mask_patterns(line: str) -> tuple[str, int]:
    """Patterns can overlap on the same value (an `AWS_ACCESS_KEY_ID = "AKIA..."` line matches
    both the AKIA-shaped pattern and the generic secret-assignment one) — skip a value that's
    already a placeholder rather than double-mask and double-count it."""
    count = 0
    for kind, pattern in _SECRET_PATTERNS:

        def _replace(m: re.Match[str], kind: str = kind) -> str:
            nonlocal count
            value = m.group("value")
            if value.startswith(MARKER_PREFIX):
                return m.group(0)
            count += 1
            return m.group(0).replace(value, _PLACEHOLDER.format(kind=kind))

        line = pattern.sub(_replace, line)
    return line, count


def redact_diff(diff_text: str, exclude_patterns: list[str]) -> RedactResult:
    lines = diff_text.split("\n")
    out: list[str] = []
    secrets_redacted = 0
    redacted_files: dict[str, None] = {}  # insertion-ordered set
    current_file: str | None = None

    for line in lines:
        current_file = _current_file(line, current_file)

        is_added = line.startswith("+") and not line.startswith("+++")
        is_removed = line.startswith("-") and not line.startswith("---")
        is_context = line.startswith(" ")
        is_content = is_added or is_removed or is_context

        if is_content and current_file and _matches_excluded(current_file, exclude_patterns):
            marker, body = line[0], line[1:]
            if body.strip():
                out.append(marker + _PLACEHOLDER.format(kind="excluded_path"))
                secrets_redacted += 1
                redacted_files[current_file] = None
            else:
                out.append(line)
            continue

        masked, count = _mask_patterns(line)
        out.append(masked)
        if count:
            secrets_redacted += count
            if current_file:
                redacted_files[current_file] = None

    return RedactResult(
        diff_text="\n".join(out),
        secrets_redacted=secrets_redacted,
        secrets_redacted_files=list(redacted_files),
    )
