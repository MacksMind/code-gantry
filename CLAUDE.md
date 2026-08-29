# Working on this codebase

[README.md](README.md) is how to use CodeGantry.
[docs/archive/rewrite-plan.md](docs/archive/rewrite-plan.md) is a closed
record of the rewrite that produced the current shape. Nothing in it is
outstanding; it is kept for its measurements, and is not a status table to
check before starting work.
[docs/architecture.md](docs/architecture.md) is the design authority on why it
works this way.
[docs/future-work.md](docs/future-work.md) is the open counterpart to the
archive: decisions that are still outstanding, each with the evidence for it.
None of them is repeated here.

This file is for whoever is *changing* the code. It records the invariants that
are easy to break without noticing, and the rules that were learned by breaking
them.

**It is also the only durable channel.** A session's findings can be written
into a compaction prompt, which carries them one hop and then ages out, or into
an agent's own memory, which is keyed on this directory's absolute path and
fails silently if the project is ever renamed or moved. Neither survives the way
a tracked file does. So when a long session ends — and a request for a
compaction prompt is the natural moment, because "what is worth keeping?" is
already the question being asked — the first move is to diff what was learned
against this file, not to summarise it somewhere more convenient. Anything that
belongs in project instructions and only reaches a summary is lost on a horizon
nobody is watching.

Run the tests with `uv run pytest -n auto`. They are fast and need no network —
there is no reason not to run the whole suite. `-n auto` is several times
faster on the whole suite, and the suite is the inner loop of working here,
which is why `pytest-xdist` is a dev dependency rather than a nicety. It is not
in `addopts` because a single-test run pays worker startup for nothing; add it
whenever you are running more than a file.

The suite must pass in a shell with the run's credentials loaded. That sounds
obvious and was not true: a test asserted `OPENAI_API_KEY` was absent from a
subprocess environment the subprocess had *inherited*, so it failed for anyone
who had configured the tool — the one environment where the suite most needs to
be trustworthy. A test about what we pass to a child must clear the variable it
is asking about, or it is asking about your shell.

## Invariants

**The planner may never author an executable field.** Enforced twice: the
structured-output schema has no field for a command, and `PLANNER_WRITABLE_FIELDS`
filters the response against an allowlist. `tests/test_config.py` pins it. Adding
a field to `Stage` means deciding, deliberately, which side of that line it sits
on — and a field the planner may not set should be impossible for it to return,
not merely discouraged.

**And the planner may not author code.** The same partition one field along,
and the harder half to see, because authored code travels perfectly well inside
a declarative field: "replace this block with exactly this block" names nothing
executable and is still the planner writing the diff. Measured over one run of
48 stages and 11 rejections — five conceded the behaviour and rejected the
*shape*, because every line of an instruction is a reject criterion; one stage
deadlocked because an authored edit stops being satisfiable once part of it is
already true on the branch; and one hand-rewrote a whitelist, faithfully
preserving an entry that named a column the table did not have, inside a stage
drawn to fix entries of exactly that kind. A planner that cannot run anything
cannot check what it writes, and an executor handed a replacement can only
transcribe it.

The fix is a format that cannot express the mistake, not a rule asking for
restraint: quoting goes through `read_excerpts` — a path and a range, read at
`stage_start_sha` — and **a reference can only point at code that already
exists**, so there is no way to write an after-image with one. A fenced block
in `instruction` is rejected by `validate_stage`; inline backticks are left
alone, because naming an identifier is a property and a rule that caught it
would be routed around rather than followed. The corollary is that an excerpt
is no longer help: it is the only code the executor gets, so a range that will
not resolve now fails the stage to the planner instead of being skipped.

**`completed` is append-only.** It is the cacheable prefix of both paid prompts.
Renumbering, reordering, or rewriting an entry in place silently multiplies the
cost of every subsequent call, and the damage is invisible in behaviour.

**Prompt ordering is the caching strategy, not presentation.** Static content
leads, churning content trails, breakpoints go between. Providers differ in ways
that matter: Anthropic extends the longest matching cached prefix, so an
append-only region before a breakpoint gets cheaper over time; GPT-5.6 caches at
an explicit breakpoint and does *not* fall back to longest-prefix, so the same
arrangement misses entirely there. The breakpoint *budget* is not the
difference — both allow four; prefix extension is. So a growing region belongs
before a breakpoint on one provider and after it on the other, and the same
document can need opposite placement in the two prompts. Measure after changing
any of this. Documented provider behaviour has been wrong twice.

**There is no push, and stages land by squash merge.** The safety guarantee is
that no method exists which could push. Squashing is what makes "every commit on
the project branch is green" and "the executor commits before it tests" both
true.

**A stage lands completely or not at all.** `advance` mutates in four steps and
a failure in any of them used to leave the repository part-way through — a
staged merge stranded on the project branch, or a plan note uncommitted in the
worktree, which the next resume then reported as the executor editing the plan.
`squash_merge` records where the branch was and restores it; `advance` unwinds
the note. Anything added to that sequence inherits the obligation, and the
rollback stays silent: the original failure is the diagnosis and must be what
reaches the caller.

**Whatever the planner draws from, the executor may not edit.** The planner
chooses `edit_files` *and* reads the plan, so without a guard it can put its own
inputs in scope and have the executor amend the instructions it will be judged
against next cycle — goalpost drift with a green suite behind it, unattended.
`_is_plan_document` is that guard, and every input added to the planner's prompt
belongs in it. It already covers the plan tree, the progress log, and the
repository's own agent-facing documents; a document that says what the machine
can do is the most important of the three, because a stage that could edit it
could retire its own constraints.

**A gate must be able to reach what decides its verdict.** The reviewer had no
tool access for most of this project's life, on the reasoning that it judges a
diff and a diff is what it is shown. But a diff does not always carry the fact
that settles it: a stage that deletes a declaration is safe exactly when
something elsewhere still covers what the declaration did, and that file is not
in the diff. Measured on one run of 31 stages, 8 were deletions of that shape,
and every one was approved. Not wrongly — the reviewer could not have said
anything else, which makes those approvals the stage instruction restated in
its own voice rather than a check on it. A checkpoint that cannot reach its
evidence produces verdicts that are indistinguishable from judgement and are
not judgement, and the artifact reads the same either way. Recording what a
gate *looked at*, not only what it decided, is what makes the difference
visible afterwards.

That last sentence went unapplied for a year in the one place it mattered most.
Preflight is the only gate that can wave a *red repository* through — it
adjudicates the opening suite exactly as the merge gate does, and a flake
verdict lets the run start — and it was the only gate keeping no artifact at
all. It excused a red suite reporting `(unnamed)`, meaning the extraction had
found no locator to name, and the bytes it decided on were parsed and dropped:
`last-run.out` held 63 lines after the run header and not one `Failed
examples`. An hour later "the runner printed no locators" and "the pattern
stopped matching" were indistinguishable, and the second had a change in it
from that morning. A gate that reaches its evidence and then discards it is one
outage away from being a gate that never reached it. The parse now goes in
beside the bytes, because `(unnamed)` against output full of locators and
`(unnamed)` against output with none are different failures that render
identically — the classifier problem again, at the level of an artifact rather
than a label.

**A record of the work must be written after the work.** The progress log is
what every later planning pass reads back as history, and for a long time its
entries were produced by the planner *while deriving the stage* — before the
executor had run — held, and published on landing under "observed while
landing". One said a controller "now permits" two fields "with a two-shop
controller-spec example reading the values back from the database", written
before a line of it existed. The stage happened to deliver it. The log had no
way to know that, and a stale premise published on landing becomes the account
of record for every call afterwards. Those notes are still worth having — the
moment you read the plan against the code is exactly when you find the plan is
wrong — but they say "observed while planning" now, and what the stage *did* is
recorded separately by the reviewer, which is the only participant that has
seen the diff. Anything written before the work is a prediction, whatever tense
it uses.

**But an event is recorded as it happens, and only the rule above makes that
sound like a contradiction.** The two are about different things: a *claim*
about what a stage did must wait for the stage, while the record of what was
actually exchanged is only ever a transcription and has nothing to predict.
`executor-conversation` was a JSON array — a container that can only be written
whole — so it was produced on the way out, which means a forty-minute attempt
had nothing to read for thirty-nine of them and an attempt that never returned
left no record at all. That is precisely the case a record is most wanted for.
It is `.jsonl` now and each item is appended as the conversation grows;
`sent-prompt.md` is written before the first call, because everything in it is
known then. `executor-loop.json` stays a single write at the end, because
totals are the one thing there that is not true until the attempt is over. Ask
of any artifact whether it is a sequence or a summary: a sequence written at the
end is a summary with extra steps, and it is missing exactly when it matters.

The mechanism is worth copying too. The transcript is a `list` subclass that
mirrors each append to disk, rather than a callback threaded through the four
places that append — because those places are in two modules, a fifth is one
refactor away, and a record that has to be *remembered* at each of them is the
shape of thing that has already gone quietly missing here between two correct
changes. Make the recording a property of the only operation that can change
the thing, and no caller can forget it.

## Rules that cost time when broken

**Project knowledge belongs in config, never in code.** This includes
model-facing strings. A tool description reading `e.g. app/controllers/order_controller.rb`
is a Rails hint shipped to every project's planner. Regexes that identify a
failing test, a seed, or a file are properties of a project, which is why several
have no default at all.

This one is easy to break while writing prose rather than code, and hard to
notice afterwards: a paragraph of guidance illustrated with the vocabulary of
whatever project is in front of you reads as helpful and ships one migration's
shape to every reviewer. It is worth a test that fails on the names — a
framework, a file extension, a directory prefix — so the rule is pinned rather
than left to the judgement of whoever edits the string next.

**Config should hold the path, not the copy.** The corollary, and it cost more
than the rule itself. Where a project already maintains a document saying how it
works, transcribing its facts into config creates a second copy that drifts —
and the copy is the one the pipeline reads, so the maintained original is right
and ignored. Point at the file instead and read it at the run's sha. Observed:
a repository's agent-facing document recorded a capability, the hand-written
config guidance omitted it, and the plan asserted the opposite; the false claim
gated five items across two streams, and nothing found it because an item that
reads as blocked is never attempted.

**Assert state, not change.** `forbidden_patterns` reads the diff's added lines
and catches what must not be *introduced*; `must_not_remain` reads file contents
and catches what must not *survive*. They read almost identically in prose and
are opposites in a diff. The same principle governs progress notes, which state
totals and never deltas — a requirement phrased as "make this change" becomes
unsatisfiable the moment the change is already true, and a stage burned its whole
budget on exactly that.

**The tool states its own behaviour.** The editor normalises final newlines and
line endings on every file it writes. No model chose it and no instruction
prevents it, so CodeGantry says so once — in the reviewer's prompt, and by
hiding line-ending churn from the diff it judges — rather than letting every
planner rediscover it by burning a rework budget. Anything the shipped machinery
does is the tool's to declare, not the operator's to work around.

**Test end to end wherever a value crosses a schema boundary.** Four separate
defects have been values computed correctly, written correctly, and lost in
transit: dropped by a `RunState` schema that did not declare the key, zeroed by a
reset spread over the top of them, or omitted from the artifact that was supposed
to prove they existed. Every one passed its unit tests on both ends. If a value
travels from a node through state to a prompt, test that journey and not just its
endpoints.

**A failure that opens a retry sequence is the diagnosis; the ones after it are
consequences.** Overwriting the first with the last turned a legible
`ArgumentError` naming a file and line into "the executor timed out", and the
planner reasoned correctly from the wrong failure for thirty-five minutes.
`opening_failure` now claims the first one write-once per stage or revision, and
it is claimed on the *retry* branch as well as the two that reach the planner —
that is where the diagnosis was actually being lost, because the real failure
retries, the retries stop changing anything, and only the guard that noticed
that ever reached the planner.

**Read artifacts; do not regex them.** Repeatedly, a pattern has produced a
confident wrong answer that a two-line read would have settled: `\s` is not valid
in POSIX ERE so `git grep -E` silently matches nothing; `File\.exists?` matches
the already-converted `File.exist?` because `?` quantifies the `s`; an indented
`order:` inside a constant hash is not an association option. When a count or an
absence is load-bearing, open the file.

**And check what a command actually returns, not what it plausibly returns.**
The same failure one level down. `git show <sha>:<path>` on a symlink returns
the link's target — a path, not the file it names — so reading a symlinked
document as content yields a document whose entire body is a filename. It would
have shipped into a cached prompt prefix on every call, and the test that caught
it used two real files with identical contents, which is not what a symlink is.
Reach for the real shape of the input before writing the test that stands in for
it.

**A guard can be unreachable for the shape its failure actually takes.** One
level below the rule above. `planner.py` carried a `stop_reason == "max_tokens"`
check with a clear message about a truncated verdict, and it had never once
fired: the SDK parses structured output *before* it returns, so a response cut
off mid-JSON raises inside the call and lands in the generic handler. What an
operator saw for an answer that ran out of room was a pydantic dump. The guard
reviews as correct and is dead code, and only a live failure can show it. When
adding one, ask which layer actually raises first — a check placed after the
parse cannot see anything the parse rejects.

**Separate what was found from what should happen next.** A reviewer blocked a
stage for a difference it had correctly noticed and whose consequence it had
not checked; the observable outcome was identical, because another mechanism
already supplied the value. The judgement was right and the routing was wrong,
and those are two decisions rather than one. A finding with no nameable
consequence belongs in the record, where a human can weigh it; rejecting on it
costs a rework cycle and returns the same diff. The corollary is the harder
half: before withholding approval over a consequence, verify the consequence —
naming one is not establishing it, and a model asserting an unchecked effect is
the same failure as a regex producing a confident wrong count.

**Provider shapes come from the installed SDK, not from recall or from docs
pinned to another version.** Two facts that only a live call produced: tools
must be declared `strict` or structured output will not auto-parse, and a
reasoning model's tool call must be echoed back together with the reasoning
item it declares as required. Neither is visible to a stub, because a stub has
no reasoning item to omit. Read the installed types — they are generated from
the provider's own spec and are on disk — and treat a documentation page as
weaker evidence than the package you are calling.

**Report a measured non-result as a non-result.** Two prompt changes were made
to get the reviewer to record out-of-scope findings, and a controlled before
and after over the same 31 stages produced the same verdicts and zero findings
both times. The temptation is to describe the mechanism as working because it
is now *capable*; capability and effect are different claims and only one had
evidence. The same discipline applies to sampling: a replay's rejection rate is
measured on stages that already passed review once, and tool-use counts on
identical inputs varied from 0 to 21 between two runs — so a single sample
showing a model "reading where it mattered" is a story, not a finding.

**Ask what else already carries it, not whether the content is good.** The
planner is fed four channels that each describe a landed stage — the
completed-stage history, the live progress log, `stage-costs.md`, and the
status tail — and the history block was reproducing what three of them already
said. 296,783 characters at 45 stages, ~1,650 an entry, resent on each of ~15
tool iterations and growing ~6,600 characters per landing; together with the
log, over half of what a planner call paid for was spent reading its own prior
output. Removing what had another home left 4,278 characters, 99% smaller.

The trap is that every field in it was individually defensible, and the first
fix proposed was to swap the instruction for a better-written summary — which
would have reintroduced the fault with nicer prose, because that summary is in
the progress log and the log is fed live on every call. Before adding anything
to a prompt, or improving something already in one, find out whether it is
already arriving by another route. `status.md` is the one channel that was
built with this in mind; it is hard-capped at 4,000 characters.

**An optional field is answered with nothing.** `observations` gives the
reviewer somewhere to report a real problem this stage did not cause. It came
back empty **278 times out of 278**, across two prompt revisions written
specifically to encourage it. Over the same period the planner filled the same
log 630 times from a field it is always expected to produce. The difference is
not diligence, it is that an optional field with a conditional trigger can
always be declined in good conscience, and a required one with an
always-answerable question cannot. If output is wanted every time, make it
required and ask something that is true every time — "what does this change
do", not "did you notice anything else".

