# review-shift

**review-shift reviews your local git branches overnight and leaves a report and an
apply-able git patch on your desk by morning — instead of the review you keep postponing
until after the merge.**

[Русская версия](README.ru.md)

> **Status: v0.1.2 released.** `pipx install review-shift` installs it from PyPI.

---

## What it does

At night, with nobody present, `review-shift`:

1. finds local branches that moved recently (`refs/heads/`, by committer date);
2. builds each branch's diff against its merge base with the base branch;
3. masks secret values before anything is sent to the model;
4. runs a read-only code review at the configured depth;
5. writes a markdown report, a `findings.json`, and two patch files — and validates every
   patch with `git apply --check` before writing it to disk.

In the morning you read one file and apply one patch. `review-shift` never applies anything
itself, never commits, never pushes, and never touches your working tree.

## Requirements

- macOS for the scheduled path (`launchd`), on mains power and awake — see Limitations.
  The CLI itself runs on Linux and macOS, x86 and ARM.
- [Claude Code](https://claude.com/claude-code) CLI **2.1.0 or newer**, already authenticated.
- Python 3.11+.

## Install

```bash
pipx install review-shift
```

## Golden path

```bash
# set the repo up: writes .review-shift/config.yml, keeps run artifacts out of git
cd ~/proj/myrepo && review-shift init

# check the environment before trusting it with a night
review-shift doctor

# schedule the nightly run (renders the launchd job, registers the wake-up)
review-shift init launchd
```

Then, in the morning:

```bash
$EDITOR .review-shift/runs/latest/report.md
```

## Configuration

`review-shift init` writes `.review-shift/config.yml`, self-documented with inline comments —
that file is the reference. A few fields worth knowing about before you first tune anything:

| Field | Default | |
|---|---|---|
| `depth` | `medium` | `low` \| `medium` — sets the review prompt, effort and max-findings preset |
| `discovery.patterns` | `[]` | fnmatch globs (or `re:`-prefixed regex) restricting which branches get discovered; empty means "every recently-moved branch" |
| `discovery.max_age_hours` | `24` | a branch is eligible only if its last commit is within this window |
| `runtime.budget_usd` | `10.00` | spend cap for one branch's review |
| `runtime.total_budget_usd` | `50.00` | spend cap for the whole run; once hit, remaining branches are marked `skipped: budget_exhausted`, not treated as a failure |
| `runtime.auth_preflight_budget_usd` | `0.01` | budget for the cheap health check `run`/`doctor` do before touching any branch; raise this if it fails with "exhausted its own budget" on a machine with a large cached system prompt (many MCP servers/plugins inflate even the very first call) |
| `patch.auto_fix_min_severity` | `high` | minimum severity that lands in `auto_fixed.patch` instead of only `all.patch` |

Any scalar `runtime`/`discovery`/`patch` field (plus `depth`/`base_branch`) can also be set via
an env var, `REVIEW_SHIFT__<SECTION>__<FIELD>` (e.g.
`REVIEW_SHIFT__RUNTIME__AUTH_PREFLIGHT_BUDGET_USD=0.10`). Precedence is config file → env var →
CLI flag, where a flag exists — `run --depth`/`--model` and a few others override both.

## Demo

```bash
asciinema play demo/review-shift.cast
```

Findings in the recording are scripted for a fast, free, reproducible playback — `init` and
the patch/report artifacts you see are real review-shift output, produced by the same
`patch.resolve` / `patch.generate_and_verify` / `report.render` code a real overnight run uses
(the recording skips the live model call `review-shift run` makes, and doesn't run `doctor`
either, since its own auth check is a live call too).

## In-session review (`/review-shift`)

`review-shift` also works from inside a Claude Code session, as a skill that shells out to
the same CLI (ADR-006) — same prompts, same lock, same report. It is capped to one branch and
`depth <= medium`. Two ways to get it, not alternatives to pick between:

```bash
# zero marketplace dependency, exact `/review-shift` command, no auto-update
review-shift init skill
```

```
# in a Claude Code session: self-hosted marketplace, auto-updatable, namespaced command
/plugin marketplace add https://github.com/yushman/review-shift
/plugin install review-shift@review-shift
# invoked as /review-shift:review-shift (Claude Code always namespaces plugin skills)
```

`init skill` writes `.claude/skills/review-shift/SKILL.md` in the current repository; re-run
it after upgrading `review-shift` to pick up a changed skill. Both channels can be installed
at once — Claude Code keeps the original `/skill-name` and the plugin copy side by side.

## Applying a patch

Deliberately manual, and deliberately three steps. The patch is bound to the branch head the
review ran against.

```bash
# 1. the sha the patch was built against is in the patch header and in run.json
git -C . rev-parse feature/payments-v2

# 2. check applicability
git switch feature/payments-v2
git apply --check .review-shift/runs/latest/patches/auto_fixed.patch

# 3. apply
git apply .review-shift/runs/latest/patches/auto_fixed.patch
```

If the branch has moved past that sha, the CLI says so and prints an explanation instead of
the recipe, rather than pretending the patch still applies.

## Output

```
.review-shift/
├── config.yml                  # meant to be committed
└── runs/
    ├── 2026-08-22T03-30-00Z-feature-payments-v2/
    │   ├── report.md           # what you read in the morning
    │   ├── findings.json
    │   ├── run.json            # branch, shas, depth, cost, timings, counters
    │   ├── events.jsonl
    │   └── patches/
    │       ├── auto_fixed.patch  # severity >= patch.auto_fix_min_severity, default high
    │       └── all.patch
    ├── index.json
    └── latest -> …
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Run finished, no critical findings |
| 1 | Run finished, critical findings present — a signal, not an error |
| 2 | Internal error (config, git, invalid model output, hard timeout) |
| 3 | Another run is already in progress |
| 4 | Authentication failed or quota exhausted |

The scheduler templates pass `--exit-zero-on-findings`, so a night that honestly finds
problems does not look like a broken job.

## Limitations — read these

- **A closed laptop on battery will not wake up.** `launchd` does not wake the machine; the
  run would happen at your next wake, which is far too late to be useful. The tool registers
  a `pmset` wake-up and wraps the run in `caffeinate`, but neither helps a machine on battery
  with the lid shut.
- **Your code is sent to the model provider.** Check your employer's policy and your plan's
  terms before pointing this at work code.
- **Secret masking reduces exposure, it does not guarantee it.** Regex heuristics miss custom
  token formats, and the agent has its own filesystem access. Not for code under regulatory
  constraints.
- **No quality numbers are published yet.** Recall and precision are only claimed once the
  benchmark bench exists (v0.2). What v0.1 measures is patch applicability.
- **Run artifacts contain code fragments** and live in your working tree.
- **v0.1 scope:** depths `low` and `medium`, local branches only, one repository per run.
  Diffs above ~2 000 changed lines are skipped with an explicit reason rather than truncated.
  `high`, chunking, retention and incremental review are v0.2; remote branches and PR
  integration are v0.3.

## License

MIT — see [LICENSE](LICENSE).
