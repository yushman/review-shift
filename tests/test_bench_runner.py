"""Runner unit tests against synthetic data (design.md, tasks.md section 5): `--force` is
always passed, budget exhaustion stops new runs and reports what did/did not complete, and a
materialize failure surfaces as a failed case rather than raising.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch as mock_patch

from bench.case import Case, GroundTruthRange
from bench.corpus import CorpusRepo
from bench.materialize import MaterializeError
from bench.runner import run_all, run_case


def _case(case_id: str, repo: str = "pydantic") -> Case:
    return Case(
        id=case_id, repo=repo, fix_sha="fff0000", introducing_sha="bbb1111",
        diff_visible=True, confirmed_at="2026-08-19T00:00:00Z", note="",
        ground_truth=(GroundTruthRange(file="a.py", start_line=1, end_line=1),),
        path=None,  # type: ignore[arg-type]
    )


def _repo(repo_id: str = "pydantic") -> CorpusRepo:
    return CorpusRepo(id=repo_id, url="https://example.com/x.git", language="python",
                       license="MIT", attribution="x")


def _write_run_dir(run_dir: Path, cost_usd: float = 0.5) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"cost_usd": cost_usd}))
    (run_dir / "findings.json").write_text(
        json.dumps({"schema_version": 1, "findings": [{"file": "a.py", "line": 1}]})
    )


def test_run_case_passes_force_and_out_dir(tmp_path: Path):
    case = _case("c1")
    repo = _repo()
    run_dir = tmp_path / "runs" / "pydantic" / "the-run"

    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        _write_run_dir(run_dir)
        return subprocess.CompletedProcess(args, 0, stdout=str(run_dir) + "\n", stderr="")

    with (
        mock_patch("bench.runner.materialize_repo", return_value=tmp_path / "repo"),
        mock_patch("bench.runner.ensure_sha", return_value=None),
        mock_patch("bench.runner.subprocess.run", side_effect=fake_run),
    ):
        result = run_case(case, repo, "medium", runs_dir=tmp_path / "runs")

    assert result.status == "ok"
    assert result.cost_usd == 0.5
    assert "--force" in captured["args"]
    assert "--branch" in captured["args"]
    assert case.introducing_sha in captured["args"]
    assert f"{case.introducing_sha}^" in captured["args"]


def test_run_case_materialize_failure_does_not_raise(tmp_path: Path):
    case = _case("c1")
    repo = _repo()
    with mock_patch("bench.runner.materialize_repo", side_effect=MaterializeError("boom")):
        result = run_case(case, repo, "medium", runs_dir=tmp_path / "runs")
    assert result.status == "materialize_failed"
    assert "boom" in (result.reason or "")


def test_run_all_stops_when_budget_exhausted(tmp_path: Path):
    cases = [_case("c1"), _case("c2")]
    repos = {"pydantic": _repo()}
    run_dir = tmp_path / "runs" / "pydantic" / "the-run"

    def fake_run(args, **kwargs):
        _write_run_dir(run_dir, cost_usd=1.0)
        return subprocess.CompletedProcess(args, 0, stdout=str(run_dir) + "\n", stderr="")

    with (
        mock_patch("bench.runner.materialize_repo", return_value=tmp_path / "repo"),
        mock_patch("bench.runner.ensure_sha", return_value=None),
        mock_patch("bench.runner.subprocess.run", side_effect=fake_run),
    ):
        results = run_all(
            cases, repos, depths=("medium",), budget_usd=1.0, runs_dir=tmp_path / "runs",
        )

    statuses = [(r.case.id, r.status) for r in results]
    assert statuses[0] == ("c1", "ok")
    assert statuses[1] == ("c2", "budget_exhausted")


def test_run_all_reports_unknown_repo(tmp_path: Path):
    cases = [_case("c1", repo="ghost")]
    results = run_all(cases, {}, depths=("medium",), budget_usd=100.0, runs_dir=tmp_path / "runs")
    assert results[0].status == "unknown_repo"