**A check may write, and only the child branch should carry it.** `checks` is
arbitrary operator-declared shell, and the useful ones often fix as well as
report: `rubocop -A`, `eslint --fix`, `gofmt -w`. The executor commits its own
work *before* verify starts, so nothing else in the loop commits what a check
changed. Left uncommitted it is swept up silently when the stage lands and
orphaned when it does not — and then the *next* stage's precheck refuses to cut
a branch over changes it cannot attribute, which stops the run on a stage that
has nothing wrong with it. `checks_commit_changes` puts it on the child branch,
where the quarantine already promises it is squashed on landing or discarded
with the branch.

**Prefer the fact to the label.** Twice in one session the tempting fix was to
have a model *declare* something the repository already knows — a stage naming
the plan item it advances, so the folding pass could tick a checklist. But a
declaration is a claim made before the work and is exactly the class of thing
that produced the prediction problem above, while the squash commit already
carries the stage id, the instruction's first line, and the diff. `git blame`
on the log answers it from facts that cannot be wrong. The addendum's own
docstring made this argument first, about shas and timestamps: embedding them
in append-only prose turns them into "claims about history that history had
invalidated".

**A prompt that describes a capability must be generated from the thing that
grants it.** `PLANNER_SYSTEM_PROMPT` asserted the executor "cannot run
commands" — true of every project until `project_tools` shipped, false the same
day for any project declaring one. The capability paragraph is built from
`cfg.project_tools` now; a project declaring none reads what it always read.

**A phantom constraint is the expensive direction.** A missing capability
produces a stage whose premise the code contradicts, and the gates catch it. A
constraint that does not exist makes work read as *blocked*, and a stage never
drawn leaves no artifact for anything to find wrong. A stream of dependency
work went undrawn on the strength of one sentence in our own prompt, and
surfaced only because the planner is asked to report contradictions in the
plan. The same reasoning put *wrong dependency* into `PlanNote.kind`: the plan
states what depends on what, the code decides whether that is true, and a
prerequisite that does not exist can hold an item closed for a project's life.
**And the same sentence goes stale twice, in two prompts, for one reason.**
The rule above was written about `PLANNER_SYSTEM_PROMPT`; the identical claim
was sitting in `_conventions_block`, telling the executor a procedure "is never
something for you to carry out" because "you cannot run commands". True of
every project until `project_tools` shipped and false since for any project
declaring one — and a model can see its own tool schema, so a reason it can
observe to be false is worse than no reason at all: it invites discounting the
instruction the reason was attached to. The instruction was right the whole
time. It was found by grepping every model-facing string for claims about the
machinery while adding a capability, which is the pass this file already
prescribes and which nothing runs on a schedule. When you fix a stale
capability sentence, search for its *paraphrases* in the other roles' prompts
before closing the task; one component describing another from memory is
rarely a single site.

**A feature's original role is the one that never gets scoped.**
`project_tools` was built for the executor, so `executortools` took the whole
declared list by history rather than by decision. Adding a `roles` field and
wiring the *new* roles through it would have scoped two of three and left the
founding role reading everything — and it would not have failed loudly, since
the tools most likely to belong elsewhere are read-only. When a capability
grows an audience, check the caller that predates the audience existing.

**Scope where a thing is reachable, not where it is advertised.** A filter over
the schema is not a constraint on the dispatcher, and a model can name a tool
it was never offered. Both go through one selector, `for_role`, with the same
role argument — one function evaluated twice rather than two filters that can
drift — and neither caller may hand over a pre-scoped list, because a caller
that could pass the wrong scope makes the wrong scope expressible.
**A renderer written against a fixed set of names is a guess once the set is
extensible.** `call_detail` names a call by the first of `path`, `pattern`,
`glob`, `question`, `ref` that it finds — exact for the five built-in read
tools it was written against, and applied to operator-declared tools it went
wrong in both directions on the first two anyone wrote. A search taking
`(gem, pattern, glob)` was logged under its *pattern*, so the ledger could not
say which dependency had been searched; a read taking
`(gem, file, first_line, last_line)` matched nothing in the list at all and
logged with no detail whatsoever. The fix takes the order from the config,
because the operator writes the identifying argument first — that is how a
signature reads — and then nothing in the renderer has to know what any
argument *means*. Whenever a fixed list of field names meets a structure an
operator can extend, the list stops being a specification and becomes a bet.

**And the same feature broke the log's one-line contract, which no test held.**
`run_argv` joins argv for the log line and its docstring said the joined form
and the list "can only disagree by whitespace in an element". True until an
element could hold a newline — a declared tool written as `sh -c '<script>'`,
which is how an operator resolves something before reading under it while
keeping the argv property, because the model's values arrive as positional
parameters and are never interpolated into the script. One call put **five
lines** into a timeline that is one line per event, with the `exit 0` attached
to whichever fragment came last. Collapsed for the log line only;
`result.command` keeps the command whole, because that is a record and the log
is a rendering. Worth noticing that the docstring stated the invariant
correctly and nothing enforced it — a sentence describing a format is not a
test of it.

**A ledger that records refusals and not successes lists only the failures.**
The executor recorded a refused declared call on the editor's ledger and an
answered one nowhere, so a tool that ran and returned appeared in no tool log,
no per-cycle count and no budget, while the same tool failing showed up. This
is the reverse of the usual shape here and reads as harmless — the error path
is the one people remember to instrument. It is the same defect either way: two
different things rendering identically, and the missing half is whichever one
nobody wrote down.

**Improving a tool's answers cannot make anything reach for it more often.**
There is no memory across runs: a model decides whether to call a tool from the
description in front of it and nothing else. The index decides what a call is
*worth*; the description decides how many calls *happen*. So the two cannot
confound each other, and an experiment designed to separate them has nothing to
attribute.

**A usage rate is not a verdict on a tool's value, and rank is not the signal.**
Semantic search sat at 0.9% of calls while returning five 2007-era migration
filenames out of six hits. Excluding `db/migrate` and an archived log turned the
same question into the chain it was actually asking about — top hit moved 0.696
to 0.679. The win is in what loses.

**The index's worst contaminant is the pipeline's own output.** Successive
exclusions of `docs/*/progress_log.md`, `docs/*/*` and `.claude/*` took our
artifacts from 6 of 42 hits to **0 of 48**, top scores unmoved. One displaced
hit was a reviewer summary of a stage that had landed twenty minutes earlier,
ranked above the model the executor was asking about. Two cautions: the
exclusion list lives in the *target repository's* indexer, so nothing here pins
it or notices a regression; and a bad query stays bad — one that was a stage id
plus loose keywords still matches unrelated classes on a single shared token,
because it was looking for a document rather than asking about code.
**A tool reads more than you hand it.** The subprocess editor this project
once shelled out to scanned the message *and its own reply* for anything
path-shaped and attached the file, auto-answering the prompt; no flag disabled
it. Putting a conventions document — dense with paths — into that message
attached the route file, the schema dump and the rest, reaching 258,854 tokens
against a 229,376 limit, so every attempt died in three seconds having written
nothing and the run looped. Its read-only channel was never scanned, which is
where the document went. The general lesson outlives the tool: a third-party
tool's behaviour is a property of its source, not of what a sensible tool would
do, and this was found by grepping that source after two wrong theories.

**The same command in both places, spelled the same way.** `rubocop -A` exits
zero *after* rewriting files. Run it one way inside the executor's loop and
another way at the gate and the exit code stops describing the artifacts — one
place reporting clean while the other has changed the tree underneath it. It
also drains the gate: correcting inside the session means the model handles
what has no autocorrection while the file is still in front of it, and verify
finds nothing left to do. Measured cost of not doing this: two consecutive
stages, one cop each with no autocorrection, three or four attempts apiece and
one planner intervention, to communicate a one-line change.

