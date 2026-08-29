# Working on this codebase

[README.md](README.md) is how to use CodeGantry.
[docs/architecture.md](docs/architecture.md) is the design authority on why it
works this way. [docs/future-work.md](docs/future-work.md) holds the decisions
still outstanding. [docs/archive/rewrite-plan.md](docs/archive/rewrite-plan.md)
is a closed record; nothing in it is outstanding.

This file is for whoever is *changing* the code: the invariants that are easy to
break without noticing, and the rules learned by breaking them. Almost every
line cost a run. The incidents are in this file's git history and in the
docstrings, which carry the reasoning and are worth reading before changing the
behaviour they describe.

**It is also the only durable channel.** A compaction prompt carries a finding
one hop; an agent's memory is keyed on this directory's absolute path. Neither
survives the way a tracked file does. When a long session ends, diff what was
learned against this file rather than summarising it somewhere convenient.

Run the tests with `uv run pytest -n auto`. They are fast and need no network,
so run the whole suite. `-n auto` is not in `addopts` because a single-test run
pays worker startup for nothing.

The suite must pass in a shell with the run's credentials loaded *and* in one
with none. A test about what we pass to a child must clear the variable it asks
about, and must never borrow a real credential's name; both directions have
broken here, and an unscoped `os.environ` write makes the outcome depend on
which xdist worker ran it.

## Invariants

**The planner may never author an executable field.** Enforced twice: the
structured-output schema has no field for a command, and
`PLANNER_WRITABLE_FIELDS` filters the response against an allowlist. A field the
planner may not set should be impossible for it to return.

**And the planner may not author code.** Authored code travels perfectly well
inside a declarative field: "replace this block with exactly this block" names
nothing executable and is still the planner writing the diff. A planner that
cannot run anything cannot check what it writes, and an executor handed a
replacement can only transcribe it. The fix is a format that cannot express the
mistake: quoting goes through `read_excerpts` — a path and a range read at
`stage_start_sha` — so **a reference can only point at code that already
exists**. A fenced block in `instruction` is rejected by `validate_stage`;
inline backticks are left alone, because a rule against naming an identifier
would be routed around. The corollary: an excerpt is the only code the executor
gets, so a range that will not resolve fails the stage rather than being skipped.

**`completed` is append-only.** It is the cacheable prefix of both paid prompts.
Rewriting an entry multiplies the cost of every subsequent call, invisibly.

**Prompt ordering is the caching strategy, not presentation.** Static leads,
churn trails, breakpoints between. Anthropic extends the longest matching cached
prefix, so an append-only region *before* a breakpoint gets cheaper over time;
GPT-5.6 caches only at an explicit breakpoint, so the same arrangement misses
entirely. One document can need opposite placement in the two prompts. Measure
after changing any of this; documented provider behaviour has been wrong twice.

**The block is the cache unit, not the prefix.** One appended byte rewrites the
whole marked block, so ordering a growing document last *inside* it protects
nothing — put churn after the mark. A breakpoint after content that changes
every call costs more than none, because it writes an entry nothing reads.

**There is no push, and stages land by squash merge.** The safety guarantee is
that no method exists which could push. Squashing is what makes "every commit on
the project branch is green" and "the executor commits before it tests" both
true.

**A stage lands completely or not at all.** `advance` mutates in four steps and a
failure used to leave the repository part-way through. `squash_merge` records
where the branch was and restores it; `advance` unwinds the note. Anything added
to that sequence inherits the obligation — and removal is the same rule
backwards, since a guard that changes hands has not gone away. The rollback
stays silent: the original failure is the diagnosis and must reach the caller.

**Whatever the planner draws from, the executor may not edit.** The planner
chooses `edit_files` *and* reads the plan, so without a guard it can put its own
inputs in scope and have the executor amend the instructions it will be judged
against next cycle. `_is_plan_document` is that guard, and every input added to
the planner's prompt belongs in it — the plan tree, the progress log, and the
repository's agent-facing documents, the last most of all, because a stage that
could edit one could retire its own constraints.

**A gate must be able to reach what decides its verdict.** A stage deleting a
declaration is safe exactly when something elsewhere still covers it, and that
file is not in the diff. A gate that cannot reach its evidence produces verdicts
indistinguishable from judgement. Record what a gate *looked at*, not only what
it decided. Preflight is the sharpest case, being the only gate that can wave a
red repository through; it keeps the bytes **and the parse beside them**, since
`(unnamed)` against output full of locators and `(unnamed)` against output with
none are different failures that render identically.

**A record of the work must be written after the work.** Progress-log entries
were once derived before the executor ran and published on landing as "observed
while landing"; a stale premise published that way becomes the account of record
for every later call. They say "observed while planning" now, and what the stage
*did* is recorded by the reviewer, the only participant that has seen the diff.
Anything written before the work is a prediction, whatever tense it uses.

**But an event is recorded as it happens.** A *claim* about a stage must wait for
the stage; a transcription has nothing to predict. `executor-conversation` was a
JSON array — writable only whole — so a long attempt had nothing to read while
it ran and one that never returned left no record at all. It is `.jsonl` now,
appended as it grows; `sent-prompt.md` is written before the first call;
`executor-loop.json` is one write at the end, because totals are the only thing
not true until the attempt is over. A sequence written at the end is a summary
with extra steps, missing exactly when it matters.

The mechanism is worth copying: the transcript is a `list` subclass mirroring
each append to disk, rather than a callback threaded through the four places
that append — a fifth is one refactor away, and a record that must be
*remembered* at each site goes quietly missing between two correct changes. Make
the recording a property of the only operation that can change the thing.

## Prompts and model-facing strings

- **Project knowledge belongs in config, never in code** — including model-facing
  strings. A tool description reading `e.g. app/controllers/foo.rb` ships one
  framework's shape to every project. Worth a test that fails on the names.
- **Config should hold the path, not the copy.** Transcribing a project's own
  document into config creates a second copy that drifts, and the copy is the one
  the pipeline reads. Point at the file and read it at the run's sha.
- **A prompt that describes a capability must be generated from the thing that
  grants it.** `PLANNER_SYSTEM_PROMPT` asserted the executor "cannot run
  commands" until `project_tools` shipped; the paragraph is built from
  `cfg.project_tools` now.
- **The same sentence goes stale in two prompts for one reason.** The identical
  claim sat in `_conventions_block`. A model can see its own tool schema, so a
  reason it can observe to be false invites discounting the instruction attached
  to it. When you fix one, grep the other roles' prompts for paraphrases.
- **And when you find one, sweep for the rest.** A deliberate pass over every
  model-facing string produced nine more, the worst being contradictions — the
  planner told to quote code in fenced blocks that `validate_stage` rejects.
  Schedule the pass after any change that removes a component; the prompts are
  where a deleted thing goes on living.
- **A prompt sentence outlives the fact it was written about.** The excerpt block
  said "treat them as current" after `resolve_excerpts` moved to reading at
  `stage_start_sha`, where a rework's own prior attempt has moved the lines. A
  docstring is checked by the code beneath it; a prompt string by nothing.
- **A prompt must not tell the executor it may not change the file it is there to
  change.** The excerpt block was headed "files you may read but not change",
  borrowed from `read_files`; most excerpts name a file the stage's scope permits.
- **And must not name a field its reader cannot see.** The rewrite cited
  `edit_files` — the planner's field name, appearing nowhere in the executor's
  prompt.
- **Both were found by a human reading a prompt, which is the only thing that
  finds this class.** No gate compares two sections of one document.
- **A phantom constraint is the expensive direction.** A missing capability
  produces a stage the gates catch; a constraint that does not exist makes work
  read as *blocked*, and a stage never drawn leaves no artifact. Same reasoning
  put *wrong dependency* into `PlanNote.kind`.
