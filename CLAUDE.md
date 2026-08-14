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
there is no reason not to run the whole suite. `-n auto` is the difference
between about 25 seconds and several minutes on 1,400-odd tests, and the suite
is the inner loop of working here, which is why `pytest-xdist` is a dev
dependency rather than a nicety. It is not in `addopts` because a single-test
run pays worker startup for nothing; add it whenever you are running more than
a file.

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
grants it.** `PLANNER_SYSTEM_PROMPT` stated flatly that the executor "cannot
run commands" and has "no tool for running anything". True of every project
until `project_tools` shipped, false the same day for any project that declares
one — while the plan documents, written by people who knew, said the opposite.
The planner found the contradiction, reported it correctly and *withheld the
work*: "the two documents and the pipeline contract disagree, so check which
holds before drawing one of these". A stream of dependency work went undrawn on
the strength of a sentence in our own prompt.

That is the expensive failure direction, and it is worth naming as a general
shape. A missing capability produces a stage whose premise the code
contradicts, and the gates catch it. A *phantom* constraint makes work read as
blocked — and a stage that is never drawn leaves no artifact for anything
downstream to find wrong, so nothing catches it at all. It surfaced only
because the planner is asked to report contradictions in the plan; without that
channel it would still be true.

So the capability paragraph is built from `cfg.project_tools` rather than
asserted, and a project declaring none reads exactly what it read before. The
same reasoning put the *wrong dependency* case into `PlanNote.kind`: the plan
states what depends on what, the code decides whether that is true, and a
prerequisite that does not exist can hold an item closed for the life of a
project.

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

**A feature's original role is the one that never gets scoped.** `project_tools`
was built for the executor, so `executortools` took the whole declared list —
by history rather than by decision. Adding a `roles` field and wiring the two
new roles through it would have scoped two of three and left the founding role
reading every declaration, and it would not have failed loudly: the tools most
likely to belong to another role are read-only, so the symptom is a boundary
that holds everywhere except where the feature started. Whenever a capability
grows an audience, the first thing to check is the caller that predates the
audience existing.

The corollary is where the boundary goes. Scoping only where the schema is
built is the `search` glob leak again — a filter over what is *advertised* is
not a constraint on what is *reachable*, and a model can name a tool it was
never offered. Both the schema builder and the dispatcher scope through one
selector, `for_role`, called with the same role argument; that is one function
evaluated twice rather than two filters that can drift, and it is why neither
caller is allowed to hand over a pre-scoped list. A caller that could pass the
wrong scope makes the wrong scope expressible.

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
There is no memory across runs, so a good result is not carried anywhere — a
model decides whether to call a tool from the description in front of it and
nothing else. The index decides what a call is *worth*; the description decides
how many calls *happen*. Which means the two can never confound a measurement,
and it is worth knowing before designing an experiment to separate them: I
proposed landing an index fix and a prompt edit separately so the effect could
be attributed, and there was nothing to attribute. It also means a usage rate
is not a verdict on value. Semantic search sat at 0.9% of calls while returning
five 2007-era migration filenames out of six hits; excluding `db/migrate` and
an archived progress log from the index turned the same question into the three
links of the chain it was actually asking about, with no change in score — the
top hit moved 0.696 to 0.679. Rank was never the signal. What changed is what
it was competing against.

Reproduced end to end six weeks later, and the contaminant had become *our own
output*. Replaying eight recorded queries against the live index: the live
progress log, the plan tree, and `.claude/skills/` between them took 6 of 42
hits, one of them a reviewer summary of a stage that had landed twenty minutes
earlier, scored above the model the executor was asking about. Three successive
exclusions — `docs/*/progress_log.md`, then `docs/*/*`, then `.claude/*` — took
it 6/42 → 5/48 → 1/48 → **0/48**, while every top score stayed put (0.679 to
0.688, 0.740 to 0.744, both explained by landed stages moving the corpus). The
freed slots on one query became the view that renders the link and the
controller method behind it. Same finding, twice, by different routes: the win
is in what loses.