**And "spelled the same way" includes the identity it runs as, which is not in
the spelling at all.** One project's container entrypoint ran `bundle check ||
bundle install` as root on every start, while the executor's declared
`bundle_install` and `bundle_update` ran the same operations as `hostuser`
against the same root-owned `/bundle` volume. Two installers with different
capabilities, and which one a stage got depended on whether the container had
been restarted since — a difference that appears nowhere in the command text, so
nothing reading the config could notice it. The asymmetry mattered most where
there was no fallback: `bundle check || bundle install` can satisfy a lockfile
and can never change one, so moving a locked version is reachable *only*
through the tool, and that was the half running with the weaker identity.
Whenever two things run what looks like the same command, compare the user, the
cwd, the environment and the stdin as well as the argv — this file has already
paid for stdin separately.

**And when you find one, sweep for the rest rather than waiting to trip over
them.** Two of these turned up in a day by accident, so the third was found by
looking: a deliberate pass over every model-facing string, checking each claim
about the machinery against what the code now does, produced **nine** more. The
worst were not the stale ones but the *contradictions* — the `instruction`
field told the planner to quote code in fenced blocks, which `validate_stage`
rejects outright; the planner was told the executor "cannot run `grep`" while
the executor has `search` and its own prompt had just been told to search
before finishing; `read_files` was described as what the executor "may read"
when `RepoReader` never consults it. Each was one component describing another
from memory. The pass costs an hour and is worth scheduling after any change
that removes a component, because the prompts are where a deleted thing goes on
living.

**A prompt sentence outlives the fact it was written about.** The excerpt block
told the executor "treat them as current — you do not need to look them up
again", and by then `resolve_excerpts` read at `stage_start_sha` rather than
from the working tree. Its own docstring said what that costs: "on a rework the
executor's own prior attempt has already moved the lines." Both were written
correctly, months apart, and the prompt was never re-read when the baseline
moved under it — so the one path where believing the sentence is expensive is
the one where it is false, and the next thing a model does with an excerpt is
quote it into an `old_string`. This is the inverse of the capability that went
missing between two correct changes: there a behaviour was dropped and nothing
said so, here a *claim* survived the change that invalidated it, which is worse
because it still reads as true. When you change where a value comes from, grep
the prose that describes it — a docstring is checked by the code beneath it and
a prompt string is checked by nothing.

**Collapse before you truncate.** `truncate_middle` keeps the head and tail
because "command output is informative at both ends" — true of most commands,
false of a progress reporter, which puts its dots first and its findings after.
Measured: 1,575 unbroken dots were 66% of the feedback handed to an executor,
ahead of the two lines that said what to fix. The latent half is worse than the
noise, because a longer run would have kept the dots as head and dropped the
offences as middle. The ordering is why `clip_for_model` exists rather than two
calls at each site — and the duplication it replaced, `_clip` written twice in
`nodes.py` and `verify.py`, is how the decision got made twice in the first
place.

**And the run log is not where a failing suite's failures are.** The same
head-and-tail truncation applies to command output in `last-run.out`, and a
test runner puts its progress dots at the head and its container teardown at
the tail, so the summary naming the failed examples is the middle and is the
part dropped. Preflight writes no artifact of its own when it refuses, either.
Re-run the suite to find out what failed rather than mining the log for it —
and grep its verdicts case-sensitively as `[FAIL]`, since `[ok  ]` and
`[warn]` are lower case and a pattern written for those reports a clean
preflight over a failing one.

**A reading is worth taking when a decision depends on the answer.**
Executor spend is $3.30 across 152 recorded stages, median $0.0079, against
$199.15 of planner spend on one run. A perfect cache discount there saves about
the price of a single planner call, so measuring it would buy a number with
nothing attached to it. The instinct to close an open question is right about
the question and wrong about the priority.

**And a zero is a reading about the instrument until proven otherwise.** Cost
accounting that reports zero for "not priced" as often as for "free" is why
prices are `None` rather than `0.0`; an accounting layer that reads two
providers' cache fields and not the third reports a silent zero that is the
reader, not the cache.
**And then it closed itself, which is the more useful half.** Replacing the
subprocess editor with an in-process client made the reading free: we now hold the usage block the
provider returns instead of scraping someone else's console. Measured over 46
attempts of one run, instrument `executor-loop.json`: **36,790,654 of 39,218,473
prompt tokens cached, 93.8%**, and on opening turns alone 207,141 of 369,840,
**56.0%**, with the largest shared prefix at 6,133 tokens. So the answer was
neither the *zero* our accounting implied nor the *99.9%* the chat/completions
probe suggested.

Note what actually unblocked it. Nobody decided to measure; the number arrived
because we stopped depending on a tool that could not report it. A question
recorded as unmeasurable is worth re-asking after any change to the layer that
could not answer it — the reason for the gap is usually a property of the
instrument rather than of the thing.

**A figure in a document must name the artifact it came from.** Two cost
numbers reached `docs/architecture.md` — "$15.13 a run", "$2.80" — in the same
session that built the code which computes them. Neither matches any
`report.md`; the tool has only ever emitted four dollar figures and those are
not among them. They were written from recollection while the real numbers were
one command away, they read as measured because everything around them was, and
they were then reasoned from in a later comparison. The rule is not "check your
arithmetic": the arithmetic was never done. It is that a number describing this
system is a *reading*, and a reading with no instrument behind it should be
deleted rather than approximated.

**A category drawn around the mechanism excludes the case drawn around the
meaning.** The transport retry was categorised as *the request never arrived* —
`APIConnectionError`. A 529 `overloaded_error` then ended a 29-stage run: the
request arrived and the provider said come back later. Same outage, same
correct response, outside the category because the category described the
plumbing rather than what the failure meant. Ask not "is this set complete" but
"what is this a set *of*", and whether the name holds if the same event arrives
by a different route.

**Classify a provider failure by status code, not by exception class.** 529 is
`OverloadedError` on Anthropic's SDK and `InternalServerError` on OpenAI's, so
a class list is right in exactly one of the two files that need it. Both SDKs
decide by status in their own `_should_retry`, and so does
`is_transient_status`, whose tests are pinned *against the installed SDKs* — a
provider changing its mind fails a test here rather than stopping a run at 3am.
429 is included, which would be unsafe as an SDK `max_retries` honouring
`retry-after` and is safe here only because our wall clock bounds the wait.
**Test the layer you are actually going to call.** Reaching `max` on the
executor needed litellm's Responses bridge, and a metadata file with
`mode: responses` triggered it perfectly — in litellm. Through the editor in
between it did nothing: its own `register_models` put the entry in a local
metadata map and, in its own comment, *deferred* registering with litellm, so
the registry the bridge consults never saw it. Every attempt died in 1.9s and the run burned
four of them plus a planner revision before it was caught. The verification had
called `litellm.register_model` directly, which proved a fact about litellm and
nothing about the thing in between. The working split is worth remembering
because it is counter-intuitive: routing comes from the model string
(`openai/responses/<model>`), pricing from the metadata file, and each is
useless for the other's job.

**A guard that changes hands has not gone away.** Moving the planner's plan
notes out of `advance` left `written` undefined in the rollback that had
existed to unwind them — so every landing failure would have raised
`NameError` instead of restoring the tree, turning a recoverable stranded merge
into a crash. `append_outcome` and `append_observations` still wrote to the same
file, so the obligation had moved to them rather than ended. The invariant above
says anything *added* to that sequence inherits the obligation; removal is the
same rule read backwards, and only the full suite caught it — the targeted tests
for the new behaviour all passed.

**A test helper that stands in for a node is laxer than the node.**
`with_stage` cuts a stage branch the way `precheck` would, and several
end-to-end tests used it — so they never ran the clean-tree guard and, once
notes moved there, never published a note either. Both failures surfaced as
assertions about the *feature*, which is the confusing way for that to arrive.
When a helper exists because a node is inconvenient to call, the tests that use
it are not end-to-end, whatever they are named.

**Publish a finding when it is found, not when the work lands.** The planner's
notes about the plan — "the two documents contradict each other", "not
drawable" — are true whether or not the stage succeeds, and they were being
discarded with abandoned stages. The code had already conceded the argument for
revisions, accumulating notes across a redraw "because a redrawn stage is the
same piece of work and its observations about the plan are still true"; nothing
stops that reasoning at abandonment. The reviewer's observations stay on the
landing gate, because those describe a diff and an abandoned diff does not
exist. Two kinds of record, one gate, and only one of them belonged behind it.

**A test that pins where a value lives passes while the value is lost.**
`plan_seconds` was measured correctly in `plan`, recorded correctly by
`advance`, and read zero in production for four hours: deriving a stage returns
`**base` and then `**fresh_stage_fields()`, so the reset zeroed the value
`base` had just set. `fresh_stage_fields`' own docstring warns about exactly
this for `pending_plan_notes` and says it cost two stages to find — and the
test written alongside the new field was `assert
fresh_stage_fields()["plan_seconds"] == 0.0`, which asserts the location of a
zero rather than the behaviour of a stage, and stayed green throughout. A field
whose value crosses nodes wants a test that drives both nodes; a test that can
be satisfied by the constant it is checking for is not testing the journey.

**An inner loop that skips the file under edit is worse than none.** The
subprocess editor's in-session test command was built from `test_paths` alone, so a stage declaring none ran
with no in-session test at all — 12 of 35 on one run. The fallback to the tests
in `edit_files` was the obvious fix and it was half of one: of the 17 stages
that *did* declare paths and also edited a test, 11 named a different file than
the one they were editing, so the loop reported green while the edit was
unverified. Between the two shapes, 23 of 35 stages had no feedback on the spec
being rewritten. The rule that shipped first — "do not second-guess a choice the
planner made" — sounded principled and was refuted by the next stage that ran.
Declaring something is not declaring the right thing, and a green loop that
never ran the changed file is a worse signal than no loop, because the attempt
ends believing it succeeded.

**Effort is priced in output tokens, and output is the minority of this bill.**
Measured across 29 stages at `xhigh` against 56 at `high`: output fell 24% per
stage, 30,144 tokens to 22,766, which is exactly what a tier buys. In money that
is $0.184 a stage — about 4% of planner spend, because the prompt is an enormous
cached prefix and output is ~13% of the total. Over the same window `high`
stopped the run twice with `revise` without a stage spec, against zero in ~87
landings at `xhigh`. A 4% saving that buys an unattended stop is not a saving.
The trap in measuring it is that *total* cost per stage rose over the same
period — $4.29 to $5.60 — entirely because the progress log had not been folded;
prompt per stage was up 34%. Read the totals and you conclude the opposite of
what the evidence supports.

**The progress log sits inside the cached prefix and changes every landing.**
It is spliced into the plan block, which carries a cache breakpoint, so each
landing invalidates the whole block and rewrites it — the log is not just
costing its own size, it drags the plan tree through the cache with it. Measured
between two folds: 292 bytes to 263KB over 66 landings, block 0 at 810KB, and an
extra per-stage cost of roughly `landings-since-fold × $0.018`. That is
quadratic in the gap, so ~$100 over 50 stages at 90 landings deep. Folding is
not housekeeping; it is the largest single lever on the run's bill.

**Content cannot move between a pinned document and a live one.** The plan is
read once at `plan_sha` so the reviewer judges a diff against the text the
planner drew from; the progress log is spliced live, because freezing it had
the planner reading 6,680 bytes of a 114,554-byte record. Both are right and
together they have a seam: a fold moves content *out* of the log and *into* the
plan documents, and a resume inherits `plan_sha` and re-reads nothing — so it
sees neither copy. `_plan_unmoved` refuses the resume rather than re-reading,
because `current` and everything queued behind it were derived against the old
text. Ask of any two inputs read at different revisions whether anything ever
moves between them.

**And the guard is about any edit to a pinned document, not only a fold.** It
fired when a human in another session rewrote the `AGENTS.md` paragraph on
`action_on_unpermitted_parameters` to say the opposite of the pinned copy;
resuming would have planned against the reverse of what the branch does. The
likeliest author of such an edit is a person working in the same repository for
unrelated reasons. A fresh run is the way forward and costs almost nothing when
the queue is empty, since landed work lives on the project branch — the
expensive case is a refusal with a derived stage and a queue behind it.
**A delimiter drawn from the content's own alphabet is not a delimiter.**
`read_file` numbered with a five-character field and *two spaces*, and
indentation is also spaces. A line indented by two arrived as four with nothing
marking where our prefix stopped and the file's bytes began, so a model quoting
it back into an `edit` quoted our padding as code and the edit was refused for
text the file does not contain. Measured over 117 refused `old_string`s:
**74 — 63% — matched the file exactly once two spaces were stripped from every
line.** The blank line was worse; ` 1470  ` made the emptiest line in a range
the one that broke it.

The diagnosis is the part worth keeping. Three plausible stories were checked
and refuted first — the model quoting from memory, a stale `git_show`, index
lag — and the fact that killed all of them was that the **median gap between
reading a file and failing to edit it was zero conversation items**. It read
what we sent and reproduced it faithfully. When a model appears to be
hallucinating a file's contents, measure how far back its source was before
believing it: the shortest distance is the most likely, and at distance zero
the fault is ours by construction.

**A classifier over rendered text cannot separate classes the text renders
identically.** `_refusal_kind` bucketed a refusal by matching words in its
message, which is fine while the message and the cause are the same thing. Once
an edit could be refused by three different fallbacks — anchor, whole-file
window, semantic — all rendering the same sentence, the bucket could not tell
them apart *by construction*, and no amount of reading it would have said so.
The route is carried on `ToolError.kind` now rather than derived. The general
form: before trusting a derived label, ask whether the thing it is derived from
still varies with what you want to know.

**An earlier branch can eat every fixture.** Three tests were named for the
semantic fallback and asserted its output; a probe counted that branch
returning a window **zero times across the entire suite**. Every fixture quoted
a first line that matched the file exactly modulo indentation — which is the
*anchor's* case — so the cheap earlier branch answered all of them and the
locator was never consulted. Two of those tests said in their own docstrings
that the surviving indexed line placed the window. It never did. A test that
calls the function and asserts a correct result is not evidence the branch you
meant ran; if a mechanism has a cheaper predecessor, instrument which one fired
rather than inferring it from the answer.

**A check that fixes must not sit behind one that can fail for unrelated
reasons.** `run_all` breaks at the first failure — right for gates, and its
docstring says so: "once one has failed the stage is failing, and running the
rest only costs time." That stops being right when a later entry also *repairs*.
Ordering `annotate` ahead of `bin/rubocop -A` would have meant any docker
hiccup skipped RuboCop for that cycle, so the model's own edits went
uncorrected and its only feedback was about a tool it never invoked — and
`checks` is the *only* run of the linter. Where a
fixer must follow a fallible step, chain it into the same entry with `&&` so a
`break` cannot leave the output uncleaned.

**And the `&&` that fixes the ordering makes an environment failure look like
a defect.** The same entry, one incident later. `annotate` runs through
`docker compose exec`, so when the container is down it exits 1 in 0.3s — and
`_layer_checks` routes an ordinary non-zero back to the executor as "a required
check failed", because only `failed.signal` gets `Route.HUMAN`. A stopped
container and a real disagreement about the routes file are the same exit code.
Measured: an attempt spent 42 minutes and 347 tool calls, 77 of them
`bundle_install`, being told repeatedly that its own work was wrong by an
environment that was not there. The signal branch exists for exactly this
distinction and 128+N is too narrow a definition of it.

**A fingerprint written before the work it stands for makes a failure
permanent.** The target's bring-up stopped its app container when the Gemfile
hash moved and wrote the new hash in the same breath, so an install that could
not succeed was recorded as done: every later call saw no change, did nothing,
and left the container stopped. This is the prediction problem with a worse
ending than usual — the record does not merely become wrong, it *suppresses the
retry that would have fixed it*, and the suppression is silent because "no
change" is indistinguishable from "nothing to do". Write the fingerprint after
the thing it fingerprints succeeds, and a failure leaves it unwritten, which is
the same as asking again.

**The diagnosis can be in a place nothing reads.** In that incident bundler
named the missing dependency and the fix — `bundle update prawn-templates` — in
the first minute. It was in `docker compose logs app`, because the install that
got far enough to know was the *entrypoint's*, not the one the executor
`exec`'d. Every one of the 93 tool results the model received was truthful and
useless: it reported on the process it ran, and that process died earlier. When
a component runs work on your behalf, its output is not in yours, and the
failure you can see is a description of the corpse. Ask where the thing that
knows would have written it.

**And a model's account of why it stopped is evidence about what it tried, not
about what is possible.** One level out from the rule above, because here the
report is accurate and still not the answer. `request_replan` returned "every
bundle_install fails while Bundler fetches the forked git source: its cache's
`.git/FETCH_HEAD` is not writable" — true, reproduced by hand, and the directory
really is root-owned. I reported the permission error as the blocker twice, and
both times the actual blocker was elsewhere: the released gem the first stage
wanted declares `rails (>= 5.1)` against a pinned 5.0.7.2, so it could never
have resolved however the cache was owned; and the installer that builds the
environment for real is the entrypoint's, running as root, which that
permission cannot touch. The executor was describing the wall in front of *it*.
Nothing it said was wrong and nothing it said established a cause. The tell is
that a fix gets proposed before a measurement exists — this file already says
that about mechanisms named from their shape, and an error message is the most
persuasive shape there is, because it arrives sounding like a finding.

**Liveness can be read without a race, if you can name why.** Waiting for a
container is the obvious place to introduce one: "not running" means "not yet"
as often as it means "dead". It is answerable here for two reasons that had to
be established rather than assumed — `docker compose start` returns with the
container already running, so any later `exited` is a new death and not a stale
reading; and no service declares a `restart:` policy, so `exited` is terminal.
Both are written beside the check as conditions to recheck, because the
reasoning fails the moment either changes. It turned an 89-second timeout into
a 5-second answer. A bounded wait is the fallback for when you *cannot* name
the ordering, not the first resort.

**Measure the artifact in the state your claim is about.** Asked to confirm
that `annotate` emits trailing whitespace, three separate checks over the
working tree found none — because the operator had already run RuboCop over it.
The measurement was correct and answered a question nobody asked. A tree is not
a fixed thing; before reporting an absence, establish that what you are looking
at is what the claim was made about.

**A pipeline exits with the status of its last stage.** `pytest | tail -2 &&
git commit` committed a red suite, because `tail` succeeded. The gate was
decorative and the failure it hid was a real one — a new module missing from
`pin_modules`. Redirect and check `$?`. This is the "check what a command
actually returns" rule pointed at the tooling around the tests rather than at
the tests, and it is worth noticing that the rule keeps being broken one level
out from wherever it was last learned.

**A monitor over an append-only log must be anchored to this run.**
`last-run.out` is appended across every resume, so grepping it for "Paused at
your request" matched **24 historical pauses** and reported a stop that had not
happened. A `pgrep -f "code-gantry resume"` in the same wait loop matched the
loop's own command line, so a run that had exited read as alive. Both failures
look like the run misbehaving and are the instrument describing itself. Anchor
to the tail, or to a line count taken at the start.

**And that applies to reading it, not only to watching it.** The same file is
the obvious place to look up which stage a timestamp belongs to, and an
unanchored grep answers from whichever run happens to match — the timestamps
repeat daily and the stage numbers restart every run, so the wrong answer is
well-formed and plausible. Take the line number of the run's own header first
and read forward from it. Per-run directories under `runs/` carry no such
ambiguity and are the better source whenever they hold what you need.

**Hiding a tool's own churn from the gate hides it from everyone.** The editor
normalises line endings on every write, the rule above says the machinery must
declare that rather than let each planner rediscover it, and `gitops`'
`ignore_line_endings` duly keeps the churn out of the diff the reviewer judges.
All correct, and the consequence was not noticed for 77 stages: **10 tracked
files were silently converted from CRLF to LF** — 4 `.js`, 5 `.erb`, one
`.haml` — each landing as a whole-file rewrite that no participant ever saw.

**A gate exemption wants a counter, or a periodic look at what it has
swallowed.** Declaring a behaviour to the one participant that would otherwise
reject it is not the same as accounting for it: the complaint stops while the
effect keeps accruing where nothing is looking. Hiding the editor's
line-ending churn from the reviewer silently converted 10 tracked files from
CRLF to LF over 77 stages, each a whole-file rewrite no participant saw.

**And a finding produced that way arrives without a magnitude — measure the
before, not only the after.** The reason first given for caring was that the
repository had been made *mixed* where it was uniform. It was never uniform:
552 of 3,184 tracked text files carried CRLF at the base sha, 17%, and the
count is 542 now. Ten files joining the majority changes nothing, the count has
not moved since so it was a burst rather than a rate, and the residual cost was
already spent and unrecoverable. The right answer was to do nothing.
**A test suite can be exercising the path you are about to delete.** The
integration tests drove a fake executor binary on `PATH`, and they were green
the whole time the in-process executor was running live — because
`executor.provider` still defaulted to the subprocess one, and nothing made the
tests
follow production. So the end-to-end coverage was entirely on the dead path
while every real stage took the other one, and the first honest signal was
23 tests failing the moment the default went away. A default that only tests
rely on is a fork in the road with no sign on it: when a component is replaced
behind a switch, grep for what still selects the old branch and make the
suite's answer the same as the run's.

**A capability can go missing between two correct changes.** Feedback used to
travel inside the executor's prompt string, and `build_executor_prompt` opened
a rework with one of two opposite instructions — a review rejection means
*replace* what is there, a gate failure means the sweep is unfinished and
repeating the approach on what was missed is the fix. Moving feedback to its
own conversation turn was right, because an opening at character zero
invalidates the cached prefix on every rework. Appending it as a bare turn was
also right in isolation. Between them the framing was dropped, and **82 stages
ran without it** — no error, no test, and the only visible symptom would be a
model treating a rejection as something to add to. When a payload changes
shape, enumerate what the old shape carried; the parts with no field of their
own are the ones that vanish.

**A test that forbids a name is not the same as a test that pins a decision.**
`FEEDBACK_OUTPUT_CHARS = 4_000` was declared in `gates.py` and again in
`nodes.py`: the function was centralised and the number it is called with was
not. Nothing fails when two copies of a constant disagree — one role simply
gives a model less of a failure to read than the other.

**Ban the second application, not the word.** The first test forbade any
function named `clip` outside `gates` and failed on a one-line delegation that
existed for a documented reason. A named wrapper is not the failure mode; a
second *application* of a budget is. Asserting the constant is spent exactly
once leaves genuinely different decisions alone — a lint diff and a one-line
log note are not copies of each other. A test that bans a word forces unrelated
things to be inlined to satisfy it.

**And the sweep is worth running deliberately:** parse every module and list
the names defined in more than one. Of five, four were delegating wrappers
whose docstrings said why, and one was this.
**Cut code with a parser, not a pattern.** Twice in five minutes, deleting the
subprocess executor by regex removed the wrong span: a method boundary matched a `def`
nested inside a *later* function and swallowed six module-level definitions,
then a docstring line reading `stage: some blocks match` matched a
"top-level assignment" pattern mid-sentence. Both were caught, one by the
compiler and one by `ast.parse` — but `ast.parse` accepts a `return` outside a
function and `compile` does not, so the first survived a syntax check and
failed at import. Python's own `ast` gives exact `lineno`/`end_lineno` for
every symbol and costs three lines to use. This is the "read artifacts; do not
regex them" rule pointed at source instead of output, and the same answer:
when a structure has a parser, the pattern is a guess.

**A ceiling that binds first is a policy nobody chose.** Read budgets and the
turn ceiling exist to stop a runaway; the moment one of them decides an
*outcome* it has become something else. `max_model_turns: 20` was silently
deciding whether stages could be done at all — one stage spent four attempts
making 85, 103, 76 and 138 tool calls, every one a read, each stopping at
exactly 20 turns having edited nothing. Eleven minutes and $0.68, and the only
thing anyone saw was the scope gate's "the attempt produced no changes", which
sent the planner to redraw a stage that was never the problem. The tell is
arithmetic: if the limit is being hit at all in normal operation, it is not a
backstop. Check what else already bounds the thing — here `run_loop` stops the
whole cycle at `request_timeout_seconds` regardless of turns, so the ceiling
never needed to be tight.

**A distribution measured under a cap cannot choose the next cap.** The
reviewer's reads were "median 5, p90 11, p99 25" against a limit of 25, and I
used those numbers to pick 80. They describe what the cap *allowed*, not what
the reviewer *wanted*, and within the hour two reviews spent 80 answered calls
and were refused ten more. The same reasoning one step out: a ceiling is set
from the tail, not the median — 8,000 lines came from multiplying the median
by the new call cap and was wrong before it shipped, because a ceiling exists
precisely so the cases above the middle are not cut off. And a limit is only
measurable once something records being refused by it, which is why the
reviewer's ledger had to be fixed before its cap could be tuned at all.

**The uncapped case is the one where a distribution can set the threshold.**
The rule above forbids reading a ceiling off a distribution the ceiling
produced; its converse is that a behaviour nothing has ever bounded gives you
the real shape, and the number to look for is not the median but the gap. Set
a threshold where legitimate use stops rather than where pathological use
begins, and state the separation in the code — `REPEAT_NUDGE_AT = 3` is
defensible because across 9,337 unbounded tool calls only five runs of
consecutive-identical calls reached three at all and every one was
pathological, so nothing legitimate pays for it. Re-measure before moving such
a number; once the guard ships, the distribution under it can no longer answer
the question.

**A value written in three places and read in none.** `ExecutorTurn.stopped`
carried the comment "the loop must not treat this as 'finished'" and nothing
ever consulted it. `executor.log` was declared "assigned by `build_runtime`"
and never assigned. The subprocess editor's timeout setting survived the
deletion of the tool it was named for, read by nothing and still written into
every drafted config.
Three shapes of the same defect in one session, all of which review as correct
— the field exists, the comment explains it, the caller is right there. Only
grepping for the *reader* finds them: after adding a field, or deleting a
component, search for who consumes it and expect an answer.

**A reader that writes destroys the evidence it was about to look for.**
`load_state` ran `CREATE TABLE IF NOT EXISTS` before reading, so a single
read against a live run's database left our table inside it — and the check
for "is this the older format", written as "is our table missing", then saw
both and reported the run as merely empty. Two lessons in one: a reader must
not mutate, and a check that names what a thing *is* survives contamination
that a check naming what it is *not* does not.

**Deleting a producer leaves its consumers guarded on a value nobody sets.**
`context_tokens` and `cost_usd` are assigned in exactly one place — from
`context_tokens_from_log` and `cost_from_log`, which scraped the subprocess
editor's console.
The in-process loop sets neither, so `advance`'s guard
`if executor_context_tokens or executor_cost_usd` is never true and
`append_stage_cost` is never called. `stage-costs.md` stopped being written the
hour the executor switched and stayed frozen for 24 landed stages, while
`executor-loop.json` carried correct usage the whole time. The planner reads
`stage-costs.md` on every call. This is the "value lost in transit" rule with
the loss one step further out — the value was never *computed* on the new path,
and a truthy guard turned that into silence rather than a zero. When replacing a
component, grep for every field only it populated, and check the guards that
read them: a guard written to suppress noise will suppress the whole channel
just as quietly.

**A pathspec is not a glob, and an empty answer is evidence.** `search`
handed `path_glob` to `git grep` as a bare pathspec, where `*` crosses `/` and
`**/` must consume a directory component — so `app/controllers/**/*` matched
only what sits two levels down and never saw the controller directly inside
`app/controllers`. Replayed against one run's tool log at the sha it was taken
at: 337 searches, 76 empty, 44 using a `**/*` glob, 29 of those empty, and
**27 of the 29 had matches**. 8% of every search, and 36% of every empty
answer, were false. The damage is not the wasted call. A model treats "no
results" as a fact about the repository and reasons forward from it — the
executor that hit a run of these abandoned `search` and asked
`semantic_search` the same question ten times in 74 seconds. A tool that
returns a wrong answer gets caught; one that returns *nothing* gets believed.

**A permission is not a record.** The batch orthogonality pass dropped a stage
whose excerpt named a file another stage listed in `edit_files`. But
`edit_files` is what a stage *may* touch, and stages very often do not touch
what they declared — so the check fired over a superset of what happened and
discarded usable work for edits that never occurred. It could also only see
batch-mates, when a file moves just as well by a human's hand, by a `checks`
entry that rewrites, or by a resume onto an advanced branch. Comparing the
blob the planner read against the blob at the stage's start answers the only
question that matters and answers it from the tree. Whenever a check reads a
declaration to predict an outcome, ask what it would cost to measure the
outcome instead — usually less, and it is right about causes nobody enumerated.

**"There is no shell" is a claim about the tool schema, and the pipeline runs
shell scripts.** The executor gets no command tool, and that is the whole
safety story — but `setup_command`, the four test commands and every `checks`
entry are operator-declared argv naming scripts *in the repository being
edited*. A stage that may edit one has arbitrary execution by a slower route,
and it reads as ordinary in-scope work: from `verify`'s side a script is a file
like any other. `no_direct_edit` is the only thing standing there. Measured on
one config, five files were reachable — including the linter's own `Gemfile`,
since `bin/rubocop` is one line resolving through it, so the loophole never
needed the script at all.

**The ban belongs on the files, not the directory.** The same `bin/` held two
exclusion lists stages had legitimately edited 94 times between them; a blanket
glob would have blocked real work to close a hole five files opened.

**And adding a tool adds a script.** A declared command may not start with
`sh`, because argv without a shell is what makes a model-supplied argument
inert — so any tool whose body is more than one program becomes a file in the
repository, which is then reachable and needs its own entry.
**A replan lands nothing and leaves everything.** `request_replan` skips the
gates and routes to the planner, so no diff is judged and no stage lands — and
the executor's edits stay committed on the stage branch and checked out in the
tree, which is deliberate, because throwing away the work is what the tool
exists to avoid. The consequence is not obvious from either half. On its first
production firing the attempt had rewritten the `Gemfile` to something the
container could not install; the branch was abandoned, nothing landed, and the
*next* stage's `precheck` ran `setup_command` against that tree — `precheck`
returns HEAD to the project branch only when it cuts a branch, which happens
after setup — so the bring-up hashed the broken manifest, stopped the app
container to install it, failed, and escalated as a broken environment against a
stage that had nothing wrong with it. 29 stages had landed; the run ended there.
Ask of any tool that hands work back what the attempt has already changed
outside its own diff, because "nothing landed" is a statement about the project
branch and says nothing about the tree the next stage will find.

**A channel that keeps restating the same fact is a fact with no durable
home.** `deferred` carried a plan step taken out of order between planner
calls, so a skipped step could not be quietly forgotten. Across every prompt on
disk it collected 11 entries — five distinct items, re-asserted between 9 and
23 times — and **not one was an ordering decision**. Every reason was a
capability boundary: a scanner needing a signed-in browser session, a document
our own scope gate reverts, a base image that has to change. Its `safe_because`
field asked "why nothing already done or still to come depends on it", which
presumes the document's line order is a queue and departures need justifying;
the plan states dependencies, so there was never a departure to justify. The
content belonged in the plan, a note says it, and the fold makes the plan say
it. Two tells that a channel is standing in for a document: the same entry
arrives call after call, and the field descriptions describe a different thing
from what the entries contain.

**Classify where the damage is, not where the tidying is.** Out-of-scope
findings — a real defect in code the plan is not about — were landing in the
progress log because the planner had nowhere else to put them; measured at 46
of 926 notes saying so in their own prose. The obvious fix is to sort them
during the fold, and it cannot work: the log is spliced live into every planner
prompt, so a finding does its damage in the hours *before* the fold, arriving
in the prompt that decides what work to do next. `kind` is declared by the
planner and `advance` routes on it, because the only place a classification can
prevent an effect is upstream of the effect. Ask of any sorting step whether
the thing being sorted is inert while it waits.

**A markdown link means two things, and following every one reads both as
inclusion.** Recursive plan resolution looks obviously right — deeper documents
are plan documents too — and the guard against escaping the plan directory,
which is the part that sounds hard, already generalises for free. The problem
is elsewhere: measured over this project's history, every depth-2 link was
either a *sibling cross-reference* or a document already supplied by another
channel, and there has never been a third tier. A recursive resolver would have
silently restored 120,249 characters that had just been removed by unlinking
two documents from the root, because a sibling still says "see also". Inclusion
is the rare case and cross-reference is the common one, so transitivity
optimises for the wrong one — and it costs the property that made the removal
possible, that reading the root tells you the whole payload.

**The rule you already wrote gets rebuilt in the next feature.** `CLAUDE.md`
records that an optional field with a conditional trigger is answered with
nothing: `observations` came back empty 278 times out of 278. Step 10 then
shipped `additional_stages` as an optional field whose description opened
"**Normally empty, and empty is the right answer**", with the cap reachable
only in `config.py` and in a silent trim — and two derivations under a cap of
five each returned one stage. Nobody ignored the rule; the new field did not
look like the old one. A written-down failure mode is only load-bearing if
something checks new work against it, so it is worth reading this file's own
rules when adding a field, not only when debugging one.

**Do not measure against a tree a live run owns.** Diagnosed at 01:55 that a
planner had read another run's in-flight working tree, and then spent forty
minutes on a replay harness that contradicted itself — 13 hits one minute, 0
the next — because it was searching the same repository while the executor
rewrote `config/routes.rb` underneath it. Clone to a scratch directory and
check out the sha the artifact was recorded at. And when a measurement
disagrees with itself, suspect the instrument before the code: the production
path was verified in one step by spying on the argv it actually built.

**Know the size of what you are about to walk.** The target repository has 36G
under one gitignored directory. A bare recursive search over it starved the
machine for two minutes at a time, and those runs are the ones whose results
made no sense. ripgrep prunes it — 4,423 files enumerated in 0.02s — because
the path is in `.gitignore`, and that is worth *verifying* rather than
assuming before pointing a walking searcher at an unfamiliar repository.

**Two runs on one repository is a five-minute window, not a crash.** A resume
believed killed was still live; a fresh run started 68 seconds later and both
ran against the same worktree. Nothing collided only because the second was
still in its planner call — the collision would have been the moment it tried
to cut a branch. `code-gantry pause` is checked at two points, and the one
after derivation and before `precheck` is what makes stopping safe here: it
holds the derived stage and never touches the tree. "I killed it" is a claim
to verify with `ps`, not a state to assume, and the run directory rather than
`last-run.out` is what tells you which run a line belongs to.

**And killing a run does not kill what the run started somewhere else.**
`setup_command`, the test commands and the `checks` all reach the work through
`docker compose exec`, and killing that client kills the client. The process on
the other side keeps running: a suite stopped mid-flight left fourteen
`parallel_rspec` workers alive in the container, still holding connections, and
the *next* run's `db:test:prepare` died on "There are 4 other sessions using the
database" — an error naming postgres, several minutes and one restart away from
the kill that caused it. `ps` on the host says the run is gone and is telling
the truth about the wrong process. The general form is the rule about a
component running work on your behalf, pointed at teardown instead of at
diagnosis: wherever a command crosses into another process space, ending it here
is not ending it there, and the cleanup has to be aimed at the far side. Aim it
narrowly — a pattern broad enough to catch the workers is broad enough to catch
the entrypoint that owns the container.

**A subprocess inherits more than argv: cwd, env, and stdin are inputs too.**
`search` passed ripgrep no path, and ripgrep searches **stdin** when stdin is
not a terminal — so the tool worked at a shell and returned nothing under
`subprocess.run` with an inherited pipe. Measured on one literal: inherited
stdin `rc=1, 0 files`; `stdin=DEVNULL` or an explicit path, `rc=0, 399 files`.
Nothing warns; every search is simply empty, which reads as "not in this
repository".

**Testing the right layer does not cover what that layer inherited.** The calls
did go through `RepoReader`; what differed was the stream, which no unit test
and no shell probe can see, because pytest and a terminal sit on opposite sides
of it. This produced five contradictory measurements over several hours, each
blamed on the target repository moving under a live run — a story that was true
once and wrong four times. When a subprocess result varies with nothing you
changed, suspect what it inherited before you suspect the world.
**And once a path is passed, a guard that read an exit code may become
unreachable.** ripgrep folds "your glob selected nothing" into status 2 only
when it has no path to search; with one supplied it exits 1 like any empty
result, so the check distinguishing the two — written the same day — could
never fire again. It is derived now, from one `rg --files` call at 0.02s on a
4,423-file repository. Reading a condition off an overloaded status is a
dependency on someone else's error taxonomy, and it changed under a fix to
something else entirely.

**A line is not a unit of size.** Every ceiling in `ReadBudget` counted lines,
which assumes a line is roughly a line's worth of text — true of source, false
of a minified bundle, a vendored asset, a `structure.sql` or a fixture with one
enormous row, and every repository of any age has some. A derivation whose
searches returned 106 and 160 "lines" was followed by a call the provider
rejected at `1103000 tokens > 1000000 maximum`, against a recorded initial
prompt of 611,008 characters: essentially the whole million arrived through
results that every line-based ceiling called small. `max_chars_per_call` is the
second dimension. The general form is to ask what unit a budget is denominated
in and whether the thing it is protecting is measured in that unit — context is
bytes, and calls, lines and files are all proxies that decouple under load.

**Do not explain the present with a component that is absent.** Roughly thirty
comments and docstrings still described current behaviour in terms of a
component that had been deleted months earlier — what it reported, what its
linter skipped, what its accounting could not distinguish. Each reads as an explanation of the
code in front of you, and none is checkable by anyone who does not already know
the tool is gone. The reasons were worth keeping and every one restated in
present terms: "that accounting reports zero for not-priced as often as for
free" is really a statement about rate tables, which is what the
`None`-rather-than-`0.0` distinction guards. The first instinct — that deleting
the history leaves the code looking arbitrary — is wrong for the same reason
the prose is: a reason is only useful if the reader can act on it.

The corollary governs deletions. Before removing something, grep for what
*cites* it: script stages had no caller and five separate docstrings naming
them as live justification, for decisions that survive on other grounds. Cut
the code and leave those, and you have manufactured the problem above.

**A field removed from a model strands the run that persisted it.** `current`
is a dumped `Stage` and `Stage` is `extra="forbid"`, so deleting a field raises
on the next *resume* — not on the next fresh run, but on one hours deep, as a
pydantic error from inside a node, for a stage that is perfectly valid. Found
by reading the live checkpoint before deleting `kind`, which it was carrying on
a 34-stage run. `current_stage` filters to declared fields now, which is the
rule `driver._merge` already applied to `RunState`. A fresh run is always the
fallback, since landed work is on the project branch rather than in the
checkpoint — but it discards the derived stage and the queue behind it.

**A limit can depend on a setting in another file, enforced by neither.** The
planner's `max_tokens` was raised to 64,000 after a batched derivation died
mid-JSON; the API accepts up to 128,000 for this model. What is invisible at
the call site is that the installed SDK refuses a *non-streaming* request whose
budget implies a long generation — `3600 * max_tokens / 128_000 > 600` — which
caps it at 21,333, below even the 32,000 already in use. It never fires only
because that check runs when no explicit timeout is passed and the planner
always passes `request_timeout_seconds`. Delete that field and every planner
call raises before it is sent. The probe that found it also nearly lied: asking
the API *without* a timeout returns "Streaming is required", which taken at
face value says our ceiling is 21,333 — a wrong answer about our own
configuration, produced by measuring a call we do not make.

**A guard belongs where its question can first be answered, not where its
answer is convenient.** `branch_identity_problems` asks four things at once,
and one of them — has `base_ref` moved — is a fact about the world at startup
that cannot become true part-way through a stage. Asked inside `verify`, it is
answered *after* a planner call and an executor attempt have been paid for.
Measured: a resume died 9 minutes and two model calls in, on a condition that
was true before the first byte of work. The other three checks are right where
they are, because HEAD wandering and a stage branch diverging can only happen
mid-stage. Two questions with different lifetimes in one guard, and the cheap
one was paying the expensive one's price.

**And that guard asked equality where the question was ancestry.** Any change
to `base_ref` stopped the run, on the stated grounds that "the baseline is no
longer what the report will claim" — which is not what the report claims. It
prints a sha, and the sha a run started from stays true however far the branch
travels. Nothing else depended on the pointer either: the flake baseline checks
out the recorded `base_sha`, stage diffs come from `stage_start_sha`, plan
documents from `plan_sha`. So a 17-stage run died because `main` had been
merged in — the correct thing to do on a migration lasting days, already proven
green over 3,775 examples. What is worth stopping for is a *rewrite*, when the
baseline is no longer reachable. This is the "category drawn around the
mechanism" rule again: the check described a pointer when it meant a history.

**A tool that edits after the model stops leaves its context stale.** `checks`
autocorrect — `rubocop -A`, `eslint --fix`, `gofmt -w` — and they run once the
model has stopped asking for things, so the tree moves under a conversation
that is already finished. The next cycle opens with the model holding file
contents that are no longer on disk; it cannot see that its edit was reverted,
and from where it sits it made the change and the gate is complaining anyway.
Measured: a stage required `Date.today` and forbade `Time.zone.today`, and
`Rails/Date` rewrites the first into the second. Three planner revisions, five
attempts, ~35 minutes, and the planner escaped only by inferring the cause from
a diff that came back twice. Stage the model's work before the checks run and
the rewrite is the unstaged remainder — the linter's diff exactly, no commit
restructuring, no snapshot. Attribute it in as many words: handed a diff
without being told whose it is, a model reads it as its own mistake and tries
the same edit again.

**A second dimension whose default ignores the first is a tightening.**
`max_total_chars` was added underneath `max_total_lines` because a line is not
a unit of size — and shipped with a default computed from the *class* default
line budget, while every real config raises the line budget three to seven
times higher. The reviewer would have got 240,000 characters against a line
budget implying 1,600,000: a seventh of the ceiling it was added to sit under,
binding first and silently, because a read-budget refusal reads the same
whichever ceiling raised it. Derive a companion limit from the configured value
of its partner, not from the constant beside it.

**The success path is the one that skips the tail.** `run_loop` returned the
moment the gates came back clean, two statements above where the cost was
computed — so every attempt that worked first time was billed at zero, and only
attempts that failed a gate or edited nothing were priced at all. 41 of 57
recorded attempts, 17.9M prompt tokens unbilled. Three tests asserted the
pricing and all were green, because their fixture makes no edits and leaves by
a different exit. Prefer one exit; when there are several, ask which one the
happy case takes.

Seen again adding a per-turn usage series to the reviewer: four exits assign the totals and were routed through one helper, and the series still came out empty on every *approved* review — because the fifth exit is a constructor, and it is the one the happy case takes. Routing the exits you can see is not the same as finding the one that leaves by a different door.

**A resume is not a fresh process with the old state.** Three defects in one
mechanism, each invisible because the work survived elsewhere. `resume_fields`
said it was "what a resume merges over the saved checkpoint" and nothing merged
it, so every resumed run began with a four-key state — no `run_id`, so the
checkpointer never fired, so the database froze 31 stages before the run
stopped. `step` counts from zero inside one `drive` call and the key is
`(run_id, step)`, so a resumed session overwrote the beginning of the previous
one while its tail survived at higher numbers; `load_state` ordered by `step`
and therefore returned whichever session ran *longest*, reliably the older.
What hid all of it is that the continuity that matters lives on the project
branch and in the progress log, so the run kept working and nothing asked.
Check that a resume starts from what it loaded, and that "latest" means last
written rather than largest.

**And it re-enters at the node it died in, not at the top.** The corollary, and
it reads as obvious only afterwards. A run killed mid-stage was resumed
expecting `precheck` to re-cut its stage branch — but `cut_stage_branch` is
only on the path through `precheck`, and a resume interrupted inside `verify`
comes back inside `verify`. What it found was HEAD on the project branch where
it expected the stage branch, which is the branch-identity escalation, nine
minutes and a planner call after the fix that was supposed to make it possible.
"Delete the stage branch and resume" buys a fresh cut on a fresh *run*; on a
resume it buys a stop.

**Read `resume_entry_point` to say where a resume will land; do not read the
checkpoint's `next_hop`.** `resume_fields` clears that field to `""`, so the
node the run was heading for when it stopped is not where it comes back. The
entry is computed from `failure_layer` — `PLANNING_FAILURES` to `plan`,
`REPO_STATE_FAILURES` to `verify`, `paused_before` to the stage it was holding
— and otherwise from `stage_has_work`, which the caller sets from whether the
stage branch carries commits. `review` is in `REPO_STATE_FAILURES`, so a stage
interrupted after a rework verdict re-runs `verify` and the reviewer against
the work already on the branch rather than the executor. Both facts are one
function; predicting from anything else is a guess that reads as a reading.

**And "nothing landed" is a claim about the project branch that says nothing
about the stage.** The same mistake one file over. The quarantine means a
stage's work sits on its own branch until it squashes, so a project branch with
no landing is exactly what a stage in progress looks like — and the stage
branch was deleted on the reasoning that nothing had landed, which discarded a
conversion of 115 tool calls over nine minutes. `stage_start_sha` outlives the
branch too: re-cutting from a newer tip leaves the recorded start where it was,
so the stage's diff silently acquires whatever landed in between. Before
deleting a stage branch, run `git log <project_branch>..<stage_branch>` and
read what is on it; an empty answer is the only evidence that nothing is there.

**A value that was private when its file was private is published when the
file moves.** Relocating the config into the target repo turned `target_repo`
from a convenience into one machine's home directory in a file other people
check out, and turned `host` into `init` writing a real machine name into a
tracked file that nobody chose to put it in. Neither value changed; their
status did. When a file changes audience, re-read every field as though seeing
it for the first time.

**Watch the process, not only its log.** A filter over log lines cannot see a
process that stopped emitting them, and a monitor stopped for volume looks
exactly like a quiet one. Measured: a run died and sat dead for 78 minutes
while being reported as healthy. The filter did match the escalation — it had
been killed earlier for also matching `[plan]` and `flake`, which fire several
times per stage. Two watches, never one: liveness on the pid, and a narrow
filter for rare events. Never mix a per-cycle signal into the rare-event
filter; the noisy entry costs the alarm.

**And a sampled window over a growing log is a lottery, not a watch.** Two
waiters written as `until grep -q "<phrase>" <(tail -c 5000 <log>); do sleep
20; done` never fired, though the phrase was written every stage — the run
emits multi-kilobyte tool-read lines, so by each sample the phrase had scrolled
out of the window. One spun for an hour and forty-four minutes against a run
that had already ended. Wait on a state that persists — a pid, a file that
appears, a line count taken at the start — never on text that must still be
inside a window at the instant you look. And keep the set enumerable: `ps`
answers "what am I actually running" in one command, and the duplicates were
found by someone else asking.

**`grep -c` counts lines and `grep -o | uniq -c` counts occurrences.** One
summary line echoing many refusals turned 17 into 842, and I used the larger
number to argue a ceiling was binding when it had been reached by 2 attempts of
85. The same day, `timeout` — which macOS does not have — returned 127 and was
read as a suite result. Both are the standing rule about checking what a
command actually returns, and both were committed to before anyone asked what
the number counted.

**A value that fits is a value that fits *where it is*.** `prompt_cache_key`
is capped at 64 characters and a slug-shaped key ran to 19. Moving the config
into the repository it describes made `work_dir` the project's identity, the
identity a path, and the key 98 — so the first reviewer call on the new layout
came back 400. Nothing changed but its length, and `len()` would have found it
at any point in the six hours before the failure. When a source moves, re-read
every field it feeds as though seeing it for the first time.

**And blind truncation is the wrong fix for a capped identifier.** Two projects
under a long shared prefix truncate to the same key and silently share a cache.
Both sites hash through one helper, with short identities passing through
unchanged so no warm cache is discarded by the fix. The `[:64]` that had
already been written at one call site never reached the other, which is the
usual shape: a fix applied where the problem was noticed rather than where it
lives.
**A tuple literal evaluates before the loop body sees anything.** The executor's
gates were the elements of one, ordered cheapest-first with a module docstring
saying so, and the full suite ran even when `patterns` had already failed — for
as long as the ordering has been documented. Harmless while every entry was a
pure question; the moment one of them prepares an environment, building a tuple
restarts containers. Laziness is not an optimisation here, it is what makes the
ordering mean anything.

**Look one line up from the field you are adding.** `executor_cost_usd`
accumulates across attempts and its comment says why: "a stage that took four
attempts paid for four and the figure worth recording is the stage's, not the
last attempt's." Eleven lines above, `executor_context_tokens` was *assigned*.
The consequence is not a slightly-low number — a stage whose final attempt is a
one-line fix records that attempt's high-water mark as the whole stage's, and
two stages on one run reported 12,933 and 16,079 against 4.4M and 1.5M prompt
tokens, in the figure the planner sizes batches from. The sentence that fixes a
field is often already written on the field beside it.

**A summary artifact must carry the number it is about.** Finding that required
inferring per-attempt context from cache writes, because `executor-loop.json` —
the *per-attempt* record — did not record the attempt's peak. It was computed,
carried to state, and rendered per stage. A sum cannot be decomposed afterwards,
so the per-item artifact has to hold the per-item figure or the only analysis
left is archaeology.

**And it must carry the whole response, because a silent omission answers.**
`planner.json` recorded a subset of the planner's structured output and nothing
said which — `status_entry`, `additional_stages` and the deferral list were all
missing. Asked whether the planner had ever deferred, I read that file, found
no key, and reported zero across 542 calls; the real answer, from the prompts
actually sent, was 50 of 70. An absent field and a zero are indistinguishable
to a reader, so an artifact that drops fields does not merely fail to answer —
it answers wrongly, with the confidence of a record.

**And the writer must be the model, not a list of keys.** `executor-loop.json`
carried ten fields of twenty; the four it omitted — `ok`, `timed_out`,
`turns_exhausted`, `log` — are between them the entire answer to *why did this
attempt end*, the only question a per-attempt record exists for. The
enumeration is also what makes the *next* field go missing: `commit_refused`
was added that morning and was already absent. The writer walks
`dataclasses.fields` now, so a field has to be excluded on purpose. Wherever a
subset is written out by hand, the hand is the defect.

**An optional keyword does not reach the call sites that predate it.**
`append_flakes` was called by `preflight` without the `examples` argument it
gained later, so **every baseline flake this project ever excused recorded no
locator** — silently, for as long as locators have existed. Nothing was wrong
at either end; the caller was simply older than the argument, and the omission
is indistinguishable from a value that was genuinely absent. The record is a
dataclass now: make the writer the thing being written.
**A sentinel is a value in the wrong field.** The same call site passed the
string `"preflight"` as its `stage_id`, because there is no stage before a run
starts. It reads as harmless and it means the ledger could separate a baseline
flake from a stage's suite going red only by string-comparing a stage that does
not exist — so any sort over the file put them in the same bucket, and any
future stage literally named `preflight` would have joined them. `origin` is
its own field now, and `run_id` is null rather than faked, because preflight
genuinely runs before one is assigned. Ask of any magic value what question it
is answering, and whether the field it is sitting in is the one that asks it.

**Convert a format while the file is small, because the window closes.** The
flake ledger was markdown parsed by a five-group regex with two optional tails,
and the ambiguity was already live: a missing seed and a missing examples
segment each meant both "not captured" and "written before that field existed",
so the reader could not tell a gap from an era. Adding the locators had cost a
compatibility branch across 276 entries — the code says so in its own comment —
and the next field would have cost another. The operator deleted the file for
unrelated reasons and it stood at twelve lines; that was the entire opportunity,
and it existed for about a day. A format whose parser needs a
backward-compatibility branch is one field from needing two, and the cost of
changing it grows with the file rather than with the change.

**A counter added underneath another is not reset by the code that resets the
first.** `plan()` cleared `calls` and zeroed `_lines_used`; `max_total_chars`
arrived later and nothing taught the reset about it, so `_chars_used`
accumulated for the life of the process. Past the ceiling, *every planner call
was refused on its first read* — 14 of 31 on one run, each drawing a stage with
no way to check a premise against the code, which is the documented cause of
all-attempts-zero-diff stages. The cliff is the tell: stage 025 got 3 reads of
7 and every call after it got zero.

**Clear spent state by replacing one object, not by zeroing a list of fields.**
A field-by-field reset is a list somebody maintains, and the next counter is
one more line to forget somewhere the omission stays invisible until a long run
crosses a ceiling. The hazard that introduces: whatever *shares* the mutable
structure must reach through the owner rather than hold it —
`SemanticSearch(calls=reader.calls)` would have gone on appending to an orphan,
losing every semantic call from the log with nothing raising.
**And the reviewer had no reset at all**, which is the same defect arriving by
the other door: not a field forgotten but a whole call site written without
one. Its ledger accumulated across every review a process made, so
`review.json` recorded 685 calls for a review that made a handful, the ceilings
named "for this step" were really for the run, and refusals climbed from zero
to 58 as late reviews were starved by their predecessors' reads. Two roles,
one mechanism, and only the one with the older code had the guard.

**A budget whose consumption is never printed cannot be seen to leak.** That
one ran for a whole run and the only outward sign was the planner saying, in
prose, that it could not read — a claim it then misdiagnosed as its context
being too large, recommending a fold that would not have moved the number by a
byte. The planner and review lines carry ` (120k/800k chars)` now. The test is
not the value but the *series*: a figure that returns to a low number each step
is a budget being reset, and one that climbs is a leak anyone can see. It
answers the tuning question too, which nothing could answer before — measured
after the fix, the planner peaks at 15% of its ceiling and the reviewer at 5%,
so neither is anywhere near binding.

**Cache writes measure what was newly cached, not how big the job was.** The
appealing alternative for sizing a multi-pass job is summing cache writes, and
the artifacts refute it: a stage that reuses an earlier stage's prefix looks
*small precisely because it was efficient*. Measured — single-attempt stages sat
a steady ~7.3k below their context figure, the shared prefix, and one wrote
21,547 while carrying 82,015. Summed per-attempt peaks have neither problem and
need no reconciliation between two providers that disagree about what a cache
write is: Anthropic reports cache *creation*, OpenAI reports a field inside
`prompt_tokens_details` from automatic caching. One column, two meanings.

**A total from a tool loop is not a context figure.** One derivation billed
6,604,374 input tokens against a 187k prompt, because a tool loop re-sends the
whole conversation once per turn and it made 32 calls. Read as capacity it is
nonsense by a factor of thirty; read as a bill it is exact, and the cost
decomposes to the cent. The planner had only totals recorded, and the planner is
the role that has actually overrun a window — rejected at 1,103,000 tokens
against a 1,000,000 ceiling with nothing recorded that would have seen it
coming. Peaks and totals answer different questions, and `accumulate_usage` maxes
any key naming a peak rather than adding it, decided in the helper because four
call sites in two modules is three too many to rely on remembering.

**Nothing can be omitted after a tool call, so the lever is what you send
first.** The API is stateless; caching changes the price of resent tokens, not
whether they are sent. Measured on one derivation: block 0 is 190,907 tokens and
strictly append-only across derivations — 99.2–99.7% shared with the previous
one, so cross-derivation caching is already doing everything it can. What
remains is that the prefix is re-read 31 times *inside* one derivation, $2.96 of
a $5.52 bill, and roughly 620k of its 687k characters are plan documents. The
only lever with that magnitude is a smaller plan, and the risk of moving it
behind tools is that a planner asked to fetch it will fetch it. That is a
question for an experiment, not for reasoning.

**A retry that behaves correctly can still describe itself wrongly.** One loop
serves both the transport budget and the spurious-400 budget, and it hardcoded
"transport failure" — so a 400 the provider *answered* announced itself as a
dropped connection, and the first minutes of diagnosing a live failure went to
the retry logic, which was working exactly as designed. The category was drawn
around the mechanism again. A log line is the interface a failure is diagnosed
through, and naming it after the machinery rather than the event costs whoever
reads it next.

**Check the instrument before the world.** Three measurements in one afternoon
were wrong in the tool rather than in the system: `grep -c` counting lines where
occurrences were wanted; a "0.0% shared prefix" that was the artifact's own
header changing, one function call away from reporting that caching had never
worked; and a project memory describing a manual remedy, read as a statement
about what the current code cannot do — when the operator's own script had been
written to handle exactly that case, with a comment citing the incident. When a
number surprises, suspect the measurement first.

**And do not propose a fix for a mechanism you have not established.** Three
times in one incident I named a cause from its shape and started building
against it: a start-up race, then a truncated bind mount, then an implied link
between 53 `delete_file` calls and a directory that vanished. Each was
plausible, each was refuted in a minute by a command I had not run, and the
first two had working code drafted before the refutation. What settled it every
time was the cheapest available reading — the gem was in the volume, the host
file was 6,581 bytes and intact, the delete log named only `Gemfile`. The tell
is that a fix arrives before a measurement does; the operator's "the guess is
this is a race condition" was worth more than the code I had written under it.
A remedy built on an unestablished mechanism is not merely wasted, it is
*confirming*, because it will be adopted and the real cause will keep firing
underneath it.

**One item from a ranked list is not a finding; the list is.** Reporting on
semantic search I pulled a single hit — a CodeGantry process document ranked
third — and built a paragraph on it, without showing the other five. The
operator noticed the list I *had* shown did not contain it, and the full result
said something better and different: four of the six hits were matching on the
token *card*, so the third-place intruder was ranked against near-noise and the
query itself was the defect. The one line supported "one more exclusion is
needed"; the six lines supported "this query was never a question". A rank means
nothing without what it outranked — which is the same sentence as the semantic
index rule above, pointed at how a result is *reported* rather than how it is
produced.

**A watermark into a concatenation indexes a list whose middle moves.**
`tools.log` was written by slicing `reader.calls + editor.calls` at one index,
and every read appended during a turn pushed the whole editor half one place
right — so the next slice began inside edits already logged, re-printed those,
and skipped the reads that displaced them. Measured over 992 calls: 479 edits
reported against 117, 267 reads against 455, and none of the 37 `git_diff`
calls, with eleven consecutive `edit(config/routes.rb)` lines standing for two
real edits. Nothing was wrong with either ledger, and the summary line beside
it in `run.log` was exact the whole time, because it *counts* the pair rather
than slicing them joined. That is the tell worth keeping: two views of the same
data disagreeing means the derivation is wrong, not the data — and a joined
view needs one watermark per source, derived from the sources so no call site
can mis-shape it. It also arrived as an observation about the *executor*
repeating itself, which the transcripts refuted: 12 duplicate calls in 992.

**A flag that filters can undo the boundary you thought bounded it.**
`search`'s tracked-only guarantee rested on ripgrep's ignore handling. `-g` is
a filter over the walk rather than within it, so a model-supplied glob
overrides `.gitignore` entirely and the guarantee held only for calls naming no
path. Under `-g '**/*'`: 41 hits out of the executor's own transcript, 18 out
of `tools.log`, and `planner.json` for the stage being executed — the planner's
reasoning, which the executor is deliberately not given. It then compounds,
because output is capped: artifacts winning the first 12k push real hits out
and the model searches again, so **a leak surfaces as repetition**. Ask what a
stated boundary is *made of*, and re-measure it under the inputs a model
supplies rather than the ones the docstring was written against.

