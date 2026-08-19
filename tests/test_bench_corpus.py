"""`bench/corpus.yml` loads and validates (spec "Corpus entry recorded")."""
from pathlib import Path

import pytest
import yaml

from bench.corpus import CorpusError, load_corpus


def test_shipped_corpus_loads():
    repos = load_corpus()
    assert set(repos) == {"duckduckgo-android", "pydantic", "cli"}
    for repo in repos.values():
        assert repo.url
        assert repo.language
        assert repo.license
        assert repo.attribution


def test_repo_missing_license_is_rejected(tmp_path: Path):
    data = {
        "schema_version": 1,
        "repos": {"x": {"url": "https://example.com/x.git", "language": "go",
                         "attribution": "x"}},
    }
    path = tmp_path / "corpus.yml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(CorpusError):
        load_corpus(path)