- **An optional field is answered with nothing.** `observations` came back empty
  every time across two revisions written to encourage it: an optional field with
  a conditional trigger can always be declined in good conscience. If output is
  wanted every time, make it required and ask something true every time — "what
  does this change do", not "did you notice anything else".
- **Ask what else already carries it, not whether the content is good.** The
  completed-stage history reproduced what three live channels already said;
  removing what had another home left it 99% smaller. The first fix proposed was
  a better-written summary, which reintroduces the fault with nicer prose.
- **A channel that keeps restating the same fact is a fact with no durable home.**
  Two tells: the same entry arrives call after call, and the field descriptions
  describe something other than the entries.
- **A markdown link means two things.** Recursive plan resolution looks obviously
  right, but every depth-2 link here was a cross-reference or a document another
  channel supplied — and transitivity costs the property that reading the root
  tells you the whole payload.
- **A model asks for one tool at a time unless told otherwise, and telling it is
  cheap.** Nothing suppressed batching; both wires default to permitted. A
  system-prompt section and a line per read tool moved it. Measure the *shipped*
  string — an experiment's paraphrase is a different string.

## Records, artifacts and ledgers

- **A summary artifact must carry the number it is about.** A sum cannot be
  decomposed afterwards, so the per-item artifact holds the per-item figure or
  the only analysis left is archaeology.
- **And the whole response, because a silent omission answers.** `planner.json`
  recorded a subset and nothing said which; I read it, found no key, and reported
  a zero the prompts contradicted. An absent field and a zero are
  indistinguishable to a reader.
- **And the writer must be the model, not a list of keys.** `executor-loop.json`
  carried ten fields of twenty, and the four omitted were the entire answer to
  *why did this attempt end*. It walks `dataclasses.fields` now.
- **An optional keyword does not reach call sites that predate it.**
  `append_flakes` was called by `preflight` without the `examples` argument it
  later gained, so every baseline flake ever excused recorded no locator.
- **A sentinel is a value in the wrong field.** That call site passed
  `"preflight"` as its `stage_id`. `origin` is its own field now and `run_id` is
  null rather than faked.
- **A ledger that records refusals and not successes lists only the failures.** An
  answered declared call appeared in no tool log, count or budget while the same
  tool failing showed up. The error path is the one people instrument.
- **A ledger that records the container cannot answer questions about the item.**
  `flakes` recorded a file and a seed and could not say whether one example
  failed many times or many failed once — the reason the file exists.
- **A watermark into a concatenation indexes a list whose middle moves.**
  `tools.log` sliced `reader.calls + editor.calls` at one index, so reads
  appended during a turn pushed the editor half right. Two views of one dataset
  disagreeing means the derivation is wrong, not the data.
- **A record published from a run is a claim in every later prompt.** A resume
  rewrote two pending planner notes; one diagnosed the failure that had just been
  backed out and reached the live progress log as `kind: correction`. Before
  restoring a pending note, ask whether what it asserts still stands.
- **Publish a finding when it is found, not when the work lands.** Planner notes
  about the plan are true whether or not the stage succeeds. Reviewer
  observations stay on the landing gate, because an abandoned diff does not exist.
- **Classify where the damage is, not where the tidying is.** Sorting out-of-scope
  findings during the fold cannot work — the log is spliced live, so the damage is
  done in the hours before. Ask of any sorting step whether the thing being sorted
  is inert while it waits.
- **Prefer the fact to the label.** The squash commit already carries the stage
  id, the instruction's first line and the diff; `git blame` answers from facts
  that cannot be wrong, where a declaration is a claim made before the work.
- **A permission is not a record.** `edit_files` is what a stage *may* touch, so a
  check reading it fires over a superset of what happened — and a file moves just
  as well by a human's hand. Comparing blobs answers from the tree. When a check
  reads a declaration to predict an outcome, ask what measuring would cost.
- **Convert a format while the file is small, because the window closes.** The
  flake ledger's five-group regex made "not captured" and "written before that
  field existed" the same bytes. A format whose parser needs a compatibility
  branch is one field from needing two.
- **Before backfilling an append-only file, ask whether the raw material is still
  on disk.** The run directories held every failing suite's output.
- **A figure in a document must name the artifact it came from.** Two cost numbers
  in `docs/architecture.md` matched no `report.md`; written from recollection,
  they read as measured and were then reasoned from. A reading with no instrument
  behind it should be deleted rather than approximated.
- **Do not explain the present with a component that is absent.** Thirty comments
  described current behaviour in terms of a tool deleted months earlier. The
  corollary governs deletions: before removing something, grep for what *cites*
  it.

## Measurement and diagnosis

- **Read artifacts; do not regex them.** `\s` is not valid in POSIX ERE so
  `git grep -E` silently matches nothing; `File\.exists?` matches the
  already-converted `File.exist?` because `?` quantifies the `s`. When a count or
  an absence is load-bearing, open the file. `str.index()` on a heading the
  format repeats is the same bet with different syntax.
- **Cut code with a parser, not a pattern.** Deleting a module by regex removed
  the wrong span twice in five minutes; `ast.parse` accepts a `return` outside a
  function and `compile` does not, so one survived a syntax check and failed at
  import. `ast` gives exact `lineno`/`end_lineno` for three lines of work.
- **Check what a command actually returns.** `git show <sha>:<path>` on a symlink
  returns the link's target. A pipeline exits with its last stage's status, so
  `pytest | tail -2 && git commit` commits a red suite. `grep -c` counts lines,
  `grep -o | uniq -c` counts occurrences. macOS has no `timeout`, which returns
  127 and reads as a suite result.
- **And what it inherits.** cwd, env and stdin are inputs too: ripgrep searches
  **stdin** when stdin is not a terminal, so `search` worked at a shell and
  returned nothing under `subprocess.run`. No unit test and no shell probe can
  see it, since pytest and a terminal sit on opposite sides of the stream.
- **A pathspec is not a glob, and an empty answer is evidence.** `path_glob`
  handed to `git grep` as a bare pathspec made a large share of empty answers
  false. A tool that returns a wrong answer gets caught; one that returns
  *nothing* gets believed.
- **A flag that filters can undo the boundary you thought bounded it.** `-g`
  filters ripgrep's walk rather than working within it, so a model-supplied glob
  overrides `.gitignore` entirely. It compounds, because output is capped: leaked
  artifacts push real hits out and the model searches again, so **a leak surfaces
  as repetition**. Ask what a stated boundary is *made of*.
- **Check the instrument before the world.** Three measurements in one afternoon
  were wrong in the tool: `grep -c` counting lines, a "0.0% shared prefix" that
  was the artifact's own header changing, and a memory read as a claim about the
  code.
- **A zero is a reading about the instrument until proven otherwise.** Accounting
  that reports zero for "not priced" as often as for "free" is why prices are
  `None`. A number *exactly* zero across every sample is a reader that cannot see
  the field. A question recorded as unmeasurable is worth re-asking after any
  change to the layer that could not answer it.
- **A ratio that is exactly constant is the instrument.** Messages-wire opening
  turns reported `prompt == 2 x cached` across stages whose instructions differ:
  the gateway reports the prompt twice. Settled without a probe, by measuring
  what the request can possibly contain.
- **Do not propose a fix for a mechanism you have not established.** Three causes
  named from their shape in one incident, each refuted in a minute by a command I
  had not run, two with code already drafted. The tell is that a fix arrives
  before a measurement. A remedy built on an unestablished mechanism is
  *confirming*, because it gets adopted while the real cause keeps firing.
- **A model's account of why it stopped is evidence about what it tried, not about
  what is possible.** An executor reported a permission error accurately and
  reproducibly, and the actual blocker was elsewhere both times.
- **The diagnosis can be in a place nothing reads.** Bundler named the missing
  dependency in the first minute — in the *entrypoint's* logs, not the process we
  ran. Every tool result the model got was truthful and useless.