**A fixture reproducing an exclusion must be checked for whether it still
excludes.** The first one force-added the ignored files, which made them
tracked, which made `git check-ignore` decline to report them — the test would
have passed by making the leak legitimate.
**A cache keyed on a string is keyed on its spelling.**
`verify._recorded_answer` skips the gate's test run when the loop already ran
*this command* on *this HEAD* — two facts compared rather than trust, and
exactly right. But it compares the command as text, and the two sides build the
path list in different orders by construction. Measured over one run: 18
adjacent pairs naming an identical set of files, **18 of 18 differing only in
order**, 790 seconds of specs re-run on a tree nothing had touched.
`resolve_test_paths` sorts now — the "same command spelled the same way" rule
applied to the argument list rather than the flags.

**Verify a mechanism *can* fire before recommending someone enable it.** The
layer was not granted on that project, so the duplication read as the
operator's choice and the fix looked like a one-line config change that would
have saved nothing. A switch that reads as the whole story and is a no-op is
worse than a switch nobody turned on.
**And the measurement that argued against granting it was about something
else.** The config had a careful note explaining why only `checks` was trusted:
over 81 verdicts the gate disagreed with the loop about `tests` twelve times and
about `checks` zero. Every word of that is true, and it is not an argument
against the grant, because `trust_executor_gates` is a *precondition* for the
memo rather than a substitute for it — an ungranted layer is refused outright,
and a granted one must still match the command as text and the HEAD it was
answered on. The twelve disagreements come from the gate's path set being a
superset of the loop's, which spells a different command, misses, and runs. So
the grant only ever unlocks the identical case. A real measurement attached to
the wrong decision is harder to argue with than a wrong measurement, and the
question that separates them is not "is this number right" but "what would have
to be true for this number to change the answer".

