"""The `version: 3` config schema, normatively defined by TDR §7. `additionalProperties:
false` at every level is ADR-009's "unknown field is a hard error, never ignored" rule.
"""
from __future__ import annotations

from typing import Any

# restructure-depth-tiers D1/D2: the ladder was relabelled one rung down and `high` was
# removed outright. Every surface that accepts a depth (the CLI flag, the config schema)
# reads this one set, and every refusal reads the same message -- a user who types the
# retired name must be told what it maps to, never silently served a shallower level.
DEPTH_VALUES: tuple[str, ...] = ("smoke", "low", "medium")

_RELABEL_NOTE = (
    "the ladder was relabelled: the previous `high` is now `medium`, the previous `medium` "
    "is now `low`, and the previous `low` is now `smoke`"
)


def depth_error_message(value: object) -> str:
    accepted = ", ".join(DEPTH_VALUES)
    msg = f"invalid depth {value!r}: accepted values are {accepted}"
    if value == "high":
        return f"{msg} -- {_RELABEL_NOTE}"
    return msg


SCHEMA_V3: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["version"],
    "properties": {
        "version": {"const": 3},
        "depth": {"enum": list(DEPTH_VALUES)},
        "base_branch": {"type": "string"},
        "discovery": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "patterns": {"type": "array", "items": {"type": "string"}},
                "exclude_patterns": {"type": "array", "items": {"type": "string"}},
                "discover_all": {"type": "boolean"},
                "max_age_hours": {"type": "number", "exclusiveMinimum": 0},
                "max_branches_per_run": {"type": "integer", "exclusiveMinimum": 0},
            },
        },
        "scope": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "include_paths": {"type": "array", "items": {"type": "string"}},
                "exclude_paths": {"type": "array", "items": {"type": "string"}},
                "full_file_review": {"enum": ["auto", "always", "never"]},
            },
        },
        "output": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "dir": {"type": "string"},
                "retention_days": {"type": "integer", "exclusiveMinimum": 0},
                "retention_min_runs": {"type": "integer", "minimum": 0},
            },
        },
        "runtime": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "soft_timeout_minutes": {"type": "number", "exclusiveMinimum": 0},
                "hard_timeout_minutes": {"type": "number", "exclusiveMinimum": 0},
                "budget_usd": {"type": "number", "exclusiveMinimum": 0},
                "total_budget_usd": {"type": "number", "exclusiveMinimum": 0},
                "auth_preflight_budget_usd": {"type": "number", "exclusiveMinimum": 0},
                "model": {"type": "string"},
                "exit_zero_on_findings": {"type": "boolean"},
            },
        },
        "launchd": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "hour": {"type": "integer", "minimum": 0, "maximum": 23},
                "minute": {"type": "integer", "minimum": 0, "maximum": 59},
                "wake_machine": {"type": "boolean"},
            },
        },
        "patch": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "auto_fix_min_severity": {
                    "enum": ["critical", "high", "medium", "low", "info"]
                },
            },
        },
        "trunk": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "enabled": {"type": "boolean"},
                "max_commits_per_run": {"type": "integer", "exclusiveMinimum": 0},
            },
        },
    },
}

# Mirrors SCHEMA_V2's shape exactly (TDR §7's documented defaults) so a value's source (file,
# env, flag, or built-in default) never changes the merged result — required for config_hash
# to be stable across equivalent configs (config-loading spec, "Same effective config").
DEFAULTS: dict[str, Any] = {
    "depth": "medium",
    "base_branch": "auto",
    "discovery": {
        "patterns": [],
        "exclude_patterns": [],
        "discover_all": False,
        "max_age_hours": 24,
        "max_branches_per_run": 10,
    },
    "scope": {
        "include_paths": [],
        "exclude_paths": [
            "**/.env*", "**/secrets/**", "**/*.pem", "**/*.key", "**/*.lock", "**/dist/**",
        ],
        "full_file_review": "auto",
    },
    "output": {
        "dir": ".review-shift/runs",
        "retention_days": 14,
        "retention_min_runs": 3,
    },
    "runtime": {
        "soft_timeout_minutes": 15,
        "hard_timeout_minutes": 45,
        "budget_usd": 10.00,
        "total_budget_usd": 50.00,
        "auth_preflight_budget_usd": 0.01,
        "model": "sonnet",
        "exit_zero_on_findings": False,
    },
    "launchd": {
        "hour": 3,
        "minute": 30,
        "wake_machine": True,
    },
    "patch": {
        "auto_fix_min_severity": "high",
    },
    "trunk": {
        "enabled": False,
        "max_commits_per_run": 10,
    },
}

# Dotted schema path -> env var name. Scalar leaves only (lists/objects aren't practical to
# express as a single env var and aren't needed by any capability yet) -- FR-5 requires
# ENV-override but TDR doesn't fix a naming scheme, so this is this change's own convention:
# `REVIEW_SHIFT__` prefix, `__` between nesting levels (fields already contain `_`).
ENV_VAR_PATHS: dict[str, tuple[str, ...]] = {
    "REVIEW_SHIFT__DEPTH": ("depth",),
    "REVIEW_SHIFT__BASE_BRANCH": ("base_branch",),
    "REVIEW_SHIFT__DISCOVERY__DISCOVER_ALL": ("discovery", "discover_all"),
    "REVIEW_SHIFT__DISCOVERY__MAX_AGE_HOURS": ("discovery", "max_age_hours"),
    "REVIEW_SHIFT__DISCOVERY__MAX_BRANCHES_PER_RUN": ("discovery", "max_branches_per_run"),
    "REVIEW_SHIFT__RUNTIME__MODEL": ("runtime", "model"),
    "REVIEW_SHIFT__RUNTIME__BUDGET_USD": ("runtime", "budget_usd"),
    "REVIEW_SHIFT__RUNTIME__TOTAL_BUDGET_USD": ("runtime", "total_budget_usd"),
    "REVIEW_SHIFT__RUNTIME__AUTH_PREFLIGHT_BUDGET_USD": ("runtime", "auth_preflight_budget_usd"),
    "REVIEW_SHIFT__RUNTIME__EXIT_ZERO_ON_FINDINGS": ("runtime", "exit_zero_on_findings"),
    "REVIEW_SHIFT__PATCH__AUTO_FIX_MIN_SEVERITY": ("patch", "auto_fix_min_severity"),
}