- **Measure the artifact in the state your claim is about.** Three checks for
  trailing whitespace found none, because the linter had already run.
- **Do not measure against a tree a live run owns.** Clone to a scratch directory
  and check out the sha the artifact was recorded at.
- **A cache-timing fault answers "not reproduced" once and "reproduced" the next
  time.** A single clean replay of a race is a *false negative*. Say "did not
  reproduce on one attempt".
- **Hedging is what protecting a hypothesis looks like from outside.** Two crashes
  were called deterministic, then a proposal to replay them was met with "it
  probably won't reproduce". Both cannot be true. Check whether the reason not to
  run an experiment was measured or invented.
- **Report a measured non-result as a non-result.** Capability and effect are
  different claims and only one usually has evidence. Same for sampling: tool-use
  counts on identical inputs have varied from 0 to 21, so one sample is a story.
- **One item from a ranked list is not a finding; the list is.** A single hit
  supported "one more exclusion is needed"; the full result showed most hits
  matching on one shared token and supported "this query was never a question".
- **A usage rate is not a verdict on a tool's value, and rank is not the signal.**
  Excluding noise from a semantic query barely moved the top score. The win is in
  what loses.
- **The index's worst contaminant is the pipeline's own output.** Excluding our
  artifacts took them from a sixth of hits to none with top scores unmoved. Two
  cautions: the exclusion list lives in the *target* repository's indexer, and a
  bad query stays bad.
- **Improving a tool's answers cannot make anything reach for it more often.**
  There is no memory across runs. The index decides what a call is *worth*; the
  description decides how many calls *happen*.
- **Know the size of what you are about to walk.** A bare recursive search over
  the target repo's tens of gigabytes starved the machine for minutes, and those
  runs are the ones whose results made no sense.
- **Send it to the endpoint.** Four defects found one dead run at a time were each
  a single parameter, answerable in one call with the payload production
  assembles. A stub answers what you taught it.
- **A stub cannot fail the way the thing it stands in for fails.**
  `scripts/smoke.py` catches what raises client-side and not what the server
  rejects; it reported green through three stages of a run whose every request
  came back 400.
- **A probe that is not the production request answers a different question.**
  Assemble the probe from the code production calls, or spend the afternoon
  chasing a difference you introduced.
- **An artifact that renders part of a payload cannot reconstruct it.**
  `planner-prompt.md` renders `messages` only — no tools, no system block, no
  schema — which is exactly the region the caching question turned on.
- **A falsification harness is code and can be broken.** A patch reintroducing a
  bug asserted on a string that appeared elsewhere, so the replacement never
  happened and the test's passing was reported as proof. Print the diff.
- **An aggregate cannot separate the cases you care about.** "The prefix cached
  every turn" and "the prefix extends every turn" land near the same rate; only a
  per-turn series tells them apart. Record the series at the item.
- **Cache writes measure what was newly cached, not how big the job was.** A stage
  reusing an earlier prefix looks small *because it was efficient*. Summed
  per-attempt peaks need no reconciliation between providers that disagree about
  what a cache write is.
- **A total from a tool loop is not a context figure.** The loop re-sends the
  whole conversation per turn: nonsense as capacity, exact as a bill.
  `accumulate_usage` maxes any key naming a peak rather than adding it, decided
  in the helper because four call sites is three too many to remember.
- **A reading is worth taking when a decision depends on the answer.** The
  instinct to close an open question is right about the question and wrong about
  the priority.
- **A confound can invert a measurement.** Comparing two effort tiers, output per
  stage fell as expected while *total* cost rose — entirely because the progress
  log had not been folded in between.
- **A real measurement attached to the wrong decision is harder to argue with than
  a wrong one.** The question is not "is this number right" but "what would have
  to be true for this number to change the answer".
- **Verify a mechanism *can* fire before recommending someone enable it.** A
  switch that reads as the whole story and is a no-op is worse than one nobody
  turned on.
- **A rule right about every case and silent about the sequence lets a
  deteriorating thing deteriorate at full speed.** The flake doctrine is applied
  per excusal with no memory, so the sixth sighting reads like the first — until
  the example stops passing alone and the cost lands on a stage that did not
  cause it. **And the tell is a record that exists with no reader:** the file
  built to make repetition countable is read by no code.

## Budgets, ceilings and counters

- **A line is not a unit of size.** Line ceilings assume a line is a line's worth
  of text — false of a minified bundle, a vendored asset, a `structure.sql` or a
  one-row fixture. `max_chars_per_call` is the second dimension. Ask what unit a
  budget is denominated in and whether the thing it protects is measured in it.
- **A second dimension whose default ignores the first is a tightening.**
  `max_total_chars` shipped with a default derived from the *class* default line
  budget while every real config raises that several times higher, so it bound
  first and silently. Derive a companion limit from its partner's configured
  value.
- **A ceiling that binds first is a policy nobody chose.** `max_model_turns: 20`
  silently decided whether stages could be done at all, and the only symptom was
  "the attempt produced no changes", which sends the planner to redraw a stage
  that was never the problem. If a limit is hit in normal operation it is not a
  backstop.
- **A distribution measured under a cap cannot choose the next cap.** "p99 25"
  against a limit of 25 describes what the cap *allowed*. A ceiling is set from
  the tail, and a limit is only measurable once something records being refused
  by it.
- **The uncapped case is where a distribution can set the threshold.** Look for
  the gap, not the median: set the line where legitimate use stops rather than
  where pathological use begins. Re-measure before moving such a number; once the
  guard ships the distribution under it can no longer answer the question.
- **A counter added underneath another is not reset by the code that resets the
  first.** `_chars_used` arrived after `_lines_used` and accumulated for the
  process's life, so past the ceiling *every planner call was refused on its
  first read*. The cliff is the tell.
- **Clear spent state by replacing one object, not by zeroing a list of fields.** A
  field-by-field reset is a list somebody maintains. Whatever *shares* the
  mutable structure must reach through the owner, or it appends to an orphan.
- **And the reviewer had no reset at all** — the same defect by the other door.
  Ceilings named "for this step" were really for the run, and late reviews were
  starved by their predecessors.
- **A budget whose consumption is never printed cannot be seen to leak.** The only
  outward sign was the planner saying in prose that it could not read, which it
  misdiagnosed. The log lines carry ` (used/ceiling chars)` now. The test is not
  the value but the *series*.
- **A budget nothing records cannot be seen to be near.** With no peak recorded,
  "how close is the reviewer to its window" was unanswerable from the artifacts.
- **A limit can depend on a setting in another file, enforced by neither.** The
  installed SDK refuses a *non-streaming* request implying a long generation; it
  never fires only because the planner always passes an explicit timeout. Delete
  that field and every call raises before it is sent.
- **And the diff a revision prompt carries has no ceiling.** `max_chars_per_call`
  bounds what the planner *reads*; one stray large file in the tree has overrun
  the window through the other channel.
- **Collapse before you truncate.** `truncate_middle` keeps head and tail because
  command output is informative at both ends — false of a progress reporter,
  which puts its dots first and its findings after. Hence `clip_for_model` rather
  than two calls at each site.
- **A test that forbids a name is not the same as a test that pins a decision.**
  `FEEDBACK_OUTPUT_CHARS` was declared in two modules: the function was
  centralised and the number was not, and nothing fails when two copies disagree.
- **Ban the second application, not the word.** Forbidding any function named
  `clip` outside `gates` failed on a documented one-line delegation; asserting
  the constant is spent exactly once leaves genuinely different decisions alone.
- **And the sweep is worth running deliberately:** parse every module and list the
  names defined in more than one.

## Gates, guards and routing

- **Assert state, not change.** `forbidden_patterns` reads the diff's added lines
  and catches what must not be *introduced*; `must_not_remain` reads file
  contents and catches what must not *survive*. Near-identical in prose, opposite
  in a diff. Progress notes state totals and never deltas, because "make this
  change" is unsatisfiable the moment the change is already true.
