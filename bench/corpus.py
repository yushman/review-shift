"""`bench/corpus.yml` — the three ground-truth repositories and the target this change runs
against, chosen by commit discipline rather than ownership or language (design.md D1). Each
entry records upstream URL, license and attribution (spec "Corpus entry recorded").
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

__all__ = ["CorpusError", "CorpusRepo", "load_corpus"]

CORPUS_PATH = Path(__file__).resolve().parent / "corpus.yml"

CORPUS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "repos"],
    "properties": {
        "schema_version": {"const": 1},
        "repos": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "language", "license", "attribution"],
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "language": {"type": "string", "minLength": 1},
                    "license": {"type": "string", "minLength": 1},
                    "attribution": {"type": "string", "minLength": 1},
                },
            },
        },
    },
}


class CorpusError(RuntimeError):
    pass


@dataclass(frozen=True)
class CorpusRepo:
    id: str
    url: str
    language: str
    license: str
    attribution: str


def load_corpus(path: Path = CORPUS_PATH) -> dict[str, CorpusRepo]:
    data = yaml.safe_load(path.read_text())
    try:
        jsonschema.validate(data, CORPUS_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise CorpusError(f"{path}: {exc.message}") from exc
    return {
        repo_id: CorpusRepo(id=repo_id, **fields) for repo_id, fields in data["repos"].items()
    }
