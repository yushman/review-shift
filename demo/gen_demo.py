#!/usr/bin/env python3
"""Builds a throwaway repo with one real bug, then produces a real patch and report.md via
review-shift's own patch/report modules -- everything downstream of the (hand-written)
findings is genuinely produced by review-shift's code. There is no live Claude call: doing
that on every re-recording would make this demo cost money and stop being reproducible.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src import patch, report  # noqa: E402

FINDINGS = [
    {
        "file": "pricing.py", "line": 2, "severity": "high", "category": "bug",
        "rationale": "pct is not clamped to [0, 100]; a caller passing pct > 100 returns a "
                     "negative total, which is never a valid price.",
        "before": "    return total - total * pct / 100",
        "after": "    pct = max(0.0, min(100.0, pct))\n"
                 "    return total - total * pct / 100",
        "confidence": "high",
    },
    {
        "file": "pricing.py", "line": 1, "severity": "medium", "category": "test-gap",
        "rationale": "No unit test covers pct outside [0, 100] or a negative total.",
        "confidence": "medium",
    },
]


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        list(args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def build_demo_repo(root: Path) -> str:
    sh("git", "init", "-q", "-b", "main", cwd=root)
    sh("git", "config", "user.email", "demo@example.com", cwd=root)
    sh("git", "config", "user.name", "demo", cwd=root)
    sh("git", "commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    sh("git", "checkout", "-q", "-b", "feature/bulk-discount", cwd=root)
    (root / "pricing.py").write_text(
        "def apply_discount(total: float, pct: float) -> float:\n"
        "    return total - total * pct / 100\n"
    )
    sh("git", "add", "pricing.py", cwd=root)
    sh("git", "commit", "-q", "-m", "add bulk discount pricing", cwd=root)
    return sh("git", "rev-parse", "HEAD", cwd=root).strip()


def review_shift_bin() -> str:
    found = shutil.which("review-shift")
    if found:
        return found
    venv_bin = REPO_ROOT / ".venv" / "bin" / "review-shift"
    if venv_bin.exists():
        return str(venv_bin)
    raise SystemExit("review-shift not found on PATH or in .venv/bin — run `uv sync` first")


def main() -> None:
    rs = review_shift_bin()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        head_sha = build_demo_repo(root)

        localized = patch.resolve(FINDINGS, root, head_sha)
        diff_text, error = patch.generate_and_verify(
            localized, root, head_sha, run_id="demo", statuses=("applicable",)
        )
        if error is not None or diff_text is None:
            raise SystemExit(f"demo patch failed to verify: {error}")

        run_meta = {
            "branch": "feature/bulk-discount", "head_sha": head_sha, "base": "main",
            "depth": "low", "started_at": "2026-08-10T03:30:00Z", "duration_ms": 1800,
            "cost_usd": 0.0,
            "findings_by_severity": {"critical": 0, "high": 1, "medium": 1, "low": 0, "info": 0},
            "findings_without_patch": 1, "auto_fix_min_severity": "high",
            "auto_fix_patch_path": ".review-shift/runs/demo/patches/auto_fixed.patch",
            "skipped": [],
        }
        report_text = report.render(run_meta, localized)

        run_dir = root / ".review-shift" / "runs" / "demo"
        (run_dir / "patches").mkdir(parents=True)
        (run_dir / "report.md").write_text(report_text)
        (run_dir / "patches" / "auto_fixed.patch").write_text(diff_text)

        print("# review-shift -- the morning ritual")
        print("# (findings below are scripted for a reproducible, free recording;")
        print("#  init and the patch/report are real review-shift output)")
        print()
        print("$ review-shift init")
        print(sh(rs, "init", "--repo", str(root), cwd=root), end="")
        print()
        print("# ... night passes, review-shift run produces .review-shift/runs/demo/ ...")
        print()
        print("$ cat .review-shift/runs/demo/report.md")
        print(report_text)
        print("$ git apply --check .review-shift/runs/demo/patches/auto_fixed.patch")
        sh("git", "apply", "--check", str(run_dir / "patches" / "auto_fixed.patch"), cwd=root)
        print("(clean -- exits 0)")
        print("$ git apply .review-shift/runs/demo/patches/auto_fixed.patch")
        sh("git", "apply", str(run_dir / "patches" / "auto_fixed.patch"), cwd=root)
        print("$ cat pricing.py")
        print((root / "pricing.py").read_text(), end="")


if __name__ == "__main__":
    main()