Two things that only the second pass showed. The exclusion list lives in the
*target repository's* indexer, so nothing here can pin it and nothing here will
notice when it regresses — the pipeline's own artifacts are indexed by a tool
this codebase does not own. And a bad query stays bad: the one call that was a
stage id plus loose keywords still returns three unrelated classes whose names
happen to share one token with it — four of six hits matching on that token
alone. It was searching for a document rather than asking about code, and removing the
document it was chasing does not turn it into a question.

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

**The executor is not free any more.** The economics the design rests on —
planner at 91% of tokens, executor at 2.2% of prompt volume — were measured
against a local model on a Spark. A hosted executor invalidates both, so
`stage-costs.md` now carries dollars beside the context figure. The
subprocess editor reported cost only when its rate table knew
`input_cost_per_token`, so a zero meant "not priced" as often as it meant
"free"; and its cache accounting read two providers' fields but never OpenAI's
`prompt_tokens_details.cached_tokens`, so a silent zero there was the
instrument, not the cache. Measured directly at
the API: an identical 16k prefix caches at 99.9% on chat/completions with
nothing configured — **and that figure does not cover the path the executor
now takes.** It routes through `openai/responses/<model>`, and the measurement
was made against chat/completions, which is the same substitution the rule two
paragraphs up was written about: a fact established on the layer beside the one
being called. Three artifacts were checked for a reading on the real path and
none carries one — its console log reports `38k sent` with no cache fields,
its raw prompt-and-response dump holds no usage block at all, and
our own accounting bills every token at full input price while
`executor-model.json` declares a `cache_read_input_token_cost` it never
applies. So the honest state is *unmeasured*, not *zero* and not *99.9%*.

Left unmeasured deliberately, which is the part worth remembering. Executor
spend is $3.30 across 152 recorded stages — median $0.0079 — against $199.15 of
planner spend on a single run. A perfect cache discount there saves about the
price of one planner call, so the measurement would buy a number with no
decision attached to it. The instinct to close an open question is right about
the question and wrong about the priority: what makes a reading worth taking is
that something changes depending on the answer.

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
meaning.** The transport retry was built for a dropped Wi-Fi and its category
was written as *the request never arrived* — `APIConnectionError`, and the
docstring said so. A 529 `overloaded_error` then ended a 29-stage run at 09:43
with the work intact: the request arrived, and the provider said come back
later. Same outage, same correct response, outside the category because the
category described the plumbing rather than what the failure meant. The
question to ask of any such set is not "is this complete" — it looked complete
— but "what is this a set *of*", and whether the name would still hold if the
same event reached us by a different route.

The fix is also a small lesson in the rule below it. The obvious
implementation is a list of exception classes, and it is wrong: 529 is
`OverloadedError` on the Anthropic SDK and `InternalServerError` on OpenAI's,
so a class list is right in exactly one of the two files that need it. Both
SDKs decide by status code in their own `_should_retry`, and so does
`is_transient_status` — which the tests pin *against the installed SDKs*, so a
provider that changes its mind fails a test here rather than stopping a run at
3am. 429 is included, which would be unsafe as an SDK `max_retries` honouring
`retry-after`, and is safe here only because the wait is bounded by our wall
clock.

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
planner drew it from; the progress log is spliced from the worktree on every
call, because freezing it had the planner reading 6,680 bytes of a 114,554-byte
record. Both are right, and together they have a seam: a fold moves content
*out* of the log and *into* the plan documents and `AGENTS.md`. A resume
inherits `plan_sha` in `**saved` and re-reads nothing, so it sees neither copy
— the live log no longer carries it and the pinned documents never did. From
the planner's side a fold is then indistinguishable from deleting the log. The
two policies are only consistent while the live document is append-only, which
is exactly what a fold ends. `_plan_unmoved` refuses the resume rather than
re-reading, because `current` and everything queued behind it were derived
against the old text and loading the new does not make them valid. Ask of any
two inputs read at different revisions whether anything ever moves between
them.

It fired for real, and not on a fold. A paused run refused to resume because
`AGENTS.md` had changed — a human, in another session, had rewritten the
paragraph on `action_on_unpermitted_parameters` to say the opposite of what the
pinned copy said: `:raise` in test and development where the old text said
nothing raises. Resuming would have planned against the reverse of what the
branch now does. So the guard is not about folds, which is all the prose above
describes; it is about *any* edit to a pinned document, and the most likely
author of one is a person working in the same repository for unrelated reasons.
A fresh run was the only way forward and cost almost nothing, because the queue
was empty and landed work lives on the project branch — the expensive case is a
refusal with a derived stage and a queue behind it.

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

