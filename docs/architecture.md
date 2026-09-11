# CodeGantry — Architecture

This describes the system as built, and mostly answers *why* rather than *what*.
[README.md](../README.md) is how to use it and [CLAUDE.md](../CLAUDE.md) is what to
know before changing it; this is the one that explains why the shape is the
shape.

It began as a specification written before any code existed, and much of it
survived contact unchanged — but not all, and where the two diverged the code
won and this document was corrected. Nearly every design note here carries the
observation that produced it, usually a failure. Those are the expensive part:
the decisions are easy to re-derive and the incidents are not, and a reader
deciding whether to change something needs to know what the current shape is
paying for.

## What this is

A standalone Python tool that drives a long, multistage code refactor across
three model roles: an executor that edits, a planner that decides what to do
next, and a reviewer that decides whether it was done acceptably. Every role
carries the same endpoint settings, so each points at whatever model the
operator chooses — local or hosted — and nothing in the design assumes which.
Where a role's cost is discussed below, that is a property of the model in
front of it on one run, not of the role.

The executor does all the editing, in-process against the provider's own SDK. The reviewer inspects each finished unit of work and approves or
rejects it. The planner derives units of work from a plan document, revises
them when they turn out to be wrongly drawn, and revises the plan itself when
reality diverges from it. CodeGantry owns the loop: it decides when to
retry, when to re-plan, when to merge, and — rarely — when to stop and wake a
human.

The operating goal is **to run as long as feasible without a human in the
loop.** A ten-page plan should ideally execute in one unattended pass,
producing a branch of granular, individually green, individually reviewed
commits that a human can examine and deploy from on their own schedule. Every
escalation path that can be converted into machine work should be.

**There is no planned human step.** A human's involvement means something
broke — a deliberate mid-run pause would contradict one-shotting a ten-page
plan. So there is no `manual` stage kind and no gate node: the run either
proceeds or it escalates, and `resume` picks up after the human has fixed
whatever stopped it.

This tool lives in its own repository. It operates on a target repo from the
outside, never from within it.

## Terminology

Four terms, and they are not all the same kind of thing. Three describe the
work and map onto branches; one describes an execution session.

| Term | What it is | Maps to |
|---|---|---|
| **project** | A whole body of work, defined by a plan document | A directory under `projects/`, and a long-lived project branch |
| **stage** | One unit of work — the shippable unit | A child branch, squash-merged to the project branch |
| **attempt** | One executor invocation within a stage | Nothing; a counter |
| **run** | One resumable execution session against a project | A directory under `runs/`; no branch |

**A stage is the shippable unit.** The planner derives it, the executor builds
it on a child branch across any number of commits, the reviewer approves it, and it
is squash-merged to the project branch as one commit. The analogy is a pull
request targeting something other than the default branch: mergeable to its
target, not necessarily deployable to production. The project branch is what
eventually targets `main`, and that outer merge is a human's decision, outside
this tool.

**A run is orthogonal to that hierarchy.** It is one invocation of
CodeGantry — possibly executing every stage in the project, possibly resumed
after an interruption. There is no run branch. Runs exist to scope
checkpoints, logs, and reports.

Do not use "subunit" or "phase". Both are synonyms for *stage*, and stage is
the term the implementation uses throughout.

## Non-goals

- Not a fleet manager. One target repo, one stage at a time, sequential. No
  parallel agents, no worktree management. Sequential execution is not a
  limitation being accepted — it is the correct shape when the bottleneck is a
  single inference host's memory bandwidth.
- No deploy-point selection. The tool produces a branch of verified commits
  and a report describing them. Deciding what to deploy and when is the
  operator's, by separate means.
- No PR automation, no GitHub integration. The outer merge to `main` is manual
  and deliberate.
- No dependency resolution, container surgery, deployment, or interaction with
  external systems. Work of that kind escalates to a human, who does it and
  resumes; there is no planned pause.
- No web UI. CLI plus logs plus a report.
- No CI integration in v1.

## Roles and the capability partition

Three models, three distinct capabilities, deliberately non-overlapping. This
partition is the core safety property of the design; everything else is
mechanism.

| Role | Default | May write | May not |
|---|---|---|---|
| **Executor** | A hosted model, called in-process | Product code, within the stage's declared `edit_files` | Anything outside that scope; any command |
| **Planner** | Opus | Stage specs (declarative fields only); plan-document revisions; the status log | Product code; any executable config field |
| **Reviewer** | An OpenAI model | Nothing | Everything |

### The declarative/executable rule

**The planner may write declarative fields. It may never write executable
ones.** This is the invariant that makes unattended operation reasonable
rather than a leap of faith.

Declarative — the planner may author and revise these:

- `instruction` — the task text the executor receives
- `edit_files`, `read_files` — globs, for executor scoping and the scope guard
- `read_excerpts` — a path and two line numbers, read at the stage's starting
  commit and quoted by CodeGantry; declarative in the strongest sense
  available, since there is no string in it that anything executes, and it can
  only point at code that already exists. That last property is what makes it
  the *only* way the planner puts code in front of the executor: a fenced block
  in `instruction` is rejected by `validate_stage` before the stage runs
- `constraints`, `acceptance` — prose the reviewer judges against
- `forbidden_patterns`, `must_not_remain` — regexes, matched in-process against
  added lines and file contents respectively, never shelled out

Executable — operator-declared in the config, never model-authored:

- `test_command`, `full_test_command`
- `setup_command`
- `checks`
- `preconditions`
- a `script` stage's `command`

CodeGantry never executes a shell command that originated from model
output. Every command it runs is declared by the operator in an approved
config file. `context_commands` inject command *output* into a prompt; no path
exists in the reverse direction.

**`kind` is not planner-writable either, which makes `script` stages
unreachable.** A `script` stage needs an operator-authored `command`, and with no
static stage list there is nowhere for the operator to put one. So every
planner-derived stage is an `agent` stage, and a mechanical transform across
hundreds of files is expressed as an instruction to write and run a script —
what the executor does inside its own edit loop is its business, and
CodeGantry still never executes model-authored shell itself. The `script` kind
remains in the schema for a future operator-authored stage source; today it is
reachable only from a test.

**Where the planner needs to influence a command, it supplies arguments, not
the command.** A stage's test run should be scoped to the specs it affects, and
the planner knows which those are — but it may not author shell. So the
operator writes the command with a slot and CodeGantry fills it:

```yaml
scoped_test_command: "docker compose run --rm test bundle exec rspec {paths}"
```

The paths come from `git diff --name-only` against the stage baseline — which
CodeGantry already computes for the scope guard — optionally widened by
planner-declared spec globs. Globs are declarative and already trusted; the
command string stays operator-authored and approval-hashed.

This restriction costs nothing operationally. The failure cases that drive
planner intervention — a red full suite, a scope-guard trip — already name
their own symptoms. The planner does not need to invent a way to detect a
problem; it needs to decide what the executor may touch and what it should be
told to do. Both declarative.

Enforce this in code as an allowlist on the planner's structured-output
schema, not as a comment. A field the planner may not set should be impossible
for it to return, not merely discouraged.

