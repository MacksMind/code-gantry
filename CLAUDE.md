# Working on this codebase

[README.md](README.md) is how to use the orchestrator. [PLAN.md](PLAN.md) is the
design authority on why it works this way. Neither is repeated here.

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

## Rules that cost time when broken

**Project knowledge belongs in config, never in code.** This includes
model-facing strings. A tool description reading `e.g. app/controllers/order_controller.rb`
is a Rails hint shipped to every project's planner. Regexes that identify a
failing test, a seed, or a file are properties of a project, which is why several
have no default at all.

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