- **A guard belongs where its question can first be answered.** "Has `base_ref`
  moved" is a startup fact that cannot become true mid-stage; asked inside
  `verify` it is answered after a planner call and an executor attempt have been
  paid for.
- **And that guard asked equality where the question was ancestry.** Any change to
  `base_ref` stopped the run, though the report prints a sha, which stays true
  however far the branch travels. What is worth stopping for is a *rewrite*.
- **A category drawn around the mechanism excludes the case drawn around the
  meaning.** The transport retry was categorised as *the request never arrived*;
  a 529 then ended a run. Ask not "is this set complete" but "what is this a set
  *of*".
- **A guard can be unreachable for the shape its failure takes.** A
  `stop_reason == "max_tokens"` check had never fired, because the SDK parses
  structured output *before* returning. Ask which layer raises first.
- **A ban can be a bug wearing a design constraint's clothes.** Three assertions
  forbade `review` escalating because "a rejected stage is a planning problem" —
  true, and no cover for a suite killed by a signal *after* approval. Check that
  what a test forbids is the thing its reason is about.
- **A check that fixes must not sit behind one that can fail for unrelated
  reasons.** `run_all` breaks at the first failure, which stops being right once a
  later entry also *repairs*: an environment hiccup then skips the linter and the
  model's only feedback is about a tool it never invoked. Chain a fixer into the
  same entry with `&&`.
- **And the `&&` makes an environment failure look like a defect.** That entry runs
  through `docker compose exec`, and `_layer_checks` routes an ordinary non-zero
  back to the executor as "a required check failed", because only `failed.signal`
  gets `Route.HUMAN`. An attempt spent 42 minutes being told its work was wrong
  by an environment that was not there. 128+N is too narrow a definition of a
  signal.
- **And it happened again because preflight proved the wrong set.**
  `_environment_checks` ran `setup_command` and both test commands and never the
  `checks`, so a host-side entry was first invoked by stage 000 — routed to the
  executor as a defect, for two planner revisions. What hid it is that all three
  proven commands went through `docker compose`: **proving a command in the
  container says nothing about the host.** Preflight runs the declared list now,
  and runs it *in full*, because the gate's break-on-first-failure had also left
  the second entry unproven for the life of the run. The general form: **the set
  preflight proves must be the set the loop runs**, and a new executable field
  in config is a new thing for preflight to prove.
- **A check may write, and only the child branch should carry it.** Useful checks
  often fix as well as report, and nothing else in the loop commits what they
  changed — left uncommitted it is swept up silently when the stage lands, and
  the next stage's precheck then refuses to cut a branch over changes it cannot
  attribute. `checks_commit_changes` puts it on the child branch.
- **A tool that edits after the model stops leaves its context stale.** The tree
  moves under a finished conversation, so the next cycle opens with file contents
  no longer on disk and from where it sits the model made the change and the gate
  is complaining anyway. Stage the model's work before the checks run and the
  rewrite is the unstaged remainder. Attribute it in as many words: handed an
  unattributed diff, a model reads it as its own mistake and tries again.
- **Ask the question that expires first.** Most gates read the tree and the tree is
  still there; a pre-commit hook reads the *index*, and the commit consumes it.
  Asked before the commit a refusal is a cycle the model fixes in session; asked
  after, there is nothing left but to escalate.
- **Two reasons an operator could not have fixed that in config.** `_gate_cycle`
  commits the model's raw work before `checks` runs, deliberately, which puts
  every autocorrecting entry downstream of the commit a hook refuses. And the
  hook came from `core.hooksPath` set **globally**, so it is not the target
  repository's property at all.
- **Run the thing rather than modelling it.** Stripping trailing whitespace on
  write fixes one hook and no other. `git hook run pre-commit` (git >= 2.36)
  invokes the hook exactly as a commit would. Two facts recall gets wrong: it
  exits **1** when no hook exists, the same status as a refusal, so presence
  comes from an executable at `git rev-parse --git-path hooks/pre-commit`; and
  the hook reads the **index**, so the gate stages first.
- **A gate is a check, not a guarantee.** `commit_refused` stays: passing means the
  hook accepted those staged bytes, not that the commit will succeed.
- **And a gate written for the first commit does not cover the second.** A stage
  branch carries several commits per cycle; the refusal came on the one after
  `checks`.
- **Attribute bytes by what ran between, not by whoever is nearest.** I twice
  called the linter's whitespace the executor's. Find the commit that last held
  the file clean and enumerate what ran after it.
- **Hiding a tool's own churn from the gate hides it from everyone.** Keeping the
  editor's line-ending normalisation out of the reviewer's diff silently
  converted tracked files from CRLF to LF as whole-file rewrites no participant
  saw. **A gate exemption wants a counter, or a periodic look at what it has
  swallowed.** The same shape is live in `checks`, where an ERB linter reformats
  every file it touches and the reviewer approves it as declared machinery.
- **And a finding produced that way arrives without a magnitude.** The stated
  reason for caring was that a uniform repository had been made mixed. It was
  never uniform, and the cost was already spent. The right answer was to do
  nothing.
- **Separate what was found from what should happen next.** A reviewer blocked a
  stage over a difference whose consequence it had not checked: the judgement was
  right and the routing was wrong. Before withholding approval over a
  consequence, *verify* the consequence — naming one is not establishing it.
- **Every gate says why.** A stage landed clean and the next printed `[plan]
  revising` a second later with nothing naming the cause; the reason reached the
  planner and the checkpoint, the two places a person does not look.
- **A guard and the reset it depends on are one decision written in two places.**
  `precheck` resolves the stage's model "only when unset", and nothing cleared
  the field between stages, so it locked the first stage's answer for the whole
  run. Write the clearing in the same edit and name the node that owns the end of
  that unit.
- **A check that exists as a side effect of something else disappears when that
  thing moves.** The executor's credentials were never checked at preflight; what
  hid the gap was an unrelated probe making a real authenticated call whose line
  printed among the preflight output. Ask of each check what it is *made of*, not
  what it appears beside.
- **A rule is checked against new work; nothing re-reads what predates it.** This
  file already said a guard belongs where its question can first be answered, and
  `run_preflight` still ran both suites before reaching an environment check.
  Worth a deliberate pass at existing code whenever a rule is added.
- **The rule you already wrote gets rebuilt in the next feature.** After the
  optional-field lesson, a later step shipped `additional_stages` as an optional
  field opening "**Normally empty, and empty is the right answer**". The new
  field did not look like the old one. Read this file when adding a field, not
  only when debugging one.
- **"There is no shell" is a claim about the tool schema, and the pipeline runs
  shell scripts.** `setup_command`, the test commands and every `checks` entry are
  operator-declared argv naming scripts *in the repository being edited*, so a
  stage that may edit one has arbitrary execution by a slower route.
  `no_direct_edit` is the only thing standing there. **The ban belongs on the
  files, not the directory** — the same `bin/` held exclusion lists stages had
  legitimately edited many times. **And adding a tool adds a script:** a declared
  command may not start with `sh`, so any tool whose body is more than one
  program becomes a file in the repository and needs its own entry.
- **A replan lands nothing and leaves everything.** `request_replan` skips the
  gates, so no stage lands — and the executor's edits stay committed on the stage
  branch and checked out in the tree, deliberately. On its first firing the
  attempt had rewritten a manifest the container could not install, and the
  *next* stage's `precheck` ran `setup_command` against that tree. Ask of any
  tool that hands work back what the attempt has already changed outside its own
  diff.

## State, resumes and branches

- **A resume is not a fresh process with the old state.** `resume_fields` said it
  was "what a resume merges over the saved checkpoint" and nothing merged it, so
  every resumed run began with a four-key state and no `run_id`. `step` counts
  from zero inside one `drive` call and the key is `(run_id, step)`, so a resumed
  session overwrote the beginning of the previous one while its tail survived at
  higher numbers, and `load_state` returned whichever session ran *longest*. What
  hid it is that the continuity that matters lives on the project branch. Check
  that a resume starts from what it loaded, and that "latest" means last written.
