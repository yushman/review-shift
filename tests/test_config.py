"""config/: load, merge (file -> env -> flags), validate, config_hash, migrations (ADR-009).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import config


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


MINIMAL_YAML = """
version: 1
depth: medium
discovery:
  patterns: ["feature/*"]
"""


def test_loads_minimal_config(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, MINIMAL_YAML)

    loaded = config.load_config(tmp_path)

    assert loaded.data["version"] == 1
    assert loaded.data["depth"] == "medium"
    assert loaded.data["discovery"]["patterns"] == ["feature/*"]
    # defaults filled in for everything the file omitted
    assert loaded.data["discovery"]["max_age_hours"] == 24
    assert loaded.data["discovery"]["max_branches_per_run"] == 10
    assert loaded.data["base_branch"] == "auto"


def test_missing_config_file_uses_all_defaults(tmp_path: Path):
    loaded = config.load_config(tmp_path)
    assert loaded.data["version"] == 1
    assert loaded.data["depth"] == "medium"
    assert loaded.data["discovery"]["patterns"] == []
    assert loaded.data["discovery"]["discover_all"] is False


def test_json_config_is_also_supported(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.json"
    _write(cfg_path, json.dumps({"version": 1, "depth": "low"}))
    loaded = config.load_config(tmp_path)
    assert loaded.data["depth"] == "low"


def test_unknown_field_fails_loudly(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, "version: 1\nnonexistent_field: true\n")

    with pytest.raises(config.ConfigValidationError, match="nonexistent_field"):
        config.load_config(tmp_path)


def test_unknown_nested_field_fails_loudly(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, "version: 1\ndiscovery:\n  typo_field: true\n")

    with pytest.raises(config.ConfigValidationError, match="typo_field"):
        config.load_config(tmp_path)


def test_patch_auto_fix_min_severity_defaults_to_high(tmp_path: Path):
    loaded = config.load_config(tmp_path)
    assert loaded.data["patch"]["auto_fix_min_severity"] == "high"


def test_patch_unknown_field_fails_loudly(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, "version: 1\npatch:\n  typo_field: true\n")

    with pytest.raises(config.ConfigValidationError, match="typo_field"):
        config.load_config(tmp_path)


def test_patch_auto_fix_min_severity_rejects_value_outside_enum(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, "version: 1\npatch:\n  auto_fix_min_severity: extreme\n")

    with pytest.raises(config.ConfigValidationError):
        config.load_config(tmp_path)


def test_cli_flag_overrides_file_value(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, "version: 1\ndepth: low\n")

    loaded = config.load_config(tmp_path, cli_overrides={"depth": "medium"})
    assert loaded.data["depth"] == "medium"


def test_env_overrides_file_but_flag_overrides_env(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, "version: 1\ndepth: low\n")

    loaded = config.load_config(
        tmp_path, env={"REVIEW_SHIFT__DEPTH": "medium"}
    )
    assert loaded.data["depth"] == "medium"

    loaded = config.load_config(
        tmp_path,
        env={"REVIEW_SHIFT__DEPTH": "medium"},
        cli_overrides={"depth": "low"},
    )
    assert loaded.data["depth"] == "low"


def test_config_hash_same_for_equivalent_config_from_different_sources(tmp_path: Path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _write(repo_a / ".review-shift" / "config.yml", "version: 1\ndepth: medium\n")
    _write(repo_b / ".review-shift" / "config.yml", "version: 1\n")

    loaded_a = config.load_config(repo_a)
    loaded_b = config.load_config(repo_b, cli_overrides={"depth": "medium"})

    assert loaded_a.config_hash == loaded_b.config_hash


def test_config_hash_differs_when_effective_config_differs(tmp_path: Path):
    _write(tmp_path / ".review-shift" / "config.yml", "version: 1\ndepth: medium\n")
    loaded_medium = config.load_config(tmp_path)
    loaded_low = config.load_config(tmp_path, cli_overrides={"depth": "low"})
    assert loaded_medium.config_hash != loaded_low.config_hash


def test_old_version_migrates_in_memory_without_touching_disk(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    original_text = "depth: medium\n"  # no `version` field at all -- pre-ADR-009 config
    _write(cfg_path, original_text)

    loaded = config.load_config(tmp_path)

    assert loaded.data["version"] == 1
    assert loaded.migrated is True
    assert cfg_path.read_text() == original_text  # untouched on disk


def test_unrecognized_version_fails_loudly(tmp_path: Path):
    cfg_path = tmp_path / ".review-shift" / "config.yml"
    _write(cfg_path, "version: 99\n")

    with pytest.raises(config.ConfigValidationError, match="99"):
        config.load_config(tmp_path)


def test_current_version_is_not_reported_as_migrated(tmp_path: Path):
    _write(tmp_path / ".review-shift" / "config.yml", "version: 1\n")
    loaded = config.load_config(tmp_path)
    assert loaded.migrated is False


# --- migrations.py scaffold, unit-level -----------------------------------------------


def test_migrate_unversioned_to_v1():
    from src.config import migrations

    migrated, was_migrated = migrations.migrate({"depth": "medium"})
    assert migrated["version"] == 1
    assert was_migrated is True


def test_migrate_current_version_is_noop():
    from src.config import migrations

    migrated, was_migrated = migrations.migrate({"version": 1, "depth": "medium"})
    assert was_migrated is False
    assert migrated == {"version": 1, "depth": "medium"}


def test_migrate_unrecognized_version_raises():
    from src.config import migrations

    with pytest.raises(migrations.UnrecognizedConfigVersion):
        migrations.migrate({"version": 42})
