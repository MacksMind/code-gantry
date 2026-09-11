# Working on this codebase

[README.md](README.md) is how to use CodeGantry.
[docs/architecture.md](docs/architecture.md) is the design authority.
[docs/future-work.md](docs/future-work.md) holds open decisions.
[docs/archive/rewrite-plan.md](docs/archive/rewrite-plan.md) is closed.

This file is for whoever is *changing* the code. Every rule cost a run. The
incidents are in this file's git history and in the docstrings; read those
before changing behaviour they describe.

**This file is the only durable channel.** Compaction carries a finding one hop
and agent memory is keyed on this path. When a long session ends, diff what was
learned against this file.

Run the tests with `uv run pytest -n auto`; run the whole suite. It must pass
both with the run's credentials loaded and with none — a test about what we pass
to a child must clear the variable it asks about and never borrow a real
credential's name. An unscoped `os.environ` write makes the result depend on
which xdist worker ran it.

## Direction and standing decisions

This section is the project's memory. It is here rather than in any
session's notes because the notes live on one machine and this file lives in
git. Update it when a decision changes; date the entries.

**Working agreements with the operator.** Do the named action first and
propose extras after. `code_gantry.yaml` in a target repository takes field
changes only, with the rationale in the commit message. Commit and push this
repository's own changes without asking. Comments and docstrings state the
rule, not the incident; the incidents are in git history. Watch for code in
the wrong layer and for duplication that comes from not having thought
through pluggability. No role is ever tied to a wire. Tests pin generated
facts and which prompt file is present, never a sentence of prose.

**Vocabulary (decided 2026-09-11).** A *bay* is a checkout plus its
containers on a host, named `<repo>-bayN` and made by the target's
`bin/mk-bay`, on offset ports. The plain-named checkout on a host is the
*primary repo copy*, the person's, never a bay, and the only one that may
bring up the default ports. A *run* occupies a bay. A *key* names a plan
node. The *ledger* is the per-host SQLite record holding the plan tree, key
states, findings and drawn stages; on a host with several bays it is one file
outside every checkout, named by `ledger.path`, and every bay reads and
writes it. An *origin* is the writer of a ledger's events, one per host,
named explicitly rather than defaulted once there is more than one host.

**Topology.** Hosts never address each other. Code moves through the git
remote: every landing is a squash, `pull --rebase`, a re-run of the suite
only if the pull brought commits, and a fast-forward push of the project
branch alone. The ledger moves through the same remote: each origin's
events are an append-only JSON-lines file on `refs/code_gantry/ledger/<origin>`,
built with plumbing and never touching a work tree, pushed only by its
owner, fetched by everyone at run start, at the plan node, at precheck and at
advance, and on the daemon's clock. A central database exists only ever as a
replica. Scopes are per host; within a host, bays share the drawn-stage
queue under the planner lock, so no scope is needed between them.

**Landing across hosts is optimistic, never leased (decided 2026-09-11).**
Rebase, test, push; a push the remote refuses after a green suite is a
retry — pull again, test again, push again — bounded by a retry count, with
a conflict or a red combined tree stopping for a person. No lease ref on the
remote: a lease can be held by a dead process and blocks a host whose suite
would have passed. Within a host the suite lock serialises landings. The
intended reordering, not yet built: squash the stage into one candidate
commit on its own branch, rebase that onto the pulled tip, run the full
suite once there, then fast-forward and push — one suite per landing, and
the candidate stays a branch a rework can amend. The same landing serves a
person's pull request, with the failure returned to whoever authored the
candidate.

**The BEAM mesh is control, never state (decided 2026-09-11).** Daemons may
form a distributed-Erlang mesh for status, start and stop, liveness and log
streaming; either side may dial, since the cookie is the only credential,
and a laptop still joins as a hidden node because it comes and goes; a cloud
host later joins the same way. Everything that must be right — plan, claims,
drawn stages, greens, landings — moves through the git remote and is
correct with no daemon anywhere. A landed change under `daemon/` is
hot-loaded by each daemon at its own safe point, with `code_change`
carrying GenServer state; Python workers restart from new code at a stage
boundary. That is how the self-improvement loop reaches both languages.

**Test bed (2026-09).** The Spark (this host, a DGX with 121 GB unified
memory, hostname `spark`) and the operator's MacBook. The two reach each
other over Tailscale in both directions; what is one-way is credentials:
the Spark holds no private keys, so SSH is Mac-to-Spark only, and anything
the Spark initiates toward the Mac must need no key — a BEAM connection
under the shared cookie qualifies, an SSH session does not. Multi-host is
proven between those two before any cloud host. On the Spark: `/home/you/projects/app/acme_app` is
the primary copy, still running as a bay for now, and `acme_app-bay2`
is the second bay, ports offset by 100. The technical-debt project's ledger
is `~/.local/share/code-gantry/acme_app/technical-debt.db`, the old
spelling until the stop. Credentials are per repository, one file at the
target's root, `<repo>/.code_gantry/env`, ignored there and named by every
project's config as `../../.code_gantry/env` (landed as add9a0a in the
target on 2026-09-11); a repository is one client. What a run writes stays
per project under `docs/<project>/.code_gantry/`. Never host-level. The Rails 5
project still keeps its ledger under its work dir. Remote landing is on for
technical-debt, off for Rails 5.

**Sequence.** (1) the ledger holds the plan — landed; (2) bays behind one
ledger, the suite and planner locks, drawn stages, leases — landed; (3) the
per-host daemon: an Elixir Mix application under `daemon/`, started with
`bin/daemon start`, reading `~/.config/code_gantry/host.exs`, supervising
`code-gantry run` per bay, making a missing bay with `bin/mk-bay`, syncing
the ledger refs on a clock, answering `bin/daemon status`; toolchain pinned
with `.tool-versions` and `mise` as a floor (the Spark's packaged Elixir is
1.14 on OTP 24, the Mac runs 1.20) — landed, tested against a fake CLI, not
yet run for real on the Spark. Its first Mac start (2026-09-11) could not
spawn `bin/mk-bay`; after the spawn fixes were pulled it made
`acme_app-bay1`, and the first run there stopped in preflight because
a host that has never run the project holds no plan until a sync brings the
other origins' events — so `run` now syncs before preflight under
`remote_landing`, and the three argv shapes the daemon composes are parsed
through the real CLI in `tests/test_daemon_argv.py`, which found the sync
and resume shapes wrong before the Mac did. The Mac's next run failed
preflight on `base_ref 'upgrade/rails-5' exists`: `mk-bay` clones from
the primary copy, so a bay holds only the branches that copy had locally.
Fixed in preflight (c41a0b4), not `mk-bay`: a base ref that does not
resolve here and a project branch missing here are fetched from origin
into local branches — the project branch only under `remote_landing`,
which is what makes origin its home — each with its own check line, since
on a fresh host `ensure_project_branch` would otherwise cut the project
branch afresh from the base. The Mac's first suite then failed 938 of
6,168 by one mechanism, every Selenium session removed at one instant for
inactivity: the grid's 300 s default, outlasted by a worker's non-browser
stretch while a second run shared the machine. The bay's compose now sets
`SE_NODE_SESSION_TIMEOUT: 1800` (target commit c72a331a1, ahead of origin
until the Mac lands). `bin/daemon retry <bay>` (6879534) relaunches a
stopped bay with no restart, and the clock sync reads a bay's config,
never the primary's (c18d7dc); (4) ledger sync over the git refs
and preflight deduplication through `suite.green` — landed; (5) remote bays and scopes at placement; (6) an observer role
that writes findings only; (7) CodeGantry improving itself from its own run
artifacts. The daemon adds uptime, capacity, reload at the pause seam and a
view, never a correctness property: a run started by hand with no daemon
must behave the same.

**Build order and open decisions (kept current; last 2026-09-11).** The
queue of what to build next, in order, and what waits on the operator. A
session picks up from here; a decision the operator makes moves from the
second list into the first or is struck.

Build next, in order:
1. The landing reorder: one candidate commit on the stage branch, rebase
   onto the pulled tip, one full suite there, fast-forward and push; retries
   bounded by a config field; the verify layer's full suite moves into
   publication. Then a rebase conflict as a rework rather than an escalation.
2. The MacBook as a host: pull, `mise install`, host file, `bin/daemon
   start`; the first cross-host sync and the first Mac landing.
