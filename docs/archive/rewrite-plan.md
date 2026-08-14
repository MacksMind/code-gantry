# Replacing the subprocess editor and LangGraph; making verify a trust boundary

> **Closed record.** Every step below landed, and the last open item — this
> project's config living in the orchestrator repo rather than the repository it
> describes — closed with it. Kept for the measurements, which are readings with
> instruments behind them and would otherwise have to be taken again; the design
> reasoning that outlived the migration has been restated in `architecture.md`
> and `CLAUDE.md`, where it is checkable against code that exists.
>
> The executor was a subprocess editor driven over a CLI. It is an in-process
> client now, and the tool it replaced is named here only where the record would
> not otherwise make sense.

## Where this stands

All ten steps are done. Each entry
below names the commit that did it, because a status line in a document is a
claim and a sha is checkable — the same reason a figure here has to name the
artifact it came from.

| step | state | commit |
|---|---|---|
| 0 · extract `openaiclient.py` | done | `b700010` |
| 1 · `gates.py`, one test-path selection | done | `c8e54c0` |
| 2 · `edittools.py` | done | `a85de00` |
| 3 · `executortools.py` | done | `0b4e23e` |
| 4 · `executorclient.py` | done | `0b4e23e` |
| 5 · the loop behind a provider switch | done | `408a12f` |
| 6 · flip the default, take a live run | done | measured in `CLAUDE.md` |
| 7 · delete the subprocess editor | done | `3959471`, `79fcd7b` |
| 8 · replace LangGraph with `driver.py` | done | `0104392`, this commit |
| 9 · simplify `nodes.execute` | done, absorbed | `3959471`, `8d7d0a5` |
| 10 · up to five stages per derivation | done | `40f133a`, `774ed52`, then `c7c5d9a`, `aaa1129`, `8e1d800` |

**Step 10 is done and measured.** One run, 17 derivations, 32 stages:

| | derivations | stages | seconds/stage |
|---|---|---|---|
| single | 9 | 9 | 524 |
| batched | 8 | 23 | 228 |

**57% of derivation time saved per stage**, with 0 stages dropped from any
batch. Over the full run to date — 19 derivations, 43 stages — the distribution
is the reassuring part:

| stages per derivation | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| derivations | 5 | 7 | 5 | 1 | 1 |

**74% of derivations batched, 88% of stages**, clustered at 2–3 with a thin
tail, and the cap of five reached exactly once — so it is not currently
shaping the distribution and raising it would buy nothing. Not bimodal: the
planner is sizing to the work rather than declining or filling.

And position in the batch does not predict trouble: stages drawn first hit a
gate or the reviewer 2 times in 19 (11%), stages drawn second or later 3 times
in 24 (12%). That is the bet the design rests on — a stage drawn four ahead of
its turn, against a tree three siblings will modify before it runs, fails at
the same rate as one drawn immediately before execution. Five events is a small
denominator and the work here is homogeneous, which is the case batching is
for; what it rules out is the failure the original broad conflict rule existed
to prevent, which would have shown up as a visible position penalty. Batch sizes were 2, 2, 2, 2, 3, 3, 4 and 5 — sized to the work rather
than filling the cap, which was the failure the feature had to be watched for.
The 5-stage derivation cost 882s against a 524s mean for a single one: asking
for five cost 1.7× and returned 5×, so the planner did not survey as though it
needed five.

Three things had to change before any of it worked, and each is worth more than
the throughput:

- **The prompt, not just the contract.** `additional_stages` shipped as an
  optional field described as best left empty, with the cap reachable only in
  `config.py` and a silent trim. Two derivations under a cap of five returned
  one stage each. Same shape as `observations`, empty 278 times out of 278.
- **The conflict rule was far too broad.** It forbade any shared file. Reads are
  live and self-correcting; `instruction` is required to state an end state, so
  writes are too; batched stages run in order and are each reviewed on their own
  diff, so they may stack. Only a quoted line range cannot re-derive itself.
