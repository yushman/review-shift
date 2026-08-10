"""SKILL.md has no runnable logic (it is instructions for the model, not code), but its
frontmatter and the ADR-006 cap it documents are exactly the kind of thing that silently
drifts from the spec it is supposed to satisfy -- this pins the contract in a test rather
than only in prose.
"""
from __future__ import annotations

import yaml

from review_shift import cli

CANONICAL_SKILL = cli.PLUGIN_SKILL_PATH


def _frontmatter(text: str) -> dict[str, object]:
    assert text.startswith("---\n")
    _, fm, _ = text.split("---\n", 2)
    return yaml.safe_load(fm)  # type: ignore[no-any-return]


def test_canonical_skill_md_exists_with_expected_frontmatter():
    text = CANONICAL_SKILL.read_text()
    fm = _frontmatter(text)
    assert fm["name"] == "review-shift"
    assert "description" in fm


def test_skill_md_documents_the_one_branch_depth_cap():
    text = CANONICAL_SKILL.read_text()
    assert "one branch" in text
    assert "depth <= medium" in text or "depth: high" in text


def test_skill_md_documents_lock_reuse():
    text = CANONICAL_SKILL.read_text()
    assert ".review-shift/.lock" in text


def test_skill_md_documents_the_standalone_fallback_recipe():
    text = CANONICAL_SKILL.read_text()
    assert "review-shift run --branch $(git branch --show-current) --depth low" in text