**A ledger that records the container cannot answer questions about the item.**
`flakes` recorded a file and an ordering seed per excusal — 277 entries, 77
naming one feature spec — and could not say whether that was one example
failing 77 times or 77 different ones. Those are different bugs, and the
question is the reason the file exists. The answer was already on the line the
parser read: RSpec ends every failure with a re-run locator, and the pattern
took the path out and discarded the rest. Recovered later: **three examples,
consecutive siblings in one context** — a shared setup, not three defects. Ask
what the ledger is for, then check that what it records is what the question is
about.

**Before backfilling an append-only file, ask whether the raw material is still
on disk.** The run directories still held every failing suite's output, so the
history was answerable from a fifteen-line script without writing anything —
and the file only needs to be right going forward.
**A rule that is right about every case and silent about the sequence lets a
deteriorating thing deteriorate at full speed.** The flake doctrine — "a file
that passes whole and standalone is green" — is applied per excusal with no
memory between them, so the second sighting reads exactly like the first. Over
one sixteen-hour run a single example was excused **six times**; six stages
landed over it; then it stopped passing alone and the cost arrived at once — a
stage that had passed every gate and been approved lost its landing to a red
suite it had not caused, an extra stage was drawn to repair the spec, three
attempts failed at ~200s each, and the run was killed.