- **And then that rule went too.** `edit_files` is a permission rather than a
  record, so predicting from it fired over a superset of what happened.
  `stale_excerpts` compares the blob the planner read against the blob at the
  stage's start — a measurement, at the moment it matters, that also catches a
  human edit, a `checks` rewrite or a resume onto an advanced branch.

Not observed in production: a revision with stages still queued behind it. Both
redrawn stages happened to be last in their batch, so the queue-preservation
path has never run live. What did hold is the premise under it —
`catalog-explicit-routes` sat queued for 49 minutes while three siblings landed
ahead of it and its spec was still valid when its turn came.

**Step 9 was absorbed into step 7 rather than done on its own.** Both removals
it named — the attach block and the `is_clean()` dance — went with the editor, and
the second was replaced by a fact rather than deleted: `diff_names()` against
the stage's start sha, because the loop commits before every gate, where the
old inference was drawing a conclusion from a subprocess that could be killed
mid-write. Kept as its own row because the plan promised a bisectable
separation and did not deliver one; a reader looking for step 9's commit should
find step 7's.

**Step 7 is done.** The last of it was worse than residue: the executor block
`orchestrator init` drafted was entirely of that era and two of its keys were on
the retired list, so a freshly drafted config could not load. The linter moved
to `stage_defaults.checks` where it now runs, its timeout setting is gone
(it lived on `Limits`, which the executor-only retirement guard could not
reach; that guard has since been removed entirely, along with the last key it
named), and `scripts/smoke.py` is the one
remaining stub-on-`PATH`, which is a test harness rather than shipped
behaviour.

**Step 8 needed a fresh run and still does for the cutover itself** — the old
`state.db` is LangGraph msgpack and `load_state` cannot read it, so a run
started before this cannot be resumed after it. The work survives regardless:
it is squash-merged onto the project branch, and `status.md`, the progress log
and `stage-costs.md` are per-project rather than per-run.

What the framework actually supplied, once routing turned out to be ours all
along, was four things: the loop, a state merge, a checkpointer and a step
ceiling. Two were load-bearing. The schema filter is a *feature* — `state.py`
says it depends on undeclared keys being dropped — and resume is explicit now,
where LangGraph resumed from a pending task and did not always consult
`resume_entry_point`. The ceiling changed behaviour deliberately: it escalates
rather than raising, which this document had named as the one failure mode the
tool must not have and the framework would not let us fix.

This file moved here from a session's plan directory on 2026-08-07. A plan with
open steps that lives outside the repository is the same failure this project
records about compaction prompts and agent memory: it carries one hop and then
ages out, where a tracked file does not.

## After the ten steps

The config moved into the repository it describes, which is a change of
audience rather than of content and pulled several things with it.

| change | commit |
| --- | --- |
| every command takes a config path; the slug is deleted | `efa3335` |
| config identified by its git sha; `approval.py` deleted | `c9ac97d` |
| `work_dir` named in config, defaulting under the plan directory | `992dbcf` |
| the linter's diff fed back to the executor, attributed | `12d970d` |
| `base_ref` may move forward; asked at startup, not after the work | `e8090d1` |
| `init` drafts a config fit to commit into a shared repo | `5eb7606` |

Not done: this project's config itself still lives in the orchestrator repo, so
`config_rel_path` is `None` and preflight warns rather than pinning a sha. The
move needs two commits in the target repo — the config, and `.code_gantry/` in
a `.gitignore` beside the plan — after which `problem_starting` becomes fatal
and an uncommitted config edit refuses to start a run.

## Context

The orchestrator drives an LLM executor over a target repo. The executor is
a pair-programming tool, run as a subprocess and being driven
headless, and the mismatch is now measured rather than suspected:

- **17% of invocations (11 of 65)** on the current run were hit by the editor's
  file-mention scan, which attaches any path-shaped string it finds in the
  message *or in its own reply* — and, per its own log line, *a reply that
  names a file loses that reply's edits*. There is no flag. We cannot stop the
  model naming a path in its own prose, so this is unfixable from outside.
- Usage accounting is blind: its cache fields read Anthropic's and
  DeepSeek's but never OpenAI's `prompt_tokens_details.cached_tokens`, and its
  "received" count excludes reasoning tokens. Recorded executor spend was
  $3.30 against $5.54 actually billed.
