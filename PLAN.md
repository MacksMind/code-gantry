# Refactor Orchestrator — Build Spec

## What this is

A standalone Python tool that drives a multistage code refactor by pairing a
local executor model with a paid reviewer model.

The executor (Aider, backed by a local model on a DGX Spark) does the actual
editing. The reviewer (a frontier model via API) inspects each stage's diff
after tests pass and either approves it or sends it back for rework. The
orchestrator owns the loop between them: it runs stages in order, decides
when to retry, when to advance, and when to stop and escalate to a human.

**A run is one shippable unit of work** — one branch, one eventual pull
request. Its stages are the internal steps of that unit, not a whole program
of work. A large refactor is many runs, executed one after another, each
landing on its own. Linearizing a program's dependency graph into a sequence
of runs is the operator's job, done in advance; the orchestrator executes one
run at a time against a config file describing it.

That boundary is deliberate. The failure mode this tool exists to avoid is
the long-lived branch: a refactor that accumulates weeks of unshipped
change and becomes impossible to review or roll back. Scoping a run to a
single shippable unit means the orchestrator's output is always something a
human can merge today.

This tool lives in its own repository. It never installs itself into, or
commits to, its own repository while running. It operates on a target repo
from the outside, the same way Aider does.

## Non-goals

- Not a fleet manager. One target repo, one linear stage sequence, one run
  at a time. No parallel agents. The orchestrator does not create or manage
  git worktrees, though `target_repo` may point at one the operator
  provisioned — some repos need that for test-database isolation.
- Not a replacement for Aider's own edit/test/fix loop. Aider handles
  iteration *within* a stage. This tool handles the loop *between* stages.
- No PR automation. The run produces a branch; opening the PR, merging, and
  deploying are the operator's. This is a v1 stop point, not an oversight —
  see "Safety requirements".
- No dependency resolution, container or image surgery, deployment, or
  interaction with external systems (log queries, dashboards, ticket
  trackers). Work of that kind is expressed as a `manual` stage the
  orchestrator sequences around, waits for, and verifies — not work it
  attempts itself.
- No web UI or dashboard. CLI plus logs.
- No CI integration in v1.

## Tech choices

- Python 3.11+
- LangGraph for the state machine (cycles, conditional edges, checkpointing)
- `uv` for dependency management, with a `pyproject.toml`
- Aider invoked as a subprocess in headless mode (`aider --message`), not
  imported as a library
- Reviewer called via direct SDK call to the model API — no agentic CLI
  wrapper, since review needs no tool access. Behind a small
  `ReviewerClient` protocol so the provider is swappable; OpenAI is the
  first and only implementation, using the SDK's native structured-output
  parsing
- `pytest` for the orchestrator's own tests
- YAML for run configuration

## Architecture

### Graph nodes

**`precheck`**
Entry point for each stage. Evaluates the stage's `preconditions` — declared
commands that must each exit zero before the stage may run (a grep
confirming an incompatible dependency is gone, a version assertion, a
migration-state check). An unmet precondition escalates immediately; it is
an ordering error in the config, not something rework can fix.

Then runs the stage's `setup_command` (or the global one), so the
environment is correct *before* the executor runs — the executor runs the
test command itself via `--auto-test`, so it cannot be handed a stale
container or unresolved dependencies. Setup failure escalates directly: a
broken environment is not a defect in a diff, and rework will not fix it.

Routes to `execute` for `agent` and `script` stages, to `gate` for `manual`
stages.

**`execute`**
Dispatches on stage kind:

- `agent` — invokes Aider against the target repo. On a first attempt, the
  prompt is the stage's instruction text plus any `context_commands` output.
  On a rework attempt, the prompt is the stage instruction plus the
  reviewer's feedback (see "Rework prompt construction"). Aider is
  responsible for its own edit → lint → test → fix cycle inside this node;
  the orchestrator does not micromanage that.
- `script` — runs the stage's declared `command`. For mechanical transforms
  that are a scripted find/replace across hundreds of files, a script is
  more reliable and vastly cheaper than a model. These stages still go
  through `verify` and `review` like any other; only the editing mechanism
  differs.

