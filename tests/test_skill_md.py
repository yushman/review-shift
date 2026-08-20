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
    assert "depth <= low" in text
    # The retired value must be named as retired, not as a deeper level to reach for.
    assert "no longer exists" in text


def test_skill_md_documents_lock_reuse():
    text = CANONICAL_SKILL.read_text()
    assert ".review-shift/.lock" in text


def test_skill_md_documents_the_standalone_fallback_recipe():
    text = CANONICAL_SKILL.read_text()
    assert "review-shift run --branch <branch> --base <base> --depth smoke" in text
    assert "review-shift run --trunk --base <base> --depth smoke" in text


def test_plugin_manifest_version_tracks_the_package_version():
    """The bundled skill gates on `review-shift >= <version>`, so a plugin manifest that lags
    the package advertises a build whose own skill refuses to run against it. It drifted from
    0.1.2 to 0.2.0 unnoticed because nothing read this file -- the drift is only visible to a
    marketplace reviewer, or to a user whose install silently does less than it claims.
    """
    import json
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    manifest = json.loads(
        (root / "review_shift" / "plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert manifest["version"] == pyproject["project"]["version"]