## Hosts

- **CodeGantry and the test suite share a host.** The suite is most of a run's
  wall clock, so that machine is chosen by what the suite is bound on — often
  single-threaded per worker and sensitive to memory latency rather than to
  core count, which is not the machine a throughput benchmark would pick. Keep
  it awake for the duration of a long unattended pass.
- **Inference is wherever an OpenAI-compatible endpoint is**, on the same
  machine or across a network. The tradeoff worth thinking about is co-hosting
  a large local model with the suite: a model resident at a usable
  quantisation can leave too little memory for the database and application
  workers, and dropping quantisation to make room buys a worse test host than
  a second machine already provides.
- **A containerised dev/test environment is worth its overhead** wherever
  production runs from containers, because that is what makes the test
  environment a proxy for the real one.

**Commands in the config are host-specific.** `setup_command`, `test_command`
and `full_test_command` assume one machine's toolchain and paths, so `validate`
validates *this host* rather than the config in the abstract. Do not record
which machine that is: the config is a tracked file, and a hostname in it is
one operator's private detail published to everyone who checks the repository
out.

## Filesystem layout

Everything CodeGantry owns lives in CodeGantry repo. The target
repo receives product code, plan-document revisions, and nothing else.

```
projects/<slug>/
  config.yaml            # the project's configuration
  approval.json          # recorded hash of the approved config
  ledger.db              # the plan tree, key states and findings
  status.md              # append-only expected-vs-actual log, one entry per stage
  runs/<run_id>/
    state.db             # one row per node, the run's own checkpointer
    run.log              # timeline of node transitions and decisions
    report.md            # the run's outcome
    stages/<n>-rev-<r>-attempt-<m>/
      executor.log
      verify.log
      review.json
      planner.json
      sent-prompt.md               # what the executor was given, before it ran
      executor-conversation.jsonl  # the exchange, appended turn by turn
      executor-loop.json           # the cycle's totals, written at the end
```

The split between the last two is which question the artifact answers. A
transcript is a sequence and can be appended to, so it is written as the
attempt happens and a stage in progress can be read; totals are only true once
the attempt is over, so they are written once. The first version wrote both at
the end, as a JSON array, and an attempt that never returned left no record of
itself at all — which is the case a record is most wanted for.

`projects/<slug>/` is what makes "project" a concrete thing without making it
a first-class object in code. Nothing in the graph needs a `Project` class.

## Lifecycle

Four commands, each separately re-runnable. That is what makes the
edit-and-reload loop cheap.

### 1. `code-gantry init <plan-doc-path>`

Produces a draft `config.yaml`. **`init` may prompt** — it is a human at a
terminal doing one-time setup, and a question is cheaper than a fail-and-retry
loop. The non-interactive requirement applies to `run` and `resume`, which
execute unattended and must never block on input.

**Derive the target repo from the plan document's location.** Walk up from the
plan doc to the git root; that is `target_repo`. If the plan doc is not inside
a git repo, init's first action is to ask where to copy it — the plan document
must end up in the target repo, because the repo copy is what CodeGantry
operates against and what the planner revises. Copy **once**, at init: the
repo copy then becomes the living document and the original is a seed. Never
re-copy on later runs, or the planner's revisions would be silently
overwritten by a stale original.

**Discovery splits along the same line as the planner's write permissions.**

*Executable fields come from deterministic repo inspection, never a model:*
the dependency manifest and its lockfile for framework and library versions; a
language-version file such as `.tool-versions` for the runtime; `bin/` and
`.github/workflows/` for the canonical test invocation; `docker-compose.yml`
for services and setup. Free, fast, reproducible.

*Declarative fields are where a model earns its keep:* reading the plan
document to propose `forbidden_patterns` and per-stage `constraints`, which no
heuristic can derive. Regexes are matched in-process, so proposing them does
not cross the invariant.

**Emit provenance comments on every discovered field**, so review is a check
of reasoning rather than of values:

```yaml
test_command: "docker compose run --rm test bundle exec rspec"
# ^ from .github/workflows/ci.yml:31; services from docker-compose.yml
```

### 2. `code-gantry validate <project>`

Proves the config works against this host, *before* a human is asked to
approve it. This is what makes approval meaningful — the operator reviews
policy, not whether `bin/test` exists. Checks:

- Target repo exists, is a git repo, has a clean working tree. **Start-only.**
  `resume` must not require it: a human's fix after an escalation is normally
  uncommitted, and enforcing cleanliness there makes every escalation
  unrecoverable.
- `base_ref` exists; the working tree matches it.
- `project_branch` either does not exist or is safe to continue on.
- The plan root resolves; every child resolves; no child escapes the plan
  root's directory (see "Plan documents").
- `setup_command` succeeds, and succeeds again when re-run — it must be
  idempotent, and this is where that contract is checked.
- `test_command` and `full_test_command` both pass on the clean tree. A target
  repo that is already red makes every subsequent verdict meaningless.
- Every `forbidden_patterns` entry compiles as a regex.
- No configured command matches the **denylist** (below).
- Both model endpoints are reachable; required env vars are set.

`run` re-runs all of this at startup. Failing fast beats failing on stage 30.

### 3. Config identity

There is no approve command. A config is identified by the git blob sha of the
file itself, recorded when a run starts and checked again on every resume, so
an edited config refuses to continue the run it was read for rather than
needing a command run against it.

This makes operator approval mechanical rather than conventional — there is no
`approved: true` field, because such a field could be set by anything, and
nothing has to remember to re-approve. The friction is small and it lands
exactly where friction belongs: on a file full of shell commands about to run
unattended for hours.

### 4. `code-gantry run <project>` / `resume <run_id>` / `status <run_id>`

`resume` continues an interrupted run, or one that escalated and has since
been fixed by a human.
`status` reports where a run stopped and why.

**Editing a repository document the run reads means `run`, not `resume`.**
The agent-facing conventions and operations documents are pinned to a sha
taken at run start — `_doc_sha` is `plan_sha or base_sha` — so a resume after
an `AGENTS.md` edit feeds the old conventions to all three roles for the rest
of the run. The plan itself is not pinned: it is rendered from the ledger on
every derivation, and a fold or a human answer reaches the next derivation
without a restart.

Everything already landed survives a restart — it is on the project branch and
in the ledger, and a fresh run rebuilds its history from those and from
`stage-costs.md` rather than from in-run memory. What restarting costs is the
completed-stage block and a cold prompt cache, which is one prefix write.

**Killing a run is second-best but not dangerous, and the danger is not where
the docstring says.** `pause` is checked before each planner call, so the stage
in flight lands or fails normally; that is the supported route. A kill taken
while the run sits in the planner leaves a clean tree too — but a kill taken
during verify orphaned an `rspec` inside the container holding an `idle in
transaction` connection, and the next run's `db:test:purge` then refused to drop
the test database. The half-applied executor edit the docstring warns about is
the obvious hazard; a child process outliving the run and holding a lock is the
one that actually stopped a resume.

### The denylist