**Hiding a tool's own churn from the gate hides it from everyone.** The editor
normalises line endings on every write, the rule above says the machinery must
declare that rather than let each planner rediscover it, and `gitops`'
`ignore_line_endings` duly keeps the churn out of the diff the reviewer judges.
All correct, and the consequence was not noticed for 77 stages: **10 tracked
files were silently converted from CRLF to LF** — 4 `.js`, 5 `.erb`, one
`.haml` — each landing as a whole-file rewrite that no participant ever saw.

**The follow-up measurement is the more useful half, because it refuted the
reason first given for caring.** That reason was that the repository had been
made *mixed* where it was uniform. It was never uniform: 552 of 3,184 tracked
text files carried CRLF at the run's base sha, 17%, and the count is 542 now.
Ten files moving to the majority convention does not meaningfully change a
repository that has been 17% CRLF for years. The count has also not moved since
— this was a burst, not a rate — so there was nothing accruing to stop. The
residual cost is `git blame` on ten files, already spent, and unrecoverable by
acting now. The right answer was to do nothing, and the operator's default of
doing nothing was better calibrated than the write-up that prompted the
question.

Keep the rule and distrust the alarm. Declaring a behaviour to the one
participant that would otherwise reject it is not the same as accounting for
it: a gate exemption suppresses the complaint while the effect keeps accruing
where nothing is looking, so an exemption wants a counter or a periodic look at
what it has been swallowing. That is exactly how the ten were found. But a
finding produced that way arrives without a magnitude, and the instinct is to
supply one from the shape of the thing rather than from a measurement — *mixed
where it was uniform* was written without ever counting the base. Ask what the
number was before, not only what it is now.

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
`nodes.py`, each with its own one-line helper — a regrowth of the duplication
`clip_for_model` was extracted to end, one level up: the function was
centralised and the number it is called with was not. Nothing fails when two
copies of a constant disagree; one role simply starts giving a model less of a
failure to read than the other.

The first test written for it forbade any function called `clip` outside
`gates`, and failed immediately on `verify._clip` — a one-line delegation
carrying the reason the ordering inside it matters. A named wrapper is not the
failure mode; a second *application* of a budget is. Rewritten to assert that
`FEEDBACK_OUTPUT_CHARS` is spent exactly once, it leaves the other budgets
alone, because a lint diff and a one-line log note are genuinely different
decisions rather than copies. A test that bans a word forces unrelated things
to be inlined to satisfy it.

The same sweep is worth running deliberately rather than by accident: parse
every module and list the names defined in more than one. Of five, four were
delegating wrappers whose docstrings said why, and one was this.

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

**A tool can read the wrong stream, and the layer was right.** `search` passed
ripgrep no path argument. Given none, ripgrep searches **stdin** whenever stdin
is not a terminal — so the tool worked at a shell and returned nothing from
`subprocess.run` with an inherited pipe. Measured against one literal on a real
repository: inherited stdin `rc=1, 0 files`; `stdin=DEVNULL` and an explicit
path both `rc=0, 399 files`. Nothing warns and nothing errors; every search is
simply empty, which reads as "not in this repository" — the same wrong answer
the pathspec bug gave, by an unrelated route, and it would strike or spare a run
according to how it happened to be launched.

The rule above it says to test the layer you are actually going to call, and that
was followed: the calls went through `RepoReader`. What differed was the
*stream* the call inherited, which no unit test and no shell probe can see,
because pytest and a terminal sit on opposite sides of it. Over several hours
this produced five contradictory measurements that were each blamed on the
target repository moving under a live run — a plausible story that was true once
and wrong four times. When a subprocess result varies with nothing you changed,
suspect what it inherited before you suspect the world: argv, cwd, env, and
stdin are all inputs, and only the first two are visible in the command you
think you ran.

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
resume it buys a stop. Before predicting what a resume will do, find the node
it stopped at and read what runs there — the graph entry point is a property of
the checkpoint, not of the command.

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