- Read-only file order comes from iterating a `set` (`base_coder.py:392`), so
  identical `--read` arguments produce a different byte order every process —
  measured, three orders in four runs. The cache prefix breaks between attempts.

None of this is about money. Executor spend is $3.30 against $199 of planner
spend; editor time is ~3,800s against 10,100s of test suite. **The case is that
we don't control the failure surface, and the failures are the expensive thing.**

Doing it now, during the Rails 4.2→5.2 migration, is deliberate: the next
project won't offer comparable work to judge it against.

LangGraph comes out at the same time because it turns out to be nearly free to
remove — two import lines in one file, three call sites in `cli.py`. Routing is
already ours (`nodes` set `next_hop`; `_router` is a 10-line table lookup).
`recursion_limit()` exists *only* to defeat LangGraph's 25-super-step default.
No interrupts, reducers, streaming, `Send`, or `update_state`. Pause is already
our own filesystem flag.

**Decisions taken:** the executor commits its own work, before tests. It runs
lint (`rubocop -A`-style) inside its own loop. Clear removal of the editor — no
dual-path beyond the transition; back out via git if unworkable.

## What verify becomes

Not a test runner — a **trust boundary**. It keeps only what the executor must
not be allowed to answer about itself:

| stays in verify | why |
|---|---|
| scope (`edit_files` + `_is_plan_document`) | adversarial: an executor that can iterate against it routes around it |
| progress (diff digest unchanged) | same |
| branch identity | structural safety |
| setup | environment; routes to a human |
| the five moved layers, **only when the facts have moved** | see below |

The split is not invented — it is what the existing route table already says.
Layers routing to `Route.EXECUTOR` (patterns, residue, tests, checks,
new_tests) are "here is what's wrong, fix it" and move into the loop. Layers
routing to `PLANNER`/`HUMAN` are "you went outside your remit" and stay out.

**Verify runs the tests exactly when nothing else has tested this tree.**

Two earlier drafts of this were wrong and the corrections are the design. The
first had verify re-run the moved layers unconditionally, as independent
confirmation. That is duplication: a deterministic set on an unchanged tree
gives the same answer twice. We only ever re-ran in verify because we could not
make the editor run the set we wanted — the re-run was compensating for not
controlling the executor, and controlling the executor is the point of this
change. It is also exactly why `_resolve_declared` and `_auto_test_command`
diverged.

The second draft made the re-run conditional on a recorded (command, HEAD sha)
pair. Better, but on the agent path that condition can never fire, so it is
dead weight dressed as a check:

- the gate's path set is a **subset** of the loop's — the gate adds test files
  from the diff, and the executor can only touch `edit_files`, whose tests the
  loop already unions in;
- nothing writes between the loop's last test run and verify, because `checks`
  now run *inside* the loop and commit there, so HEAD does not move either.

What is left are the two paths with **no loop at all**, and they are the real
reason the capability survives:

- **`script` stages** — `run_script_stage` runs an operator command and never
  enters a model loop. Something must test the result.
- **resume-into-verify** — `resume_entry_point` routes straight to `verify`
  when a human fixed something by hand mid-stage.

So `GateResult` carries the command run and the HEAD sha, and verify's rule is
one branch: no green record for this HEAD from a command matching what it would
run → run it. On the agent path it does nothing at all.

Flake adjudication does **not** move to the executor. It belongs wherever tests
are run, which is `gates.run_tests`, so both callers inherit it.

## Design

### New modules

- **`edittools.py`** — write-side counterpart to `repotools.py`. Same posture:
  no model, refuses with `ToolError`, records what it did. `Edit`,
  `apply_edits`, `normalise`, `FileEditor` with `edit` / `create_file` /
  `delete_file`.
- **`executortools.py`** — schemas + dispatch, mirroring `plannertools.py`.
  Imports `plannertools.READ_TOOLS` **by reference, not copy**.