`validate` rejects any configured command matching a hardcoded denylist,
**regardless of operator approval**. At minimum: `git push`; `git checkout` or
`git switch` targeting anything outside the project branch namespace; `git
merge` into `base_ref`; deploy invocations; `gem install` outside a bundle.

This exists because a human will skim a sixty-line YAML once, motivated to
start a run. A denylist the tool refuses to override is the backstop for
exactly that moment.

## The plan, in the ledger

A project is defined by a plan: a tree of documents, sections and items, each
with a key like `{#r5.017}`, a prose body, an owner (`pipeline` or `human`)
and a blocking flag. It lives in the project's ledger, a per-project SQLite
file under the work dir, not in the target repository's tree. Markdown is the
import and export format: `plan import` reads the documents a project already
keeps — closed items become `landed` or `struck` records carrying their sha
and evidence — and `plan export` renders a document back for editing.

**The plan root is a document, not a directory**, and `--follow-links` imports
only the documents it links, one level. Pointing an import at `docs/` would
sweep runbooks, ADRs and onboarding notes into every paid call.

**The ledger is one append-only table of events; everything else is a view.**
The tree, each key's state and the findings table are rebuilt from the events
on read, so no status column can disagree with the history that produced it.
Every event names the origin that wrote it and its sequence within that
origin, which makes a later exchange between hosts a fetch of "origin X after
seq N" rather than a merge.

**The planner is sent two halves rendered from it.** The plan text — the tree
with its keys and the marks a fold has written — sits in the cached block and
changes only at a fold or a plan edit. The projection — landings not yet
marked, keys claimed by other runs, questions waiting on a person, and open
findings with their ids — follows the cache mark and is bounded by open keys
times `ledger.note_chars`. When the projection outgrows `ledger.fold_ratio`
of the plan text the run folds at the derivation seam: marks and answered
findings move into the node bodies, with no model and no commit.

**Findings have a lifecycle.** A planner note opens one at derivation, keyed
to an item, with `needs: pipeline` or `needs: human`; a reviewer observation
opens one at landing, for a person. A landing on a leaf key resolves its
findings; the reviewer's `resolved` list closes the ones the stage proposed
to settle; a later note on the same key and subject supersedes; a person
answers with `fold`, `discard`, `debt` or `raise`.

**Nothing in the tree is the plan, so the executor cannot reach it.** What it
can reach — the config, the agent-facing documents and the work dir — is
refused at the write tool and again at the scope gate.

The expected-vs-actual log still goes to `status.md`, append-only, one entry
per stage; nothing reads it back.

## Branch topology

```
main
 └── <project_branch>                          # cut once; the tool never merges it

<project_branch>-stage/001-<id>                # child branch; squash-merged, then deleted
<project_branch>-stage/002-<id>
...
```

**Child branches sit beside the project branch, not under it.** Git refs are
filesystem paths, so `refs/heads/upgrade/rails-5` is a file and
`refs/heads/upgrade/rails-5/stage-001-x` would need it to be a directory. Git
refuses with `cannot lock ref`. Appending `-stage` makes the namespace a
sibling path component, which is a legal ref and still leaves the group
greppable and deletable together:
`git branch --list 'upgrade/rails-5-stage/*'`.

- The project branch is cut from `base_ref` once, and the tool never merges it
  anywhere. The outer merge to `main` is the operator's.
- Each stage gets a child branch in a namespace derived from the project
  branch, so fifty of them stay greppable, deletable as a group, and unable to
  collide with real branches.
- A stage is squash-merged to the project branch on approval, producing one
  commit per stage. The executor's intermediate commits — some of them red,
  since it commits before it tests — are discarded by the squash. **This is why
  "every commit on the project branch is green" and "the executor commits
  before it tests" are both true.** Do not replace the squash with `--no-ff`; it would
  drag red commits onto the project branch.
- **All CodeGantry work stays on the project branch and its children.**
  Nothing else is ever written.
- Set `gc.auto=0` in the target repo for the duration of a run. Rework
  discards child branches, and the reflog is the only recovery path for a
  rejected attempt the operator later wants to inspect.

Expect the project branch to drift from `main` over a long run. The tool never
touches `main`, so reconciliation happens at the outer merge, with the human,
at the point of highest accumulated change. That is the cost of one-shotting
rather than shipping incrementally, and it is accepted deliberately.

## Architecture

### Graph nodes

**`plan`**
Invokes the planner. Two modes, distinguished by why the node was entered:

- *Derive* — produce the next stage spec, given the plan snapshot, completed
  stage history, and `status.md`. May instead return `project_complete`.
- *Intervene* — revise the current stage after a failure the executor cannot
  fix. May revise declarative fields, insert a new stage before the current
  one, or return `blocked`.

The planner runs out-of-process with read access to the target repo so it can
ground decisions in what is actually there. Its output is a structured stage
spec, constrained by schema to declarative fields only, and passed through
per-stage `validate` checks before anything acts on it.

Writes an entry to `status.md` on every invocation.

**`precheck`**
Evaluates the stage's `preconditions` — declared commands that must each exit
zero. Then runs `setup_command`, so the environment is correct before the
executor runs. Cuts the stage's child branch from the project branch tip and
records `stage_start_sha`.

Routes to `execute`.

**`execute`**
Dispatches on stage kind:

- `agent` — runs the executor's edit loop on the child branch. First attempt:
  the stage instruction plus any `context_commands` output. Rework attempt: the
  instruction plus the specific failure detail. The loop owns its own edit →
  lint → commit → test cycle inside this node.
- `script` — runs the stage's declared `command`. For mechanical transforms
  across hundreds of files, a script is more reliable and vastly cheaper than a
  model. These stages still go through `verify` and the review gate; only the
  editing mechanism differs.

**`verify`**
The pre-review gate. An ordered sequence of checks, cheapest first,
short-circuiting on the first failure. See "Layered verification".

**`review`**
The composite merge gate: reviewer approval **and** a green full suite. See
"The review gate".

**`advance`**
Squash-merges the child branch into the project branch with a stage-labeled
message, deletes the child branch, records the stage as complete with its
commit sha, resets per-stage counters, reloads config if it changed, and
returns to `plan`.

**`finalize`**
Runs `full_test_command` once more on the project branch tip and writes the
final report. Reached when the planner returns `project_complete`.

**`escalate`**
Terminal node. Writes a summary of where the run stopped and why, notifies,
and exits non-zero.

### Edges

