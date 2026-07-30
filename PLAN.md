# Refactor Orchestrator — Build Spec

## What this is

A standalone Python tool that drives a multistage code refactor by pairing a
local executor model with a paid reviewer model.

The executor (Aider, backed by a local model on a DGX Spark) does the actual
editing. The reviewer (a frontier model via API) inspects each stage's diff
after tests pass and either approves it or sends it back for rework. The
orchestrator owns the loop between them: it runs stages in order, decides
when to retry, when to advance, and when to stop and escalate to a human.

This tool lives in its own repository. It never installs itself into, or
commits to, the repository being refactored. It operates on a target repo
from the outside, the same way Aider does.

## Non-goals

- Not a fleet manager. One target repo, one linear stage sequence, one run
  at a time. No parallel agents, no worktrees, no PR automation.
- Not a replacement for Aider's own edit/test/fix loop. Aider handles
  iteration *within* a stage. This tool handles the loop *between* stages.
- No web UI or dashboard. CLI plus logs.
- No CI integration in v1.

## Tech choices

- Python 3.11+
- LangGraph for the state machine (cycles, conditional edges, checkpointing)
- `uv` for dependency management, with a `pyproject.toml`
- Aider invoked as a subprocess in headless mode (`aider --message`), not
  imported as a library
- Reviewer called via direct HTTP/SDK call to the model API — no agentic
  CLI wrapper, since review needs no tool access
- `pytest` for the orchestrator's own tests
- YAML for run configuration

## Architecture

### Graph nodes

**`execute`**
Invokes Aider against the target repo for the current stage. On a first
attempt, the prompt is the stage's instruction text. On a rework attempt,
the prompt is the stage instruction plus the reviewer's feedback plus the
diff that was rejected (see "Rework prompt construction" below).

Aider is responsible for its own edit → lint → test → fix cycle inside this
node. The orchestrator does not micromanage that; it hands Aider a task and
waits.

**`verify`**
Runs the configured test command against the target repo and captures exit
code plus output. This is a belt-and-braces check that the repo is actually
green after Aider returns — Aider is configured with its own `--test-cmd`,
but the orchestrator confirms independently rather than trusting the
subprocess's word for it.

**`review`**
Computes the diff for the current stage (`git diff <stage_start_sha>..HEAD`
in the target repo), sends it to the reviewer model along with the stage
instruction and the overall refactor plan, and parses a structured verdict.

**`escalate`**
Terminal node. Writes a summary of where the run stopped and why, and exits
non-zero.

**`advance`**
Records the stage as complete, resets the retry counter, increments the
stage index, and either loops to `execute` for the next stage or terminates
successfully if the stage list is exhausted.

### Edges

```
execute  → verify
verify   → execute   (tests failed, retries remain)
verify   → escalate  (tests failed, retries exhausted)
verify   → review    (tests passed)
review   → advance   (verdict: approved)
review   → execute   (verdict: rework, retries remain)
review   → escalate  (verdict: rework, retries exhausted)
review   → escalate  (verdict: blocked — see below)
advance  → execute   (more stages remain)
advance  → END       (stage list exhausted)
```

### State schema

```python
class RunState(TypedDict):
    config_path: str
    target_repo: str
    stages: list[Stage]
    stage_index: int
    stage_start_sha: str        # HEAD before this stage's first attempt
    attempt: int                # rework attempts for the current stage
    last_test_output: str | None
    review_feedback: list[str]  # accumulated feedback for current stage
    history: list[StageResult]  # completed stages, for the final report
    status: Literal["running", "complete", "escalated"]
```

## Reviewer contract

The reviewer must return structured output, not prose. Prompt it to reply
with JSON only, and parse it:

```json
{
  "verdict": "approved" | "rework" | "blocked",
  "summary": "one-line assessment",
  "issues": [
    {
      "severity": "major" | "minor",
      "file": "path/to/file.rb",
      "description": "what's wrong and why it matters"
    }
  ]
}
```

- `approved` — advance to the next stage.
- `rework` — loop back to `execute` with the issues as feedback.
- `blocked` — the reviewer believes the stage instruction itself is wrong,
  or that the refactor plan has a flaw that reworking this stage won't fix.
  Escalate immediately without consuming a retry; this is a human decision.

Parse defensively: strip markdown fences, and treat an unparseable response
as `blocked` rather than guessing.

**Review prompt should include:** the overall refactor plan (all stage
instructions, so the reviewer can catch drift from earlier decisions), the
current stage's instruction, and the stage diff. It should explicitly ask
the reviewer to flag contradictions with decisions made in *earlier* stages,
since catching cross-stage architectural drift is the main reason this node
exists — the local model only ever sees one stage at a time.

## Rework prompt construction

When looping back to `execute` after a rework verdict, start a **fresh**
Aider invocation rather than continuing the prior conversation. Pass:

1. The original stage instruction.
2. The reviewer's issues, formatted as a list.
3. A note that a previous attempt was rejected.

Do **not** carry forward the failed attempt's full Aider conversation
history. Rationale: keeps the prompt focused, avoids the local model
anchoring on its own earlier reasoning, and keeps context small — which
matters on bandwidth-constrained local inference.

The rejected code itself stays in git (the stage's commits are not reverted
before rework; Aider edits forward from the current state). If a stage
escalates, the operator can inspect or reset from `stage_start_sha`.

> This is a deliberate default, not a settled conclusion. Implement it
> behind a config flag `rework_strategy: fresh | continue` so the
> alternative can be tested later without a rewrite.

## Configuration

One YAML file per refactor, living in this project (not the target repo):

```yaml
target_repo: /Users/me/code/some-app
test_command: "bundle exec rspec"

executor:
  # llama.cpp on the Spark exposes an OpenAI-compatible endpoint
  model: "openai/qwen3-coder-next"
  api_base: "http://spark.tailnet:8080/v1"
  api_key_env: "LOCAL_API_KEY"     # dummy value is fine for llama.cpp
  lint_command: "bundle exec rubocop -a"

reviewer:
  provider: "anthropic"
  model: "<model-id>"
  api_key_env: "ANTHROPIC_API_KEY"

limits:
  max_test_retries: 3      # per stage, tests failing
  max_rework_retries: 2    # per stage, reviewer rejections
  aider_timeout_seconds: 1800

rework_strategy: fresh

stages:
  - id: "extract-service-objects"
    instruction: |
      Extract the order-processing logic from OrdersController into
      a service object under app/services. Preserve existing behavior;
      all existing specs must continue to pass.
  - id: "introduce-result-type"
    instruction: |
      ...
```

API keys are read from environment variables named in the config. Never
put a key in the YAML itself, and add a `.gitignore` rule for any
`*.local.yaml` pattern so run configs with sensitive paths can be kept
out of history if desired.

## Aider invocation

Build the subprocess call roughly as:

```
aider
  --message "<constructed prompt>"
  --yes-always
  --no-stream
  --model <executor.model>
  --openai-api-base <executor.api_base>
  --test-cmd "<test_command>"
  --auto-test
  --lint-cmd "<executor.lint_command>"
```

with `cwd` set to `target_repo`. Capture stdout and stderr to the run log.
Enforce `aider_timeout_seconds`; on timeout, kill the process and treat it
as a failed attempt.

Verify the exact flag names against `aider --help` at build time rather
than trusting this list — Aider's CLI surface changes between releases.

## Checkpointing and resume

Use a LangGraph checkpointer backed by SQLite, stored in this project under
`./runs/<run_id>/state.db`. The target repo must stay free of orchestrator
artifacts.

The CLI must support resuming an interrupted run from its last checkpoint,
picking up at the stage and attempt count where it stopped.

## CLI

```
orchestrator run <config.yaml>              # start a new run
orchestrator resume <run_id>                # continue an interrupted run
orchestrator status <run_id>                # where a run stopped and why
orchestrator validate <config.yaml>         # check config without executing
```

`validate` should confirm: target repo exists and is a clean git working
tree, the test command runs, both model endpoints are reachable, and
required env vars are set. Run these checks automatically at the start of
`run` too — failing fast beats failing on stage 6.

## Logging

Per run, write to `./runs/<run_id>/`:

- `run.log` — human-readable timeline of node transitions and decisions
- `stages/<n>-attempt-<m>/aider.log` — raw Aider output
- `stages/<n>-attempt-<m>/review.json` — the reviewer's parsed verdict
- `report.md` — written on completion or escalation: stage-by-stage outcome,
  retry counts, reviewer issues raised, and the commit range for each stage

Log every reviewer API call's token usage so cost per stage is visible in
the report. The whole point of the split is that the paid model is invoked
at checkpoints rather than continuously — the report should make it easy to
confirm that's actually happening.

## Safety requirements

- Refuse to start if the target repo has uncommitted changes.
- Record the target repo's HEAD sha at run start and include it in the
  report so the entire run can be reset with a single command.
- Never run `git push`, and never modify any branch other than the one
  checked out at run start.
- The orchestrator itself never runs arbitrary shell commands from model
  output — only the configured test and lint commands.

## Build order

1. Project scaffolding, `pyproject.toml`, config loading and validation.
2. Aider subprocess wrapper with timeout and log capture — testable
   standalone against a throwaway repo.
3. Reviewer client with structured-output parsing — testable standalone
   against a canned diff.
4. LangGraph state machine wiring the two together, with checkpointing.
5. CLI, logging, and report generation.
6. Tests: unit tests for config validation, prompt construction, and verdict
   parsing; an integration test that runs a two-stage refactor against a
   fixture repo with a stubbed reviewer.

Commit at each step.
