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
`review-shift run` CLI uses -- same prompts (`review_shift/prompts/{depth}.md`), same
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
3. Resolve the base branch -- this decides both the diff and which command to run below:
   1. `base_branch` from `.review-shift/config.yml`, if it is set to anything other than
      `auto`.
   2. Otherwise `git symbolic-ref --short refs/remotes/origin/HEAD`. This is unset for a
      remote added by hand (`git remote add`) rather than `git clone` -- an ordinary repo
      shape, not a broken one.
   3. Otherwise the single local `main`/`master`, only when exactly one of the two exists.
   4. Otherwise ask the user which branch is the base. Do not guess: both or neither of
      `main`/`master` existing is genuinely ambiguous, and a wrongly resolved base reviews the
      wrong diff -- the same reasoning that makes `ambiguous` beat "nearest match" in patch
      localization (CLAUDE.md, "where bugs will be silent" #3).
4. Resolve depth: `low` or `medium`, defaulting to `medium` if unspecified.
5. Check the installed CLI is new enough: `review-shift --version`. This skill requires
   review-shift >= 0.1.0 (the version that first shipped the `--branch`/`--depth` flags this
   skill relies on). If the command is not found, or prints an older version, stop and tell
   the user to upgrade (`pipx upgrade review-shift`, or reinstall) instead of running `run`
   and surfacing whatever flag-mismatch error it would raise on its own.
6. Select the command from the branch and base resolved above -- `--base` is passed explicitly
   in both rows, even when `origin/HEAD` would also resolve it, so the command is reproducible
   on its own:

   | resolved shape | command |
   |---|---|
   | `branch` != `base` | `review-shift run --branch <branch> --base <base> --depth <low\|medium>` |
   | `branch` == `base` | `review-shift run --trunk --base <base> --depth <low\|medium>` |

   The second row is not an error path -- a repository whose only branch is the base branch
   (trunk-based) is a normal, supported shape. When it applies, tell the user this reviews
   commits landed directly on `<base>` since the last trunk run, not "changes on `<branch>`."
7. Run the selected command from the repository root. This takes `.review-shift/.lock` itself
   -- if a standalone `review-shift run` batch (e.g. the nightly launchd job) already holds it,
   this invocation blocks on the same lock rather than racing it or taking a separate one.
8. Report the outcome using the run directory's own artifacts (`report.md`, `patches/`,
   `run.json`'s exit reason) -- do not re-summarize findings from the model's raw stdout if
   the run directory has already written them. If step 6 ran the trunk command, also read
   `run.json`'s `trunk_outcome`:
   - `bootstrap` means this was the first trunk run in this repository: the watermark just
     initialized to the current head and zero commits were reviewed, by design. State that
     plainly -- do not present it as a clean review that found nothing, and do not retry it.
     Findings appear on the next trunk run, once new commits land on `<base>`.
   - `nothing_new` means no commits have landed on `<base>` since the last trunk run, so again
     nothing was reviewed. Say so plainly for the same reason -- zero findings here is not a
     clean bill of health, it is an empty review span. This is the ordinary outcome of running
     twice without committing in between, and it is the one a repeat user hits most often.
   - `units` means commits were actually reviewed: report it the same way a branch run's
     outcome is reported.

   The distinction to never blur: "reviewed, found nothing" and "there was nothing to review"
   both surface as zero findings, and only the first is a clean review.

## Fallback: no wired slash command

If `/review-shift` has not been wired as an actual slash command in the current environment
(product-analysis.md §6's cut order -- this is the first Must to degrade, not a defect),
resolve the branch and base the same way ("Running the review" steps 2-3) and run the matching
command directly at the repository root:

```bash
# ordinary feature branch (branch != base)
review-shift run --branch <branch> --base <base> --depth low

# trunk-based repository, or reviewing the base branch itself (branch == base)
review-shift run --trunk --base <base> --depth low
```

Substitute the branch and base actually resolved. Do not run
`--branch $(git branch --show-current)` unconditionally: in a trunk-based repository that
resolves to the base branch itself, and `run` rejects it (`--branch '<name>' is the resolved
base branch`).