**`gate`** *(manual stages)*
Prints the stage's `human_steps`, writes the report as it stands,
checkpoints, sets status to `awaiting_human`, and exits zero. The run is
paused, not failed.
`orchestrator resume <run_id>` re-enters the graph at `verify` for this same
stage, confirming the human's work landed green before advancing.

This is what lets a single run span steps the orchestrator cannot do — a
runtime version bump, a dependency upgrade requiring resolution, a
production deploy — instead of terminating at them.

**`verify`**
The advance gate. Runs an ordered sequence of checks, cheapest first,
short-circuiting on the first failure. See "Layered verification" below.
The orchestrator confirms the repo's state independently rather than
trusting the executor subprocess's word for it.

**`review`**
Computes the stage diff (see "Diffs and commits") and sends it to the
reviewer model along with the stage instruction, the stage's declared
constraints, the other stages in the run, and any configured reference
documents, then parses a structured verdict.

Skipped when the stage sets `review: false` (the default for `manual`
stages, where a human already owns the change).

**`advance`**
Commits anything still uncommitted with a stage-labeled message, records the
stage as complete along with its commit range, resets the retry and flake
counters, increments the stage index, and either loops to `precheck` for the
next stage or moves to `finalize` if the stage list is exhausted.

**`finalize`**
Runs the run-level `full_test_command` once — the whole suite, as opposed to
the scoped per-stage commands used during iteration — then writes the final
report. A red full suite escalates: the stages passed individually but
their composition did not.

**`escalate`**
Terminal node. Writes a summary of where the run stopped and why, and exits
non-zero.

### Edges

```
precheck → execute   (kind: agent | script; preconditions and setup pass)
precheck → gate      (kind: manual; preconditions and setup pass)
precheck → escalate  (a precondition failed, or setup failed)

execute  → verify
gate     → END       (status: awaiting_human; resume re-enters at verify)

verify   → escalate  (setup or scope guard failed — no retry consumed)
verify   → execute   (patterns, tests, checks, or new-tests failed;
                      retries remain)
verify   → escalate  (same, retries exhausted)
verify   → review    (all layers pass)
verify   → advance   (all layers pass, review: false)

review   → advance   (verdict: approved)
review   → execute   (verdict: rework, retries remain)
review   → escalate  (verdict: rework, retries exhausted)
review   → escalate  (verdict: blocked — see below)

advance  → precheck  (more stages remain)
advance  → finalize  (stage list exhausted)

finalize → END       (full suite green, report written)
finalize → escalate  (full suite red)
```

### State schema

```python
class Stage(TypedDict, total=False):
    id: str
    kind: Literal["agent", "script", "manual"]   # default "agent"

    instruction: str            # agent stages: the task
    command: str                # script stages: the transform to run
    human_steps: str            # manual stages: what the operator must do

    preconditions: list[str]    # must each exit zero before the stage runs
    context_commands: list[str] # stdout injected into the executor prompt
    setup_command: str | None   # overrides the global setup for this stage

    edit_files: list[str]       # globs the executor may edit; scope guard
    read_files: list[str]       # globs passed as read-only context

    forbidden_patterns: list[str]  # regexes barred from the diff's added lines
    constraints: str            # invariants the reviewer must enforce
    acceptance: str             # what "done" means, for greenfield stages

    test_command: str | None    # scoped override of the global command
    checks: list[str]           # extra commands that must exit zero
    require_new_tests: bool     # fail if the diff adds no test files
    review: bool                # default True; default False for manual


class RunState(TypedDict):
    run_id: str
    config_path: str
    target_repo: str
    base_ref: str               # branch the run's work is cut from
    base_sha: str               # base_ref's sha at run start
    branch: str                 # branch the run commits to
    stages: list[Stage]
    stage_index: int
    stage_start_sha: str        # HEAD before this stage's first attempt
    attempt: int                # rework attempts for the current stage
    last_test_output: str | None
    failure_layer: Literal[
        "precondition", "setup", "scope", "patterns",
        "tests", "checks", "new_tests",
    ] | None                    # which gate failed, for the report
    flake_reruns: int           # re-runs that passed on retry, this stage
    review_feedback: list[str]  # accumulated feedback for current stage
    history: list[StageResult]  # completed stages, for the final report
    status: Literal["running", "complete", "escalated", "awaiting_human"]
```

