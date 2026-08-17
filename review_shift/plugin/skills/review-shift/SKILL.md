---
name: review-shift
description: >-
  Autonomous code review of one local git branch, reusing review-shift's own prompts, lock
  and CLI rather than reimplementing review logic in-session. Use when the user asks to
  review a branch, review my changes, run review-shift on this branch, or check this branch
  for findings from inside a Claude Code session. Capped at depth low/medium and exactly one
  branch per invocation (ADR-006) -- refuses a "high" depth or multi-branch request before
  starting anything.
license: MIT
metadata:
  author: review-shift
  version: "1.0"
---

# review-shift (in-session)

This skill is the in-session entry point into the same pipeline the standalone
`review-shift run` CLI uses -- same prompts (`src/prompts/{depth}.md`), same
`.review-shift/.lock` (ADR-007), same redaction, patch generation and report. It does not
re-derive or reimplement any review logic; it shells out to the real CLI (ADR-006).

## Before doing anything: resolve the cap

In-session review is capped to **one branch** and **`depth <= medium`** (ADR-006's "In-session
run blocks the chat and eats session context, so it is restricted to `depth <= medium` and
**one** branch"). Before running any command:

1. If the request names more than one branch, or asks for "all branches" / discovery: refuse.
   Tell the user in-session review handles exactly one branch, and give them the standalone
   command instead:
   ```
   review-shift run   # discovers and reviews every branch per .review-shift/config.yml
   ```
2. If the request asks for `depth: high` (or just "deep"/"thorough" review): refuse. `high` is
   a real depth (`review-shift run --depth high`) but is out of scope in-session per ADR-006
   — an interactive session is not the place for the longest, widest-reading review. Offer
   `depth: medium` instead, or the standalone command for `high`.

Do not silently downgrade a `high` request to `medium` and proceed -- refuse first, explain
why, and only continue if the user asks for `low` or `medium` explicitly.

## Running the review

1. Resolve the repository root: `git rev-parse --show-toplevel`. Every path from here on is
   relative to that root, not to the current working directory (ADR-006).
2. Resolve the branch: use the one the user named, or default to the current branch
   (`git branch --show-current`) if they just said "review this branch."
3. Resolve depth: `low` or `medium`, defaulting to `medium` if unspecified.
4. Check the installed CLI is new enough: `review-shift --version`. This skill requires
   review-shift >= 0.1.0 (the version that first shipped the `--branch`/`--depth` flags this
   skill relies on). If the command is not found, or prints an older version, stop and tell
   the user to upgrade (`pipx upgrade review-shift`, or reinstall) instead of running `run`
   and surfacing whatever flag-mismatch error it would raise on its own.
5. Run, from the repository root:
   ```
   review-shift run --branch <branch> --depth <low|medium>
   ```
   This takes `.review-shift/.lock` itself -- if a standalone `review-shift run` batch (e.g.
   the nightly launchd job) already holds it, this invocation blocks on the same lock rather
   than racing it or taking a separate one.
6. Report the outcome using the run directory's own artifacts (`report.md`, `patches/`,
   `run.json`'s exit reason) -- do not re-summarize findings from the model's raw stdout if
   the run directory has already written them.

## Fallback: no wired slash command

If `/review-shift` has not been wired as an actual slash command in the current environment
(product-analysis.md §6's cut order -- this is the first Must to degrade, not a defect), the
equivalent recipe is:

```
review-shift run --branch $(git branch --show-current) --depth low
```

Run this directly from a terminal at the repository root instead.