- **`gates.py`** — the shared execute/verify layer. `check_patterns`,
  `check_residue`, `check_new_tests`, `run_checks`, `run_tests`,
  `resolve_test_command(..., for_loop: bool)`. `GateResult` carries the
  command run and the HEAD sha it ran against, so verify can decide whether
  re-running it would ask a question already answered.
- **`executorclient.py`** — OpenAI Responses client, structured like
  `reviewer.py`.
- **`openaiclient.py`** — `TokenUsage`, `_extract_usage`, `_merge_usage`,
  `_tool_request`, `_transport_errors`, extracted from `reviewer.py`.
- **`driver.py`** — the hand-rolled graph driver replacing LangGraph.

### The edit tool

Structured tool calls (`path`, list of `old_string`/`new_string`), not diff
parsing. `old_string` must occur exactly once unless `replace_all`; zero
occurrences and multiple occurrences are **different refusals**, as
`RepoReader._require_readable` already distinguishes "not there" from "there
and you may not have it". A batch applies to an in-memory buffer and writes
once — any failure leaves the file byte-identical, because a partially applied
batch leaves the model reasoning against a file neither party has seen.

What replaces its fuzzy matching: the editor compensated for a *lossy channel*
— a text format reproduced byte-exactly inside free-form prose. A tool call
removes the channel (provider-escaped JSON, `strict` schema), returns the
refusal *inside the same turn* naming the file and occurrence count, and the
model has a read tool to close the loop itself. Its model learned from an
exit code one reflection later. This claim is **bounded and unproven for this
project** until step 6 measures `edit_refusals` per cycle — which is why that
field exists.

**Scope is enforced at the tool boundary**, not only at the gate:
`_resolve_writable` applies the inside-repo rule (`RepoReader._resolve`, which
catches symlinks pointing out) plus `matches_any(rel, stage.edit_files)` plus
`_is_plan_document`. The gate stays as defence in depth — `checks` rewrite
files the tool never saw, and the gate is the half that routes to the planner.

### The tool set

Read tools (`read_file`, `list_files`, `search`, `git_show`, `git_diff`,
optional `semantic_search`) reused from `plannertools`, plus the three edit
tools. **Lint, test and commit are loop steps, not tools** — handing the model
the scheduling is what the budget gets spent on, and a model that must *ask*
for a test result can decline to ask and declare itself done. There is no tool
that runs a command; that invariant gets its own test.

One boundary changes: `RepoReader` is tracked-only, and the executor must read
files it just created. Relax tracked-only for paths matching `stage.edit_files`
and nothing else — the reason tracked-only exists (`.env`, `cdk.context.json`
never reach a cloud API) survives intact.

### The loop

```
per cycle:
  model edits until it stops calling tools (bounded by max_model_turns)
  lint   (may rewrite files — feed back which ones)
  commit (before tests, per the squash-merge invariant)
  patterns -> residue -> new_tests -> tests   (cheap first)
  green -> return; else append feedback, next cycle
```