```
plan      → precheck   (stage derived, or revised by restart; validate passes)
plan      → verify     (revised by extend — the stage's work stands, so the
                        question is whether it now passes, not what the
                        executor would write over it a second time)
plan      → finalize   (verdict: project_complete)
plan      → plan       (the spec it just produced failed validation — redraw.
                        Nothing was cut and no stage exists, so this is not a
                        revision; it is the same derivation, attempted again
                        with the problems named)
plan      → escalate   (verdict: blocked, or planner budget exhausted)

precheck  → execute    (preconditions and setup pass)
precheck  → plan       (a precondition failed — an ordering error the planner owns)
precheck  → escalate   (setup failed — a broken environment, not a planning defect)

execute   → verify
execute   → plan       (a context command failed, so the prompt would have been
                        built from missing information)
execute   → execute    (the executor failed to run at all — transport, a
                        rejected request, a missing credential — so there is
                        nothing for a gate to look at)
execute   → escalate   (a commit hook refused the work: not a gate and not a
                        planning defect, and the next attempt is refused
                        identically)

verify    → review     (every layer passed, and the stage wants review)
verify    → advance    (every layer passed, and it does not)
verify    → escalate   (setup or branch-identity failure — no retry consumed)
verify    → plan       (a scope violation, or an identical diff twice: the fix
                        lies outside declared scope, or outside anything another
                        attempt can reach)
verify    → execute    (patterns, residue, tests, checks or new-tests failed;
                        retries remain)
verify    → plan       (same, retries exhausted)

review    → advance    (reviewer approved and full suite green)
review    → execute    (rejected or suite red; rework retries remain)
review    → plan       (rejected or suite red; rework retries exhausted)
review    → escalate   (the full suite was killed by a signal after approval —
                        the work is correct and the environment is gone, so
                        this is the one review exit that is not a rejection)

advance   → plan       (next stage)

finalize  → END        (full suite green, report written)
finalize  → escalate   (full suite red)
```

START routes on how the run last stopped, so a resume does not redo work:

```
START     → verify     (stopped at a repo-state failure — setup, branch, scope,
                        patterns, tests, checks. A human has since changed the
                        repo, so check it before doing anything else)
START     → plan       (stopped at a planning failure — blocked, or a budget
                        exhausted. A human has since changed the plan document
                        or the config, so re-plan)
START     → precheck   (a fresh run)
```

Re-entering at `precheck` after an escalation would re-run the stage from the
top and throw away the human's fix. Getting this wrong produces a loop that
looks like progress and never makes any.

Note what is largely *absent*: paths straight to `escalate`. That is the
point — and one of them was removed rather than designed away. A stage spec
that failed validation escalated on the first occurrence, under a comment
arguing it was "the planner's error to fix". A malformed spec is precisely a
stage drawn wrongly, which is the planner's tier; the escalation cost a human
for something one more planner call resolves. It became worth changing when
validation started rejecting authored code in an instruction, since that rule
asks a model to break a habit and a single slip would stop an unattended run.
The redraw is bounded by the same counter as every other way the planner fails
to make progress — which had to be widened to see it, having been conditioned
on a stage being in flight and so unreachable for the one failure that leaves
none.

### State schema

```python
class Stage(TypedDict, total=False):
    id: str
    kind: Literal["agent", "script"]   # default "agent"

    # --- planner-writable (declarative) ---
    instruction: str
    edit_files: list[str]          # globs the executor may edit; scope guard
    read_files: list[str]          # globs passed as read-only context
    constraints: str               # invariants the reviewer must enforce
    acceptance: str                # what "done" means, for greenfield stages
    forbidden_patterns: list[str]  # regexes barred from the diff's added lines

    # --- operator-only (executable) ---
    preconditions: list[str]
    context_commands: list[str]    # stdout injected into the executor prompt
    setup_command: str | None
    test_command: str | None       # scoped override of the global command
    checks: list[str]

    # --- operator-only (policy) ---
    require_new_tests: bool
    review: bool                   # default True
    full_suite_on_approval: bool | None   # overrides the project default


class RunState(TypedDict):
    run_id: str
    project_slug: str
    config_hash: str               # which config this stage ran under
    target_repo: str
    base_ref: str
    base_sha: str
    project_branch: str
    stage_branch: str | None
    completed: list[StageResult]   # append-only history
    current: Stage | None
    stage_index: int
    revision: int                  # planner revisions of the current stage
    verify_attempt: int            # retryable verify failures, this revision
    rework_attempt: int            # review-gate rejections, this revision
    stage_start_sha: str
    last_failure: FailureDetail | None
    failure_layer: Literal[
        "precondition", "setup", "branch", "scope", "patterns",
        "tests", "checks", "new_tests", "review", "full_suite",
    ] | None
    flake_reruns: int
    planner_interventions: int     # global, across the run
    review_feedback: list[str]
    status: Literal["running", "complete", "escalated"]
```

**Two retry counters, not one.** `max_test_retries` and `max_rework_retries`
are separate budgets and a single `attempt` cannot enforce both. They reset
together when the planner revises the stage or when it completes.

**`completed` holds completions only.** An escalation records which stage
stopped the run in a separate field, never as a history entry. A stage that
escalates, gets fixed by a human, and completes on resume must appear once with
one outcome — appending escalations produces two contradictory records for the
same stage, and it also breaks the append-only property the reviewer's cache
prefix depends on.

A static `stages` list is gone. History is append-only; exactly one stage is
pending at a time. That is what keeps the reviewer's context prefix stable even
when the planner inserts a stage — completed history never changes, so it
remains a genuine prefix.

### Diffs and commits

Every stage diff is computed as `git diff <stage_start_sha>` — against the
working tree, **not** `<stage_start_sha>..HEAD`.

The executor commits its own work, so `..HEAD` happens to work for `agent`
stages, but a
`script` stage leaves its transform uncommitted, and so does a human's fix
after an escalation. A `..HEAD` diff would be empty for both, which means the
scope guard would pass vacuously, `forbidden_patterns` would match nothing, and
the reviewer would approve an empty diff. Diffing against the working tree
covers every case uniformly.

On re-review after rework the reviewer sees the **cumulative** stage diff, not
the delta since the last rejection — the same way a PR re-review shows the
whole diff. The alternative would let a rework introduce a regression in code
the reviewer already cleared.

## Layered verification

`verify` is an ordered sequence, cheapest gate first, short-circuiting on the
first failure. Ordering is economic: free deterministic checks run before
anything costing minutes of compute or a paid API call.

**0. Setup.** The stage's `setup_command`, or the global one. This is where a
containerized target repo gets its image rebuilt or test database reloaded —
the steps that must happen after a stage changes the environment itself.

`precheck` already ran setup once. It runs again here because the stage's own
edits may be what invalidated the environment: an escalated run resumes at
`verify`, and a rebuild has to happen after the human's fix, not before it.
**`setup_command` must therefore be idempotent.**

**1. Branch identity.** HEAD is on the expected child branch and is a
descendant of the project branch tip; the remote ref has not moved.

This layer exists because `script` stages, `checks`, `preconditions`, and
`setup_command` are all arbitrary operator-authored shell, any of which could
contain a stray `git checkout` or `git push`. Over a ten-hour unattended run,
that is the failure you would least like to discover afterward. Free,
deterministic, escalates immediately.

**2. Scope guard.** `git diff --name-only <stage_start_sha>` must fall entirely
within the stage's `edit_files` globs.

A violation routes to `plan`, not to a human. There are two possible causes —
the executor wandered, or the executor correctly concluded the fix lies outside
its box — and they are indistinguishable from the diff.

