# Working on this codebase

[README.md](README.md) is how to use the orchestrator.
[docs/architecture.md](docs/architecture.md) is the design authority on why it
works this way. Neither is repeated here.

This file is for whoever is *changing* the code. It records the invariants that
are easy to break without noticing, and the rules that were learned by breaking
them.

Run the tests with `uv run pytest`. They are fast and need no network — there is
no reason not to run the whole suite.

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
prevents it, so the orchestrator says so once — in the reviewer's prompt, and by
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

**A tool reads more than you hand it.** Aider scans the user message *and its
own reply* for anything path-shaped and attaches the file, with `--yes-always`
answering; there is no flag to disable it, and `--detect-urls` covers URLs
only. Putting a conventions document — dense with paths — into the message
attached `config/routes.rb`, `db/structure.sql` and the rest, reaching 258,854
tokens against a 229,376 limit, so every attempt died in three seconds having
written nothing and the run looped. Files supplied through `--read` are never
scanned, which is where they go now. The general lesson is that the shipped
tool's behaviour is discovered by reading its source, not by reasoning about
what a sensible tool would do: this one was found by grepping
`check_for_file_mentions`, after two wrong theories.

**The same command in both places, spelled the same way.** `rubocop -A` exits
zero *after* rewriting files. Run it one way inside the executor's loop and
another way at the gate and the exit code stops describing the artifacts — one
place reporting clean while the other has changed the tree underneath it. It
also drains the gate: correcting inside the session means the model handles
what has no autocorrection while the file is still in front of it, and verify
finds nothing left to do. Measured cost of not doing this: two consecutive
stages, one cop each with no autocorrection, three or four attempts apiece and
one planner intervention, to communicate a one-line change.

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
`stage-costs.md` now carries dollars beside the context figure. Aider reports
cost only when litellm knows `input_cost_per_token`, so a zero means "not
priced" as often as it means "free"; and its cache accounting reads Anthropic's
and DeepSeek's fields but never OpenAI's `prompt_tokens_details.cached_tokens`,
so a silent zero there is the instrument, not the cache. Measured directly at
the API: an identical 16k prefix caches at 99.9% on chat/completions with
nothing configured — **and that figure does not cover the path the executor
now takes.** It routes through `openai/responses/<model>`, and the measurement
was made against chat/completions, which is the same substitution the rule two
paragraphs up was written about: a fact established on the layer beside the one
being called. Three artifacts were checked for a reading on the real path and
none carries one — `aider-chat.md` reports `38k sent` with no cache fields,
`aider-llm.txt` holds prompt and response text with no usage block at all, and
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
`mode: responses` triggered it perfectly — in litellm. Through Aider it did
nothing: `register_models` puts the entry in Aider's own `local_model_metadata`
and, in its own comment, *defers registering with litellm*, so the registry the
bridge consults never sees it. Every attempt died in 1.9s and the run burned
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

**An inner loop that skips the file under edit is worse than none.** Aider's
`--test-cmd` was built from `test_paths` alone, so a stage declaring none ran
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
they get an end-to-end one.

The docstrings carry the reasoning, usually including the incident that produced
it. They are worth reading before changing the behaviour they describe.
