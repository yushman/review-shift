import json
import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest


@pytest.fixture(autouse=True)
def _stub_auth_preflight():
    """Every batch run now does an auth-liveness preflight before touching a branch
    (budget-and-resilience spec "Auth preflight before the batch"). Tests that aren't about
    auth failure shouldn't need a live `claude` login, so this stubs a passing preflight by
    default; a test that wants to exercise auth/quota failure overrides it locally with its
    own `mock_patch` inside the test body (nesting shadows this one for its duration)."""
    events = [{"type": "system"}, {"type": "result", "subtype": "success", "is_error": False}]
    ok = subprocess.CompletedProcess(args=["claude"], returncode=0,
                                      stdout=json.dumps(events), stderr="")
    with mock_patch("review_shift.review._run_preflight", return_value=ok):
        yield


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    """A tiny real git repo with one commit, used to test patch generation against real
    `git show`/`git diff --no-index`/`git apply --check` rather than fixtures of fixtures."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "src").mkdir()
    (repo / "src" / "foo.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "\n"
        "def sub(a, b):\n"
        "    return a - b\n"
    )
    (repo / "src" / "bar.py").write_text("VALUE = 1\nOTHER = 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def head_sha(fixture_repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(fixture_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


@pytest.fixture
def branched_repo(fixture_repo: Path) -> Path:
    """fixture_repo with a `main` branch and a `feature/x` branch carrying one real commit."""
    _git(fixture_repo, "branch", "-m", "main")
    _git(fixture_repo, "checkout", "-q", "-b", "feature/x")
    (fixture_repo / "src" / "bar.py").write_text("VALUE = 1  # off by one\nOTHER = 2\n")
    _git(fixture_repo, "commit", "-q", "-am", "fix value comment")
    _git(fixture_repo, "checkout", "-q", "main")
    return fixture_repo