Terminates on: a green cycle; budget exhausted (commit what's there); two
consecutive identical diff digests; or the model stopping having changed
nothing. **Budget exhaustion still commits** — the loop is in-process and
cooperative, so "the executor committed before verify" becomes a guarantee
rather than the `git.is_clean()` inference at `nodes.py:781`.

`ok=False` narrows to *the executor itself broke* (transport, auth, unhandled
exception). Every substantive verdict comes from verify, as before.

`max_test_retries` is unchanged in mechanism but shifts in meaning — an attempt
is now a whole inner loop. Set `max_cycles` low (3) and **tune one at a time**.

### `ExecutionResult`

Add `usage` (real provider counts, priced by the existing
`pricing.price_usage`), `cycles`, `model_turns`, `edits_applied`,
`edit_refusals`, `commits`, `in_loop_failures`. `cost_usd` becomes
`float | None` — `None` for unpriced, because a zero has meant "not priced" as
often as "free". Delete `cache_tokens`, `results`, `unapplied_edit`,
`attached_files`.

### LangGraph removal

`driver.py`: a `while` loop over `NODES[hop]`, `_router` moved inline, one JSON
row per step in sqlite, and a `load_state` replacing `graph.get_state`. Three
behaviours must be reproduced deliberately:

1. **Partial-update merging** — trivial, no reducers exist.
2. **Schema key filtering** — LangGraph silently drops keys not in `RunState`,
   and `state.py:180-186` documents relying on it. A plain `dict.update` stops
   dropping them. Keep the filter explicit.
3. **Crash-mid-node resume** — LangGraph resumes from a pending task, not
   necessarily `START`, so `resume_entry_point` isn't always consulted. Our
   loop makes this explicit, which is more predictable but is a behaviour
   change to pin with tests.

Existing `state.db` files are not readable by a new writer. The live run must
finish, or be re-run, before the cutover.

## Ordering

Each step keeps the suite green.

0. **Extract `openaiclient.py`** from `reviewer.py`. Pure refactor.
1. **`gates.py`; unify test-path selection.** Merge `verify._resolve_declared`
   with `executor._auto_test_command` into one `resolve_test_command(...,
   for_loop=)`. These two diverged correctly and each divergence has an
   incident behind it — the fix is making them impossible to change
   independently. **The old path still runs.** Largest risk-free win; lands first.
2. **`edittools.py`** — pure module, full test file, nothing calls it.
3. **`executortools.py`** — schemas + dispatch, nothing calls it.
4. **`executorclient.py`** — Responses client, scripted-fake tested.
5. **The loop, behind `ExecutorConfig.provider`, either path.** Both
   paths live, both suites green. A live run happens here, before any deletion.
6. **Flip the default; migrate project configs; take a live run.** Measure
   attempts/stage, cycles/attempt, edit refusals/cycle, and **the cache read
   rate on the Responses path** — the figure `CLAUDE.md` currently records as
   unmeasured.
7. **Delete the subprocess editor.** `shield_path_mentions`, `attached_by_mention`,
   the argv builder, the three log-scrapers, `CommandRunner.run_argv`,
   `NO_BROWSER`, `GIT_CONFIG_OVERRIDES`, the CLI-flag check,
   `scripts/smoke.py`'s stub-on-PATH (replaced by an injected scripted
   client), and the dead `ExecutorConfig` fields. Removing a config field
   fails `_Strict` load on existing project configs — the error must name the
   key and its replacement, not dump a pydantic schema.
8. **Replace LangGraph with `driver.py`.**
9. **Simplify `nodes.execute`** — remove the attach block (`nodes.py:706-747`)
   and the `is_clean()` dance (`784-797`). Kept separate from step 7 so a
   bisect can tell a deletion from a routing change.