### Diffs and commits

Every stage diff is computed as `git diff <stage_start_sha>` — against the
working tree, **not** `<stage_start_sha>..HEAD`.

This is not a stylistic choice. Aider auto-commits, so `..HEAD` happens to
work for `agent` stages, but a `script` stage leaves its transform
uncommitted in the working tree and a resumed `manual` stage may too. A
`..HEAD` diff would be empty for both, which means the scope guard would
pass vacuously, `forbidden_patterns` would match nothing, and the reviewer
would approve an empty diff. Diffing against the working tree covers all
three stage kinds uniformly.

`advance` is the only node that commits on the orchestrator's behalf, and
only to squash whatever the stage left uncommitted into one stage-labeled
commit. The commit range it records is what the report cites.

## Layered verification

`verify` is not one test command. It is an ordered sequence, cheapest gate
first, short-circuiting on the first failure. Ordering matters
economically: the free deterministic checks run before anything that costs
minutes of compute or a paid API call.

**0. Setup.** Run the stage's `setup_command`, or the global one if the
stage does not override it. This is where a containerized target repo gets
its image rebuilt, dependencies installed, or test database reloaded — the
steps that must happen after a stage changes the environment itself.

`precheck` already ran setup once for this stage. It runs again here because
the stage's own edits may be what invalidated the environment: a `manual`
runtime bump is resumed at `verify`, and its rebuild has to happen after the
human's work, not before it. **`setup_command` must therefore be
idempotent** — the orchestrator will run it more than once per stage.
Validate rejects nothing here; it is a contract on the operator.

Setup failure escalates directly, as in `precheck`.

**1. Scope guard.** `git diff --name-only <stage_start_sha>` must fall
entirely within the stage's `edit_files` globs. A violation escalates
without consuming a retry — an executor editing outside its declared scope
is a containment failure, not a quality problem, and the operator should see
it immediately.

Skipped for `manual` stages, where a human legitimately touches whatever the
change requires.

**2. Forbidden patterns.** Each regex in the stage's `forbidden_patterns` is
matched against the diff's **added lines only**; any hit fails the stage and
loops back to `execute` with the offending lines as feedback. This is free,
deterministic, and it runs before any test suite or reviewer call.

The point is to catch scope and compatibility violations mechanically rather
than hoping the reviewer notices. The canonical case: staged migrations
where an API is legal in a later stage but not the current one. Declaring
the later-stage syntax as a forbidden pattern turns a subtle review question
into a regex.

Added-lines-only is load-bearing, not a detail. A stage whose whole purpose
is *removing* a construct would otherwise flag itself the moment it
succeeded, and a stage replacing a bare form with a qualified one needs the
bare form forbidden on the way in while still deleting it on the way out.

**3. Tests.** The stage's `test_command` if set, otherwise the global one.
Per-stage commands should be *scoped* — the specific specs the stage
touches — because this command runs on every attempt, and a large suite
multiplied by retries and rework rounds dominates the run's wall clock.

On failure, re-run once before consuming a retry. If the re-run passes,
increment `flake_reruns` and continue; the stage is marked flaky in the
report. Suites with browser-driven or timing-sensitive tests otherwise burn
their entire retry budget on noise and escalate falsely.

**4. Checks.** Each command in the stage's `checks` must exit zero. This is
the hook for verification a test suite does not provide: a route-table
snapshot diff, a dead-link or endpoint scanner, an asset build, a boot
check, a lint gate distinct from the executor's own.

It exists because "the tests pass" is frequently an insufficient advance
gate. A change can be green and still silently alter behavior no spec
covers — and on a legacy codebase, the areas with the thinnest coverage are
exactly the ones a refactor is most likely to disturb. Where a project
already has tooling that catches that class of regression, `checks` is how
the orchestrator runs it.

**5. New tests.** If the stage sets `require_new_tests: true`, the diff must
add at least one test file. See "Greenfield and test-first stages".

**Routing.** Layer 0 (setup) and layer 1 (scope guard) escalate without
consuming a retry: both are containment or environment failures rather than
defects the executor can be asked to fix. Layers 2 through 5 consume a
retry and loop back to `execute` with the specific failure as feedback,
escalating only when `max_test_retries` is exhausted.