- **And it re-enters at the node it died in, not at the top.** `cut_stage_branch`
  is only on the path through `precheck`, so a resume interrupted inside `verify`
  comes back inside `verify`. "Delete the stage branch and resume" buys a fresh
  cut on a fresh *run*; on a resume it buys a stop.
- **Read `resume_entry_point`; do not read the checkpoint's `next_hop`,** which
  `resume_fields` clears. The entry is computed from `failure_layer` —
  `PLANNING_FAILURES` to `plan`, `REPO_STATE_FAILURES` to `verify`,
  `paused_before` to the stage it was holding — and otherwise from
  `stage_has_work`. `review` is in `REPO_STATE_FAILURES`, so a stage interrupted
  after a rework verdict re-runs `verify` and the reviewer against the work
  already on the branch.
- **A resume hands the planner the failure a human just fixed.**
  `opening_failure` survives into a resume that re-enters at `plan`; the planner
  reads a deterministic harness error, concludes correctly that no stage could
  change it, and blocks — then re-blocks on every subsequent resume. A fresh run
  is the workaround.
- **The failure that opens a retry sequence is the diagnosis; the ones after it
  are consequences.** Overwriting the first with the last turned a legible error
  naming a file and line into "the executor timed out". `opening_failure` claims
  the first write-once, and is claimed on the *retry* branch too — that is where
  the diagnosis was being lost.
- **Content cannot move between a pinned document and a live one.** The plan is
  read at `plan_sha` so the reviewer judges against the text the planner drew
  from; the progress log is spliced live. Both are right and together they have a
  seam: a fold moves content out of the log and into the plan documents, and a
  resume inherits `plan_sha` and re-reads nothing. `_plan_unmoved` refuses rather
  than re-reading, because `current` and its queue were derived against the old
  text. Ask of any two inputs read at different revisions whether anything ever
  moves between them.
- **And the guard is about any edit to a pinned document, not only a fold.** It
  fired when a human in another session rewrote an `AGENTS.md` paragraph to say
  the opposite of the pinned copy.
- **A field removed from a model strands the run that persisted it.** `Stage` is
  `extra="forbid"`, so deleting a field raises on the next *resume* — hours deep,
  for a stage that is perfectly valid. `current_stage` filters to declared fields.
- **A test that pins where a value lives passes while the value is lost.**
  `plan_seconds` read zero because deriving a stage returns `**base` then
  `**fresh_stage_fields()`, and the reset zeroed what `base` had just set. The
  test asserted the location of a zero rather than the behaviour of a stage.
- **Look one line up from the field you are adding.** `executor_cost_usd`
  accumulates across attempts and says why; eleven lines above,
  `executor_context_tokens` was *assigned*, so a stage whose last attempt is a
  one-line fix records that attempt's peak as the whole stage's.
- **"Nothing landed" is a claim about the project branch, not about the stage.** A
  project branch with no landing is what a stage in progress looks like, and a
  stage branch was deleted on that reasoning. `stage_start_sha` outlives the
  branch, so re-cutting from a newer tip silently acquires whatever landed in
  between. Run `git log <project_branch>..<stage_branch>` first.
- **Approved work does not survive a redraw.** A redrawn stage comes back with
  `fresh=True`, which deletes and recreates the branch, so the executor runs
  again and the approval is paid for twice.
- **A value that was private when its file was private is published when the file
  moves.** Relocating the config into the target repo turned `target_repo` into
  one machine's home directory in a file other people check out. When a file
  changes audience, re-read every field as though seeing it for the first time.
- **A value that fits is a value that fits *where it is*.** `prompt_cache_key` is
  capped at 64 characters; making `work_dir` the project's identity took a
  19-character key to 98 and the first reviewer call came back 400. **And blind
  truncation is the wrong fix** — two projects under a long shared prefix
  truncate to the same key and silently share a cache. Both sites hash through
  one helper.
- **A relative path is a decision the launch command makes, and it appears in no
  config, log or artifact.** `PRICE_MAP_FILENAME` was a bare filename at three
  call sites, so a third-party rate table cached against the *process cwd*,
  landed in a tracked directory, and overran a revision prompt's window almost
  single-handedly. **A `.gitignore` line suppresses the symptom in the repository
  that noticed and leaves the mechanism running everywhere else.**
- **Project a third-party copy rather than keeping it whole.** The table is
  thousands of entries and a run prices three — *hold the path, not the copy*
  honoured at the config layer and abandoned one layer out.
- **And the guard existed at one of the two call sites.** `executorloop` memoised
  the table; `nodes._stage_spend` called the loader directly, so every landed
  stage refetched. The memo is in the loader now, where a third caller inherits
  it.

## Providers, SDKs and the wire

- **Provider shapes come from the installed SDK, not from recall or docs pinned to
  another version.** Only a live call produced these: tools must be declared
  `strict` or structured output will not auto-parse, and a reasoning model's tool
  call must be echoed back with the reasoning item it declares as required.
  Neither is visible to a stub.
- **Classify a provider failure by status code, not by exception class.** 529 is
  `OverloadedError` on Anthropic's SDK and `InternalServerError` on OpenAI's, so
  a class list is right in exactly one of the two files that need it.
  `is_transient_status` is pinned *against the installed SDKs*. 429 is included,
  safe only because our wall clock bounds the wait.
- **Retry types belong to the SDK the call goes out on, not the module you
  imported them from.** `executorclient` asked the OpenAI client for them —
  silently wrong the day the executor became wire-polymorphic, so on the Messages
  wire nothing had ever been retried. It is a property of the dialect now, and
  raises rather than defaulting to empty, because no retrying looks identical to
  nothing having failed.
- **A retry that behaves correctly can still describe itself wrongly.** One loop
  serves two budgets and hardcoded "transport failure", so a 400 the provider
  *answered* announced itself as a dropped connection. A log line is the
  interface a failure is diagnosed through.
- **A rejection of a *pointer to a cache* cannot be answered by sending it
  again.** `Cache content <id> is expired.` is excluded from the spurious-400
  budget and answered by resending the same context with nothing marked, cold for
  the rest of the attempt — a handle dead now is dead next turn.
- **Moving one component onto a new axis leaves its neighbours on the old one.**
  Making the wire a property of the model moved the *client* and left
  `prompt_cache_key`, `input_text`, `prompt_cache_breakpoint` and `extract_usage`
  behind: four defects, one shape, found one live run at a time. The refactor
  reviews as complete because the thing it was about is complete. Ask what *else*
  touches the request.
- **A parameter no provider declares is not ignored; it excludes every provider.**
  `output_config` is Anthropic-native, so through a gateway with
  `provider.require_parameters` on it answers **404 "No endpoints found that can
  handle the requested parameters"** rather than 400. Read as a routing problem it
  sends you looking at the model; it is a request problem.
- **The spelling follows the route, not the model family.** `output_config` is
  accepted direct and via the gateway to Claude but 404s via the gateway to
  Gemini; `extra_body.reasoning` is the reverse. `GET /api/v1/models` carries
  `supported_parameters` per model and is the cheapest way to ask.
- **A documented parameter can be accepted and ignored.** Through the gateway
  `tool_choice` survives while its `disable_parallel_tool_use` sub-field is
  dropped in silence — worse than a rejection, because a request that reads as
  constrained and is not sends nobody looking.
- **Ask which direction a knob points before reaching for it.** A default that
  already allows the thing means the knob is for forbidding it.
- **Cold on the Messages wire means `input_tokens: 0`.** The prefix lands entirely
  in the cache fields on the turn that writes it, so read with OpenAI's extractor
  a cold turn records *no prompt tokens at all* — and the opening turn is
  precisely where the whole prefix is a write.
