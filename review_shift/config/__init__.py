"""Load `.review-shift/config.{yml,json}`, merge env and CLI overrides (flags win), validate
against the `version: 1` schema, and derive `config_hash` — TDR FR-5, ADR-009.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from review_shift.config import migrations
from review_shift.config.schema import DEFAULTS, ENV_VAR_PATHS, SCHEMA_V2

__all__ = ["ConfigError", "ConfigValidationError", "LoadedConfig", "load_config"]


class ConfigError(RuntimeError):
    pass


class ConfigValidationError(ConfigError):
    pass


@dataclass
class LoadedConfig:
    data: dict[str, Any]
    config_hash: str
    migrated: bool
    source_path: Path | None


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _find_config_path(repo_root: Path) -> Path | None:
    base = repo_root / ".review-shift"
    for name in ("config.yml", "config.yaml", "config.json"):
        candidate = base / name
        if candidate.exists():
            return candidate
    return None


def _parse_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text()
    if path.suffix == ".json":
        parsed = json.loads(text)
    else:
        parsed = yaml.safe_load(text) or {}
    if not isinstance(parsed, dict):
        raise ConfigValidationError(f"{path}: config must be a mapping at the top level")
    return parsed


def _coerce_env_value(raw: str, schema_type: dict[str, Any]) -> Any:
    if "type" in schema_type:
        t = schema_type["type"]
    elif "enum" in schema_type or "const" in schema_type:
        return raw
    else:
        t = "string"
    if t == "boolean":
        if raw.lower() in ("1", "true", "yes"):
            return True
        if raw.lower() in ("0", "false", "no"):
            return False
        raise ConfigValidationError(f"cannot parse {raw!r} as a boolean")
    if t == "integer":
        return int(raw)
    if t == "number":
        return float(raw)
    return raw


def _schema_for_path(path: tuple[str, ...]) -> dict[str, Any]:
    node = SCHEMA_V2
    for part in path:
        node = node["properties"][part]
    return node


def _env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for var_name, path in ENV_VAR_PATHS.items():
        if var_name not in env:
            continue
        value = _coerce_env_value(env[var_name], _schema_for_path(path))
        node = overrides
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return overrides


def _config_hash(data: dict[str, Any]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def load_config(
    repo_root: Path,
    *,
    config_path: Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> LoadedConfig:
    path = config_path or _find_config_path(repo_root)
    file_data: dict[str, Any] = _parse_config_file(path) if path else {}

    try:
        migrated_data, was_migrated = migrations.migrate(file_data)
    except migrations.UnrecognizedConfigVersion as exc:
        raise ConfigValidationError(str(exc)) from exc

    merged = _deep_merge(DEFAULTS, migrated_data)
    merged["version"] = migrations.CURRENT_VERSION
    if env:
        merged = _deep_merge(merged, _env_overrides(env))
    if cli_overrides:
        merged = _deep_merge(merged, cli_overrides)

    try:
        jsonschema.validate(merged, SCHEMA_V2)
    except jsonschema.ValidationError as exc:
        raise ConfigValidationError(f"{path or '<no config file>'}: {exc.message}") from exc

    return LoadedConfig(
        data=merged,
        config_hash=_config_hash(merged),
        migrated=was_migrated,
        source_path=path,
    )