Every failing layer produces feedback the executor can act on, not just an
exit code — the matched forbidden lines, the failing test names, the
check's stderr. A retry given no information about why the last attempt
failed is a wasted retry.

## Reviewer contract

The reviewer must return structured output, not prose. Use the provider's
native structured-output support — for OpenAI, `chat.completions.parse` with
a Pydantic `response_format`, which enforces the schema server-side rather
than hoping the model complies. The shape:

```json
{
  "verdict": "approved" | "rework" | "blocked",
  "summary": "one-line assessment",
  "issues": [
    {
      "severity": "major" | "minor",
      "file": "path/to/file.ext",
      "description": "what's wrong and why it matters"
    }
  ]
}
```

- `approved` — advance to the next stage.
- `rework` — loop back to `execute` with the issues as feedback.
- `blocked` — the reviewer believes the stage instruction itself is wrong,
  or that the plan has a flaw that reworking this stage won't fix.
  Escalate immediately without consuming a retry; this is a human decision.

Parse defensively anyway, and treat anything that does not yield a valid
verdict as `blocked` rather than guessing. Schema enforcement makes malformed
JSON unlikely but not impossible: a refusal, a `length` finish reason
truncating the response, or a transport error all produce no usable verdict,
and the safe interpretation of "the reviewer did not answer" is "stop and
ask a human."

Expect `blocked` to fire regularly on real work, and treat that as the node
earning its cost rather than as a malfunction. A stage instruction written
from a plan document is a hypothesis about a codebase; on legacy code the
plan is frequently ahead of, or behind, what is actually there.

### Review prompt contents

1. **Reference documents.** The paths in `reference_docs`, included
   verbatim. A stage's instruction text is a pointer into a plan, not a
   substitute for it; where the authority for a decision lives in project
   documentation, the reviewer needs that documentation to judge whether a
   diff honors it.
2. **The other stages in this run**, so the reviewer can catch drift from
   decisions made in earlier stages.
3. **The current stage's instruction.**
4. **The stage's `constraints`**, presented as explicit reject-criteria
   rather than background. This is the reviewer's primary job: the executor
   only ever sees one stage at a time and has no way to know that a
   technically-correct edit is illegal in *this* stage's context.
5. **The stage diff.**

**This order is the caching strategy, not just presentation.** Items 1 and 2
are large and byte-identical across every stage of a run; items 3 through 5
change per stage. OpenAI caches automatically on matching prompt *prefixes*,
so putting the stable payload first and the stage-specific diff last is what
makes the cache hit. Reordering these for readability would silently double
the cost of every review.

Log the cached-token count so a regression here is visible rather than
merely expensive. The economic argument for splitting executor from reviewer
depends on the paid model being invoked at checkpoints with a mostly-cached
prefix.

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

By default, `git reset --hard <stage_start_sha>` before the rework attempt,
so each attempt produces one clean single-purpose diff. Without the reset,
the stage's final diff contains the rejected attempt *and* its correction,
which is exactly the unreviewable history a thin shippable unit is supposed
to avoid. The rejected attempt is preserved in the run log either way.

> Both of these are deliberate defaults, not settled conclusions. Implement
> them behind config flags `rework_strategy: fresh | continue` and
> `rework_reset: true | false` so the alternatives can be tested later
> without a rewrite. `rework_reset: false` is the right choice for stages
> where a rework is genuinely additive to a partially-correct attempt.

If a stage escalates, the operator can inspect or reset from
`stage_start_sha`.

## Greenfield and test-first stages

The same loop drives initial development, not only refactors, but two
assumptions have to relax.

**There may be no suite to run.** `test_command` may be absent for a
bootstrap stage, with `checks` carrying verification instead — the project
builds, the binary runs, the server answers. A stage with neither a test
command nor any checks is a config error; `validate` rejects it.

**"Tests pass" is trivially true when there are no tests.** Set
`require_new_tests: true` on stages implementing behavior, so a stage that
writes no tests fails rather than passing vacuously. This makes the
test-first expectation a machine check instead of a hope about what the
executor chose to do.

**There is no prior behavior to preserve.** Per-stage `acceptance` prose
gives the reviewer criteria to judge against, since on new code the diff
cannot be assessed as "does this change behavior."

