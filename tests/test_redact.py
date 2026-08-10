"""Value masking never changes line count (system-analysis.md F1, ADR-008)."""
from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from src.patch import REDACTED, resolve
from src.redact import DEFAULT_EXCLUDE_PATTERNS, merge_exclude_paths, redact_diff

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Lines built only from these characters, plus occasional injected secret-shaped values, so
# hypothesis explores structure (quotes, `=`, digits) without ever generating a literal "\n"
# mid-element -- that would make the input itself ambiguous about what "one line" means.
_LINE_CHARS = st.text(
    alphabet=st.characters(blacklist_characters="\n", blacklist_categories=("Cs",)),
    max_size=40,
)

_SECRET_LOOKALIKES = st.sampled_from(
    [
        'AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
        "AKIAABCDEFGHIJKLMNOP",
        'API_TOKEN: "sk-abcdefghijklmnopqrstuvwx"',
        'password = "hunter2hunter2"',
        "plain context line with nothing secret",
        "",
    ]
)


def _diff_line(kind: str, body: str) -> str:
    prefix = {"add": "+", "remove": "-", "context": " "}[kind]
    return prefix + body


_diff_lines = st.builds(
    _diff_line,
    kind=st.sampled_from(["add", "remove", "context"]),
    body=st.one_of(_LINE_CHARS, _SECRET_LOOKALIKES),
)


@given(st.lists(_diff_lines, max_size=30))
def test_line_count_never_changes(lines: list[str]) -> None:
    diff_text = "\n".join(lines)
    result = redact_diff(diff_text, list(DEFAULT_EXCLUDE_PATTERNS))
    assert result.diff_text.count("\n") == diff_text.count("\n")
    assert len(result.diff_text.split("\n")) == len(diff_text.split("\n"))


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=500))
def test_line_count_never_changes_on_arbitrary_text(diff_text: str) -> None:
    result = redact_diff(diff_text, list(DEFAULT_EXCLUDE_PATTERNS))
    assert len(result.diff_text.split("\n")) == len(diff_text.split("\n"))


def test_aws_access_key_is_masked_in_place() -> None:
    diff_text = '+AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
    result = redact_diff(diff_text, list(DEFAULT_EXCLUDE_PATTERNS))
    assert "AKIAABCDEFGHIJKLMNOP" not in result.diff_text
    assert "<<REDACTED:" in result.diff_text
    assert result.secrets_redacted == 1


def test_line_with_no_secret_is_untouched() -> None:
    diff_text = "+def add(a, b):\n+    return a + b\n"
    result = redact_diff(diff_text, list(DEFAULT_EXCLUDE_PATTERNS))
    assert result.diff_text == diff_text
    assert result.secrets_redacted == 0
    assert result.secrets_redacted_files == []


def test_file_matching_exclude_pattern_gets_every_line_masked() -> None:
    diff_text = (
        "diff --git a/.env b/.env\n"
        "index 000..111 100644\n"
        "--- a/.env\n"
        "+++ b/.env\n"
        "@@ -1,1 +1,1 @@\n"
        "-OLD_VALUE=abc\n"
        "+DB_PASSWORD=supersecret\n"
    )
    result = redact_diff(diff_text, list(DEFAULT_EXCLUDE_PATTERNS))
    assert "supersecret" not in result.diff_text
    assert "abc" not in result.diff_text
    assert ".env" in result.secrets_redacted_files


def test_merge_exclude_paths_cannot_remove_defaults() -> None:
    merged = merge_exclude_paths([])
    for default in DEFAULT_EXCLUDE_PATTERNS:
        assert default in merged


def test_merge_exclude_paths_keeps_user_additions() -> None:
    merged = merge_exclude_paths(["**/*.custom"])
    assert "**/*.custom" in merged
    for default in DEFAULT_EXCLUDE_PATTERNS:
        assert default in merged


def test_aws_key_fixture_masks_to_same_length_and_finding_is_redacted() -> None:
    """Day 8's other 'done when': a real AWS-key-shaped diff, masked in place, with a
    finding that echoes the masked line back getting `status: redacted` (ADR-008 point 3)."""
    diff_text = (FIXTURES_DIR / "aws_key_diff.txt").read_text()
    result = redact_diff(diff_text, list(DEFAULT_EXCLUDE_PATTERNS))

    assert len(result.diff_text.split("\n")) == len(diff_text.split("\n"))
    assert "AKIAIOSFODNN7EXAMPLE" not in result.diff_text
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in result.diff_text
    assert result.secrets_redacted == 2
    assert "config/settings.py" in result.secrets_redacted_files

    masked_line = next(
        line for line in result.diff_text.split("\n") if line.startswith("+AWS_ACCESS_KEY_ID")
    )
    finding = {
        "file": "config/settings.py", "line": 2, "severity": "critical", "category": "security",
        "rationale": "hardcoded AWS access key", "before": masked_line[1:],
        "after": 'AWS_ACCESS_KEY_ID = "<redacted, rotate this key>"',
    }
    localized = resolve([finding], Path("."), "deadbeef")
    assert localized[0].status == REDACTED