- **A rule fixed in one role's type does not reach the role using the other
  type.** `PlannerUsage` grew `peak_prompt_tokens`; the reviewer's `TokenUsage`
  did not, so its records carried tool-loop totals while the log line renders a
  total as `(N prompt, M cached)`, which reads exactly like a peak. Fix it where
  one reading is its own peak — `extract_usage`. **And add the field last on a
  positionally-built dataclass.**
- **When a fix moves a value into a shared type, grep for the private copy the
  shared one was modelled on.** `ExecutorTurn` tracked its own peak years before
  `TokenUsage` had one. Noticing a duplication and writing it up is the worse
  half.
- **Test the layer you are actually going to call.** A metadata file registered a
  model perfectly in litellm and did nothing through the layer in between, which
  deferred registering; the verification had called `litellm.register_model`
  directly. The working split is counter-intuitive: routing comes from the model
  string, pricing from the metadata file, each useless for the other's job.
- **A tool reads more than you hand it.** A subprocess tool once used here scanned
  the message *and its own reply* for anything path-shaped and attached the file,
  so a conventions document dense with paths blew the context limit. A
  third-party tool's behaviour is a property of its source, and this was found by
  grepping that source after two wrong theories.

## Tests

- **Test end to end wherever a value crosses a schema boundary.** Four defects
  have been values computed and written correctly and lost in transit — dropped
  by a schema that did not declare the key, zeroed by a reset, or omitted from
  the artifact meant to prove they existed. Every one passed its unit tests.
- **A test suite can be exercising the path you are about to delete.** The
  integration tests drove a fake executor binary and were green the whole time
  the in-process executor ran live. A default that only tests rely on is a fork
  in the road with no sign on it.
- **A test helper that stands in for a node is laxer than the node.** `with_stage`
  cuts a branch the way `precheck` would, so tests using it never ran the
  clean-tree guard — and the failure surfaced as an assertion about the *feature*.
- **A fixture can make a whole file's tests laxer than production.** The shared
  repo fixture has no plan root, so a blocking preflight check failed in a dozen
  tests and the suites ran anyway — a state no caller can reach, green for
  months. Ask what the fixture *omits*, then whether production could run with
  that omission.
- **An earlier branch can eat every fixture.** Three tests named for the semantic
  fallback asserted its output, and a probe counted that branch returning a
  window zero times across the suite, because every fixture was really the
  *anchor's* case. Instrument which branch fired rather than inferring it from
  the answer.
- **A test that searches for a constant's value cannot find the code that names
  it.** A test asserting no other module contains `PRICE_MAP_FILENAME` passed
  against all three offenders, because the imported symbol evaluates to the
  filename while the code spells the *identifier*. Run the equivalent search by
  hand once and make the two agree.
- **Two green tests can contradict each other if neither drives the seam between
  them.** `nodes.execute` returns `_escalate(...)` on a refused commit while
  `EDGES` never listed `escalate` from `execute`, and `driver._next` raises. Each
  test was right about its own end. **Derive the table from the code** — `EDGES`
  is checked by parsing `nodes.py` for every `next_hop` each node can return.
- **Three maintained statements of one fact, none compared.** Both escalation
  branches were correct and covered; the edge table was correct and covered; the
  architecture document had drifted from both.
- **A value written in three places and read in none.** `ExecutorTurn.stopped`
  carried a comment saying the loop must not treat it as finished, and nothing
  consulted it. After adding a field, or deleting a component, grep for the
  *reader*.
- **Deleting a producer leaves its consumers guarded on a value nobody sets.**
  `context_tokens` and `cost_usd` came from scrapers of a tool that no longer
  exists, so `advance`'s truthy guard was never true and `stage-costs.md` — read
  by the planner on every call — stopped being written. A guard written to
  suppress noise suppresses the whole channel just as quietly.
- **A capability can go missing between two correct changes.**
  `build_executor_prompt` once opened a rework with one of two opposite framings.
  Moving feedback to its own turn was right; appending it as a bare turn was
  right in isolation; between them the framing was dropped and dozens of stages
  ran without it, with no error and no test. When a payload changes shape,
  enumerate what the old shape carried; the parts with no field of their own
  vanish.
- **A fixture reproducing an exclusion must be checked for whether it still
  excludes.** The first force-added the ignored files, making them tracked — the
  test would have passed by making the leak legitimate.
- **A reader that writes destroys the evidence it was about to look for.**
  `load_state` ran `CREATE TABLE IF NOT EXISTS` before reading, and the check for
  "is this the older format", written as "is our table missing", then saw both. A
  reader must not mutate, and a check naming what a thing *is* survives
  contamination that a check naming what it is *not* does not.
- **An inner loop that skips the file under edit is worse than none.** The
  in-session test command was built from `test_paths` alone, so a stage declaring
  none ran with no test at all — and the obvious fallback was half a fix, because
  most stages that declared paths *and* edited a test named a different file.
  Declaring something is not declaring the right thing.
- **The success path is the one that skips the tail.** `run_loop` returned the
  moment the gates came back clean, two statements above where cost was computed,
  so every attempt that worked first time was billed at zero — with three green
  tests whose fixture leaves by a different exit. Prefer one exit; ask which one
  the happy case takes. Seen again with a per-turn usage series: four exits were
  routed through one helper and the series was still empty, because the fifth
  exit is a constructor.

## Data, formats and classifiers

- **A classifier over rendered text cannot separate classes the text renders
  identically.** `_refusal_kind` matched words in a refusal message, fine while
  the message and the cause are the same thing — once three fallbacks rendered
  the same sentence it could not tell them apart *by construction*. The route is
  carried on `ToolError.kind` now.
- **A delimiter drawn from the content's own alphabet is not a delimiter.**
  `read_file` numbered with a field and *two spaces*, and indentation is also
  spaces, so a model quoting a line back into an `edit` quoted our padding as
  code — nearly two thirds of refused `old_string`s matched once two spaces were
  stripped. The diagnosis is the part worth keeping: three plausible stories were
  refuted by the **median gap between reading a file and failing to edit it being
  zero conversation items**. When a model appears to hallucinate a file's
  contents, measure how far back its source was.
- **A renderer written against a fixed set of names is a guess once the set is
  extensible.** `call_detail` named a call by the first of five field names —
  wrong in both directions on the first two operator-declared tools anyone wrote.
  The order comes from the config now, because the operator writes the
  identifying argument first, and then nothing in the renderer has to know what
  any argument *means*.
- **And the same feature broke the log's one-line contract, which no test held.**
  `run_argv`'s docstring said the joined form and the list "can only disagree by
  whitespace in an element" — true until an element could hold a newline.
  Collapsed for the log line only; `result.command` keeps it whole, because that
  is a record and the log is a rendering.
- **An operator's regex is data, and code must not depend on its spelling.**
  `failed_file_pattern` opens `^\s*`, and `^` in multiline mode can anchor on the
  blank line above, so `match.start()` sat on the previous line's break and the
  extraction returned `{}` — reading as "this runner prints no locators". Anchor
  on `match.end()`. **Measure before deciding, then fix the dependency rather
  than the instance:** correctness that depends on the next project's pattern is
  a defect waiting on a config nobody will check.
- **A conversation with batched parallel calls cannot be read positionally.** A
  reader pairing each call with the next output keeps only the last call of each
  batch. The transcript could not be paired either, because `_plain` built each
  line from a hand-written field list and `call_id` reached the record only on
  the outputs. It is a denylist now: a field nobody thought to add is invisible,
  a field nobody thought to exclude merely costs space.
- **A cache keyed on a string is keyed on its spelling.**
  `verify._recorded_answer` compares command text and HEAD rather than trusting
  the loop, which is exactly right — but the two sides built the path list in
  different orders, so pairs naming an identical set of files missed every time.
  `resolve_test_paths` sorts.