## Configuration

One YAML file per run, living in this project (not the target repo):

```yaml
target_repo: /Users/me/code/some-app
base_ref: main
branch: refactor/extract-order-service

# Runs before the executor and again before verify, so it must be
# idempotent. Where a target repo's tests execute in a container or need
# dependencies installed, this is what makes that happen.
setup_command: "docker compose up -d db"

# Scoped commands during iteration; the full suite runs once at the end.
test_command: "bin/test"
full_test_command: "bin/test --all"

# Included verbatim in every review prompt, and cached.
reference_docs:
  - docs/refactor_plan.md
  - docs/architecture_decisions.md

executor:
  # llama.cpp on the Spark exposes an OpenAI-compatible endpoint
  model: "openai/<local-model-id>"
  api_base: "http://<host>:<port>/v1"
  api_key_env: "LOCAL_API_KEY"     # dummy value is fine for llama.cpp
  lint_command: "<lint command>"
  map_tokens: 0                    # repo map off; stages declare their files

reviewer:
  provider: "openai"              # the only implementation in v1
  model: "<model-id>"
  api_key_env: "OPENAI_API_KEY"

limits:
  max_test_retries: 3      # per stage, any retryable verify layer failing
  max_rework_retries: 2    # per stage, reviewer rejections
  aider_timeout_seconds: 1800
  command_timeout_seconds: 3600   # setup, tests, checks, context commands

rework_strategy: fresh
rework_reset: true

stages:
  - id: "extract-service-objects"
    kind: agent
    preconditions:
      - "! grep -rq 'LegacyOrderMixin' app/"
    context_commands:
      - "bin/inventory-actions OrdersController"
    edit_files:
      - "app/services/**"
      - "app/controllers/orders_controller.rb"
      - "spec/services/**"
    read_files:
      - "app/controllers/application_controller.rb"
    test_command: "bin/test spec/services spec/controllers/orders_controller_spec.rb"
    checks:
      - "bin/route-snapshot --diff"
    forbidden_patterns:
      - "SomeApiNotAvailableYet"
    constraints: |
      This diff must remain compatible with <current platform version>.
      Reject any API introduced in a later version.
    instruction: |
      Extract the order-processing logic from OrdersController into
      a service object under app/services. Preserve existing behavior;
      all existing specs must continue to pass.

  - id: "annotate-generated-files"
    kind: script
    command: "bin/annotate-all"
    edit_files:
      - "db/migrate/**"
    # No model tokens spent; still verified and reviewed like any stage.

  - id: "bump-runtime"
    kind: manual
    human_steps: |
      Bump the runtime version in .tool-versions, the Dockerfile base
      image, and CI config. Resolve dependencies. Deploy and confirm
      green in production before resuming this run.
    setup_command: "docker compose build && docker compose up -d db"
    review: false
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
  --test-cmd "<stage test_command or global>"
  --auto-test
  --lint-cmd "<executor.lint_command>"
  --map-tokens <executor.map_tokens>
  --file <each edit_files glob, expanded>
  --read <each read_files glob, expanded>
```

with `cwd` set to `target_repo`. Capture stdout and stderr to the run log.
Enforce `aider_timeout_seconds`; on timeout, kill the process and treat it
as a failed attempt. Omit `--test-cmd` and `--auto-test` when the stage has
no test command — on a greenfield stage there is nothing yet to run, and
`verify`'s `checks` layer carries the verification instead.

`--file` / `--read` scoping is not optional on a repo of any size. Handing
a local model an unscoped repository means the repo map alone consumes the
context window before the task is stated, and edit quality collapses. Every
`agent` stage declares the files it may edit and the files it needs for
context; if a stage cannot be expressed that way, it is too broad to be a
stage.

Verify the exact flag names against `aider --help` at build time rather
than trusting this list — Aider's CLI surface changes between releases.

## Checkpointing and resume

Use a LangGraph checkpointer backed by SQLite, stored in this project under
`./runs/<run_id>/state.db`. The target repo must stay free of orchestrator
artifacts.

The CLI must support resuming an interrupted run from its last checkpoint,
picking up at the stage and attempt count where it stopped. Resume is also
the mechanism for continuing past a `manual` stage: a run in
`awaiting_human` status re-enters at `verify` for the gated stage.