**A value that fits is a value that fits *where it is*.** `prompt_cache_key` is
capped at 64 characters and nothing had been near it: a slug-shaped key ran to
19. Moving the config into the repository it describes made `work_dir` the
project's identity, the identity a path, and the key 98 — so the first reviewer
call of the first run on the new layout came back 400. Nothing about the value
changed except its length. That is the same door as a value being private only
while its file was private, and the same discipline answers both: when a source
moves, re-read every field it feeds as though seeing it for the first time. This
one was measurable at any point in the six hours between the move and the
failure by taking `len()` of a string.

And the fix was already written once. `executor.py` had met the same limit and
answered `[:64]` at its own call site; the reviewer's site never got it. Blind
truncation is also wrong — two projects under a long shared prefix truncate to
the same key and silently share a cache — so both sites hash through one helper
now, with short identities passing through unchanged so no warm cache is thrown
away by the fix.

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

**And the writer must be the model, not a list of keys.** Both rules above were
written and `executor-loop.json` still carried ten fields of twenty. The four
it omitted — `ok`, `timed_out`, `turns_exhausted`, `log` — are between them the
entire answer to *why did this attempt end*, which is the only question the
per-attempt record exists for. Found by making the mistake: asked why an
attempt stopped after three reads and no edits, I read the file, got nothing
for those four, and was one step from reporting that the loop had not recorded
it. It had — in `executor.log` next door, where the model said the fix lay in a
file outside `edit_files`. The enumeration is also what makes the *next* field
go missing: `commit_refused` was added the same morning and was already absent.
So the writer walks `dataclasses.fields` and a field has to be excluded on
purpose. The rule generalises past artifacts: wherever a subset is written out
by hand, the hand is the defect.

A third instance, in a module nobody would have looked in. `append_flakes`
assembled its line field by field, and `preflight` — written before the
`examples` argument existed — called it without one, so **every baseline flake
this project has ever excused recorded no locator**, silently, for as long as
locators have existed. Nothing was wrong at either end: the writer was right,
the caller was right, and the caller was simply older than the argument. That
is the shape to expect from an optional keyword — a new one does not reach the
call sites that predate it, and the omission is indistinguishable from a value
that was genuinely absent. The record is a dataclass now, and the fix is the
same one twice over: make the writer the thing being written.

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
arrived later, under `max_total_lines`, and nothing taught the reset about it.
`_chars_used` then accumulated for the life of the process, and past the
ceiling *every planner call was refused on its first read* — 14 of 31 on one
run, each drawing a stage with no way to check a premise against the code,
which is the documented cause of all-attempts-zero-diff stages. The cliff is
the tell: stage 025 got 3 reads of 7 and every call after it got zero.

The fix that matters is not a tidier reset. Clearing field by field is a list
somebody maintains, and the next counter is one more line to forget in a place
whose omission stays invisible until a long run crosses a ceiling. The spent
state is one object now and clearing it is replacing it. That introduced its
own hazard worth knowing: something else held the list being replaced —
`SemanticSearch` was constructed with `calls=reader.calls` — so it would have
gone on appending to an orphan, losing every semantic call from the log with
nothing raising. Whatever shares a mutable structure has to reach *through* the
owner, not hold the structure.

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
`search`'s docstring stated the tracked-only boundary as ripgrep's ignore
handling — "ignored files out, untracked-but-not-ignored in" — and named this
operator's convention of keeping identifiable values in ignored files as the
reason it was safe. `-g` is a filter over the walk rather than within it, so a
model-supplied glob overrides `.gitignore` entirely, and the guarantee held for
exactly the calls that named no path. Under `-g '**/*'`: 41 hits out of the
executor's own conversation transcript, 18 out of `tools.log`, and
`planner.json` for the stage being executed — the planner's reasoning, which
the executor is deliberately not given. Then it compounds, because output is
capped: 31 of 369 searches hit the cap, so artifacts winning the first 12k push
the real hits out and the model searches again. A leak surfaced as repetition.
Ask what a stated boundary is *made of*, and re-measure it under the inputs a
model actually supplies rather than the ones the docstring was written against.

The fix carries a second lesson. The first fixture force-added the ignored
files, which made them tracked, which made `git check-ignore` decline to report
them — the test would have passed by making the leak legitimate. A fixture that
has to reproduce an exclusion must be checked for whether it still excludes.