**Do not discard the branch on a scope violation.** The child branch is already
the quarantine: nothing reaches the project branch without passing this gate,
so containment does not require destroying work. Hours of correct effort should
not be thrown away because one unexpected spec file was touched. Instead:

1. Keep the branch and hand the planner the list of out-of-scope paths.
2. If the planner widens `edit_files` to include them, the existing work
   revalidates as-is and usually just passes — cost zero.
3. Only if the planner declines to widen do those specific paths get reverted
   to the stage baseline, leaving the in-scope work intact for the next
   attempt.

Reverting the whole stage is never the right move, and reverting nothing would
let out-of-scope edits ride along into a later attempt that happened to pass.

**3. Progress.** This attempt's diff is not byte-identical to the last one.

Routes to `plan`, immediately, without consuming a retry. An attempt that
reproduces the previous diff exactly proves the feedback changed nothing, and
another attempt against the same instruction will change nothing either — the
stage needs redrawing, not retrying. It sits here rather than lower down
because "is this the same answer as last time" makes every question below it
moot, and answering it costs a hash.

**4. Forbidden patterns.** Each regex matched against the diff's **added lines
only**. Free, deterministic, before any test suite.

The point is catching scope and compatibility violations mechanically rather
than hoping the reviewer notices. The canonical case is staged migrations where
an API is legal in a later stage but not the current one — on a Rails upgrade,
`load_defaults` and `new_framework_defaults` flags are exactly this. Declaring
the later-stage syntax forbidden turns a subtle review question into a regex.

Added-lines-only is load-bearing. A stage whose purpose is *removing* a
construct would otherwise flag itself the moment it succeeded, and a stage
replacing a bare form with a qualified one needs the bare form forbidden on the
way in while still deleting it on the way out.

**5. Residue.** Each `must_not_remain` regex matched against the **contents** of
the files the stage owns.

The exact complement of layer 4, and the two read almost identically in prose
while being opposites in a diff. `forbidden_patterns` asks what the stage
*introduced*; `must_not_remain` asks what *survived*. A stage whose whole
purpose is removing a construct cannot be checked by the first — succeeding
looks the same as never starting — and is checked precisely by the second.

Its characteristic false positive is worth knowing: an executor that removes
something and leaves a comment explaining what used to be there has written the
forbidden token back into the file. That is good practice colliding with a
state check, and it has cost real attempts.

**6. Tests.** The stage's `test_command` if set, otherwise the global one.
Per-stage commands should be *scoped* — the specific specs the stage touches —
because this runs on every attempt, and a large suite multiplied by retries
dominates wall clock. On a Rails upgrade the prework tail scopes well; the
version-bump stage does not, and should simply declare the full suite as its
own test command.

On failure, re-run once before consuming a retry. If the re-run passes,
increment `flake_reruns` and continue; the stage is marked flaky in the report.
Suites with browser-driven or timing-sensitive tests otherwise burn their
entire retry budget on noise.

**7. Checks.** Each command in `checks` must exit zero. The hook for
verification a test suite does not provide: a route-table snapshot diff, an
endpoint scanner, an asset build, a boot check.

"The tests pass" is frequently an insufficient gate. A change can be green and
still silently alter behavior no spec covers — and on a legacy codebase, the
areas with the thinnest coverage are exactly the ones a refactor is most likely
to disturb.

**A check may also write.** The useful linters fix as well as report, and an
autocorrecting one is worth running precisely so the executor does not spend an
attempt on a line break. But the executor commits its own work before verify
starts, so nothing else in the loop commits what a check changed — and left
uncommitted it is swept up silently when the stage lands and orphaned when it
does not, at which point the next stage's precheck refuses to cut a branch over
changes it cannot attribute. `checks_commit_changes` commits it to the child
branch instead, where it is squashed on landing or discarded with the branch.

Note the ordering this implies: checks run *after* the tests, so a fix applied
here is not covered by the suite run that just passed. What covers it is the
full suite at the merge gate, which runs after review and before anything
lands.

**8. New tests.** Two rules, and only the second is conditional.

Any test file the stage touched must not be empty — always, whatever the stage
was configured to require. The editor creates a file the moment it is named in
scope, so a reply that was never applied as an edit leaves a zero-byte file
behind, and that file passes every other gate honestly: the suite is green in
seconds because there is nothing in it to run, and the diff really did add a
test file. Only reading the contents catches it.

And if `require_new_tests` is set, the diff must *touch* at least one test file.
Touched rather than added, deliberately: adding cases to an existing spec is
legitimate test-first work, and demanding a brand-new file pushes the executor
into creating redundant ones. What counts as a test file comes from
`test_file_patterns`, so a project that names them unconventionally can say so.
See "Greenfield and test-first stages".

The split matters because the flag answers whether a stage *must* write tests,
which is a planning decision — not whether a file it did write is worth
anything, which is not.

**Feedback, not exit codes.** Every failing layer produces something the
executor can act on — the matched forbidden lines, the failing test names, the
check's stderr. A retry given no information about why the last attempt failed
is a wasted retry. The same applies with more force to a planner intervention,
since a planner invocation costs more than an executor attempt.

## The review gate

**A stage passes review when the reviewer approves it *and* the full suite is
green.** One composite gate, one verdict, one rework budget.

Evaluate the two conditions **cheapest first, short-circuiting**: the reviewer
call is seconds and pennies, a full Rails suite is minutes. A stage the
reviewer would reject never pays for a full suite run. And because the full
suite runs only after approval, it costs one execution per stage that lands —
linear in stages, not in attempts.

The full-suite half is controlled by `full_suite_on_approval`, a project
default with per-stage override. It is the right gate for a Rails upgrade,
where each project-branch commit should stand on its own. It is not right for
every project, so it is configuration rather than policy.

`review: false` opts a stage out of the reviewer half. **It defaults to `True`
for both stage kinds.** A `script` stage lands a commit on the project branch
like any other, and a scripted transform across hundreds of files is not
obviously safer than a model's — opting out should be an explicit per-stage
decision, never a consequence of kind.

**Flake handling matters most here.** A flaky full suite blocks a stage that is
fine, and the full suite has far more surface for ordering and timing flakes
than a scoped run. Apply the same re-run-once rule, and distinguish "flaked at
the review gate" from "flaked during iteration" in the report — the former
wastes planner interventions, which are the expensive kind.

### Reviewer contract

Structured output, enforced by the provider's native support (for OpenAI,
`chat.completions.parse` with a Pydantic `response_format`) rather than prompt
discipline:

```json
{
  "verdict": "approved" | "rework" | "blocked",
  "summary": "one-line assessment, justifying the verdict",
  "record": "what the change does, for the ledger and the landing commit",
  "resolved": ["ids of the proposed findings the diff actually settles"],
  "issues": [
    {
      "severity": "major" | "minor",
      "file": "path/to/file.rb",
      "description": "what's wrong and why it matters"
    }
  ],
  "observations": [
    { "file": "...", "finding": "...", "detail": "..." }
  ]
}
```

