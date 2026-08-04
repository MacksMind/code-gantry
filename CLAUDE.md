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