- **The same command in both places, spelled the same way.** An autocorrecting
  linter exits zero *after* rewriting files, so running it one way in the loop
  and another at the gate stops the exit code describing the artifacts. It also
  drains the gate: correcting in session means the model handles what has no
  autocorrection while the file is still in front of it.
- **And "spelled the same way" includes the identity it runs as.** One project's
  entrypoint ran an installer as root while the executor's declared tools ran the
  same operations as an unprivileged user against the same root-owned volume.
  Compare the user, the cwd, the environment and the stdin as well as the argv.
- **Deny-list the noise; never allow-list the signal.** A filter written for the
  shapes already seen drops the one nobody has seen — filtering test output to
  first-party paths would have discarded the only new warning class.
- **A stream is not a log.** Warnings on a process's stderr can never appear in a
  file written by an application's logger. Ask which writer owns a file before
  assuming anything can reach it.
- **An empty final turn reads as success.** A model returning `end_turn` with no
  text and no tool calls is, to the loop, a model that has finished — one attempt
  made a hundred searches, applied no edits, and recorded `ok: true`. Only the
  absence of *any* content separates finishing from giving up.
- **A stop the operator asked for must not render as a failure.** `pause` exits
  **1** and logs under `[escalate]` beside a message saying nothing is wrong, so
  a watch keyed on the tag announced an escalation and the harness reported the
  run as failed — both reading the only two channels a monitor can read
  unattended. An intentional stop wants its own exit code and its own tag.

## Operating a run

- **A write that grows a file in place can be read at its old length.**
  `Path.write_text` rewrites the same inode, and Docker's file sharing caches a
  stat nothing invalidates: a comment edit made a `Gemfile` longer, the container
  kept reporting the *old* size while serving the *new* bytes, and bundler
  resolved without two dependencies and wrote that lockfile back to the host. Two
  runs died hours apart, armed by a comment — all that mattered was that the file
  got longer. **Put a new inode at the path:** `atomic_write` — sibling temp
  file, `os.replace`. Whenever a tool of ours writes a file another process reads
  across a boundary we do not control, the question is whether the *name* now
  points somewhere the reader has never looked.
- **A fingerprint written before the work it stands for makes a failure
  permanent.** A bring-up stopped its container when a manifest hash moved and
  wrote the new hash in the same breath, so an install that could not succeed was
  recorded as done and every later call did nothing. It *suppresses the retry
  that would have fixed it*, silently, because "no change" is indistinguishable
  from "nothing to do".
- **A state predicate is not a completion signal.** A bring-up polled `bundle
  check`, which the entrypoint asks once *before* installing and bundler takes no
  lock for, so the answer flipped partway through. **And a remedy stacked on a
  misjudged state manufactures the fault it was written to recover from:** the
  restart killed the install. What replaced it is the entrypoint's own handoff —
  under `bash -e` it installs and only then `exec`s, so PID 1 is the entrypoint
  until the install succeeds. Ask whether a readiness check reads something
  finished when the work is finished, or merely *becoming* true in the middle.
- **Liveness can be read without a race, if you can name why.** Answerable here
  for two reasons that had to be established: `docker compose start` returns with
  the container already running, so any later `exited` is a new death; and no
  service declares a `restart:` policy, so `exited` is terminal. Both are written
  beside the check as conditions to recheck. A bounded wait is the fallback for
  when you *cannot* name the ordering.
- **A check that loads part of a thing has certified part of it.** A declared
  `bundle_install` proved the app boots in the test environment, which loads only
  some groups, so a resolve that moved a development-only gem reported exit 0 on
  a bundle the app container could not boot. **Loading the file is the only
  instrument that separates a gem's metadata from its source.** The group list
  comes from bundler rather than hand-written, because the manifest had a group a
  hand-written list would have skipped.
- **Watch the process, not only its log.** A filter over log lines cannot see a
  process that stopped emitting them, and a monitor killed for volume looks
  exactly like a quiet one — a run sat dead for 78 minutes while being reported
  as healthy. Two watches, never one: liveness on the pid, and a narrow filter
  for rare events. Never mix a per-cycle signal into the rare-event filter.
- **A watch that exits on the process must re-read the log after it.** `while kill
  -0 $pid; do grep …; sleep 30; done` leaves by its *condition*, so a run that
  escalates and exits inside one sleep is reported as "exited with no escalation"
  over a log containing it.
- **A monitor over an append-only log must be anchored to this run.**
  `last-run.out` is appended across every resume, so a grep for a pause matched
  two dozen historical ones, and a `pgrep -f "code-gantry resume"` in the same
  loop matched the loop's own command line. **And that applies to reading it, not
  only watching it** — timestamps repeat daily and stage numbers restart every
  run, so an unanchored lookup gives a plausible wrong answer. Take the line
  number of the run's own header and read forward; per-run directories under
  `runs/` carry no such ambiguity.
- **A watch must print the anchor it is using.** `A=$(wc -l < file)` carries
  leading whitespace on macOS, so the assignment failed and `tail -n +$((A+1))`
  read the whole file. The anchor is itself a measurement and can be wrong.
- **A sampled window over a growing log is a lottery, not a watch.** `until grep
  -q "<phrase>" <(tail -c 5000 <log>)` never fired though the phrase was written
  every stage, because multi-kilobyte lines scrolled it out between samples. Wait
  on a state that persists — a pid, a file that appears, a line count taken at
  the start. And keep the set enumerable: `ps` answers "what am I running".
- **The run log is not where a failing suite's failures are.** Head-and-tail
  truncation drops the middle, and a test runner puts its dots at the head and
  teardown at the tail, so the summary naming failed examples is what goes.
  Preflight writes no artifact when it refuses, either. Re-run the suite — and
  grep verdicts case-sensitively as `[FAIL]`, since the other tags are lower case.
- **Two runs on one repository is a five-minute window, not a crash.** A resume
  believed killed was still live when a fresh run started a minute later; nothing
  collided only because the second was still in its planner call. `code-gantry
  pause` is checked after derivation and before `precheck`, which is what makes
  stopping safe: it holds the derived stage and never touches the tree. "I killed
  it" is a claim to verify with `ps`.
- **And killing a run does not kill what the run started somewhere else.** The
  test commands reach the work through `docker compose exec`, and killing that
  client kills the client: a suite stopped mid-flight left workers alive holding
  connections, and the next run's test setup died on "there are other sessions
  using the database". Aim the cleanup narrowly — a pattern broad enough to catch
  the workers is broad enough to catch the entrypoint.
- **The progress log sits inside the cached prefix and changes every landing.** It
  is spliced into the plan block, which carries a cache breakpoint, so each
  landing invalidates and rewrites the whole block — the log drags the plan tree
  through the cache with it, at a cost proportional to landings since the last
  fold, so the total is quadratic in the gap. Folding is not housekeeping; it is
  the largest single lever on the run's bill.
- **Nothing can be omitted after a tool call, so the lever is what you send
  first.** The API is stateless; caching changes the price of resent tokens, not
  whether they are sent. Block 0 is append-only across derivations and almost
  entirely plan documents, re-read once per tool turn *inside* one derivation, so
  the only lever with that magnitude is a smaller plan — and the risk of moving
  it behind tools is that a planner asked to fetch it will fetch it.

## Where things live

`nodes.py` holds the loop's decisions — which failures route to the executor,
which to the planner, which to a human. `verify.py` is the layered gate, ordered
cheapest-first, short-circuiting. `config.py` is the safety story: the capability
partition and the command denylist. `prompts.py` is pure string building, kept
apart from the clients so it can be tested without a model.

`state.py` documents why `completed` and the two retry counters are shaped as
they are, and holds the reset helpers — anything a resume or a landing must
clear belongs there rather than inline, because inline has no seam to test at
and a value that is never cleared is invisible until it reaches an operator.