**And the tell is a record that exists with no reader.** The file built to make
repetition countable is read by no code: `recent_flakes` has nine callers and
all nine are tests. It made the problem visible to a human who happened to sort
it, and invisible to the pipeline.
**Approved work does not survive a redraw.** The stage above had a clean diff,
all gates green and a reviewer approval, and its branch still holds three
commits. It will not land from them: `cut_stage_branch` reuses an existing
branch only in the *extend* case, and a stage the planner redraws after an
unrelated failure comes back with `fresh=True`, which deletes and recreates it.
So the executor runs again and the reviewer judges again, and the approval is
paid for twice. Worth knowing before deciding whether to squash such a branch by
hand — the diff is right there and the alternative is a repeat, which is the one
case where reaching into the quarantine is cheaper than letting the machine
redo it.

**An operator's regex is data, and code must not depend on its spelling.**
`failed_file_pattern` opens `^\s*`; `\s` matches newlines and `^` in multiline
mode can anchor on the blank line above, so `match.start()` sat on the previous
line's break and slicing from it yielded the blank line. The extraction
returned `{}` — not an error, not a partial answer, an empty result reading as
"this runner prints no locators", which is the empty search that gets believed.
Anchor on `match.end()`, which is always inside the line the capture came from,
and the dependency is gone.

**Measure before deciding, then fix the dependency rather than the instance.**
1,743 locator lines across every archived log were flush left — the `\s*` had
never matched a character, and dropping it changes nothing (replayed over 1,531
logs, zero disagreements). Dropping it is still not the fix: the next project's
pattern is written by someone else, and correctness that depends on it not
beginning with `\s*` is a defect waiting on a config nobody will check.
**A check that loads part of a thing has certified part of it.** The declared
`bundle_install` proved the app boots with `RAILS_ENV=test bundle exec rails
runner "exit"` — which loads `Bundler.require(*Rails.groups)`, in test only
`:default` and `:test`. A resolve that moved a `:development`-only gem was
certified by a check that never opened it, reported exit 0 on a bundle the app
container could not boot, and killed the run at the next bring-up against an
unrelated stage.

**Loading the file is the only instrument that separates a gem's metadata from
its source.** That one declared `required_ruby_version >= 2.4.0` and used 2.6
syntax. The check evaluates `Bundler.require(*Bundler.definition.groups)` now,
with the group list from bundler rather than hand-written — the Gemfile already
had a `group :staging, :production` a hand-written list would have skipped in
silence.
**A state predicate is not a completion signal.** A bring-up waited for its
container by polling `bundle check` — the same question the entrypoint asks,
but the entrypoint asks it *once, before* installing and the poll asks it
*repeatedly, during*. Bundler wraps `Installer#run` in `ProcessLock` and
`bundle check` takes no lock, so the check reads a tree the installer is still
writing and its answer flips partway through. Measured: the wait returned in
under a second while the container log was still printing `Fetching savon`.

**And a remedy stacked on a misjudged state manufactures the fault it was
written to recover from.** The restart that followed killed the install,
leaving the volume more partially populated each attempt. What replaced it is
the entrypoint's own handoff — under `bash -e` it installs and only then
`exec`s the real command, so PID 1 is the entrypoint until the install returns
*successfully*: a property of the process rather than of the tree it is
writing, and one that cannot be true early. Ask of any readiness check whether
what it reads is finished when the work is finished, or merely *becomes* true
somewhere in the middle.
**A rule is checked against new work; nothing re-reads what predates it.** This
file already says a guard belongs where its question can first be answered,
written about a startup question asked from inside `verify`. `run_preflight`
then spent 5m30s running both suites before reaching the check whose first act
is `env_var not in os.environ` — an answer available before the function did
anything. The rule was right, was written down, and did not fire, because the
code was older than the rule and nothing goes back over it. That is the
complement of the entry above about a written failure mode being rebuilt in the
next feature: one asks that new work be checked against the rules, this asks
that the rules be checked against old code, and only the first ever happens by
itself. Worth a deliberate pass when a rule is added, aimed at the code that
already existed.

**A fixture can make a whole file's tests laxer than production.** The shared
repo fixture has no plan root, so `plan root resolves` was blocking in twelve
preflight tests — and the suites ran anyway, because nothing yet stopped them.
Those tests were driving the environment checks through a preflight that had
already failed, which is a state no caller can reach: all three exit on a
blocking check. Every one was green and had been for months.

Only moving the gate revealed it, which is the uncomfortable part — the tests
could not have told you, because they were passing. It is the "helper laxer
than the node" defect arriving through a fixture instead of a helper, and the
tell is available in advance: ask what the fixture *omits*, then ask whether
production could ever run with that omission. The new test had the same fault
on its first draft, passing on the plan-root failure without exercising the
ordering it was written for, so it now asserts the blocking list by name.

**A stop the operator asked for must not render as a failure.** `pause` is the
most deliberate stop there is, and it exits **1** and logs under `[escalate]`,
beside a message that says in its own prose that nothing is wrong. A watch
keyed on the tag announced an escalation; the harness reported the run as
failed. Both were reading the only two channels a monitor can read
unattended, and both were wrong. This is the classifier problem at the level of
a run's exit surface — a requested pause and a broken environment are different
events that a machine cannot tell apart, and the distinguishing information
exists only in prose meant for a human. An intentional stop wants its own exit
code and its own tag.


**A write that grows a file in place can be read at its old length.**
`Path.write_text` truncates and rewrites the same inode, and Docker's file
sharing caches a stat nothing then invalidates. Measured on a bind mount: a
comment edit made `Gemfile` 87 bytes longer, the container kept reporting the
*old* size while serving the *new* bytes, and every reader inside saw the file
clipped — bundler announced 79 dependencies instead of 81, resolved without
`redis` and `connection_pool`, and wrote that lockfile back to the host. Two
runs died hours apart. What armed it was a *comment*: all that mattered was
that the edit made the file longer.

**Put a new inode at the path.** `atomic_write` — sibling temp file,
`os.replace` — is what no stale stat can answer for; `git checkout` was
measured and is not a writer of this kind, since it unlinks and creates.
Whenever a tool of ours writes a file another process reads across a boundary
we do not control, the question is not whether the bytes are right but whether
the *name* now points somewhere the reader has never looked.
**A cache-timing fault answers "not reproduced" once and "reproduced" the next
time.** The same three calls — resolve, edit, install — were replayed twice
against the same clean tree. The first run stayed green and I reported it as a
result; the second reproduced the corruption exactly. Nothing differed but the
timing of a cache. A single clean replay of a race is a *false negative*, and
reporting it as evidence of absence is how a live fault gets argued away. Say
"did not reproduce on one attempt", never "does not reproduce".