`record` is required, and `observations` is not, which is the whole difference
between them: the optional one came back empty 278 times out of 278 across two
prompt revisions, while a field that is always answerable is always answered.
It is separate from `summary` because `summary` justifies a routing decision
and reads like one — every instance opens by confirming the diff matches the
stage and closes on what was not introduced, which is the right shape for a
gate and the wrong shape for something read a year later.

The reviewer writes it because it is the only participant that has seen the
diff. Nothing else in the loop records what a stage actually did: the stage
instruction says what was asked for, and the planner's notes are composed
before the executor runs.

- `approved` — proceed to the full-suite half of the gate.
- `rework` — loop back to `execute` with the issues as feedback.
- `blocked` — the stage instruction itself is wrong, or the plan has a flaw
  reworking this stage will not fix. Routes to `plan`, not to a human: with a
  planner in the loop, "the instruction is wrong" is a planning problem with a
  planning fix.

Parse defensively regardless. Treat anything that does not yield a valid
verdict — a refusal, a `length` finish reason, a transport error — as
`blocked`. The safe reading of "the reviewer did not answer" is "do not
merge."

Expect `blocked` to fire regularly on real work, and treat it as the node
earning its cost. A stage instruction derived from a plan document is a
hypothesis about a codebase; on legacy code the plan is frequently ahead of, or
behind, what is actually there.

### Review prompt contents, in this order

1. **The plan snapshot**, included verbatim.
2. **Completed stage history** — so the reviewer can catch drift from decisions
   made in earlier stages.
3. **The current stage's instruction.**
4. **The stage's `constraints`**, presented as explicit reject-criteria rather
   than background. This is the reviewer's primary job: the executor sees one
   stage at a time and cannot know that a technically-correct edit is illegal in
   *this* stage's context.
5. **The cumulative stage diff.**

**The history is thin, deliberately.** A landed stage is described to the
planner by four separate channels — this history, the ledger's projection,
`stage-costs.md`, and the status tail — and for a long time this one
reproduced what three of them already said: the stage's full instruction
verbatim, the reviewer's verdict summary, and the executor's context cost.
Measured on one run at 45 stages that was 296,783 characters, resent on every
tool iteration and growing by ~6,600 per landing.

It now carries only what nothing else records: which stages this run landed,
their revision counts, the reference files the executor never received, and
the ids that tie the other three channels together — 4,278 characters for the
same 45 stages. The instruction and the reviewer's record both live in the
landing commit; the landing itself is in the ledger, rendered on every call;
the cost is in `stage-costs.md`, which spans every run rather than one.

**This order is the caching strategy, not presentation.** Items 1 and 2 are
large and append-only; items 3 through 5 change per stage. Providers cache on
matching prompt *prefixes*, so the stable payload goes first. Note that history
is included but *projected future stages are not* — they do not exist yet, and
including them would break the prefix every time the planner inserted one.

Log cached and uncached token counts so a regression here is visible rather
than merely expensive.

## Planner contract

Structured output, same discipline as the reviewer, with a schema that makes
executable fields unrepresentable.

```json
{
  "verdict": "next_stage" | "revise" | "project_complete" | "blocked",
  "reasoning": "why — recorded in status.md",
  "stage": { "...": "declarative fields only" },
  "revision_mode": "extend" | "restart",
  "status_entry": "goal / expected / actual / divergence / next goal",
  "plan_revisions": [ { "path": "...", "rationale": "..." } ]
}
```

The planner reads the repository through the same bounded tools the reviewer
does, and reads *narrowly*: measured over 1,583 calls across two runs, the
median read is 41 lines and fewer than 1% reach the per-call cap. It read one
1,935-line file twenty-seven times in ranges of fifteen to thirty-three lines.
`read_excerpts` exists because that knowledge used to stop there — the stage it
then wrote handed the executor whole-file globs, and the executor had to find
the same region again before it could act on it.

It carries a second job now, and the two fit together. The planner used to
paste code into the instruction: the current text of a file, and the exact text
that should replace it. Measured over one run of 48 stages, five of eleven
rejections conceded that the behaviour was right and rejected the shape,
because every line of an instruction is a reject criterion; one stage deadlocked
because an authored edit stops being satisfiable once part of it is already
true on the branch; and one rewrote a whitelist by hand, preserving an entry
that named a column the table did not have, in a stage drawn to fix entries of
exactly that kind. A reference has none of those failure modes — it is read
rather than remembered, so it cannot be stale, and it cannot express an
after-image at all. Excerpts are therefore read at `stage_start_sha`, the same
baseline the reviewer's diff and the planner's revision block use, and a range
that will not resolve fails the stage to the planner rather than being skipped:
with no code in the instruction, the excerpt *is* the code.

- `next_stage` — a new stage spec. `revision` resets to 0.
- `revise` — a revised spec for the *same* stage id. `revision` increments,
  `attempt` resets. Same stage in the report, so it reads "stage 14, revision
  2, attempt 3" rather than pretending stages 14–16 were different work.
- `project_complete` — the plan is executed. Routes to `finalize`.
- `blocked` — cannot proceed without a human. Escalates.

**`revision_mode` matters.** A scope-widening revision means the existing
child-branch work is still correct and merely incomplete: keep the branch and
build on it (`extend`). A re-specification means the approach was wrong:
discard the branch, re-cut from the project branch tip, start over
(`restart`). The planner knows which it is doing, so it declares it.

**The planner may not veto approved work.** It has no verdict that rejects a
stage the reviewer has already passed. Its authority is forward-looking: what
the next stage should be, whether the current one was drawn correctly, and
whether the plan still matches reality.

**What the planner needs at an intervention** is the specific failure detail,
not an exit code. Which tests failed and what files they live in is what
distinguishes "widen this stage by two files" from "we skipped a prerequisite,
insert a stage before this one."


### What each role reads, and what it costs

The repository's own agent-facing documents are split by audience rather than
by file. `agent_context` is conventions — how code here has to look — and goes
to all three roles: the planner draws against it, the executor writes code that
must obey it, and the reviewer judges whether the code did. `operations_context`
is build, test and deploy, and goes to the planner alone. That asymmetry is the
point: the planner is the role deciding what is *possible*, and the one that
read five plan items as blocked because nothing had told it the container
reinstalls the bundle on a Gemfile edit; the executor runs no commands, so a
page of them invites it to narrate one it never ran.

The executor's copy arrives inside the cached prefix, ahead of the plan and
inside the same breakpoint. Both are fixed for the life of a run, so that
placement is paid for once rather than per stage, and the model it is sent to
caches at an explicit breakpoint without falling back to the longest matching
prefix — static content after the mark misses every time.

Reasoning effort is per-role config rather than a constant in each client:
`planner.effort` and `reviewer.effort` name the provider's own literals, and
`executor.reasoning_effort` becomes a `reasoning.effort` field on the request,
omitted entirely when unset so a model that does not reason is never sent it.
The ceiling is a property of the *endpoint* rather than of the model —
chat/completions refuses `max` for both GPT-5.6 models while `/v1/responses`
accepts it — which is one reason the executor calls the responses endpoint.

