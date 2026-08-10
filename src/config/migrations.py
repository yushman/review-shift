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

CURRENT_VERSION = 1


class UnrecognizedConfigVersion(RuntimeError):
    pass


def _v0_to_v1(cfg: dict[str, Any]) -> dict[str, Any]:
    return {**cfg, "version": 1}


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    0: _v0_to_v1,
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