## CLI

```
orchestrator run <config.yaml>              # start a new run
orchestrator resume <run_id>                # continue an interrupted or gated run
orchestrator status <run_id>                # where a run stopped and why
orchestrator validate <config.yaml>         # check config without executing
```

`validate` should confirm:

- The target repo exists, is a git repo, and has a clean working tree.
- `base_ref` exists and the working tree matches it.
- `branch` either does not exist or is safe to continue on.
- The global `setup_command` succeeds.
- The global `test_command` and `full_test_command` pass on the clean tree —
  a target repo that is already red makes every subsequent verdict
  meaningless.
- Every stage is well-formed for its kind: `agent` has an instruction,
  `script` has a command, `manual` has human steps; every stage has either a
  test command or at least one check.
- Every stage's `forbidden_patterns` compile as regexes.
- Every reference document in `reference_docs` exists and is readable.
- Both model endpoints are reachable and required env vars are set.
- Every stage precondition that can be evaluated against the current tree
  is reported as met or unmet. Unmet preconditions for later stages are a
  warning, not an error — an earlier stage may satisfy them — but the report
  makes a mis-ordered stage list visible before anything runs.

Run these checks automatically at the start of `run` too — failing fast
beats failing on stage 6.

## Logging

Per run, write to `./runs/<run_id>/`:

- `run.log` — human-readable timeline of node transitions and decisions
- `stages/<n>-attempt-<m>/executor.log` — raw Aider or script output
- `stages/<n>-attempt-<m>/verify.log` — each layer's command and result
- `stages/<n>-attempt-<m>/review.json` — the reviewer's parsed verdict
- `report.md` — written on completion, gating, or escalation

Per stage, the report records: stage id and kind, outcome, commit range,
wall-clock time, test-suite runtime, retries consumed, flake re-runs,
which verify layer failed when one did, reviewer issues raised, and
reviewer token usage and cost.

Log every reviewer API call's token usage, cached and uncached, so cost per
stage is visible. The whole point of the split is that the paid model is
invoked at checkpoints rather than continuously — the report should make it
easy to confirm that's actually happening. Test-suite runtime belongs
alongside it because on a large suite, verify time rather than token cost is
usually what makes a run expensive.

## Safety requirements

- Refuse to start if the target repo has uncommitted changes.
- Record the target repo's `base_sha` at run start and include it in the
  report so the entire run can be reset with a single command.
- Create or check out `branch` at run start and commit only there. Never
  run `git push`, and never modify `base_ref` or any other branch. Opening
  the pull request is the operator's action, deliberately — the
  orchestrator's output is reviewed by a human before it goes anywhere.
- The orchestrator never runs shell commands originating from model output.
  Every command it executes — setup, tests, checks, preconditions, context
  commands, script-stage transforms — is declared in the config file by the
  operator. `context_commands` inject command *output* into a prompt; no
  path exists in the other direction.
- Enforce `command_timeout_seconds` on every declared command, not only on
  the executor subprocess.

## Build order

1. Project scaffolding, `pyproject.toml`, config loading and validation.
2. The declared-command runner: subprocess execution with timeout, output
   capture, and structured result. Setup, tests, checks, preconditions,
   context commands, and script stages all go through it, as does the
   layered `verify` sequence built on top of it. This is the most testable
   piece in the system and everything else depends on it — build it before
   anything that needs a model.
3. Aider subprocess wrapper with file scoping, timeout, and log capture —
   testable standalone against a throwaway repo.
4. Reviewer client with reference-document assembly, prompt caching, and
   structured-output parsing — testable standalone against a canned diff.
5. LangGraph state machine wiring these together, with checkpointing,
   branch management, and the `gate` / resume path.
6. CLI, logging, and report generation.
7. Tests: unit tests for config validation, prompt construction, verdict
   parsing, scope-guard glob matching, and forbidden-pattern matching; an
   integration test that runs a multi-stage refactor against a fixture repo
   with a stubbed reviewer, covering one `agent` stage, one `script` stage,
   one gated `manual` stage resumed to completion, and one escalation from
   each verify layer.

Commit at each step.
