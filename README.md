# Refactor Orchestrator

Drives a multistage code refactor by pairing a **local executor model** with a
**paid reviewer model**. Aider (backed by a local model) does the editing; a
frontier model inspects each stage's diff once the tests pass and either
approves it or sends it back. The orchestrator owns the loop between them.

[PLAN.md](PLAN.md) is the design document and the authority on why things work
the way they do. This file is how to use it.

## Install

```bash
uv sync --group dev
uv run orchestrator --help
```

Requires Python 3.11+, `git`, and — for `agent` stages — `aider` on PATH.

## The shape of a run

**A run is one shippable unit of work: one branch, one eventual pull request.**
Its stages are the internal steps of that unit, not a whole programme of work.
A large refactor is many runs. Splitting a programme into runs is your job,
done up front.

Each stage is one of three kinds:

| Kind | Who edits | Use it for |
|---|---|---|
| `agent` | Aider + local model | The actual refactoring work |
| `script` | A declared command | Mechanical transforms across many files — a `sed`, not a model |
| `manual` | You | Version bumps, dependency resolution, deploys. The run pauses and waits |

## Commands

```bash
orchestrator validate config.yaml    # check everything without running a stage
orchestrator run config.yaml         # start a run
orchestrator resume <run_id>         # continue an interrupted or paused run
orchestrator status <run_id>         # where a run stopped and why
```

Exit codes: `0` complete, `1` failed or escalated, `2` paused waiting on you.
A paused run is not a failure.

Run `validate` first. It checks the config, that the target repo is a clean git
tree on the right base, that your test commands actually pass, that `aider`
still has the flags this tool builds, and that the reviewer is reachable.
Failing fast beats failing on stage 6.

## Configuration

```yaml
target_repo: /path/to/app
base_ref: main
branch: refactor/extract-order-service    # created for you; never base_ref

setup_command: "docker compose up -d db"  # must be idempotent; runs more than once
test_command: "bin/test"                  # scoped, runs on every attempt
full_test_command: "bin/test --all"       # runs once at the end

reference_docs:                           # included in every review prompt
  - docs/refactor_plan.md

executor:
  model: "openai/<local-model-id>"
  api_base: "http://<host>:<port>/v1"
  api_key_env: "LOCAL_API_KEY"
  lint_command: "<lint command>"
  map_tokens: 0                           # repo map off; stages declare their files

reviewer:
  model: "gpt-5.5"
  api_key_env: "OPENAI_API_KEY"

limits:
  max_test_retries: 3
  max_rework_retries: 2
  aider_timeout_seconds: 1800
  command_timeout_seconds: 3600

rework_strategy: fresh
rework_reset: true

stages:
  - id: extract-service
    kind: agent
    instruction: |
      Extract the order-processing logic into a service object.
    edit_files:                           # required: the scope guard needs it
      - "app/services/**"
      - "spec/services/**"
    read_files:                           # context only, not editable
      - "app/controllers/application_controller.rb"
    constraints: |
      Must remain valid on <current platform version>.
    forbidden_patterns:                   # checked against added lines only
      - "SomeLaterVersionOnlyApi"
    test_command: "bin/test spec/services"
    checks:
      - "bin/route-snapshot --diff"
    preconditions:
      - "! grep -rq 'LegacyMixin' app/"
    context_commands:                     # stdout is injected into the prompt
      - "bin/inventory-actions OrdersController"
```

Every command the orchestrator runs is declared here by you. Nothing is ever
taken from model output — `context_commands` push command output *into* a
prompt; no path exists in the other direction.

## What `verify` actually checks

Cheapest gate first, stopping at the first failure:

| # | Gate | On failure |
|---|---|---|
| 0 | `setup_command` | Escalates — a broken environment is not a bad diff |
| 1 | Scope guard: nothing changed outside `edit_files` | Escalates — containment, not quality |
| 2 | `forbidden_patterns` against **added lines** | Retries with the offending lines as feedback |
| 3 | `test_command` (re-run once before consuming a retry) | Retries |
| 4 | `checks` — each must exit zero | Retries |
| 5 | `require_new_tests` — diff touches a test file | Retries |

A failing test is re-run once before it costs a retry, so a flaky
browser-driven suite does not burn the budget and escalate falsely. Flake
re-runs are reported.

## Reviewer verdicts

- **approved** — advance.
- **rework** — loop back with the issues as feedback. By default the tree is
  reset to the stage baseline first, so each attempt produces one clean
  single-purpose diff rather than a rejected attempt plus its correction.
- **blocked** — the stage instruction itself is wrong. Escalates immediately
  without consuming a retry, because grinding through reworks will not fix a
  problem upstream of the executor.

Anything that yields no usable verdict — a refusal, a truncated response, a
transport failure — is treated as `blocked`. "The reviewer did not answer"
means stop and ask a human.

## Output

```
runs/<run_id>/
  run.log                                  timeline of decisions
  report.md                                stage outcomes, costs, undo command
  state.db                                 checkpoint; resume reads this
  run.json                                 which config this run used
  stages/<n>-<id>-attempt-<m>/
    prompt.md                              exactly what the executor was told
    executor.log
    verify.log                             each gate's command and result
    review.json                            the parsed verdict and token usage
```

The target repo never receives orchestrator artifacts.

`report.md` always includes a single command that undoes the entire run, and
the reviewer's cached-token proportion — the economic premise of splitting
executor from reviewer is that the paid model fires at checkpoints with a
mostly-cached prefix, and the report is where you confirm that is still true.

## Safety

- Refuses to start unless the target repo is a clean tree at `base_ref`.
- Commits only to `branch`, which must differ from `base_ref`.
- **No push, ever** — there is no code in this project that can.
- Opening the pull request is yours. Deliberately.

## Tests

```bash
uv run pytest
```

The suite drives the real graph, checkpointer, verify layers and git
operations against fixture repos; only the two model calls are stubbed.
