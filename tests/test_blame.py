"""blame.py: the trunk-review survival filter and attribution index built from one
`git blame --line-porcelain` pass per touched file (design.md D5, trunk-review spec "A single
blame pass filters superseded commits and attributes findings").
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from review_shift import blame


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_out(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_touched_files_reads_post_image_paths():
    diff_text = (
        "diff --git a/a.py b/a.py\n"
        "index 111..222 100644\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    assert blame.touched_files(diff_text) == {"a.py"}


def test_touched_files_excludes_pure_deletion():
    diff_text = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "index 111..000\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-x = 1\n"
    )
    assert blame.touched_files(diff_text) == set()


def test_fully_rewritten_commit_does_not_survive(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("a\nb\nc\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")
    (repo / "f.txt").write_text("a\nORIGINAL\nc\n")
    _git(repo, "commit", "-q", "-am", "c1: add ORIGINAL")
    c1 = _git_out(repo, "rev-parse", "HEAD")
    # c2 fully overwrites the line c1 added -- nothing of c1 survives at head
    (repo / "f.txt").write_text("a\nREPLACED\nc\n")
    _git(repo, "commit", "-q", "-am", "c2: replace it")
    head = _git_out(repo, "rev-parse", "HEAD")

    index = blame.build_blame_index(repo, head, {"f.txt"})
    assert blame.commit_outcome(index, {"f.txt"}, c1) == "superseded"


def test_partially_surviving_commit_still_survives(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("a\nb\nc\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")
    (repo / "f.txt").write_text("KEEP\nb\nCHANGED\n")
    _git(repo, "commit", "-q", "-am", "c1: two edits")
    c1 = _git_out(repo, "rev-parse", "HEAD")
    # c2 only overwrites the second edit; the first (KEEP) survives
    (repo / "f.txt").write_text("KEEP\nb\nOVERWRITTEN\n")
    _git(repo, "commit", "-q", "-am", "c2: overwrite one line")
    head = _git_out(repo, "rev-parse", "HEAD")

    index = blame.build_blame_index(repo, head, {"f.txt"})
    assert blame.commit_outcome(index, {"f.txt"}, c1) == "survives"


def test_attribution_names_the_right_commit_on_interleaved_authorship(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@example.com")
    _git(repo, "config", "user.name", "Alice")
    (repo / "f.txt").write_text("a\nb\nc\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")

    _git(repo, "config", "user.email", "b@example.com")
    _git(repo, "config", "user.name", "Bob")
    (repo / "f.txt").write_text("a\nBOB\nc\n")
    _git(repo, "commit", "-q", "-am", "bob's line")
    bob_sha = _git_out(repo, "rev-parse", "HEAD")

    _git(repo, "config", "user.email", "a@example.com")
    _git(repo, "config", "user.name", "Alice")
    (repo / "f.txt").write_text("ALICE\nBOB\nc\n")
    _git(repo, "commit", "-q", "-am", "alice's line")
    alice_sha = _git_out(repo, "rev-parse", "HEAD")
    head = alice_sha

    index = blame.build_blame_index(repo, head, {"f.txt"})
    sha, author = index.attribute("f.txt", 2)
    assert sha == bob_sha
    assert author == "Bob"
    sha, author = index.attribute("f.txt", 1)
    assert sha == alice_sha
    assert author == "Alice"


def test_deleted_file_at_head_is_removed_not_superseded(tmp_path: Path):
    """A file gone by head_sha is a different situation from a later commit overwriting the
    same lines -- conflating the two under `superseded` would tell a reader "handled by a
    later commit" about a case that's actually "the whole file disappeared"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "gone.txt").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add gone.txt")
    c1 = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "gone.txt")
    _git(repo, "commit", "-q", "-m", "delete gone.txt")
    head = _git_out(repo, "rev-parse", "HEAD")

    index = blame.build_blame_index(repo, head, {"gone.txt"})
    assert "gone.txt" in index.missing_files
    assert blame.commit_outcome(index, {"gone.txt"}, c1) == "removed"


def test_one_file_deleted_one_rewritten_is_superseded_not_removed(tmp_path: Path):
    """A commit touching two files where only one is gone and the other's lines were
    overwritten is `superseded`, not `removed` -- `removed` is reserved for when every touched
    file is gone, per commit_outcome's "files and files <= missing_files" rule."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "gone.txt").write_text("x\n")
    (repo / "kept.txt").write_text("a\nb\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "c0")
    (repo / "kept.txt").write_text("a\nORIGINAL\n")
    _git(repo, "commit", "-q", "-am", "c1: touches both files")
    c1 = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "rm", "-q", "gone.txt")
    (repo / "kept.txt").write_text("a\nREPLACED\n")
    _git(repo, "commit", "-q", "-am", "c2: delete one, overwrite the other")
    head = _git_out(repo, "rev-parse", "HEAD")

    index = blame.build_blame_index(repo, head, {"gone.txt", "kept.txt"})
    assert blame.commit_outcome(index, {"gone.txt", "kept.txt"}, c1) == "superseded"