`runtime.py` assembles the collaborators and binds whatever the model clients
need from the run; values wired there have no unit test on either side, so they
get an end-to-end one. It also holds `pin_modules`, the reason this codebase can
be edited while a run is live: several modules are imported inside functions to
break cycles, so a module not yet loaded was read from disk when first needed,
putting new code in front of an old class in memory — and the same edit was
harmless whenever the module happened to be cached, so every time it worked
taught the wrong lesson. After `pin_modules` returns, a live run finishes on the
code it started with.

`dialects.py` replaced role-decides-wire. Two dialects, RESPONSES and MESSAGES,
and `dialect_for(model)` maps a model family to one — answering RESPONSES for a
family nobody has classified, because a router can resolve to anything and an
unknown model must not end a run. A dialect owns every spelling that differs
between the endpoints: structured-output kwarg, effort kwarg, text-block type,
cache markers and TTL, request cache options, the cache-key parameter, tool
schemas, reading tool calls, echoing the model's turn, shaping tool results,
stop detection, refusals, closing text, splitting the system prompt, the
base-URL suffix, usage normalisation, and building the client. **Shape belongs
to the endpoint, not the vendor** — the same Google model returns
`function_call` items on Responses and `tool_use` blocks on Messages, and on the
Messages wire is reached through the Anthropic SDK. `normalise` translates at
`send`, rather than at the seven places that construct blocks, because the
eighth will not remember.

`gateway.py` is what OpenRouter needs, decided from the endpoint host rather
than declared in config — `session_id`, `provider.require_parameters`, and the
effort spelling. It also holds `resolve_policy`, which turns a routing policy
into the model it picks today with one throwaway call, because nothing reports
what a router *would* choose. `wirecheck.py` warns when a role's model wants a
wire that role cannot speak; only the executor is wire-polymorphic.

`executorclient.request_extras` is the single assembly of every top-level
keyword the executor's call carries. It is one function because two outages were
a keyword the endpoint does not take, and both were then pinned by a test that
rebuilt the dict by hand — a copy of an assembly is not a check on it. An AST
test asserts the loop adds nothing beside it.

`executorclient` also holds `REPEAT_NUDGE_AT` and `REPEAT_ABORT_AT`, bounding
consecutive tool calls with byte-identical arguments. At the nudge the answer is
*replaced* by a sentence naming the tool and the count, because a model that has
ignored a payload twice will ignore it under a warning and re-sending it is most
of what the turn costs. At the abort the turn ends with `repeated_call` set,
which travels to `ExecutionResult`, into `executor-loop.json`, and through
`nodes.execute` into `executor_note` — the one stop for which that note is
published on a first attempt, because every other stop on that branch is
something the scope gate can fairly summarise as "produced no changes" and this
one is not. **When an attempt reports no changes, read `repeated_call` before
believing the stage was badly drawn.** The guard is on the executor only; a
ceiling added where nothing is hitting it is a policy nobody chose.

A withheld call goes on the reader's ledger through `record_refusal` with
`kind="repeated"`, because `tools.log`, `tool_counts` and `refusal_counts` are
built from those ledgers and only `dispatch` writes to them. **Anything that
answers a tool call without dispatching it owes the ledger an entry**, or the
guard renders as two calls and then silence.

`gates.py` is the layer shared by the executor's loop and `verify.py` —
patterns, residue, new tests, checks, tests — so the two cannot select different
test paths, which they had done for five separately-incident-shaped reasons; it
returns the selection *sorted*, so the loop's record and the gate's question are
the same string whenever they are the same set.

`flake.py` decides whether a red suite is the stage's fault or the suite's and
writes `flakes.jsonl`: one append-only record per excusal carrying the file, the
seed, the runner's own locators, and which run and stage — or `origin:
preflight` — produced it, which makes "which flake is worst" a sort rather than
a log scan.

`edittools.py` is the write-side counterpart to `repotools.py`: no model,
refuses with `ToolError`, records what it did. `executorloop.py` is the cycle —
edit until the model stops asking, lint, **commit, then test** — and
`executortools.py` and `executorclient.py` are its schemas and its provider
call. `repotools.number_lines` is the single renderer of numbered source; three
copies of that format string is how it drifted while every test stayed green.
`repotools.Spend` is everything mutable about a read budget in one object, so
clearing it is replacing it; `count_calls`, `count_refusals` and `render_counts`
are the one summariser all three roles report through, after each grew its own.

`addendum.py` is where a planner note is *routed*, not merely formatted:
`LOGGED_KINDS` decides what reaches the progress log and therefore every later
prompt, and `append_findings` writes the rest to `findings.md`, uncommitted and
read by nobody. The filter lives in `append_notes` rather than at the call site,
for the reason the transcript is a `list` subclass.

What the planner is sent is overwhelmingly the plan itself, then the
repository's agent and operations documents and the layout, with everything
about *this run* under one percent. Two channels were removed to get there and
both were the planner reading its own prior output: the deferral list, and a
tail of `status.md`. `status.md` is still written and hard-capped at 4,000
characters; nothing reads it back.

`projecttools.py` is the menu an operator adds to the built-in tools:
`ProjectTool` declares a name, a description and an **argv list**, and the
executor calls it as it calls `read_file`. Argv and never a shell is the whole
safety story — a model-supplied value is one inert element — and a placeholder
must occupy an entire element, which stops a value being interpolated into a
larger string. An operator who writes a shell into their own config has chosen
that; the protection is on the model's arguments. Nothing gates which stage may
call which tool, deliberately: the scope gate measures the outcome from the
tree, and a per-stage permission would be a claim used to predict what an
existing gate observes.

**A feature's original role is the one that never gets scoped.**
`project_tools` was built for the executor, so `executortools` took the whole
declared list by history rather than by decision; wiring only the *new* roles
through a `roles` field would have left the founding role reading everything,
and would not have failed loudly, since the tools most likely to belong
elsewhere are read-only. **Scope where a thing is reachable, not where it is
advertised** — a filter over the schema is not a constraint on the dispatcher,
and a model can name a tool it was never offered. Both go through one selector,
`for_role`, and neither caller may hand over a pre-scoped list, because a caller
that could pass the wrong scope makes the wrong scope expressible.

`pricing.py` turns token counts into dollars from a table nobody here maintains.
`price_map_path` is the single selector for where the cache goes — under
`work_dir`, gitignored by construction, and `None` rather than a cwd-relative
fallback, because "somewhere arbitrary" is what cost a run. `project_entries`
keeps only the models `configured_models` names, entries whole: projecting by
*key* would be a hand-written subset of an upstream schema. `cached_price_map`
is the one memo, so a caller cannot fetch per landing.

`configversion.py` replaced `approval.py`: a config is identified by its git blob
sha, recorded at run start and checked on every resume, so an edited config
refuses to continue a run rather than needing a command run against it.
`ProjectPaths` is built from `cfg.work_dir` and there is no slug — the work dir
is the project's identity, which is what the prompt cache key needs.
`cachekey.py` bounds that identity to the provider's 64 characters.

**A project's config lives in the repository it describes**, beside the plan,
with `.code_gantry/` gitignored next to it for everything the run writes.
`target_repo`, `work_dir` and `host` are absent from it: the first two are
derived from where the file was read, and the third became somebody's hostname
the moment the file was tracked. `env_file` names a credentials file, resolved
against the config's directory, parsed rather than sourced, applied with
`setdefault` so the shell wins.

`executor.py` is now only what shapes an attempt before it starts — the read
budget, the excerpts, the conventions — plus `run_script_stage`.

`scripts/smoke.py` stands up one HTTP server for all three roles and no binary
on `PATH`. It used to plant a stub executable, back when the executor shelled
out; keeping that would have driven a code path production no longer takes. A
test asserts the stub is gone, because that is the sort of thing that grows back.