And the executor is priced now. It never was, because a local model is free and
the measured economics — the planner at 91% of tokens against the executor's
2.2% of prompt volume — assumed that. The figure comes from the usage block the
provider returns on the call, which is what makes it trustworthy: an accounting
scraped from another tool's console reports a zero for "not priced" as readily
as for "free", and reads whichever cache fields that tool happened to
implement. Holding the provider's own numbers removed both blind spots and cost
nothing to add.

The planner and reviewer are priced too, and not from a table anyone here
maintains. The first design put hand-written rates in project config, which is
the "config holds the path, not the copy" mistake with money in it — a second
copy that drifts, and the copy is the one the report reads. The rates live in
`model_prices_and_context_window.json`, published at a public URL and kept
current by people who are not us; the copy bundled in any package that vendors
it is a stale fallback. `pricing.py` reads that URL, caches it, and never fails
a run over it — a price list is a report, not a gate. Cache writes get their own bucket because
they are billed above base (6.25e-06 against 5e-06 on Opus), which meant
threading `cache_write_tokens` through `RunState`: both clients had computed it
per call for as long as they had existed, and it stopped at the artifact.

Measured on one project, this settles an argument the leaderboards cannot. Over
a 29-stage run the planner cost $136.32 and the reviewer $13.67, both at 92%
cached prompt — about $5 a landed stage, corroborated at $4.92 by a separate
six-stage run. Priced against the same rate table, **output is 18% of the
planner's bill and 13% of the reviewer's**; the rest is prompt, most of it
cached. Effort buys output tokens, so doubling reasoning moves those bills by
roughly those fractions, not the 2× a benchmark's cost axis implies — a
benchmark task is short and output-dominated, while these runs resend an
enormous cached prefix on every call. The ratio does not transfer.

The per-role figures come from the run report, which is the only place they
exist. An earlier revision of this paragraph carried two numbers that no
artifact supports; they were written from recollection during the session that
built the pricing code, which is the failure that code was meant to end. A cost
figure in this document should be traceable to a `report.md`, and if it cannot
be, it should be deleted rather than rounded.

## Escalation tiers

Three tiers. The design goal is that a run stops only for a good reason.

1. **Executor retry.** A failure the executor can plausibly fix within its
   declared scope. Loops to `execute` with specific feedback. Bounded by
   `max_test_retries` and `max_rework_retries`.
2. **Planner intervention.** The failure indicates the *stage* was drawn
   wrongly — retries exhausted, a scope violation, an unmet precondition, a
   `blocked` verdict from the reviewer. Loops to `plan`, which revises the
   stage or inserts a predecessor. Bounded by `max_planner_interventions`.
3. **Human.** Everything the planner could not fix, plus setup failure and
   branch-identity failure, which go straight here: a broken environment is not
   a planning defect, and a containment breach does not negotiate.

   This is also where work CodeGantry cannot do at all arrives — a
   runtime bump, a dependency upgrade needing resolution. The planner returns
   `blocked`, the run notifies and stops, the human does the work, and `resume`
   re-enters at `verify` to confirm it landed green. That is the same machinery
   a planned pause would have needed, without a stage kind whose existence
   contradicts unattended operation.

   One asymmetry worth naming in the report: a mis-written `precondition` is
   *unfixable by the planner*, because preconditions are operator-only. The
   planner will spend its whole global budget failing to route around one, so
   the escalation must say "precondition X never passed" rather than presenting
   it as a planning failure.

### Below all three: waiting out the provider

A failure that never reached a model is not an escalation of any tier, and
`retry.py` absorbs it before the tiers see it. The waiting sits above the SDK
rather than inside it, because both SDKs clamp every wait at eight seconds —
covering fifteen minutes there costs 116 retries at best and 154 at worst, the
same `max_retries` would spend all 154 on a genuine API error, and their
retries log at DEBUG where `run.log` never sees them. A silent fifteen-minute
wait and a hung process look identical from outside, which is how the first
outage was found: a human noticed. So the wait is bounded by wall clock
(`transport_retry_seconds`, 900 by default), costs about ten attempts rather
than a hundred and fifty, and says what it is doing on every one.

What counts as worth waiting out is decided by **status code, not exception
class**. The original set was the exceptions meaning the request never
arrived; a 529 then ended a 29-stage run, and a request that arrives and is
told to come back later is the same outage one layer up. `is_transient_status`
now covers 429 and every 5xx — the SDKs' own rule — and rejects 4xx, which
will say the same thing in fifteen minutes and should reach an operator
immediately. The code rather than the class because the two SDKs disagree:
Anthropic raises `OverloadedError` for 529, OpenAI raises
`InternalServerError`. 429 is included here and would not be safe as an SDK
`max_retries`, since honouring a `retry-after` header can mean hours; bounded
by our own clock it cannot.

`max_planner_interventions` is a **global** budget across the run, not
per-stage. Per-stage caps let a pathological project consume unbounded paid
inference one stage at a time.

## Budgets

All limits are safety nets, not tuning knobs. They exist because nobody is
watching.

```yaml
limits:
  max_test_retries: 3            # per stage-revision
  max_rework_retries: 2          # per stage-revision
  max_planner_interventions: 12  # global, across the run
  max_stages: 60                 # global; runaway-planner cap
  command_timeout_seconds: 3600
  wall_clock_hours: 14           # abort the run cleanly at this point
```

The most expensive cycle in the system is review-gate failure → planner
intervention → executor attempts → review gate again: a full suite run plus a
planner call plus a reviewer call per turn of it.
`max_planner_interventions` is the cap least worth raising when a run
escalates.

## Rework prompt construction

Each attempt is a **fresh** conversation rather than a continuation of the
prior one, carrying:

1. The stage instruction, at its current revision.
2. The specific failure — reviewer issues, failing test names, matched lines.
3. **The diff the stage has accumulated so far**, framed as the work being
   amended rather than as a description of what to do.
4. An opening that reflects which gate sent it back.

The failed attempt's conversation history is not carried forward. That
keeps the prompt focused, avoids the executor anchoring on its own earlier
reasoning, and keeps context small — which costs something wherever the model
runs, and most where inference is bandwidth-constrained.

Point 3 is what makes editing forward reasonable: a reviewer leaves comments on
the work in front of it and does not ask for the work again, and an author who
cannot see their own diff is in no position to amend it. Point 4 matters more
than it sounds. The opening said "a previous attempt was rejected, do not repeat
the rejected approach" on every path for a long time, and across one 35-stage
run in which the reviewer rejected nothing, it fired about a dozen times and was
wrong every time — on seven of them instructing the executor to do the opposite
of what the feedback below it asked for. A gate failure means the work stands
and something specific is missing; only a review rejection means start over.

`rework_reset: true` restores the older behaviour of `git reset --hard
<stage_start_sha>` before each attempt. It defaulted to on, for one clean
single-purpose diff per attempt — but stages land by squash merge, so the diff
anyone ever sees is the net one regardless, and the cost showed up the first
time a rejection said the behaviour and scope were correct and only an
explanatory comment contradicted the code. Resetting rebuilt a correct file from
nothing in order to change one sentence. Worth knowing what the flag now buys:
with it on, an attempt starts from the baseline carrying nothing forward but the
feedback sentence, so the retries become independent samples rather than
iterations — close to setting `max_rework_retries` to zero, and slower.