**A cache keyed on a string is keyed on its spelling.** `verify._recorded_answer`
skips the gate's test run when the loop already ran *this command* on *this
HEAD* — two facts compared rather than trust, and exactly right. It compares
the command as text, and the two sides build the path list in different orders
by construction: the gate leads with what the diff says was touched, the loop
with what the stage declared. Same set, different string, and the record missed
in silence. Measured over one run's log: 18 adjacent pairs naming an identical
set of files, **18 of 18 differing only in the order**, 790 seconds of specs
re-run on a tree nothing had touched.

The trap is one level out from the miss. The layer was not granted on that
project, so the duplication read as the operator's choice and the fix looked
like a one-line config change — which would have saved nothing and said
nothing. A switch that reads as the whole story and is a no-op is worse than a
switch nobody turned on, so verify that the mechanism *can* fire before
recommending that someone enable it. `resolve_test_paths` sorts now, which also
means two runs of the same set are visibly the same command in the log; this is
the "same command in both places, spelled the same way" rule applied to the
argument list rather than the flags.

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
The ledger — `flakes.md` then, `flakes.jsonl` now — recorded a file and an
ordering seed for every excused flake — 277
entries, 77 of them naming one feature spec — and could not say whether that
was one example failing 77 times or 77 different ones. Those are different
bugs, and the question is the reason the file exists. The answer had been on
the line the parser was already reading: RSpec ends every failure with a re-run
locator, and `failed_file_pattern` matched that line, took the path out of it,
and discarded the rest. Recovered afterwards from archived logs, it was **three
examples, consecutive siblings in one context** — a shared setup, not three
defects. Ask what the ledger is for, then check that the thing it records is
the thing the question is about.

The recovery is worth its own note, because the instinct was to add a field and
move on: the run directory still held every failing suite's output, so the
history was answerable without writing anything to the ledger. Before proposing
a backfill of an append-only file, ask whether the raw material is still on
disk — the answer arrived from a fifteen-line script and the operator declined
the backfill, correctly, because the file only needs to be right going forward.

**And nothing reads the ledger, which is where the whole doctrine leaks.** The
flake rule is "a file that passes whole and standalone is green", applied per
excusal, with no memory between them. Watched end to end over one sixteen-hour
run: `order_funnel_add_item_spec.rb[1:2:1:2]` was excused **six times**, and I
had already reported that morning that it was the run's worst offender by
example — four sightings then, which is exactly the signal the locators were
added to produce. Six stages landed over it. Then it stopped passing alone, and
the cost arrived all at once: a stage that had passed every gate and been
approved by the reviewer lost its landing to a red suite it had not caused, a
whole extra stage was drawn to repair the spec, three attempts of it failed at
~200s each, and the run was killed.

Every individual excusal was correct. The doctrine has no escalation on
*repetition*, so the second sighting reads exactly like the first, and the file
built to make repetition countable is read by no code — `recent_flakes` has
nine callers and all nine are tests. The ledger made the problem visible to a
human who happened to sort it; nothing made it visible to the pipeline. A rule
that is right about each case and silent about the sequence will let a
deteriorating thing deteriorate at full speed, and the tell is that the record
proving it exists and has no reader.

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
`failed_file_pattern` opens `^\s*`, `\s` matches newlines, and `^` in multiline
mode can anchor on the blank line above — so `match.start()` sat on the
*previous* line's break, and slicing a line from it yielded the blank line.
The extraction returned `{}`: not an error, not a partial answer, an empty
result that reads as "this runner prints no locators", which is the same shape
as the empty search that gets believed. Anchoring on `match.end()`, which is
always inside the line the capture came from, removes the dependency entirely.

Measured before deciding anything: 1,743 locator lines across every archived
log, **every one flush left**, none indented, none preceded by a carriage
return — so the `\s*` had never matched a character of horizontal whitespace,
and dropping it changes nothing (replayed over 1,531 logs, zero disagreements
in files, seeds or locators). But dropping it is not the fix. The next
project's pattern is written by someone else, and correctness that depends on
it not beginning with `\s*` is a defect waiting on a config nobody will think
to check.

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