10. **Let one derivation produce up to five stages.** After step 8, because it
    adds a queue to `RunState` and there is no sense writing that into a
    checkpointer about to be replaced.

    The planner is the expensive participant — $199 of a run against $3.30 for
    the executor — and a derivation is 5 to 7 minutes of a ~13.5 minute stage.
    On homogeneous work it re-answers a question it has already answered:
    across 16 stages of the current run, `backfill-batch-1` through `batch-13`
    differ only in which three to eight spec files they name, and each
    re-surveys the same helper and the same route table to find that out.

    **The risk is that a stage spec is a prediction**, which this project has
    already paid for twice: an unresolvable `read_excerpts` range now fails the
    stage back to the planner, costing exactly the derivation being saved, and
    "an authored edit stops being satisfiable once part of it is already true
    on the branch" deadlocked a stage into two redraws. Stage 3 of 5 is drawn
    against a tree stages 1 and 2 have not touched yet.

    So the batch is constrained rather than trusted, in the house style — make
    the mistake unexpressible: **no stage's `edit_files` may intersect any
    other batched stage's `edit_files`, `read_files`, or `read_excerpts`
    paths.** Staleness is then impossible by construction. Truncate to the
    longest safe prefix rather than rejecting the batch, so heterogeneous work
    degrades to one stage and today's behaviour. The measured fit is good: only
    4 of 120 stage pairs in this run share an `edit_files` entry — though note
    that nearly every batch quotes `spec/support/migrated_controller_inventory.rb`,
    which is exactly the shared file the constraint exists to catch.

    **Rework is never batched, and the queue survives it.** A stage that fails
    routes to the planner with its child branch intact, and a branch belongs to
    one stage — so the planner revises *that* stage and cannot answer with a
    batch. `next_stages` applies to the `next_stage` verdict only; a `revise`
    returns one stage, as today.

    The queued stages behind it are still good work and are kept. What can
    change is the revised stage: a revision widening `edit_files` to fix a
    scope violation may now name a file a queued stage reads, and the guarantee
    that made the batch safe would quietly stop holding. So **the conflict
    check is re-run rather than the queue discarded** — the survivors are kept
    and only what the revision actually collides with is dropped.

    This needs no new mechanism. Putting the revised stage at the head of the
    list and the queue behind it is the same question `orthogonal_stages`
    already answers: the revised stage is first so it is always kept, the queue
    was already pairwise orthogonal, and the only drops that can appear are the
    ones the revision caused. An earlier draft of this plan discarded the whole
    queue on any route to `plan`, which was sound and wasteful — the invariant
    only requires re-checking, not forgetting.

    The other transitions: a stage that lands keeps the queue, because nothing
    was rethought. A stage retried by the executor keeps it, because the
    planner has not touched the spec. A pause keeps and persists it — the queue
    lives in `RunState` and the pause is checked after the squash, so a run
    stops between queued stages rather than mid-batch.

    Measure "stages dropped per revision" regardless. If a revision routinely
    invalidates the queue behind it, the batch was never independent and the
    cap should come down.

    **This is not parallelism, and the distinction is the whole design.**
    Stages still run strictly one at a time. Many of them need a tree that does
    not move underneath them, which is exactly why `verify` compares against
    `stage_start_sha` and why the executor commits before it tests. What the
    batch amortises is the *derivation*, not the execution. The orthogonality
    rule is not there to permit concurrency — it is there so that stage 3's
    spec, written before stages 1 and 2 ran, is still true when its turn comes.

    **Discarding the queue discards no work.** A stage that fails routes to
    `plan` with its child branch intact, and the planner still chooses
    `extend` (keep it) or `restart` (cut fresh) as it does today — the branch
    is only deleted when it asks for that. So what a discarded queue throws
    away is predictions about stages that never started, never a diff.

    **The planner owns the order.** The batch is filtered, never reordered: a
    conflicting stage is dropped and the earlier of the pair wins, because it
    is the one already accepted. `orthogonal_stages` filters rather than
    truncates, so a collision at position four still leaves one, two, three and
    five — truncating there would discard later stages for a collision they had
    nothing to do with. Each candidate is judged against what was *kept*, never
    against what was dropped.

    What the check cannot see is a stage whose instruction refers to another in
    prose — "extend the helper the previous stage adds" — because that
    dependency touches no file. Batched stages must stand alone, and the schema
    has to say so, since any one of them may be the one dropped.

    **Everything that goes wrong has to be cued back to the planner, and it is
    an array.** Today `_failure_block` presents one failure about one stage. A
    batch produces several facts at once: this one landed, that one failed with
    this diagnosis, the three after it were discarded from the queue, and
    another was dropped at validation for sharing a file. Without all of it the
    planner re-derives blind and can produce the same conflicting batch again —
    a loop that costs a derivation each time round. The rework block becomes a
    list of what happened to the batch, not a single failure.

    **A batch can run out of output before it runs out of stages.** Measured
    over 465 stage specs: instruction length is median 5,139 characters, p90
    9,766, max 15,167. Five at p90 is ~48,800 characters — about 12,200 tokens
    of output before any reasoning, against `max_tokens=32,000` which thinking
    also comes out of. And truncation here is not a clean stop: the SDK parses
    structured output before returning, so a response cut mid-JSON raises
    inside the call and surfaces as a pydantic dump, which `_call_failure`
    exists to translate. So bound the batch by size as well as by count, and
    raise the ceiling — it is a ceiling rather than an allocation, and the cost
    of headroom is nothing against a discarded derivation.

    **Watch for the ceiling becoming a target.** The likeliest way this fails
    is not a bad stage but a slower derivation: asked for up to five, the
    planner surveys as though it needs five, and one call that costs five
    calls' worth of thinking has saved nothing. The read cap is 100 now, so
    there is ample room to overthink into, and the risk grew when that was
    raised.

    The saving being claimed is amortising **one survey**, so the measurement
    has to be **derivation seconds per stage produced**, not per derivation.
    The baseline is on the record: single-stage derivations on this project ran
    171, 264, 268, 311, 339 and 435 seconds, so roughly 5 minutes a stage. A
    five-stage batch at 25 minutes is exactly break-even and a worse artifact,
    because four of those stages were drawn before the first one ran. The run
    log prints `read N thing(s) over Ns` beside every derivation, which is the
    instrument — it exists because a 19-minute derivation had nothing to look
    at, and it turns out to be what tells us whether this feature works.

    Frame the field as a ceiling that is normally 1. This is the *inverse* of
    the `observations` lesson: there, an optional field with a conditional
    trigger came back empty 278 times out of 278, and the fix was to make it
    required and always answerable. Here declining is the good outcome, so the
    conditional trigger is the right shape and the schema should make one stage
    the unremarkable answer — a number in the prose invites filling it.

    Also measure: batch size actually produced, stages discarded per failure,
    and the failure rate of position-2+ stages against position-1. If
    position-2+ fails materially more, the lost feedback is real and the cap
    comes down. Do not report the saving from token cost alone; a batch that
    fails at position 2 costs a derivation *and* an attempt.