## Greenfield and test-first stages

The same loop drives initial development, not only refactors, but two
assumptions relax.

**There may be no suite to run.** `test_command` may be absent for a bootstrap
stage, with `checks` carrying verification instead — the project builds, the
binary runs, the server answers. A stage with neither a test command nor any
checks is a config error; `validate` rejects it.

**"Tests pass" is trivially true when there are no tests.** Set
`require_new_tests: true` on stages implementing behavior, so a stage that
writes no tests fails rather than passing vacuously.

**There is no prior behavior to preserve.** Per-stage `acceptance` prose gives
the reviewer criteria to judge against.

## Configuration

```yaml
# <target-repo>/docs/<project>/code_gantry.yaml
# No host, target_repo or work_dir: the first would publish one machine's name
# in a tracked file, and the other two are derived from where this was read.
base_ref: main
project_branch: upgrade/rails-5

plan_root: docs/rails_upgrade_plan.md   # repo-relative; children resolve beside it

# Runs before the executor and again before verify — must be idempotent.
setup_command: "docker compose up -d db redis && docker compose build test"

test_command: "docker compose run --rm test bundle exec rspec"
full_test_command: "docker compose run --rm test bundle exec rspec"

# Optional. When set, per-stage iteration runs only the specs the stage
# touched. {paths} is filled by CodeGantry from the stage diff.
scoped_test_command: "docker compose run --rm test bundle exec rspec {paths}"

full_suite_on_approval: true

executor:
  # Must match a model id the endpoint serves — `curl <api_base>/models` is the
  # list. The executor calls the responses endpoint, which is also what makes
  # the top reasoning effort reachable; chat/completions refuses it.
  model: "<model-id-from-/v1/models>"
  # The endpoint address lives in the environment, not in this file. A hostname
  # is an infrastructure fact rather than a project decision, and this file is
  # tracked and hashed. `api_base` remains available for a literal.
  api_base_env: "CODE_GANTRY_EXECUTOR_API_BASE"
  # Omit for an endpoint that serves without auth. Set one only for an
  # authenticating gateway in front of it.
  api_key_env: "OPENAI_API_KEY"
  reasoning_effort: "high"               # omitted from the request when unset

planner:
  provider: "anthropic"
  model: "<model-id>"
  api_key_env: "ANTHROPIC_API_KEY"

reviewer:
  provider: "openai"
  model: "<model-id>"
  api_key_env: "OPENAI_API_KEY"

limits:
  # see Budgets
```

API keys are read from environment variables named here. Never put a key in the
YAML. `.gitignore` a `*.local.yaml` pattern so configs with sensitive paths can
be kept out of history.

Note there is no `stages:` list. Stages are derived by the planner at runtime.

### Config reload

The config may be edited and reloaded mid-run, subject to three rules:

1. **Reload only at stage boundaries** — in `advance`, never mid-stage.
2. **Re-run `validate` and re-check the approval hash on reload.** If either
   fails, keep the old config and log the refusal. An unapproved edit cannot
   enter a running session.
3. **Record `config_hash` in every stage's result.** Without it, a mid-run
   config change makes the report uninterpretable — there would be no way to
   know which rules a given stage ran under.

## Logging and reporting

`run.log` carries the timeline of node transitions and decisions. Per
stage-revision-attempt directories carry executor, verify, review, and planner
artifacts.

**`report.md` is the artifact the operator actually reads.** Structure each
stage record like a pull request, since that is the mental model:

- Stage id, kind, revision count
- Base sha and the resulting squash commit sha
- What the stage was asked to do
- Reviewer issues raised across all revisions
- Verify layer failures, and which layer
- Flake re-runs, distinguished by gate
- Wall-clock and suite runtime
- Planner interventions and their reasoning
- Reviewer and planner token usage, cached and uncached

That makes the report a list of PR-shaped, individually verified commits —
which is what the operator wants when deciding what to deploy, and what makes
the whole thing convertible to real PRs later if the without-GitHub choice
stops paying off.

Notify on completion, gating, and escalation. Unattended operation's
characteristic failure is a run that quietly stopped hours ago.

## Safety properties

These are the guarantees, and each is enforced somewhere specific rather than
by convention. Where a guarantee could be stated as a rule *or* made
unrepresentable, it is made unrepresentable — a rule is a thing a future edit
can forget.

- It refuses to start unless the config hash matches an approved one, and
  refuses to start on a dirty target repo.
- `base_sha` is recorded at run start and appears in the report, so the whole
  project branch can be reset with one command.
- It commits only to the project branch and its children, never modifies
  `base_ref` or any other branch, and **no method exists that could push.** The
  outer merge is the operator's action, deliberately.
- **It never runs a shell command originating from model output.** Every command
  it executes is declared by the operator in an approved config. This lives in
  the planner's output schema rather than in a check: there is no field for a
  command, so there is nothing to filter — and the allowlist filters the
  response anyway, because two enforcements of the property that matters most
  is the right number.
- The denylist applies regardless of operator approval.
- `command_timeout_seconds` bounds every declared command, not only the
  executor subprocess.
- `full_test_lock` names a host lock the full suite runs under, whichever
  run or project starts it. The suite takes every core, so two at once buy
  nothing; the wait is recorded apart from the suite's own duration and the
  log names the holder.
- `gc.auto=0` for the run's duration, so the reflog can recover a discarded
  attempt.
- Branch identity is asserted as a verify layer on every stage, and a failure
  goes straight to a human: a containment breach does not negotiate.

## What the layering is built on

The dependency order is still visible in the code and worth knowing, because it
explains why some things are easy to test and others are not.

Everything that touches the outside world goes through **one declared-command
runner** — setup, tests, checks, preconditions, context commands, and script
stages alike — and the layered `verify` sequence is built on top of it. That is
why `verify` is exercised end to end in the test suite without a model
anywhere: its inputs are a git repository and a subprocess, both real in the
tests. **Git operations** sit at the same level, and `init` and `approve` above
them, which is why those are testable against a fixture repo with nothing
stubbed.

The three model clients sit above that, and the graph above them. Only the
three model calls are stubbed in the suite; the graph, the checkpointer, the
merges and the subprocess runner are all real.

One number is computed rather than configured: `default_max_steps` comes from
the stage and retry budgets, because this graph loops far more than any
plausible fixed ceiling and a run must not stop on arithmetic it did not
choose. Exhausting it routes through `escalate` like every other stop, so an
operator reads it in the run's own vocabulary — which is the whole reason the
loop is ours. Under the framework it surfaced as an opaque error, and that was
named here as the one failure mode this tool must not have while being the one
thing that layer would not let us fix.

What the provider's SDK actually accepts is established by calling it, not by
trusting this document. Two facts here were only ever produced by a live call —
that tools must be declared `strict` for structured output to auto-parse, and
that a reasoning model's tool call must be echoed back with the reasoning item
it declares as required. Neither is visible to a stub.