**Hedging is what protecting a hypothesis looks like from outside.** Two
identical crashes were called deterministic here, in those words. When the
operator proposed replaying the sequence, the answer that came back was that
the precondition had been cleared and it "probably won't reproduce" — an
unfalsifiable reason not to run the experiment, produced to defend a
stale-cache story that had never been established. Both claims cannot be true.
The operator's flat "it's deterministic, run it" was right, and the run
produced the whole diagnosis within two commands. When a proposal to test
something is met with a reason it will not work, check whether that reason was
measured or invented — and note that the measurement offered in its defence
(the container's view was healthy) was the *precondition of both failures*
rather than protection from them.

**A conversation with batched parallel calls cannot be read positionally.**
The executor emits several tool calls per turn — five `read_file`s, then five
outputs — and a reader that pairs each call with the next output keeps only
the last call of each batch and drops the rest. That produced a confident,
wrong account of which `bundle` call corrupted a lockfile, including a
"refutation" of the truncation theory that was really an artifact of the
scramble. The transcript could not be paired properly either, because
`_plain` built each line from a hand-written list of `type`, `name`,
`arguments`: `call_id` reached the record on the *outputs*, which are dicts
passed through whole, and never on the calls that declare it. It is a denylist
now — a field nobody thought to add is invisible, a field nobody thought to
exclude merely costs space. This file already stated that rule about
`executor-loop.json`; the identical defect was sitting one module over, which
is the standing lesson that nothing re-reads what predates a rule.

**A record published from a run is a claim in every later prompt.** A crash
left two planner notes unpublished; the checkpoint still held them, and on
resume they were rewritten and committed. That was reported as good news. It
was not: one of them diagnosed the very failure that had just been backed out,
and it reached the progress log — which is spliced live into every planner
call — as `kind: correction`, aimed at rewriting a plan document. Its basis was
a single `read_file(bin/dc_start)`: the planner had restated that file's own
comment in its own voice, with no measurement between them, and its causal
claim was refuted by the timeline within the hour. Before restoring a pending
note, ask what it asserts and whether the work it describes still stands.

**A watch must print the anchor it is using.** `A=$(wc -l < file)` carries
leading whitespace on macOS, so `A=  109580` made the shell try to execute
`109580`, left the variable empty, and turned `tail -n +$((A+1))` into a read
of the whole file. The watch then matched a pause line from a previous run and
announced an escalation that had not happened. The rule about anchoring a
monitor to this run was already written here; what was missing is that the
anchor itself is a measurement and can be wrong. Have the watch state its
anchor on the first line, where it is checked by whoever reads the output.

**And `str.index()` on a repeated heading is a regex mistake in a costume.**
Excising one note from the progress log by slicing between two located
headings duplicated 221 lines instead of removing 8, because the second
heading's text occurs once per stage and `index()` found an earlier one. The
file was restored and the second attempt asserted both boundary lines by
content before deleting. "Read artifacts; do not regex them" is usually read as
being about patterns; a string search for a delimiter that the format repeats
is the same bet with different syntax.

**A relative path is a decision the launch command makes, and it appears in no
config, no log and no artifact.** `PRICE_MAP_FILENAME` was a bare
`"model-prices.json"` at three call sites, so litellm's rate table cached
against the *process cwd*. Launched from beside the plan documents — the
obvious cwd — 1.76MB of somebody else's JSON landed in a tracked directory of
the target repo, was swept onto a stage branch by a `checks` commit, and the
revision prompt was refused at 1,020,584 tokens against a 1,000,000 ceiling.
1,807,718 of that block's 1,829,531 characters were the one file; every other
file in the diff came to 12,266.

**A `.gitignore` line suppresses the symptom in the repository that noticed and
leaves the mechanism running everywhere else.** That is what had already been
done once.

**Project a third-party copy rather than keeping it whole.** The table is 3,055
entries and a run prices three — 5,929 bytes rather than 1,758,871. Avoiding a
hand-maintained rate table by making a verbatim copy of someone else's is the
`hold the path, not the copy` instinct honoured at the config layer and
abandoned one layer out.

**And the diff a revision prompt carries has no ceiling.** `max_chars_per_call`
bounds what the planner *reads*; this arrives by a channel with no budget on
it.
**And the guard existed at one of the two call sites.** `executorloop` memoised
the table with a comment explaining that the loader reaches the network on
every call and that pricing per attempt without one would make hundreds of HTTP
calls a run. `nodes._stage_spend` called the loader directly and is reached
from `advance`, so every landed stage refetched 1.76MB and rewrote the file.
Same reasoning, same module pair, written once. The memo is in the loader now,
where a third caller inherits it rather than having to remember it — the same
answer as the transcript being a `list` subclass.

**A test that searches for a constant's value cannot find the code that names
it.** The test written to pin the single path selector asserted that no other
module contains `PRICE_MAP_FILENAME` — and passed against all three offenders,
because the imported symbol evaluates to `"model-prices.json"` while the code
in question spells the *identifier*. It reads exactly like a test that
verified something. Grep found the three files in one command; the test found
none, and would have gone on approving them. When a check is written over
source text, run the equivalent search by hand once and make the two agree
before trusting the green.

**Two green tests can contradict each other if neither drives the seam between
them.** `nodes.execute` returns `_escalate(...)` on a refused commit;
`EDGES["execute"]` never listed `escalate`, and `driver._next` raises rather
than rerouting. So a careful escalation came out as `RuntimeError: node
'execute' routed to 'escalate'`, 16 landings into an overnight run, when a hook
refused three lines of trailing whitespace. `test_commit_refused` asserted the
node returns `escalate`; `test_edges_match_the_spec` asserted `escalate` is
unreachable from `execute`. Each passed and each was right about its own end.

**Derive the table from the code rather than maintaining both.** `EDGES` is
checked by parsing `nodes.py` for every `next_hop` each node can return,
resolved to a fixpoint through module-level helpers — the second time the two
had drifted, the first being `execute → execute`.
**A ban can be a bug wearing a design constraint's clothes.** Three assertions
forbade `review` escalating, one named `test_review_cannot_escalate_directly`
with the reason "a rejected stage is a planning problem, not a human's
problem". True, and it does not cover a full suite killed by a signal *after*
approval, where the work is correct and the environment is gone. The category
was drawn around rejections and a different kind of exit was added underneath
it years later, so the tests read as stating a rule while describing a crash
nobody had hit. When a test forbids something, check that the thing it forbids
is the thing its reason is about.

**Three maintained statements of one fact, none compared.** Both escalation
branches were correct, commented and covered; the edge table was correct and
covered; the architecture document had drifted from both.
**Ask the question that expires first.** For each gate, ask whether its subject
still exists after the step below it. Most read the tree and the tree is still
there; a pre-commit hook reads the *index*, and the commit consumes it. One
refused three lines of trailing whitespace and ended a run 16 stages in —
`commit_refused` escalated correctly, because by then there was nothing left to
do, while the same refusal asked *before* the commit is a cycle the model fixes
in session from a message that names the file and the line.

**Two reasons an operator could not have fixed it in config.** `_gate_cycle`
commits the model's raw work *before* `checks` runs, deliberately, so a
linter's rewrite lands as its own attributable commit — which puts every
autocorrecting entry downstream of the commit a hook refuses. And the hook was
`core.hooksPath` set **globally**, so it is not the target repository's
property at all and nothing in its config describes it.
**Run the thing rather than modelling it.** The tempting fix was to strip
trailing whitespace on write, beside the normalisation the editor already does.
That fixes one hook and no other — a hook is operator policy, and the next rule
it grows is not ours to predict — while normalising files behind a model that
has stopped, in formats where two trailing spaces are a hard line break. `git
hook run pre-commit` (git ≥ 2.36) invokes the hook exactly as a commit would:
same cwd, environment and argv, so the gate cannot disagree with the commit it
stands for.

**Two facts that recall would have got wrong.** `git hook run` exits **1** with
"cannot find a hook named pre-commit" when there is none — the same status as a
refusal — so presence comes from an executable existing at `git rev-parse
--git-path hooks/pre-commit`, which resolves `core.hooksPath`; matching the
message instead would be the classifier-over-rendered-text mistake again. And a
pre-commit hook reads the **index**, so the gate stages first or it passes on
work the commit is then refused for.

**A gate is a check, not a guarantee.** `commit_refused` stays: passing means
the hook accepted those staged bytes, not that the commit will succeed, since a
hook may read the clock or the network. Removing a backstop because the check
usually catches it is how the unusual case becomes a stack trace.
**A rule fixed in one role's type does not reach the role using the other
type.** `PlannerUsage` grew `peak_prompt_tokens`; the reviewer uses
`TokenUsage`, which did not — so eleven records carried tool-loop totals from
473,595 to 2,211,906 and **no context figure**, while the log line renders a
total as `(N prompt, M cached)`, which reads exactly like one. I twice reported
the reviewer as near its ceiling on that basis. The totals track *call count*:
8 calls → 473k, 32 → 1,505k, about 45-70k of real context each, or 6% of the
window rather than the 143% the largest total appears to say.

**Fix it where one reading is its own peak** — `extract_usage` — so no call
site can forget and any caller that merges gets it free.

**And add the field last on a positionally-built dataclass**, where a new field
in the middle silently reassigns every positional caller.
**When a fix moves a value into a shared type, grep for the private copy the
shared one was modelled on.** `ExecutorTurn` tracked its own peak years before
`TokenUsage` had one; putting the field where it belonged left both — correct
that day, one number in two places afterwards. It is one computation in
`merge_usage` now, with a test asserting no second copy. Noticing a duplication
and writing it up is the worse half: a duplication reported is a duplication
shipped.

**A budget nothing records cannot be seen to be near.** With no peak recorded,
"how close is the reviewer to its window" was unanswerable from the artifacts —
the state the planner was in before being rejected at 1,103,000 tokens against
a 1,000,000 ceiling. For scale: the reviewer's `max_total_chars` resolves to
2,400,000 here, roughly 600,000 tokens or half the window, reachable by reads
alone; measured consumption peaked at 71k. Nothing is near it, and until the
figure existed nothing could have said so.
**Attribute bytes by what ran between, not by whoever is nearest.** A stage
branch carries several commits per cycle, and "the executor's work" names one
of them. A hook refused three lines of trailing whitespace and I called them
the executor's, twice. They were `rubocop -A`'s: the stage's own commit landed
clean at 06:11:22 with the model's edit byte-for-byte as recorded, the linter
ran at 06:11:25, and the after-checks commit was refused at 06:11:34. Before
attributing a byte, find the commit that last held the file clean and enumerate
what ran after it.

**And a gate written for the first commit does not cover the second.** The
hook gate asks above the *first* commit, which succeeded; the refusal came on
the one after `checks`. "Checks run after the commit the hook refuses" is true
of the first and false of the second, and conflating them made a
correct-looking gate miss the only occurrence anyone had seen.
**And it is worth knowing what the linter is doing when nothing refuses it.**
The collision with the hook is the only reason this surfaced. `rubocop-erb`
has been reformatting ERB on every stage that touches one, the reviewer
approves it because a tool's own rewrite is declared machinery rather than the
stage's diff, and the landed file is now worse formatted than what the model
wrote. A gate exemption suppresses the complaint while the effect keeps
accruing — the same finding as the ten CRLF files, arriving through the
`checks` layer instead of the editor's.

**A watch that exits on the process must re-read the log after it.** The loop
`while kill -0 $pid; do grep …; sleep 30; done` checks, sleeps, and then leaves
by its *condition* — so a run that escalates and exits inside one sleep is
reported as "exited with no escalation", over a log containing the escalation.
It happened on the night's first watch: `[escalate] the planner blocked the
run` was sitting in the file while the watch said there was none. The anchor
was right, the pattern was right, and the control flow discarded the last
observation. Check once more after the loop, for the same reason a monitor is
anchored to this run: the instrument's own shape is the thing most likely to be
lying.

**Send it to the endpoint.** The night this file grew the four entries below,
every one of them was found by starting a run, waiting twenty minutes, and
reading a corpse. Each was a single parameter or a single field, and each was
answerable in one call against the live API with the payload production
actually assembles. The operator's correction is the rule: *write your code,
take the output of that code, send it to the endpoint, see what you get back.*
A stub answers what you taught it; the endpoint answers what is true. Two
probes that afternoon settled the caching question that six hours of reasoning
had not, and the same two commands would have caught all four defects before
the first run started.

**A stub cannot fail the way the thing it stands in for fails.**
`scripts/smoke.py` serves canned JSON, so it catches what raises *client-side*
— a keyword the SDK does not declare — and cannot catch what the server
rejects. Its Messages arm reported 29 checks passed for three consecutive
stages of a live run whose every request came back 400. Adding a check that
the request's content-block discriminators are in
`anthropic.types.ContentBlockParam` closed that particular hole, and the
general form does not close: a stand-in validates what its author thought to
validate. Anything a stub says green about is a hypothesis until an endpoint
agrees.

**Moving one component onto a new axis leaves its neighbours on the old one.**
Making the wire a property of the model moved the *client* and left everything
that builds a request behind, each of which then spoke Responses to a Messages
endpoint: `prompt_cache_key`, a Responses-only parameter, hardcoded at two call
sites; `input_text` and `prompt_cache_breakpoint`, Responses-only content
fields, hardcoded at seven; `extract_usage`, OpenAI's reader, applied to every
response whatever wire it came from. Four defects, one shape, found one live
run at a time over a night. The refactor reviews as complete because the thing
it was about is complete. Ask instead what *else* touches the request, and go
through them before the first run rather than after each failure.

**A parameter no provider declares is not ignored; it excludes every provider.**
`output_config` is Anthropic-native, so through OpenRouter it works where the
upstream is Anthropic and nowhere else — and with `provider.require_parameters`
on, the gateway answers **404 `No endpoints found that can handle the requested
parameters`** rather than 400. Read as a routing problem it sends you looking
at the model; it is a request problem.

**The spelling follows the route, not the model family** — the same model takes
different spellings depending on how you reach it. Measured across all three
endpoints, because recall and the docs were both wrong:

| spelling               | Anthropic direct | OR → claude | OR → gemini |
| `output_config`        | OK               | OK          | **404**     |
| `extra_body.reasoning` | **400**          | OK          | OK          |

`GET /api/v1/models` carries `supported_parameters` per model and is the
cheapest way to ask.
**Cold on the Messages wire means `input_tokens: 0`.** Anthropic reports three
orthogonal counts and the prefix lands entirely in the cache fields on the turn
that writes it. Read with OpenAI's extractor — which looks for
`input_tokens_details.cached_tokens` — a cold turn records **no prompt tokens
at all**, not merely no cache. That is why every `opening_turn` of one run is
`{0, 0}`: the opening turn is precisely the one where the whole prefix is a
write. The dollar figure survived because a gateway puts `cost` at the top
level, which is what made the loss look like a cache-rate question rather than
a token-accounting one.

The operator caught it by reading their provider dashboard against our log
line. `CLAUDE.md` already says to check the instrument before the world; I had
even written that a categorical zero usually means a mechanism, and then went
looking for the mechanism in the provider. A number that is *exactly* zero
across every sample is a reader that cannot see the field, until proven
otherwise.

**A resume hands the planner the failure a human just fixed.** A harness fault
— a keyword the endpoint refuses — is recorded in `opening_failure` and
survives into the resume, which re-enters at `plan` because the failure layer
was the planner's. The planner then reads a deterministic harness error,
concludes correctly that no stage it could draw would change which keyword
arguments the harness sends, and blocks. It re-blocks on every subsequent
resume, having run nothing. `--reset-progress-budget` exists for exactly this
shape one field over, and its docstring already argues the case: "a config
change or a code fix does not clear it by itself; someone has to say that the
earlier failures no longer apply." Nothing says it for `opening_failure`. A
fresh run is the workaround and costs a preflight plus a derivation.

**An empty final turn reads as success.** A model that returns `end_turn`
carrying no text and no tool calls is, to the loop, a model that has finished.
One attempt made 96 searches and 3 reads across 101 turns, applied no edits,
attempted none — `edit_refusals` was empty — produced not one assistant text
block, and recorded `ok: true`, `turns_exhausted: false`, `log: ""`, $0.35. The
scope gate then reports "the attempt produced no changes", which reads as a
badly drawn stage and sends the planner to redraw one that was never the
problem. Finishing and giving up are different events and the wire renders them
identically; only the absence of *any* content separates them, and nothing
looks at it.

**An artifact that renders part of a payload cannot reconstruct it.**
`planner-prompt.md` exists because a rejected prompt left no way to find out
what was in it, and its own docstring says reconstruction "cannot be made to
converge". It renders `messages` only — no tools, no system block, no
structured-output schema. That is 17,622 tokens, and it is the exact region
this project's cache question turned on. Rebuilding a payload from it produced
something 4,300 tokens short, which missed the cache by construction, and the
miss was reported as a reproduction of the very thing being investigated. An
artifact whose purpose is reconstruction has to carry everything the call
carries, or it is a trap laid for whoever trusts it.

**What the planner's cache actually covers, measured.** The cached region is
exactly the pre-message prefix — tools, system block and output schema, 17,622
tokens, matching `count_tokens` to within its own rounding. Everything in the
messages, 276,891 tokens of which block 0 is essentially all, is written fresh
and read back never across derivations. Block 0 is 735,413 characters: the
repository's agent documents 9%, the layout 2%, and the plan documents 89%,
with `progress_log.md` last at 106,156 characters because it is the one that
grows. Every landing rewrites the whole segment.

Three hypotheses were tested and refuted, all at full scale against the live
endpoint: `session_id` does not affect matching (identical payload, fresh
session, full 294,586-token read); the `1h` TTL is honoured and reported in
`ephemeral_1h_input_tokens`; and size does not decay it (293,532 read back
after eleven minutes). One pair of runs six minutes apart with byte-identical
prompts did miss, and it has never reproduced. Report that as n=1, not as a
mechanism.

**A falsification harness is code and can be broken.** Reintroducing a bug to
prove a new test catches it: the patch asserted on a string that also appeared
elsewhere in the file, so the replacement never happened, the assert passed
anyway, and the test's passing was reported as proof it would not catch the
bug. `CLAUDE.md` already says a test that searches for a constant's value
cannot find the code that names it; the same trap is waiting in the throwaway
script written to check a test. Print the diff and confirm the file changed
before believing what the run tells you.

**A prompt must not tell the executor it may not change the file it is there to
change.** The excerpt block was headed "Lines from files you may read but not
change", borrowed from the `read_files` block above it where the claim is true.
`read_excerpts` exists because the planner may not write an after-image, so the
excerpt is very often *the thing being rewritten*: **1,562 of 2,586 excerpts —
60% — name a file the stage's own scope permits**.

**And a prompt must not name a field its reader cannot see.** The rewrite then
said `edit_files` "above is the only thing that decides" — but that block
renders as **Files you may change**, and the string appears nowhere in the
executor's prompt. It is the planner's field name: precise-sounding, pointing
at nothing.

**Both were found by a human reading a prompt, which is the only thing that
finds this class.** Every participant downstream reads it as intended and no
gate compares two sections of one document. Sweep the other roles' prompts for
paraphrases when you fix one; that sweep was clean, which is the useful half.
**A knob that only restricts cannot be the lever you want.** The executor made
6,645 tool calls across 6,764 turns on one run — **0.98 per turn, never once
more than one** — while a tool loop re-sends the whole conversation every turn,
so one stage paid 1,186,709 prompt tokens for a 45,806-token context. The
natural suspicion is that something in our request suppresses batching, and the
natural fix is a parameter. Neither survives: `tool_choice` is set for no role
and is on `RESERVED_REQUEST_KEYS` so an operator cannot set it either, the
tools reach the Messages wire in the same shape the planner's do, and *neither
wire has a setting that encourages parallel calls* — Messages offers
`disable_parallel_tool_use` and Responses `parallel_tool_calls`, both defaulting
to permitted. There was nothing to turn on. Before reaching for a parameter,
check which direction it points; a default that already allows the thing means
the knob is for forbidding it.

**And a documented parameter can be accepted and ignored.** Measured on the
gateway route: `tool_choice: {"type": "none"}` returned zero calls three times
and forcing a named tool returned exactly that tool three times, so the
parameter survives the route. `disable_parallel_tool_use: true` — which the
installed SDK's own docstring says means "the model will output at most one
tool use" — returned **2, 3, 2, 2, 2**. The parent is honoured and the
sub-field is dropped in silence. That is worse than a rejection, which is the
standing shape here: a 404 sends you looking, and a silently ignored field
leaves a request that reads as constrained and is not. Anything relied on for
*correctness* through a translating gateway has to be observed doing its job,
not merely accepted.

**What did move it was one sentence, and it was worth measuring first.** The
model defaults to one call a turn and can be told otherwise: sampled five times
an arm against the live route with the same tools and the same wire, the
opening turn asked for one thing 5/5 without a sentence about independent
calls, two 5/5 with one, and a deliberately harder-pushing version bought
nothing further. Told to read three named files it emitted three calls at once
on the first try, so the capability was never in question. The shipped wording
then measured 2, 3, 2, 2, 2 — checked separately, because the experiment's
paraphrase and the string that ships are different strings and only one of them
is in the prompt. Roughly half the turns, which is a real saving and not a
transformation; claiming more would be the capability-versus-effect mistake
this file already records.


**The block is the cache unit, not the prefix.** Ordering a growing document
last inside a marked block does not protect what precedes it: the breakpoint
covers the whole block, so one appended byte rewrites all of it. Block 0 ran to
735,413 characters, was 99.2-99.7% identical to the previous derivation, and
was read back from cache never. Put churn *after* the mark, not at the end of
what the mark covers.

**A breakpoint after content that changes every call costs more than no
breakpoint.** It writes an entry at cache-write rates that nothing ever reads.
Before marking a block, ask what varies between calls; if the answer is
"something", the mark belongs earlier.

**An aggregate cache rate cannot separate a flat cache from a growing one.**
With a large prefix and small per-turn growth, "the opening prefix cached every
turn" and "the prefix extends every turn" both land near 89%. Only a per-turn
series tells them apart, and a sum cannot be decomposed afterwards — record the
series at the item.

**A probe that is not the production request answers a different question.**
Three byte-identical reviewer-shaped calls reported `cached: 0` while
production was caching ~200k a turn on the same route. Assemble a probe from
the same code production calls — schema, tools, effort, cache options, gateway
body — or expect to spend the afternoon chasing a difference you introduced.

**Ask which direction a knob points before reaching for it.** Neither wire has
a setting that *encourages* parallel tool calls; both default to permitted, so
there was nothing to turn on. And a documented parameter can be accepted and
ignored: `tool_choice` is honoured through the gateway — `none` returns zero
calls, a named tool returns exactly that one — while its
`disable_parallel_tool_use` sub-field is dropped in silence. A request that
reads as constrained and is not is worse than one that is refused.

**Deny-list the noise; never allow-list the signal.** A filter written for the
shapes already seen drops the one nobody has seen. Filtering test output to
`^/current/(app|lib|config).*: warning:` would have discarded a first-party
`WARNING: Slash dates in ...`, which is the only warning class that was new.
Exclude what is known to be mechanical and show the rest, whatever shape it
arrives in.

**A stream is not a log.** Warnings written to a process's stderr can never
appear in a file written by an application's logger — they are produced by
things that have never heard of it. One suite put 203 first-party warnings at
five sites on stderr while the tally read only the Rails log, so a green run
reported nothing. Ask which writer owns a file before assuming anything can
reach it.

**A model asks for one tool at a time unless told otherwise, and telling it is
cheap.** 8,083 consecutive calling turns with never more than one call, against
a loop that re-sends the whole conversation every turn. Nothing suppressed it.
A section in the system prompt and a line on each read tool's description moved
it, and the live effect is front-loaded into the opening survey where several
targets are knowable at once — 3.6% of turns, not the 53% a synthetic harness
predicted. Measure the lever where the work happens, not where it is easy.

**Every gate says why; one did not.** A stage landed clean, the next printed
its precheck header and `[plan] revising` one second later, and nothing in
between named the cause. The reason reached the planner and the checkpoint —
the two places a person does not look. When a check routes, it logs.

**A guard and the reset it depends on are one decision written in two
places.** `precheck` resolves the stage's model "only when unset", which is
what lets a revision keep the model its stage was given — and nothing cleared
the field between stages. So it locked the first stage's answer for the whole
run: exactly the per-run behaviour the change existed to remove, keyed on
whichever stage happened to be first. Both halves were written in the same
commit and contradict each other; the comment beside the guard claimed the
value was "always overwritten at the next precheck", which the guard is
precisely what prevents. Observed within the hour: 042 resolved, 043 cut its
branch and never asked. When you add a field whose meaning is "already decided
for this unit of work", write the clearing in the same edit and name the node
that owns the end of that unit.

**Retry types belong to the SDK the call goes out on, not to the module you
imported them from.** `executorclient` asked `openaiclient.transport_errors()`
for them — right while every executor call was a Responses call, and silently
wrong the day the executor became the wire-polymorphic role. An
`anthropic.APIStatusError` is not an `openai.APIStatusError`, so on the
Messages wire `retry_on` matched nothing and every failure propagated on its
first raise: no backoff, no log line. **Nothing on that wire had ever been
retried** — not 429s, not dropped sockets. Four attempts died in four seconds
and spent a stage's whole rework allowance without one request reaching a
model. It is a property of the dialect now, beside `client`, and raises rather
than defaulting to empty, because no retrying looks identical to nothing
having failed.

**A rejection of a *pointer to a cache* cannot be answered by sending it
again.** `Cache content <id> is expired.` arrived four times identically, with
the gateway recording no generation for any of them — rejected before dispatch,
zero tokens. The conversation was never the problem: a handle built for our
marked prefix had died. It is excluded from the spurious-400 budget and
answered by resending the same context with nothing marked, cold for the rest
of the attempt rather than for one request, since a handle that is dead now is
dead next turn.

**A ratio that is exactly constant is the instrument.** Messages-wire opening
turns reported `prompt == 2 x cached` on 282 of 356 samples — 36,420 and
18,210, unchanged across 30 stages whose instructions differ. Our normaliser is
right (`input + read + written`); the gateway reports the whole prompt as
`input_tokens` *and* again as a cache read. Settled without a probe, by
measuring what the request can possibly contain: the executor's whole payload
is ~21,300 tokens, so 36,420 is more than the prompt that exists. Every
Messages-wire prompt figure we have recorded is inflated about 2x.

**A check that exists as a side effect of something else disappears when that
thing moves.** The executor's credentials were never checked at preflight — it
had `GET /models`, which proves an endpoint exists and nothing about whether we
may call it. What made the gap invisible was `resolve_policy` making a real
authenticated completion whose line printed among the preflight output, and
only when the configured model was a routing policy. Moving that probe to stage
start took the accident with it. Ask of each check what it is *made of*, not
what it appears beside.

**Measured, and worth not re-deriving.** The executor's request is ~85,300
characters: **88% identical stage to stage** — system block 9.4k, tool schemas
20.0k (17 tools, 80% prose), the target repo's `AGENTS.md` whole at 47.5k — and
7.8k of per-stage Task, constraints, file lists and excerpts. Cross-stage cache
carryover is therefore worth **under 1%** of an attempt, because a first
attempt spends ~1.97M prompt tokens re-sending the conversation every turn;
within-attempt caching runs 84-95% and is untouched by changing model between
stages. Cost per landed stage, all three roles: **$5.94** on
`gemini-3.7-flash`, of which the executor is $0.79 and derivation $3.47.

## Where things live

`nodes.py` holds the loop's decisions — which failures route to the executor,
which to the planner, which to a human. `verify.py` is the layered gate, ordered
cheapest-first, short-circuiting. `config.py` is the safety story: the capability
partition and the command denylist. `prompts.py` is pure string building, kept
apart from the clients so it can be tested without a model. `state.py` documents
why `completed` and the two retry counters are shaped as they are, and holds the
reset helpers — anything a resume or a landing must clear belongs there rather
than inline at the call site, because inline has no seam to test at and a value
that is never cleared is invisible until it is reported back to an operator.
`runtime.py` assembles the collaborators and is where anything the model clients
need from the run — the log, the config, the repository — is bound to them;
values wired there are exactly the ones with no unit test on either side, so
they get an end-to-end one. It also holds `pin_modules`, which is the reason
this codebase can be edited while a run is live: several modules are imported
inside functions to break cycles, so before it existed a module not yet loaded
was read from disk at the moment it was first needed, putting new code in front
of an old class already in memory. That is not hypothetical — it stopped a run
with `AttributeError: 'ExecutorConfig' object has no attribute
'semantic_search'` forty seconds after an edit. The worst property of it was
that the same edit was harmless whenever the module happened to be cached
already, so every time it worked taught the wrong lesson. After `pin_modules`
returns, a live run finishes on the code it started with, and edits take effect
at the next start.

`dialects.py` is what replaced role-decides-wire. Two dialects, RESPONSES and
MESSAGES, and `dialect_for(model)` maps a model family to one of them —
answering with RESPONSES for a family nobody has classified, because a router
can resolve to anything and an unknown model must not end a run. A dialect owns
every spelling that differs between the two endpoints: structured-output kwarg,
effort kwarg, text-block type, cache markers and their TTL, request cache
options, the cache-key parameter, tool schemas, reading tool calls, echoing the
model's turn, shaping tool results, stop detection, refusals, closing text,
splitting the system prompt, the base-URL suffix, usage normalisation, and
building the client. **Shape belongs to the endpoint, not the vendor** — the
same Google model returns `function_call` items on Responses and `tool_use`
blocks on Messages, and a Google model on the Messages wire is reached through
the Anthropic SDK. `normalise` translates a conversation built in Responses
vocabulary into the other wire, at `send` rather than at the seven places that
construct blocks, because the eighth will not remember.

`gateway.py` is what OpenRouter needs, decided from the endpoint host rather
than declared in config — `session_id`, `provider.require_parameters`, and the
effort spelling, which follows the route rather than the model. It also holds
`resolve_policy`, which turns a routing policy into the model it picks today
with one throwaway call, because nothing reports what a router *would* choose.
`wirecheck.py` warns when a role's configured model wants a wire that role
cannot speak; the planner and reviewer each call their own SDK method and are
pinned to one wire, so only the executor is wire-polymorphic.

`executorclient.request_extras` is the single assembly of every top-level
keyword the executor's call carries. It is one function because two outages
were a keyword the endpoint does not take, and both were then pinned by a test
that rebuilt the dict by hand — a copy of an assembly is not a check on it. An
AST test asserts the loop adds nothing beside it.

`executorclient` also holds `REPEAT_NUDGE_AT` and `REPEAT_ABORT_AT`, which
bound consecutive tool calls carrying byte-identical arguments. At the nudge
the answer is *replaced* by a sentence naming the tool and the count, because
a model that has ignored a payload twice will ignore it under a warning, and
re-sending it is most of what the turn costs. At the abort the turn ends with
`repeated_call` set, which travels to `ExecutionResult`, into
`executor-loop.json`, and through `nodes.execute` into `executor_note` — the
one stop for which that note is published on a first attempt, because every
other stop on that branch is a decision the scope gate can fairly summarise as
"produced no changes" and this one is not. **When an attempt reports no
changes, read `repeated_call` before believing the stage was badly drawn.**
The guard is on the executor only: the reviewer has not shown this behaviour,
and a ceiling added where nothing is hitting it is a policy nobody chose.

`gates.py` is the layer shared by the executor's loop and `verify.py` — patterns,
residue, new tests, checks, tests — so the two cannot select different test
paths, which they had done, correctly, for five separately-incident-shaped
reasons — and it returns the selection *sorted*, so the loop's record and the
gate's question are the same string whenever they are the same set. `flake.py`
decides whether a red suite is the stage's fault or the suite's, and writes
`flakes.jsonl`: one append-only record per excusal carrying the file, the
ordering seed, the runner's own locators for the examples that failed, and which
run and stage — or `origin: preflight` — produced it, which is what makes "which
flake is worst" a sort rather than a log scan. JSONL because the markdown it
started as was parsed by a five-group regex whose optional tails made "not
captured" and "written before that field existed" the same bytes.
`edittools.py` is the write-side counterpart to `repotools.py`: no
model, refuses with `ToolError`, records what it did. `executorloop.py` is the
cycle itself — edit until the model stops asking, lint, **commit, then test** —
and `executortools.py` and `executorclient.py` are its schemas and its provider
call. `repotools.number_lines` is the single renderer of numbered source; three
copies of that format string is how it drifted while every test stayed green.

`addendum.py` is where a planner note is *routed*, not merely formatted:
`LOGGED_KINDS` decides what reaches the progress log and therefore every later
prompt, and `append_findings` writes the rest to `findings.md` at the top of
the work directory, uncommitted and read by nobody. The filter lives in
`append_notes` rather than at the call site, for the reason the transcript is a
`list` subclass — a routing decision the caller has to remember is one a later
caller will not.

What the planner is sent, measured on a real derivation: 540,622 characters in
three blocks, of which the plan is **89.5%**, the repository's own agent and
operations documents 8%, the layout 2.5%, and everything about *this run* —
the cost table and the completed-stage list — **0.5%**. Two channels were
removed to get there and both were the planner reading its own prior output:
the deferral list, and a 4,000-character tail of `status.md`, which carries
`status_entry` and `reasoning` verbatim. `status.md` is still written; nothing
reads it back.

`projecttools.py` is the menu an operator adds to the eight built-in tools:
`ProjectTool` in config declares a name, a description and an **argv list**,
and the executor calls it as it calls `read_file`. Argv and never a shell is
the whole safety story — a model-supplied value is one inert element, so there
is no metacharacter to escape — and a placeholder must occupy an entire
element, which is what stops a value being interpolated into a larger string.
An operator who writes a shell into their own config has chosen that; the
protection is on the model's arguments, not on the operator. Nothing gates
which stage may call which tool, deliberately: the scope gate already measures
the outcome from the tree, and a per-stage permission would be a claim used to
predict what an existing gate observes.

`repotools.Spend` is everything mutable about a read budget in one object, so
clearing it is replacing it rather than zeroing a list of fields; `count_calls`,
`count_refusals` and `render_counts` are the one summariser all three roles
report through, after each had grown its own.

`pricing.py` turns token counts into dollars from a table nobody here
maintains. `price_map_path` is the single selector for where the cache goes —
under `work_dir`, which is gitignored by construction, and `None` rather than a
cwd-relative fallback when there is no work dir, because "somewhere arbitrary"
is what cost a run. `project_entries` keeps only the models `configured_models`
names, entries whole: projecting by *key* would be a hand-written subset of an
upstream schema, and it would save kilobytes on a file that is now kilobytes.
`cached_price_map` is the one memo, so a caller cannot fetch per landing.

`configversion.py` is what replaced `approval.py`: a config is identified by
its git blob sha, recorded at run start and checked on every resume, so an
edited config refuses to continue a run rather than needing a command run
against it. `ProjectPaths` is built from `cfg.work_dir` and there is no slug —
the work dir is the project's identity, which is what the prompt cache key
needs and the one thing a derived handle could disagree with. `cachekey.py`
bounds that identity to the provider's 64 characters, because a path is longer
than a slug.

**A project's config lives in the repository it describes**, beside the plan,
with `.code_gantry/` gitignored next to it for everything the run writes.
`target_repo`, `work_dir` and `host` are all absent from it: the first two are
derived from where the file was read, and the third was a note to self that
became somebody's hostname the moment the file was tracked. `planner.guidance`
is empty there and the comment in its place records why — every paragraph it
held was either the machine describing itself, or already arriving through the
stage-costs block, or a hand copy of a live feed that the feed had overtaken.

`executor.py` is now only what shapes an attempt before it starts — the read
budget, the excerpts, the conventions — plus `run_script_stage`. The subprocess
editor is gone (2,418 lines), and with it nine `ExecutorConfig` settings. A
removed key is reported by `extra="forbid"` exactly as a typo is, which was
worth a naming-and-replacement table while there were configs still carrying
them; there are none now, and the table went with the last entry rather than
being kept warm for a hypothetical one.

`scripts/smoke.py` stands up one HTTP server for all three roles and no binary
on `PATH`. It used to plant a stub executable, back when the executor shelled
out; keeping that after the switch would have driven a code path production no
longer takes, which is the "a test suite can be exercising the path you are
about to delete" failure with the roles reversed. A test asserts the stub is
gone, because that is the sort of thing that grows back.

The docstrings carry the reasoning, usually including the incident that produced
it. They are worth reading before changing the behaviour they describe.