## Verification

- `uv run pytest` green at every step (1,262 tests today).
- **New**: `test_edittools.py` (uniqueness refusals distinct, failed batch
  leaves file byte-identical, out-of-scope and symlink writes refused,
  created-file readable before commit while `.env` is not),
  `test_executortools.py` (strict schemas; read-tool descriptions byte-identical
  to the planner's; **no tool runs a command**; no description names a
  framework or file extension), `test_gates.py` (`for_loop` divergence asserted
  in one test), `test_executor_loop.py` (**the commit precedes the test run**,
  asserted by a stub test command recording `git.head_sha()`),
  `test_driver.py` (resume merge, schema filtering, crash-mid-node).
- **End to end in `test_integration.py`**, per the standing rule that a value
  crossing a schema boundary gets a journey test: cost and context tokens from
  scripted usage → `ExecutionResult` → state → `report.md`/`stage-costs.md`;
  `cycles` reaching an artifact; a model attempting an out-of-scope write with
  the tool refusing *and* the scope gate never firing; and `execute` then
  `verify` on a real fixture proving the commit is on the stage branch — driven
  through the nodes, not `with_stage`, which is laxer than the node.
- **Live**: step 5 runs against a real project with both providers available; step 6
  flips the default and measures. `executor_context_tokens` changes instrument
  at the cutover — write one line into `stage-costs.md` marking it, because a
  series that silently changes instrument is worse than a gap.

## Not in scope

Qdrant/semantic search and the litellm price map stay.

`gitops`' `ignore_line_endings` and `strip_added_trailing_whitespace` were
listed here to be revisited *after* step 7, because they change what the
reviewer sees. Both were, and both stay.

`strip_added_trailing_whitespace` never depended on the editor: it exists because
`git diff --cached --check` is the usual form of a pre-commit hook and a linter
that dispatches on a file's detected language does nothing for ERB, YAML or
most non-source files. It leaves carriage returns alone, so it has no bearing
on the question below.

`ignore_line_endings` was the one worth measuring, and the measurement said do
nothing. Ten files were silently converted CRLF→LF over 77 stages, and the
argument for acting had been that this made the repository mixed where it was
uniform. It was never uniform — 552 of 3,184 tracked text files carried CRLF at
the base sha, 17%, and 542 do now, unchanged since. A burst, not a rate.
`.gitattributes` would not have prevented it either: `text=auto` normalises
only content git treats as new, so files already stored with CRLF keep it until
an explicit `git add --renormalize`, which would convert all 542 in one commit —
strictly more churn than doing nothing, in a repository mid-migration.
