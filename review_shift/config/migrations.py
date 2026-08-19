"""Forward-only config version migrations (ADR-009).

Migrations run in memory only. Persisting a migrated config to disk is `doctor --fix`'s job
alone, with a backup first — a headless nightly run must never rewrite the user's file.

`version` 0 means "no `version` field at all": every config written before ADR-009 existed.
Treating that as a real, migratable version (rather than a schema-validation failure) is the
whole point of ADR-009 — an old file must not break the 3:30am run.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

CURRENT_VERSION = 3

# restructure-depth-tiers D3: every level survived the relabel under a new name, so an exact
# remap always exists and anything less would silently change what a user's nightly run does.
_V2_TO_V3_DEPTH = {"low": "smoke", "medium": "low", "high": "medium"}


class UnrecognizedConfigVersion(RuntimeError):
    pass


def _v0_to_v1(cfg: dict[str, Any]) -> dict[str, Any]:
    return {**cfg, "version": 1}


def _v1_to_v2(cfg: dict[str, Any]) -> dict[str, Any]:
    """Inserts the `trunk` block (trunk-review capability) with its defaults; config-loading
    spec "Migration adds the trunk block to older configs" — a v1 config that already sets
    `trunk` (unlikely, since the field didn't exist) keeps its own value."""
    trunk = {"enabled": False, "max_commits_per_run": 10, **cfg.get("trunk", {})}
    return {**cfg, "version": 2, "trunk": trunk}


def _v2_to_v3(cfg: dict[str, Any]) -> dict[str, Any]:
    """Relabels `depth` one rung down (restructure-depth-tiers D3). A v2 config that omits
    `depth` was running at v2's own default, `medium` -- which is now called `low`; the value
    is materialized here rather than left to v3's default, which is a rung deeper and would
    change the run's behavior and its bill without the user touching anything.

    An unrecognised value is left as-is so schema validation refuses it by name, rather than
    being coerced to a level the user never asked for.
    """
    current = cfg.get("depth", "medium")
    return {**cfg, "version": 3, "depth": _V2_TO_V3_DEPTH.get(current, current)}


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: _v0_to_v1,
    1: _v1_to_v2,
    2: _v2_to_v3,
}


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Returns `(migrated_config, was_migrated)`. Applies migrations forward, one version at
    a time, until `version == CURRENT_VERSION`; raises `UnrecognizedConfigVersion` if no
    migration path exists from the config's current version."""
    version = config.get("version", 0)
    if version == CURRENT_VERSION:
        return config, False

    migrated = dict(config)
    migrated["version"] = version
    was_migrated = False
    while migrated["version"] != CURRENT_VERSION:
        step = MIGRATIONS.get(migrated["version"])
        if step is None:
            raise UnrecognizedConfigVersion(
                f"no migration path from config version {migrated['version']!r}"
            )
        migrated = step(migrated)
        was_migrated = True
    return migrated, was_migrated