3. The daemon's control plane: distributed-Erlang mesh with the Mac as a
   hidden node; one status view across hosts; start and stop from anywhere;
   liveness for leases; hot reload of `daemon/` at a safe point. The first
   verb is in (2026-09-11): the daemon starts as `--sname code_gantry_daemon`
   with a cookie under its state dir, writes its node name beside the
   status file, and `bin/daemon retry` is an `--rpc-eval` into `Control`;
   status, stop and the mesh go through that same door. Bay names are
   registered locally, never in `:global`, which would kill one of two
   `bay1`s on the first connection between daemons. The daemon
   also fetches and fast-forwards its own code-gantry checkout at that
   point and starts the next worker from it, so one landing reaches every
   host without anyone pulling. Decide here what Python moves into Elixir:
   move a piece when its reason to change is the daemon's (locks, leases,
   the take-before-derive queue, the sync clock, status); leave what is
   bound to a provider, a repository or a gate (model clients and dialects,
   gates, edit tools, git, the ledger's derivations). Prompts are files read
   per call, so they need no restart, but they reach another host only by
   that same fetch: a landed prompt change is live everywhere one interval
   after it lands, and out of sync until then.
4. The decision queue reachable from Telegram: every finding with
   `needs: human`, every escalated or paused run, every candidate waiting on
   a person, delivered as a message with the reply that answers it — answer,
   release, drop, resume, pause — writing the same ledger events and CLI
   calls a person at a terminal would. No human-in-the-loop Markdown
   document; the ledger's findings are the queue and Telegram is a client of
   it, served by the daemon.
5. Pull requests as candidates: intake, review comments as the rework
   channel, keys on a pull request closing plan items, branch protection
   routing merges through the orchestrator.
6. The observer role that writes findings only, and CodeGantry improving
   itself from its own run artifacts, both languages. Decided 2026-09-11:
   the operator is a fourth role, `claude -p` spawned by the daemon on
   the events a person used to watch for — a bay exiting 1 or 2, a
   finding with `needs: human`, a red preflight suite — with a prompt
   file, a bounded remit (read and measure anything; findings on the
   ledger; `retry`; a candidate branch, never the project branch) and a
   transcript per invocation. Its work on CodeGantry is accounted
   separately from the target project's: its records, ledger and costs
   live under CodeGantry's own project, and the target's run holds only
   the finding that pointed at it. The Claude Code CLI is installed and
   authenticated on the Spark and the Mac. A landing to code-gantry is
   picked up by every host from the remote — fast-forward, verify on the
   host's toolchain, hot-load Elixir, pause and resume every bay when
   Python changed — and a nudge over the mesh, sent after the push, only
   shortens the wait; `bin/daemon reload` loads local code into one
   daemon for a test and never nudges. A laptop catches up on wake,
   detected by the VM's time-offset monitor.

Waiting on the operator:
- Whether the executor comparison, Flash-Next against Luna, is worth a
  measured run once the Spark is idle, or whether Luna stands.
- Whether to enable `remote_landing` for the Rails 5 project and move its
  ledger beside technical-debt's.
- When the Spark's runs stop. Decided 2026-09-11: the Spark takes the
  Mac's shape — the primary copy handed back to its person, `bay1` made
  at offset 200 (bay2 holds 100 and the primary the defaults), both under
  the daemon. In order at the stop: `code-gantry pause` on both runs, a
  last hand-sync so the hyphen ref's events reach the `code_gantry` one,
  the ledger path and field moved, the lock symlink removed, the hyphen ref
  deleted, this branch pulled and `mise install` on the Spark, the host
  file written, `bin/daemon start`. Only the moment is still open; the
  Spark's bays then run current code, which is what makes the ledger
  exchange two-way for the first time.
- How a green tree is tracked. `suite.green` carries a tree fact and a host
  fact in one row and `proven_green` ANDs them, so a Spark green never lets
  the Mac skip a suite; and preflight asks about HEAD before `precheck`
  pulls, so it proves a tree the run then discards. Proposed: the tree
  fact readable from any origin, a separate host fact that a red suite
  proves as well as a green one, and a pull before preflight under
  `remote_landing`. Measured 2026-09-11: zero `suite.green` events in 20
  landings, because every Spark bay predates c3e5bf8 — the reader has
  never had a record to read.
- The technical-debt config's `ledger.path` still spells
  `~/.local/share/code-gantry/…`; every other host path is `code_gantry`
  now (decided 2026-09-11: one spelling for directories, the command name
  stays `code-gantry`). Move the file and the field at the stop. Until no
  run started on the old code is live, `/tmp/code_gantry-<uid>` is a symlink
  to the old lock directory so old and new runs share one set of locks;
  remove the link then. The same runs push their ledger to the hyphenated
  `refs/code-gantry/ledger/host-b`; the `code_gantry` ref that current
  code fetches advances only when synced by hand from the Spark (`uv run
  code-gantry ledger sync --config <target config>` with
  `CODE_GANTRY_ORIGIN=host-b`) until those runs stop; delete the old
  ref on GitHub then.

- A `checks` entry in the technical-debt config, backed by a script in the
  target's `bin/`, failing on a quoted path after an HTTP verb in an added
  line under `spec/requests/`: three reviewer reworks on 2026-09-11 were
  that convention, and `forbidden_patterns` exempts test files by design.

**Measured facts worth keeping.** Through OpenRouter, Fable 5.1 on the
Messages wire drops the schema and refuses tools; on Responses it carries both
and never caches; on chat completions it carries schema, strict tools, effort
and a one-hour cache, and it serves the full planner prompt. The local
Flash-Next executor took about four times Luna's median wall clock per
attempt on one stage and serves one request at a time, which makes it the
wrong executor for more than one bay; the technical-debt project runs Luna.
Two bays ran together on 2026-09-11 for three hours: nine landings on
`technical-debt` through origin, one derivation feeding both bays, the
suite lock and the planner lock holding on first contention, a bay taking
a drawn stage after waiting 530 s for the other's derivation, and one
bounce on a stage both held that exposed and fixed three defects (head
taken at derivation, a bounce dropping only its own record, a bounce never
becoming a revision). The reviewer reworked three stages for the same
route-helper convention; the fix is a mechanical check, listed above.
The Mac's full suite is 13 minutes on a 14-cpu, 16 GB Docker VM.
`preflight-suite.log` under the work dir is a failed preflight's raw
capture and the only place its `Failures:` blocks survive: the run log
clips the middle of a 5 MB report and the tail is container teardown. At
21:14 on 2026-09-11 the Spark's live hyphen ref was 22 events past the
hand-synced `code_gantry` one.

**Operating this host.** The Claude Code harness stops its own background
tasks on a "low memory" reading that page cache alone can trigger; it killed
runs and suites here while the machine had 100 GB available. Start a run
with `nohup` in a terminal, never as a harness background task, and do not
run this repository's suite while a bay is in preflight. `pkill -f` and
`pgrep -f` match the shell that runs them; anchor patterns. `pytest -q`
under xdist prints no summary; read the exit code. Tests that drive the
plan node must set `CODE_GANTRY_LOCK_DIR`, or they litter the host's lock
directory.

**Operating the Mac.** Idle sleep on AC is one minute: `caffeinate -i -s
-w <daemon pid>` after every `bin/daemon start`, or a suite stops when the
lid does. A bay that stopped for a person is answered with `bin/daemon
retry <bay>`; `stop` and `start` end every bay's run on the host. The
daemon reads only the bays; the primary copy is the person's.

## Invariants

**The planner may never author an executable field.** Enforced twice: the
schema has no field for a command, and `PLANNER_WRITABLE_FIELDS` filters the
response. A field the planner may not set should be impossible for it to return.

**And the planner may not author code.** Authored code travels inside a
declarative field — "replace this block with exactly this block" is still the
planner writing the diff. Quoting goes through `read_excerpts`, a path and range
read at `stage_start_sha`, so **a reference can only point at code that already
exists**. `validate_stage` rejects fenced blocks in `instruction`; inline
backticks are allowed, because a rule against naming an identifier gets routed
around. An excerpt is the only code the executor gets, so an unresolvable range
fails the stage.

**`completed` is append-only.** It is the cacheable prefix of both paid prompts;
rewriting an entry multiplies the cost of every later call, invisibly.

**Prompt ordering is the caching strategy, not presentation.** Static leads,
churn trails, breakpoints between. Anthropic extends the longest matching cached
prefix, so an append-only region *before* a breakpoint gets cheaper over time;
GPT-5.6 caches only at an explicit breakpoint. One document can need opposite
placement in the two prompts. Measure after changing any of this — documented
provider behaviour has been wrong twice.

**The block is the cache unit, not the prefix.** One appended byte rewrites the
whole marked block, so put churn *after* the mark, not last inside it. A
breakpoint after content that changes every call costs more than none.

**The pipeline pushes only the configured project branch, fast-forward, and
only under `remote_landing`.** `Git.push` has no force flag and `precheck` and
`advance` are its only callers. The one other push is `Git.push_ref`, which
refuses any name outside `refs/code_gantry/` — the ledger refs, built with
plumbing and never a branch — so it cannot become a second way to move code. After a squash the bay pulls with rebase,
re-runs the full suite only if the pull brought commits — two landings each
verified on their own tree were never verified together — and pushes; a
refused push pulls again; a conflict or a red combined tree escalates with the
landing complete locally, and the next precheck pulls again. The whole
publication, pull to push, runs under the host's suite lock, re-entered by
the suite it runs, so a bay landing small stages quickly cannot keep moving
origin under a neighbour's re-test until its retries run out — which is what
two bays did on 2026-09-11 before the section existed. Squashing is what
makes "every commit on the project branch is green" and "the executor commits
before it tests" both true.

**A stage lands completely or not at all.** `squash_merge` restores where the
branch was and `advance` unwinds the note. Anything added to that sequence
inherits the obligation; removal is the same rule backwards. The rollback is
silent — the original failure is the diagnosis and must reach the caller.

**Whatever the planner draws from, the executor may not edit**, or the planner
can put its own inputs in scope and have the executor amend the instructions it
will be judged against. `_is_plan_document` at the gate and `protected_paths`
on the editor are the guard, and every input added to the planner's prompt
belongs in them — the config, the repository's agent-facing documents, and the
work dir that holds the ledger. The plan itself is not in the tree, which is
what makes it unreachable.

**A gate must be able to reach what decides its verdict.** A gate that cannot
reach its evidence produces verdicts indistinguishable from judgement. Record
what a gate *looked at*, not only what it decided. Preflight is the sharpest
case, being the only gate that can wave a red repository through; it keeps the
bytes **and the parse beside them**.

**A record of the work is written after the work.** Anything written before it
is a prediction, whatever tense it uses. What a stage *did* is recorded by the
reviewer, the only participant that has seen the diff.

**But an event is recorded as it happens.** A *claim* must wait for the stage; a
transcription has nothing to predict. `executor-conversation.jsonl` is appended
as it grows, `sent-prompt.md` is written before the first call, and
`executor-loop.json` is one write at the end because totals are the only thing
not true until the attempt is over.

**Make recording a property of the operation that changes the thing.** The
transcript is a `list` subclass mirroring each append to disk, not a callback
threaded through four call sites — a record that must be *remembered* at each
site goes quietly missing.

## Prompts and model-facing strings

- **Project knowledge belongs in config, never in code**, including model-facing
  strings. A tool description naming one framework's paths ships that shape to
  every project. Worth a test that fails on the names.
- **Config holds the path, not the copy.** A transcribed document drifts, and
  the copy is the one the pipeline reads.
- **A prompt describing a capability is generated from the thing that grants
  it.** `PLANNER_SYSTEM_PROMPT`'s capability paragraph is built from
  `cfg.project_tools`.
- **A prompt must not ask a role for a channel it does not own.** The executor
  has no commit tool: `executorloop` composes `[stage-id] executor cycle N` and
  every one is squashed away, while the landing message is `_commit_message`
  built from the reviewer's account of the diff.
- **A phantom capability costs more than a phantom constraint.** A missing
  capability produces a stage the gates catch; a constraint that does not exist
  makes work read as blocked; a capability that does not exist spends a full
  attempt per rework until the retry ceiling.
- **A record belongs where it can *fail*.** Ask for an assertion that breaks
  when the behaviour changes, not a comment — a comment is a claim nothing
  checks, and requiring one makes its wording a reject criterion.
- **When you fix one prompt, grep the other roles' for paraphrases.** A model
  can see its own tool schema, so a reason it can observe to be false discredits
  the instruction attached to it.
- **Sweep every model-facing string after removing a component.** The prompts
  are where a deleted thing goes on living.
- **A prompt sentence outlives the fact it was written about.** A docstring is
  checked by the code beneath it; a prompt string by nothing. Nor is a docstring
  about a *neighbouring* component held true by anything — `runtime.plan` and
  `plandoc` both described a behaviour neither module had.
- **A prompt must not tell the executor it may not change the file it is there
  to change**, and must not name a field its reader cannot see. Both were found
  by a human reading a prompt, which is the only thing that finds this class.
- **Render the string the model receives; do not re-read what you typed.** The
  defect lives in the gap between source and assembled string — call
  `tool_schema` and print it.
- **A true sentence can carry a false implicature, and the model acts on it.**
  Phrase a claim about the run, not the file: "nothing in this run changes them"
  survives a human editing from another session.
- **An optional field is answered with nothing.** `observations` came back empty
  278 times out of 278, across two revisions written to encourage it: an
  optional field with a conditional trigger can always be declined in good
  conscience. If output is wanted every time, make it required and ask something
  true every time — "what does this
  change do", not "did you notice anything else".
- **Ask what else already carries the content, not whether it is good.** A
  channel restating the same fact is a fact with no durable home; the tells are
  an entry arriving call after call, and field descriptions that describe
  something other than the entries.
- **A markdown link means two things.** Transitive plan resolution costs the
  property that reading the root tells you the whole payload; depth-2 links are
  cross-references or documents another channel already supplies.
- **An undescribed argument is where the refusals are.** Sweep for tool
  properties carrying no `description` rather than checking case by case; on
  `git_show` and `git_diff` that sweep found every argument-shaped refusal.
- **A required-and-nullable field is answered with the word.** `as_strict_tool`
  rewrites an optional property as `["string","null"]` *and required*, so `null`
  is the only spelling for nothing and models send the string `"null"` or invent
  `WORKTREE`. Describe what absent *means* and denylist the observed spellings,
  scoped to arguments where that cannot eat a real value.
- **A default that answers where a refusal would have asked is the wrong
  trade.** Ask what the call it rescues will now answer.
- **Two tools whose main argument is "some text" owe each other a sentence.**
  `search` takes a regular expression and `edit` takes literal bytes.
- **Disclose every ceiling.** A budget the model cannot see is one it can only
  discover by spending; the read-budget sentences are generated from
  `RepoReader.budget` per role.
- **A model asks for one tool at a time unless told otherwise.** Nothing
  suppresses batching. Measure the *shipped* string — a paraphrase is a
  different string.
- **A description that names one instance is obeyed on that instance.** The
  `apply_patch` schema said "No `*** Begin Patch` envelope"; the model omitted
  the opener and still closed with `*** End Patch`. Name the class.

## Records, artifacts and ledgers

- **A summary artifact carries the number it is about**, and the whole response:
  an absent field and a zero are indistinguishable to a reader. The writer must
  be the model, not a hand-written list of keys — `executor-loop.json` walks
  `dataclasses.fields`.
- **An optional keyword does not reach call sites that predate it.**
- **A sentinel is a value in the wrong field.** Give it its own field and leave
  the other null rather than faked.
- **A ledger that records refusals and not successes lists only failures.** The
  error path is the one people instrument. `tools.log`, `tool_counts` and
  `refusal_counts` are built from the reader's ledgers, so **anything that
  answers a tool call without dispatching it owes `record_refusal` an entry.**
- **A ledger that records the container cannot answer about the item.** A file
  and a seed cannot say whether one example failed many times or many once.
- **A watermark into a concatenation indexes a list whose middle moves.**
  `tools.log` sliced `reader.calls + editor.calls` at one index. Two
  views of one dataset disagreeing means the derivation is wrong, not the data.
- **A record published from a run is a claim in every later prompt.** Before
  restoring a pending note on resume, ask whether what it asserts still stands.
- **Publish a finding when it is found, not when the work lands.** Planner notes
  open findings at derivation, true whether or not the stage succeeds; reviewer
  observations stay on the landing gate, because an abandoned diff does not
  exist.
- **State is derived from events, never stored.** The ledger's tree, key states
  and findings are rebuilt from one append-only table on read, so no status
  column can disagree with the history that produced it — the `gate_history`
  idiom applied to the whole record. Every event names the origin that wrote it
  and its sequence within that origin, so a later exchange between hosts is a
  fetch, not a merge.
- **A fold is a rendering policy, not a document edit.** Landings and answered
  findings move from the projection into the node bodies when the projection
  outgrows `ledger.fold_ratio` of the plan text; the run does it at the
  derivation seam with no model and no commit.
- **The landing commit is the durable copy.** Its trailers carry the keys, the
  resolved findings, which model held which role, the config and the base; the
  gitignored ledger can be rebuilt from history.
- **Classify where the damage is, not where the tidying is.** Ask whether the
  thing being sorted is inert while it waits.
- **Prefer the fact to the label.** `git blame` answers from facts that cannot
  be wrong; a declaration is a claim made before the work.
- **A permission is not a record.** `edit_files` is what a stage *may* touch, so
  a check reading it fires over a superset of what happened. Comparing blobs
  answers from the tree.
- **Convert a format while the file is small.** A parser needing one
  compatibility branch is one field from needing two.
- **Before backfilling an append-only file, ask whether the raw material is
  still on disk.**
- **A figure in a document must name the artifact it came from.** A reading with
  no instrument behind it should be deleted rather than approximated.
- **Do not explain the present with a component that is absent.** Before
  removing something, grep for what *cites* it.
- **A field on the type is not a field in the artifact.** `provider_cost_usd`
  is declared on `PlannerUsage` and read from the response, and appears in no
  `planner.json` on either route. Grep the artifact, not the dataclass.
- **Two types with the same field names in different order are one
  transposition away.** `TokenUsage` and `PlannerUsage` swap `cached_tokens`
  and `completion_tokens`, and both are built positionally.

## Measurement and diagnosis

- **Read artifacts; do not regex them.** `\s` is not valid in POSIX ERE, so
  `git grep -E` silently matches nothing; `File\.exists?` matches the
  already-converted `File.exist?` because `?` quantifies the `s`. `str.index()`
  on a repeated heading is the same bet with different syntax.
- **A repeated heading returns the wrong instance.** `parallel_rspec` prints a
  full report per worker and again as an aggregate; take the last, or anchor
  deliberately.
- **Cut code with a parser, not a pattern.** `ast.parse` accepts a `return`
  outside a function and `compile` does not, so a bad deletion can pass a syntax
  check and fail at import. `ast` gives exact `lineno`/`end_lineno`.
- **Check what a command actually returns.** `git show <sha>:<path>` on a
  symlink returns the link's target. A pipeline exits with its last stage's
  status, so `pytest | tail -2 && git commit` commits a red suite. `grep -c`
  counts lines, `grep -o | uniq -c` counts occurrences. macOS has no `timeout`,
  which returns 127 and reads as a suite result. Git does not report empty
  directories, so a `git status --porcelain` assertion can pass on the absence
  of a *file* rather than the absence of a change.
- **And what it inherits.** cwd, env and stdin are inputs. ripgrep searches
  **stdin** when stdin is not a terminal, so a search worked at a shell and
  returned nothing under `subprocess.run` — invisible to a unit test and a shell
  probe alike.
- **A pathspec is not a glob, and an empty answer is evidence.** A `path_glob`
  handed to `git grep` as a bare pathspec made a large share of empty answers
  false. A tool that
  returns a wrong answer gets caught; one that returns *nothing* gets believed.
- **A flag that filters can undo the boundary you thought bounded it.** `-g`
  filters ripgrep's walk rather than working within it, so a model-supplied glob
  overrides `.gitignore`. Output is capped, so leaked artifacts push real hits
  out and **a leak surfaces as repetition**.
- **Check the instrument before the world.** A zero is a reading about the
  instrument until proven otherwise; a value *exactly* zero across every sample
  is a reader that cannot see the field; a ratio that is exactly constant is the
  instrument. Re-ask any question recorded as unmeasurable after changing the
  layer that could not answer it.
- **The instrument that diagnoses a bug can be the wrong one for confirming the
  fix.** Counting non-ASCII bytes proved a single-quoted `é` was ASCII; the
  same count on the double-quoted fix still reads zero, because the escape is
  interpreted at parse time. Same command, opposite validity, because the
  question moved a layer.
- **Do not propose a fix for a mechanism you have not established.** The tell is
  a fix arriving before a measurement. A remedy on an unestablished mechanism is
  *confirming*: it gets adopted while the real cause keeps firing.
- **A model's account of why it stopped is evidence about what it tried, not
  about what is possible.**
- **A pattern that matches in a log has not told you which row it is in.** An
  ordinal question needs the sequence read in order with its neighbours, not a
  grep whose hit count implies a position.
- **The diagnosis can be in a place nothing reads.** Every tool result a model
  gets can be truthful and useless.
- **Measure the artifact in the state your claim is about**, and never against a
  tree a live run owns — clone to scratch and check out the recorded sha.
- **Measure the string the model received, not the artifact it came from.**
  `full-suite.log` is the merge gate's raw capture, not the clipped feedback an
  executor is handed; that population is `executor-conversation.jsonl`.
- **A small n answers no question.** A rate over a handful of mixed samples is a
  reading about the sample. Report a measured non-result as a non-result;
  capability and effect are different claims and only one usually has evidence.
- **A cache-timing fault answers "not reproduced" once and "reproduced" the next
  time.** Say "did not reproduce on one attempt".
- **Hedging is what protecting a hypothesis looks like from outside.** Check
  whether the reason not to run an experiment was measured or invented.
- **One item from a ranked list is not a finding; the list is.** A usage rate is
  not a verdict on a tool's value — the win is in what loses.
- **The index's worst contaminant is the pipeline's own output.** The exclusion
  list lives in the *target* repository's indexer, and a bad query stays bad.
- **Improving a tool's answers cannot make anything reach for it more often.**
  There is no memory across runs: the index decides what a call is *worth*, the
  description decides how many calls *happen*.
- **Know the size of what you are about to walk.**
- **Send it to the endpoint.** A stub answers what you taught it and cannot fail
  the way the thing it stands in for fails. Assemble a probe from the code
  production calls, or spend the afternoon chasing a difference you introduced.
- **An artifact that renders part of a payload cannot reconstruct it.**
  `planner-prompt.md` renders `messages` only — no tools, system block or schema.
- **A falsification harness is code and can be broken.** Print the diff, and
  check the mutation failed a test *for the reason you expect* — one failing for
  another reason is telling you about the fixture.
- **An aggregate cannot separate the cases you care about.** Record the series
  at the item.
- **Cache writes measure what was newly cached, not how big the job was.**
- **A total from a tool loop is not a context figure.** The loop re-sends the
  whole conversation per turn: nonsense as capacity, exact as a bill.
  `accumulate_usage` maxes any key naming a peak.
- **A real measurement attached to the wrong decision is harder to argue with
  than a wrong one.** Ask not "is this number right" but "what would have to be
  true for it to change the answer" — and take the reading rather than
  estimating, because an estimate standing in for an available measurement is
  how a nine-minute cost gets argued about as an hour.
- **A confound can invert a measurement.** Output per stage fell while total
  cost rose, entirely because the progress log had not been folded in between.
- **Verify a mechanism *can* fire before recommending someone enable it.**
- **A rule right about every case and silent about the sequence lets a
  deteriorating thing deteriorate at full speed.** Flake excusal has no memory,
  so the sixth sighting reads like the first. The tell is a record with no
  reader.
- **A capability list is not the request.** OpenRouter advertises `tools` on
  every `claude-fable-5.1` endpoint and `provider.require_parameters` excludes
  all four anyway — a 404 that reads as routing and is metadata. Reading the
  parameter list said the swap was safe; four probes said which parameter.
  Send the request.

## Budgets, ceilings and counters

- **A line is not a unit of size** — false of a minified bundle, a vendored
  asset, a `structure.sql` or a one-row fixture. `max_chars_per_call` is the
  second dimension. Ask what unit a budget is denominated in.
- **A second dimension whose default ignores the first is a tightening.**
  `max_total_chars` shipped with a default derived from the *class* line budget
  while every real config raises that, so it bound first and silently. Derive a
  companion limit from its partner's *configured* value.
- **A ceiling that binds first is a policy nobody chose.** If a limit is hit in
  normal operation it is not a backstop, and the symptom is indirect — a turn
  cap reads as "the attempt produced no changes".
- **A distribution measured under a cap cannot choose the next cap.** Set a
  ceiling from the tail of the *uncapped* case, at the gap where legitimate use
  stops rather than where pathological use begins. Re-measure before moving it;
  once the guard ships, the distribution under it can no longer answer.
- **A counter added underneath another is not reset by the code that resets the
  first.** `_chars_used` arrived after `_lines_used` and accumulated for the
  process's life, so past the ceiling every planner call was refused on its
  first read; the cliff is the tell. Clear spent state by replacing one object, not by zeroing fields, and
  whatever *shares* the mutable structure must reach through the owner.
- **A budget whose consumption is never printed cannot be seen to leak**, and
  one nothing records cannot be seen to be near. The test is the *series*.
- **A limit can depend on a setting in another file, enforced by neither.** The
  installed SDK refuses a non-streaming request implying a long generation; it
  never fires only because the planner always passes an explicit timeout.
- **And the diff a revision prompt carries has no ceiling.**
  `max_chars_per_call` bounds what the planner *reads*.
- **Collapse before you truncate.** `clip_for_model` wraps `truncate_middle` so
  the order is not decided twice: a progress reporter puts its noise first and its findings after,
  and a truncated uncollapsed run spends the budget on dots. It does the heavy
  lifting — a lint failure collapses to a few hundred usable characters with no
  truncation at all.
- **A runner's report is an anchored region, not a suffix, and the tail of its
  output is the *other stream*.** `CommandResult.output` appends stderr after
  stdout and the test commands go through `docker compose exec`, so container
  chatter can be the literal last thing in the buffer. The report fits a
  4,000-character budget several times over; it is simply at neither end. **A
  weighting cannot reach it; an anchor can** — `^Failures:$` forward, where
  `clip_report_for_model`'s tail weighting does not. The runners
  differ: `bin/parallel_rspec` buries the report under tens of thousands of
  characters of teardown, a scoped `bin/rspec` trails about 900.
- **A test that forbids a name is not the same as a test that pins a decision.**
  Ban the second application, not the word: assert the constant is spent exactly
  once. Worth a deliberate sweep — parse every module and list names defined in
  more than one.

## Gates, guards and routing

- **Assert state, not change.** `forbidden_patterns` reads the diff's added
  lines and catches what must not be *introduced*; `must_not_remain` reads file
  contents and catches what must not *survive*. Near-identical in prose,
  opposite in a diff — a sweep needs the second, because sites the executor
  missed never appear as added lines. Progress notes state totals, never deltas,
  because "make this change" is unsatisfiable once the change is already true.
- **A guard belongs where its question can first be answered**, and must ask the
  right question. "Has `base_ref` moved" is a startup fact; asked inside `verify`
  it is answered after a planner call and an attempt have been paid for. It also
  asked equality where the question was ancestry — what is worth stopping for is
  a *rewrite*.
- **A category drawn around the mechanism excludes the case drawn around the
  meaning.** A transport retry categorised as *the request never arrived* let a
  529 end a run; `request_replan`'s `unsatisfiable` listed three mechanisms, so
  an executor whose case was *no tool of mine can do this* concluded the tool
  did not apply. Ask not "is this set complete" but "what is this a set *of*".
- **A guard can be unreachable for the shape its failure takes.** A
  `stop_reason == "max_tokens"` check never fired, because the SDK parses
  structured output *before* returning. Ask which layer raises first.
- **A guard that counts unproductive calls is blind to a productive loop.** An
  executor with `edit` and a test runner but no way to print will build a REPL:
  write a probe, run one example, read the value out of a deliberate `raise`.
  Every call changes something, so `FRUITLESS_*` never fires. It is
  self-limiting — a raising probe cannot land green — and it is what a model
  does when it can evaluate but not observe.
- **A ban can be a bug wearing a design constraint's clothes.** Check that what
  a test forbids is the thing its reason is about.
- **A check that fixes must not sit behind one that can fail for unrelated
  reasons.** `run_all` breaks at the first failure, which stops being right once
  a later entry also *repairs*. Chain a fixer into the same entry with `&&`.
- **And an environment failure must not look like a defect.** `_layer_checks`
  routes an ordinary non-zero back to the executor as "a required check failed",
  because only `failed.signal` gets `Route.HUMAN`. 128+N is too narrow a
  definition of a signal.
- **The set preflight proves must be the set the loop runs**, and in full —
  `_environment_checks` once ran the commands and never the `checks` entries, and
  `run_preflight` ran both suites before reaching an environment check;
  break-on-first-failure leaves later entries unproven. **Proving a command in
  the container says nothing about the host.** A new executable field in config
  is a new thing for preflight to prove.
- **A check may write, and only the child branch should carry it.** Left
  uncommitted it is swept up when the stage lands, and the next stage's precheck
  refuses to cut a branch over changes it cannot attribute.
  `checks_commit_changes` puts it on the child branch.
- **A tool that edits after the model stops leaves its context stale.** Stage
  the model's work before the checks run so the rewrite is the unstaged
  remainder, and attribute it in as many words — handed an unattributed diff, a
  model reads it as its own mistake and tries again.
- **Ask the question that expires first.** Most gates read the tree and the tree
  is still there; a pre-commit hook reads the *index*, and the commit consumes
  it. Asked before the commit a refusal is a cycle the model fixes in session.
- **Run the thing rather than modelling it.** `git hook run pre-commit`
  (git >= 2.36) invokes the hook exactly as a commit would. Two facts recall
  gets wrong: it exits **1** when no hook exists, the same status as a refusal,
  so presence comes from an executable at
  `git rev-parse --git-path hooks/pre-commit`; and the hook reads the **index**,
  so the gate stages first. A gate is a check, not a guarantee — `commit_refused`
  stays. Two things an operator cannot fix in config: `_gate_cycle` commits the
  model's raw work before `checks` runs, which puts autocorrecting entries
  downstream of the commit a hook refuses; and the hook can come from
  `core.hooksPath` set **globally**, so it is not the target repository's
  property at all.
- **A ceiling on the model's argument is not a ceiling on the pipeline's
  call.** `max_values` bounds one `rspec` call of the executor's; the gate's
  scoped run through the same `build_argv` was refused at twelve files. So
  `capped` is a required keyword — each caller says whose call it is — and
  **a gate that cannot be built escalates rather than raises**: the
  `ToolError` left `run_loop`, `execute` and `main` as a traceback with the
  checkpoint still saying `running`. `gate_unrunnable` on the loop,
  `Route.HUMAN` in `verify._layer_tests`.
- **A gate written for the first commit does not cover the second.** A stage
  branch carries several commits per cycle.
- **Attribute bytes by what ran between, not by whoever is nearest.** Find the
  commit that last held the file clean and enumerate what ran after it.
- **Hiding a tool's own churn from the gate hides it from everyone.** An
  exemption wants a counter, or a periodic look at what it has swallowed — and a
  finding produced that way arrives without a magnitude, so establish the cost
  before acting on it.
- **Separate what was found from what should happen next.** Before withholding
  approval over a consequence, *verify* the consequence; naming one is not
  establishing it.
- **A second sample scored more harshly than the first is not a second
  check.** `finalize` re-ran the full suite on a tree the last landing's review
  gate had already run it on — verified on run 20260904-120923, tip
  `05e65fc5ba44`, nothing between the two runs but a derivation — so it could
  only resample the suite's nondeterminism, and it did so without the
  `flake.adjudicate` path the review gate gives the same command. 22 stages
  landed green; one failure in 6,139 examples escalated the run, and `_clip`
  dropped the `Failure/Error:` block from the middle of its own diagnosis
  because finalize writes no `full-suite.log`. It now asserts the tip is the
  commit the last landing produced and the tree is clean: what a suite cannot
  answer, since a suite reads the tree and passes on the altered one.
- **Ask what a repeated check is a second sample *of*.** If the tree is
  identical the answer is "the runner", and that is a question about the
  runner, not the work.
- **A failure layer is declared in one place and routed in another.**
  `resume_entry_point` reads `failure_layer`, so an unregistered one is not a
  type error. `budget`, `commit` and `gates` were emitted and persisted while
  absent from `FailureLayer`; an AST test over `nodes.py`'s `_escalate` calls
  found all three at once, the same idiom that derives `EDGES`. **Declaring is
  not routing** — and none of the three needed routing, which is only knowable
  by reading `resume_entry_point` to the end rather than reading the two
  frozensets and stopping. `budget` is handled by name below them, and the
  fallback under those asks a sharper question than membership: work on the
  stage branch goes to `verify`, none goes to `precheck`. **A set membership
  answers unconditionally where a fallback can ask.**
- **Every gate says why.** A reason that reaches only the planner and the
  checkpoint reaches the two places a person does not look.
- **A guard and the reset it depends on are one decision written in two
  places.** Write the clearing in the same edit and name the node that owns the
  end of that unit.
- **A check that exists as a side effect of something else disappears when that
  thing moves.** Ask what each check is *made of*, not what it appears beside.
- **A rule is checked against new work; nothing re-reads what predates it.** Do
  a deliberate pass over existing code when a rule is added, and read this file
  when adding a field, not only when debugging one — the rule you already wrote
  gets rebuilt in the next feature, because the new field does not look like the
  old one — `additional_stages` shipped as an optional field right after the
  optional-field lesson.
- **"There is no shell" is a claim about the tool schema, and the pipeline runs
  shell scripts.** `setup_command`, the test commands and every `checks` entry
  are operator-declared argv naming scripts *in the repository being edited*, so
  a stage that may edit one has arbitrary execution by a slower route.
  `no_direct_edit` is the only thing standing there, and **the ban belongs on
  the files, not the directory**. Adding a tool adds a script: a declared command
  may not start with `sh`, so any tool whose body is more than one program
  becomes a file needing its own entry.
- **A capability withheld from the schema can be handed back through an
  argument.** Enumerating every spec file through a repeated `paths` argument is
  running the whole suite, spelled differently. Enforce the cap in `build_argv`,
  not only as `maxItems` — a schema constraint is a request, the dispatcher is
  where it becomes one. Ask what the permitted tools compose into.
- **One question having to union two config fields is the tell that the fields
  are one field.** A predicate that ORs over a config surface is describing that
  surface, not the world.
- **A replan lands nothing and leaves everything.** `request_replan` skips the
  gates, and the executor's edits stay committed on the stage branch and checked
  out in the tree, deliberately. Ask of any tool that hands work back what the
  attempt has already changed outside its own diff.

## State, resumes and branches

- **A resume is not a fresh process with the old state.** `resume_fields` is
  what a resume merges over the checkpoint. `step` counts from zero inside one
  `drive` call and the key is `(run_id, step)`, so a resumed session can
  overwrite the beginning of the previous one. Check that a resume starts from
  what it loaded, and that "latest" means last written.
- **And it re-enters at the node it died in, not at the top.**
  `cut_stage_branch` is only on the path through `precheck`, so "delete the
  stage branch and resume" buys a fresh cut on a fresh *run* and a stop on a
  resume.
- **Read `resume_entry_point`; do not read the checkpoint's `next_hop`,** which
  `resume_fields` clears. It comes from `failure_layer` — `PLANNING_FAILURES` to
  `plan`, `REPO_STATE_FAILURES` to `verify`, `paused_before` to the held stage —
  and otherwise from `stage_has_work`. `review` is in `REPO_STATE_FAILURES`.
- **A resume hands the planner the failure a human just fixed.**
  `opening_failure` survives into a resume entering at `plan`; the planner reads
  a deterministic harness error and blocks, then re-blocks on every later
  resume. A fresh run is the workaround.
- **The failure that opens a retry sequence is the diagnosis; the ones after it
  are consequences.** `opening_failure` claims the first write-once, on the
  retry branch too.
- **The plan is not pinned; the repository's own documents are.** The plan is
  rendered from the ledger on every derivation, so a fold or a human answer
  reaches the next call without a restart. `plan_sha` now pins only the
  conventions, operations and layout, which a resume still inherits — editing
  one of those means `run`, not `resume`.
- **A field removed from a model strands the run that persisted it.** `Stage` is
  `extra="forbid"`, so deleting a field raises on the next *resume*;
  `current_stage` filters to declared fields.
- **A test that pins where a value lives passes while the value is lost.**
  Deriving a stage returns `**base` then `**fresh_stage_fields()`, and the reset
  zeroed what `base` had just set. Assert the behaviour, not the location.
- **Look one line up from the field you are adding.** Beside an accumulating
  field sat an *assigned* one, so a one-line final attempt recorded its peak as
  the whole stage's.
- **"Nothing landed" is a claim about the project branch, not about the stage.**
  `stage_start_sha` outlives the branch, so re-cutting from a newer tip silently
  acquires whatever landed in between. Run
  `git log <project_branch>..<stage_branch>` first.
- **Approved work does not survive a redraw.** `fresh=True` deletes and
  recreates the branch. An `extend` revision keeps it, and is what makes a
  rework cheap: the branch holds the prior work and the feedback names one thing.
- **A value that was private when its file was private is published when the
  file moves.** When a file changes audience, re-read every field as though
  seeing it for the first time.
- **A value that fits is a value that fits *where it is*.** `prompt_cache_key`
  is capped at 64 characters, and blind truncation is the wrong fix — two
  projects under a long shared prefix truncate to the same key and silently
  share a cache. Both sites hash through one helper.
- **A relative path is a decision the launch command makes, and it appears in no
  config, log or artifact.** `PRICE_MAP_FILENAME` as a bare filename cached against the *process cwd*. A
  `.gitignore` line suppresses the symptom in the repository that noticed and
  leaves the mechanism running everywhere else.
- **Project a third-party copy rather than keeping it whole**, and put the memo
  in the loader where a third caller inherits it.
- **`pin_modules` protects the code, not the tree.** A live run finishes on the
  orchestrator code it started with, so editing this repository mid-run is safe.
  Editing the *target* repository's tracked config is not: it is swept into the
  executor's cycle commit and fails the scope gate as "the stage edited the plan
  it is being drawn from", and the blob sha then refuses the next resume.

## Providers, SDKs and the wire

- **Provider shapes come from the installed SDK, not from recall or docs pinned
  to another version.** Only a live call produced these: tools must be declared
  `strict` or structured output will not auto-parse, and a reasoning model's
  tool call must be echoed back with the reasoning item it declares as required.
- **Classify a provider failure by status code, not by exception class.** 529 is
  `OverloadedError` on Anthropic's SDK and `InternalServerError` on OpenAI's.
  `is_transient_status` is pinned against the installed SDKs; 429 is included,
  safe only because our wall clock bounds the wait.
- **Retry types belong to the SDK the call goes out on**, not the module you
  imported them from. It is a property of the dialect, and raises rather than
  defaulting to empty, because no retrying looks identical to nothing failing.
- **A retry that behaves correctly can still describe itself wrongly.** A log
  line is the interface a failure is diagnosed through.
- **A rejection of a *pointer to a cache* cannot be answered by sending it
  again.** An expired cache id is excluded from the spurious-400 budget and
  answered by resending the same context with nothing marked.
- **Moving one component onto a new axis leaves its neighbours on the old one.**
  Making the wire a property of the model moved the *client* and left
  `prompt_cache_key`, `input_text`, `prompt_cache_breakpoint` and
  `extract_usage` behind. The refactor reviews as complete because the thing it
  was about is complete. Ask what *else* touches the request.
- **A parameter no provider declares is not ignored; it excludes every
  provider.** Through a gateway with `provider.require_parameters`, an
  unsupported parameter answers **404 "No endpoints found that can handle the
  requested parameters"** rather than 400 — which reads as a routing problem and
  is a request problem.
- **The spelling follows the route, not the model family.** `output_config` is
  accepted direct and via the gateway to Claude but 404s via the gateway to
  Gemini; `extra_body.reasoning` is the reverse. `GET /api/v1/models` carries
  `supported_parameters` per model and is the cheapest way to ask.
- **A documented parameter can be accepted and ignored.** `tool_choice` survives
  the gateway while `disable_parallel_tool_use` is dropped in silence — worse
  than a rejection, because a request that reads as constrained and is not sends
  nobody looking.
- **Ask which direction a knob points before reaching for it.** A default that
  already allows the thing means the knob is for forbidding it.
- **Cold on the Messages wire means `input_tokens: 0`.** The prefix lands
  entirely in the cache fields on the turn that writes it, so read with OpenAI's
  extractor a cold turn records no prompt tokens at all.
- **A rule fixed in one role's type does not reach the role using the other
  type.** `PlannerUsage` grew `peak_prompt_tokens`; the reviewer's `TokenUsage`
  did not. Fix it where one reading is its own peak — `extract_usage`. Add the
  field last on a positionally-built dataclass, and grep for the private copy
  the shared type was modelled on.
- **Test the layer you are actually going to call.** Routing comes from the
  model string, pricing from the metadata file, each useless for the other's job.
- **A tool reads more than you hand it.** A third-party tool's behaviour is a
  property of its source; grep the source before theorising.
- **The model id follows the route, like the spelling.**
  `anthropic/claude-fable-5.1` through the gateway is `claude-fable-5-1`
  direct; the prefix is OpenRouter's routing id and the dots are its
  convention. `dialect_for` reads the same needle either way.
- **Anthropic reports cache writes in two buckets, and a sum cannot be
  decomposed later.** `cache_creation.ephemeral_5m_input_tokens` and
  `ephemeral_1h_input_tokens` arrive separately and
  `cache_creation_input_tokens` is their total; a 1h write is 2x base against
  5m's 1.25x. Pricing the total at the cheaper rate understated the planner by
  25% a derivation — $2.60 reported against $3.24 — starting the day the plan
  block's TTL began being read correctly, so **the report drifted exactly when
  the bill improved**. `cache_write_1h_tokens` is a *component* of
  `cache_write_tokens`, so every reader that does not care about rates is
  unchanged, and `price_usage`'s `writes_1h` is **required and keyword-only**
  because a caller holding the breakdown and forgetting it reproduces the
  defect in silence. A rate table with no `above_1hr` key cannot separate them
  and falls back to base: an unpriced distinction should cost the old
  arithmetic, not a guess in the expensive direction.
- **A test that pins the last field stops saying the rule when a field is
  added.** `TokenUsage` and `PlannerUsage` are built positionally, so the rule
  is *new fields append*; asserting `names[-1] == "provider_cost_usd"` merely
  described where the growing edge happened to be. Pin the prefix instead —
  which also writes down the transposition, since the two orders differ at
  positions 1 and 2.

## Tests

- **Test end to end wherever a value crosses a schema boundary.** Four defects
  have been values computed correctly and lost in transit — dropped by a schema
  that did not declare the key, zeroed by a reset, or omitted from the artifact
  meant to prove they existed. Every one passed its unit tests.
- **A CLI command with no test is untested however green the suite is.** A
  missing method inside a function body is a runtime `AttributeError`. Drive the
  real `click` entry point.
- **A test suite can be exercising the path you are about to delete.** A default
  that only tests rely on is a fork in the road with no sign on it.
- **A test helper that stands in for a node is laxer than the node.** A helper
  that cuts a branch the way `precheck` would skips `precheck`'s guards.
- **A fixture can make a whole file's tests laxer than production.** Ask what
  the fixture *omits*, then whether production could run with that omission. A
  fixture repo missing production's `.gitignore` puts `.code_gantry/` into `git
  status`, so a test asking "was the tree touched" answers a question about our
  own artifacts.
- **An earlier branch can eat every fixture.** Instrument which branch fired
  rather than inferring it from the answer.
- **A test that searches for a constant's value cannot find the code that names
  it.** The imported symbol evaluates to the value while the code spells the
  *identifier*. Run the equivalent search by hand once and make the two agree.
- **Two green tests can contradict each other if neither drives the seam between
  them.** `nodes.execute` returned `_escalate(...)` on a refused commit while
  `EDGES` never listed `escalate` from `execute`, and `driver._next` raises.
  Derive the table from the code — `EDGES` is checked by parsing
  `nodes.py` for every `next_hop` each node can return.
- **A value written in three places and read in none.** After adding a field, or
  deleting a component, grep for the *reader*.
- **Deleting a producer leaves its consumers guarded on a value nobody sets.**
  `context_tokens` and `cost_usd` came from a deleted scraper, so `advance`'s
  truthy guard was never true and `stage-costs.md` stopped being written. A
  guard written to suppress noise suppresses the whole channel just as quietly.
- **A capability can go missing between two correct changes.** When a payload
  changes shape, enumerate what the old shape carried; parts with no field of
  their own vanish with no error and no test.
- **A green suite cannot report the tests you deleted.** A file with no tests
  reports nothing. Compare `def test_` counts **per file against `HEAD`** after
  any edit that replaces a span, and prefer an exact anchor to a slice whose far
  end is implied.
- **A falsification test must be checked for whether it *can* fail.** One
  asserted a bracket example-id survives with no shell — but POSIX `sh` leaves
  an *unmatched* glob unchanged, so the fixture would have passed through a
  shell too. Perturbations that bite: a path containing a space, a glob with a
  matching file present. Ask what a green falsification test would look like if
  the guard were removed, then remove it.
- **A fixture reproducing an exclusion must be checked for whether it still
  excludes.** Force-adding ignored files makes them tracked, and the test then
  passes by making the leak legitimate.
- **A reader that writes destroys the evidence it was about to look for.**
  `load_state` ran `CREATE TABLE IF NOT EXISTS` before a read made "is our table missing" true in
  both cases. A check naming what a thing *is* survives contamination that a
  check naming what it is *not* does not.
- **An inner loop that skips the file under edit is worse than none.** Declaring
  something is not declaring the right thing.
- **The success path is the one that skips the tail.** `run_loop` returned the
  moment the gates came back clean, above where cost was computed, so it billed every first-time-clean attempt at zero, with green tests
  whose fixture left by a different exit. Prefer one exit; ask which the happy
  case takes — and remember a constructor is an exit.
- **A fixture can supply what production cannot reach.** Every planner-prompt
  test passed `SimpleNamespace(cache_ttl="1h")` while `cache_ttl` lives on
  `PlannerConfig` and the builder is handed the `ProjectConfig`. The known
  laxness is a fixture that *omits*; this is the inverse and it hides more.
  Drive the real config wherever a value crosses a config boundary.
- **`getattr(x, "f", None)` cannot tell a wrong object from an unset field.**
  The plan block's TTL read that way for months under a comment describing the
  same defect as already fixed. Reach through the owner and let a missing
  *owner* raise; only the field itself may be absent.

## Data, formats and classifiers

- **A classifier over rendered text cannot separate classes the text renders
  identically.** Carry the route on the error (`ToolError.kind`), not in prose.
- **A delimiter drawn from the content's own alphabet is not a delimiter.**
  Numbering with a field and *two spaces* meant a model quoting a line back into
  an `edit` quoted our padding as code. When a model appears to hallucinate a
  file's contents, measure how far back its source was.
- **A renderer written against a fixed set of names is a guess once the set is
  extensible.** Take the order from the config, so nothing in the renderer has
  to know what an argument *means*.
- **A record is not a rendering.** `run_argv`'s joined form and its list can
  disagree once an element holds a newline. Collapse for the log line; keep
  `result.command` whole.
- **An operator's regex is data, and code must not depend on its spelling.**
  `failed_file_pattern` opens `^\s*`, and `^` in multiline mode can anchor on the blank line above, so `match.start()`
  can sit on the previous line's break. Anchor on `match.end()`, and fix the
  dependency rather than the instance.
- **A conversation with batched parallel calls cannot be read positionally.** A
  reader pairing each call with the next output keeps only the last of each
  batch. Build transcript lines from a denylist: a field nobody thought to add
  is invisible, a field nobody thought to exclude merely costs space.
- **A cache keyed on a string is keyed on its spelling.**
  `verify._recorded_answer` compares command text and HEAD rather than trusting
  the loop, so `resolve_test_paths` sorts and both sides build the list in the
  same order.
- **The same command in both places, spelled the same way.** An autocorrecting
  linter exits zero *after* rewriting files, so running it one way in the loop
  and another at the gate stops the exit code describing the artifacts. "Spelled
  the same way" includes the identity it runs as — compare the user, cwd,
  environment and stdin as well as the argv.
- **Deny-list the noise; never allow-list the signal.** A filter written for the
  shapes already seen drops the one nobody has seen.
- **A stream is not a log.** Warnings on a process's stderr can never appear in
  a file written by an application's logger. Ask which writer owns a file.
- **An empty final turn reads as success.** A model returning `end_turn` with no
  text and no tool calls is, to the loop, a model that has finished.
- **A stop the operator asked for must not render as a failure.** An intentional
  stop wants its own exit code and its own tag.

## Operating a run

- **A write that grows a file in place can be read at its old length.**
  `Path.write_text` rewrites the same inode, and Docker's file sharing caches a
  stat nothing invalidates. **Put a new inode at the path:** `atomic_write` —
  sibling temp file, `os.replace`. Whenever a tool of ours writes a file another
  process reads across a boundary we do not control, ask whether the *name* now
  points somewhere the reader has never looked.
- **A fingerprint written before the work it stands for makes a failure
  permanent.** It suppresses the retry that would have fixed it, because "no
  change" is indistinguishable from "nothing to do".
- **A state predicate is not a completion signal.** Ask whether a readiness
  check reads something *finished* or merely becoming true in the middle — and a
  remedy stacked on a misjudged state manufactures the fault it was written to
  recover from. Prefer the entrypoint's own handoff: under `bash -e` it installs
  and only then `exec`s, so PID 1 is the entrypoint until the install succeeds.
- **Liveness can be read without a race, if you can name why.** Here: `docker
  compose start` returns with the container already running, so a later `exited`
  is a new death; and no service declares a `restart:` policy, so `exited` is
  terminal. Both are conditions to recheck. A bounded wait is the fallback for
  when you cannot name the ordering.
- **A check that loads part of a thing has certified part of it.** A declared
  `bundle_install` proves the app boots in the *test* environment only. Loading the file is
  the only instrument that separates a gem's metadata from its source, and the
  group list comes from bundler rather than hand-written.
- **Watch the process, not only its log.** Two watches, never one: liveness on
  the pid, and a narrow filter for rare events. Never mix a per-cycle signal
  into the rare-event filter, and re-read the log after the process exits —
  `while kill -0 $pid; do grep …; done` leaves by its *condition*.
- **A monitor over an append-only log must be anchored to this run.**
  `last-run.out` is appended across every resume, timestamps repeat daily and
  stage numbers restart, so an unanchored lookup gives a plausible wrong answer.
  Take the line number of the run's own header and read forward. A `pgrep -f` in
  the same loop matches the loop's own command line.
- **A watch must print the anchor it is using.** `A=$(wc -l < file)` carries
  leading whitespace on macOS. The anchor is itself a measurement.
- **A sampled window over a growing log is a lottery, not a watch.** Wait on a
  state that persists — a pid, a file that appears, a line count taken at the
  start. Keep the set enumerable: `ps` answers "what am I running".
- **The run log is not where a failing suite's failures are.** Truncation drops
  the middle and preflight writes no artifact when it refuses. Re-run the suite,
  and grep verdicts case-sensitively as `[FAIL]`.
- **Two runs on one repository is a five-minute window, not a crash.** "I killed
  it" is a claim to verify with `ps`. `code-gantry pause` is checked after
  derivation and before `precheck`, which is what makes stopping safe: it holds
  the derived stage and never touches the tree. `code-gantry unpause` withdraws
  a pause the run has not read yet; `resume` also clears the flag but starts a
  process, so it is for a run that has *stopped*.
- **Killing a run does not kill what the run started somewhere else.** The test
  commands reach the work through `docker compose exec`, and killing that client
  kills the client. Aim cleanup narrowly — a pattern broad enough to catch the
  workers is broad enough to catch the entrypoint.
- **One suite per host at a time.** `full_test_lock` maps the suite command
  to a host lock in `CommandRunner.exclusive`, keyed by command text, so every
  path that runs the suite — the executor's cycle, verify, the re-test after
  a pull, preflight — waits on the same file under `host_lock_dir()`. The
  wait is `waited_seconds`, never `duration_seconds`, and the log names the
  holder. The lock dies with its holder, so nothing is cleaned up by hand.
- **The projection is the churning half and it sits after the mark.** The
  rendered plan is block 0 and changes only at a fold or a plan edit; what has
  changed since sits in block 1, bounded by open keys times `note_chars`. The
  planner's *peak* prompt tokens will not show a change here — peak tracks how
  much work a derivation did — so measure cache writes across derivations that
  drew the same number of stages, against `scripts/planner_cache_series.py`'s
  baseline.
- **Nothing can be omitted after a tool call, so the lever is what you send
  first.** The API is stateless; caching changes the price of resent tokens, not
  whether they are sent. Block 0 is almost entirely the rendered plan, so the
  only lever with that magnitude is a smaller plan — and a planner asked to
  fetch a document behind a tool will fetch it.

## Where things live

`nodes.py` — the loop's decisions: which failures route to the executor, which
to the planner, which to a human. `verify.py` — the layered gate, cheapest
first, short-circuiting. `config.py` — the safety story: the capability
partition and the command denylist. `prompts.py` — pure string building, kept
apart from the clients so it can be tested without a model.

`state.py` holds the reset helpers. Anything a resume or a landing must clear
belongs there rather than inline, because inline has no seam to test at.

`runtime.py` assembles the collaborators and binds what the model clients need;
values wired there have no unit test on either side, so they get an end-to-end
one. It holds `pin_modules`, which is why this codebase can be edited while a
run is live: several modules are imported inside functions to break cycles, so
an unloaded module was read from disk when first needed, putting new code in
front of an old class in memory.

`promptfiles.py` reads `prompts/`: `render(name, **fields)` fills a file's
placeholders and refuses one the code did not supply; `text(name)` is the same
for a file that takes none. `prompts.py` assembles the reviewer's and the
executor's messages and the planner's user message; the planner's system
prompt is assembled in `planner.py` beside the capability paragraphs it
generates from the declared tools.

`dialects.py` replaced role-decides-wire. Two dialects, RESPONSES and MESSAGES;
`dialect_for(model)` answers RESPONSES for an unclassified family, because a
router can resolve to anything and an unknown model must not end a run. A
dialect owns every spelling that differs between endpoints: structured-output
and effort kwargs, text-block type, cache markers and TTL, request cache
options, cache-key parameter, tool schemas, reading tool calls, echoing the
model's turn, shaping tool results, stop detection, refusals, closing text,
splitting the system prompt, base-URL suffix, usage normalisation, client
construction. **Shape belongs to the endpoint, not the vendor** — the same
Google model returns `function_call` items on Responses and `tool_use` blocks on
Messages. `normalise` translates at `send` rather than at the seven places that
construct blocks, because the eighth will not remember.

`gateway.py` is what OpenRouter needs, decided from the endpoint host rather
than declared in config — `session_id`, `provider.require_parameters`, the
effort spelling. `resolve_policy` turns a routing policy into the model it picks
today with one throwaway call, because nothing reports what a router *would*
choose. Every role speaks every wire: `dialect_for` picks from the model and
the route, and an Anthropic model reached through OpenRouter goes on chat
completions, the third dialect, because the gateway drops the schema and
refuses tools on Messages. `roleloop.run_structured_loop` is the one tool loop
the planner and reviewer share; `request_extras` in `dialects` is the one
request assembly all three roles call.

`executorclient.request_extras` is the single assembly of every top-level
keyword the executor's call carries — one function, because a copy of an
assembly is not a check on it. An AST test asserts the loop adds nothing beside
it.

`executorclient` holds two guards against an attempt going nowhere.
`REPEAT_NUDGE_AT`/`REPEAT_ABORT_AT` bound consecutive byte-identical calls;
`FRUITLESS_NUDGE_AT`/`FRUITLESS_ABORT_AT` bound consecutive calls that *changed
nothing* — refused, or a search matching nothing. Identity is a property of the
request; fruitlessness is a property of the answers, which is why the first
guard was unreachable for a model that was searching. The nudges are opposite by
design: the repeat nudge *replaces* the payload, the fruitless nudge *appends*,
because there the payload is a refusal and the only actionable thing present.
Both set **`unproductive_stop`**, one field named for the meaning rather than
either mechanism, which travels through `ExecutionResult` and
`executor-loop.json` into `executor_note`. **When an attempt reports no changes,
read `unproductive_stop` before believing the stage was badly drawn.** The
guards are on the executor only.

`gates.py` is the layer shared by the executor's loop and `verify.py` —
patterns, residue, new tests, checks, tests — so the two cannot select different
test paths. It returns the selection *sorted*, so the loop's record and the
gate's question are the same string for the same set.

`flake.py` decides whether a red suite is the stage's fault or the suite's and
writes `flakes.jsonl`: one append-only record per excusal with the file, seed,
runner locators, and which run and stage — or `origin: preflight` — produced it,
so "which flake is worst" is a sort rather than a log scan.

`edittools.py` is the write-side counterpart to `repotools.py`: no model,
refuses with `ToolError`, records what it did. Two ways to state one change,
failing differently. `edit` identifies a span by quoting the whole of it — and
**a span identified by its content can have the wrong far end and still apply**,
reporting success while welding the tail of one construct onto new code.
`apply_patch` takes a V4A hunk where **every removed line is named**, so that
mistake cannot be expressed. The format is OpenAI's; the matching policy is
ours: exact, no fuzz, no nearest-match fallback, because a context diff's
ordinary failure is a hunk landing somewhere plausible and wrong. **Adopt a
format; never adopt its tolerance.**

**But V4A has two canonical spellings and models emit both.** The API form is
structured `{path, type, diff}` with hunks alone; the CLI form is one string
fenced by `*** Begin Patch` / `*** End Patch`. `parse_v4a` strips the envelope
— including a bare `***`, which is how the markers arrive truncated — because
it is redundant beside `path` and `type` rather than wrong. Two things stay
strict: a named header pointing at another file refuses, having no safe
reading, and any other `***` refuses. Tolerating a redundant wrapper is not
tolerating a fuzzy match.

It is an ordinary *function* tool rather than the SDK's hosted
`{"type": "apply_patch"}`, which has zero files under `types/chat/` and none in
the Anthropic SDK, while most executor
turns come back on Messages. **The shareable part of somebody else's tool is the
payload format, not the declaration mechanism**; the hosted type also has no
description field, and a description decides how many calls happen.

**A write answers with what the file now says.** Both writing tools return the
changed regions numbered as `read_file` numbers them, so the next quote comes
from the file rather than the model's prediction of it — the model's own edit is
the first thing to make its picture of the file wrong.

`executorloop.py` is the cycle: edit until the model stops asking, lint,
**commit, then test**. `executortools.py` and `executorclient.py` are its
schemas and its provider call. `repotools.number_lines` is the single renderer
of numbered source. `repotools.Spend` is everything mutable about a read budget
in one object, so clearing it is replacing it; `count_calls`, `count_refusals`
and `render_counts` are the one summariser all three roles report through.

A green suite is a `suite.green` event on the ledger, sha plus command plus
origin, written by preflight after it runs one and by `advance` for the tip
it pushed when the stage's full suite passed and the publication did not
escalate. Preflight skips a tree this origin has proven and never one another
origin has: the tree fact travels, the environment fact does not.

`ledgersync.py` carries a ledger between hosts: our origin's events to
`refs/code_gantry/ledger/<origin>` on the remote, every origin's ref fetched
and ingested with origin and sequence kept, so the same log can be ingested
any number of times. `sync_at` is the exchange at a seam — it logs a failure
rather than raising, since the local ledger is this host's record and the
remote is a replica — called by `nodes._sync_ledger` inside a run and by
`cli.run` before preflight, where a fresh host first needs the plan.

`ledger.py` is the record: one append-only `events` table, views derived from
it, `open_ledger` the only creator, `read_ledger` never creating. Views are
rebuilt when SQLite's `data_version` moves, because several bays share one
file through `ledger.path` and a cache that knew only its own writes would
miss another bay's claim; `transaction()` holds the write lock across a read
and the writes it decides, which is what keeps a fold from being written
twice. A run's `key_scope` is fixed at start, carried in the checkpoint, and
applied at three seams: the renderers mark what is outside it, `validate_stage`
refuses a stage citing outside it, and nothing else needs to know. A
derivation is written as `stage.derived` records before anything runs, and
`plan` takes a waiting record before it calls the planner, under a host lock
named for the ledger file with the queue checked again once held, so a killed
run or a second bay never pays for the same derivation twice. Claims on keys
and findings carry the holder's pid; `release_dead_holders` gives back what a
run that exited on this host still held. `hostlock.py` is the one lock; the
runner and `plan` both take theirs through it. `planmodel.py`
reads Markdown into the tree and renders it back; `render.py` produces the two
halves the planner is sent; `ledgercli.py` is the operator's `plan …` and
`ledger …`. The pipeline's writes are in `nodes.py`: findings at derivation,
claims at precheck, landings and resolutions after the squash.

What the planner is sent is overwhelmingly the plan, then the repository's agent
and operations documents and the layout, with everything about *this run* under
one percent. Two channels were removed to get there, both the planner reading
its own prior output. `status.md` is still written, hard-capped at 4,000
characters, and read by nothing.

`projecttools.py` is the menu an operator adds to the built-in tools:
`ProjectTool` declares a name, a description and an **argv list**, called as the
executor calls `read_file`. Argv and never a shell is the whole safety story — a
model-supplied value is one inert element, and a placeholder must occupy an
entire element. That protection is on the model's *arguments*; it buys nothing
for a tool whose argument is itself code. Nothing gates which stage may call
which tool, deliberately: the scope gate measures the outcome from the tree, and
a per-stage permission would be a claim used to predict what a gate observes.

**A feature's original role is the one that never gets scoped**, so
`executortools` took the whole declared list by history rather than decision.
**Scope where a thing is reachable, not where it is advertised** — a filter over
the schema is not a constraint on the dispatcher, and a model can name a tool it
was never offered. Both go through `for_role`, and neither caller may hand over
a pre-scoped list, because a caller that could pass the wrong scope makes the
wrong scope expressible.

`pricing.py` turns token counts into dollars from a table nobody here maintains.
`price_map_path` is the single selector for the cache location — under
`work_dir`, gitignored by construction, and `None` rather than a cwd-relative
fallback. `project_entries` keeps only the models `configured_models` names,
entries whole: projecting by *key* would be a hand-written subset of an upstream
schema. `cached_price_map` is the one memo, so a caller cannot fetch per landing.

`configversion.py` identifies a config by its git blob sha, recorded at run
start and checked on every resume, so an edited config refuses to continue a
run. `ProjectPaths` is built from `cfg.work_dir` and there is no slug — the work
dir is the project's identity, which is what the prompt cache key needs.
`cachekey.py` bounds that identity to the provider's 64 characters.

**A project's config lives in the repository it describes**, beside the plan,
with `.code_gantry/` gitignored next to it for everything the run writes.
`target_repo`, `work_dir` and `host` are absent: the first two are derived from
where the file was read, the third became somebody's hostname the moment the
file was tracked. `env_file` names a credentials file, resolved against the
config's directory, parsed rather than sourced, applied with `setdefault` so the
shell wins.

`executor.py` is only what shapes an attempt before it starts — read budget,
excerpts, conventions — plus `run_script_stage`.

`daemon/` is the per-host daemon, an Elixir Mix application with no
dependencies: `Host` reads the host file, `Bay` supervises one run per bay
and makes a missing bay with the target's `bin/mk-bay`, `Sync` runs the
ledger sync on a clock, `Status` writes the status file. It drives the CLI
through `host.command` and never through anything else, so its tests run
against a fake CLI. The CLI's exit codes are its contract: 0 finished, 1
failed before or outside a stage, 2 escalated, 3 paused; anything else is a
crash and is resumed.

`scripts/smoke.py` stands up one HTTP server for all three roles and no binary
on `PATH`. A test asserts the old stub executable is gone, because that is the
sort of thing that grows back.
