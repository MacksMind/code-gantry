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
arrangement misses entirely there. Measure after changing any of this. Documented
provider behaviour has been wrong twice.

**There is no push, and stages land by squash merge.** The safety guarantee is
that no method exists which could push. Squashing is what makes "every commit on
the project branch is green" and "the executor commits before it tests" both
true.

## Rules that cost time when broken

**Project knowledge belongs in config, never in code.** This includes
model-facing strings. A tool description reading `e.g. app/controllers/order_controller.rb`
is a Rails hint shipped to every project's planner. Regexes that identify a
failing test, a seed, or a file are properties of a project, which is why several
have no default at all.

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

**Read artifacts; do not regex them.** Repeatedly, a pattern has produced a
confident wrong answer that a two-line read would have settled: `\s` is not valid
in POSIX ERE so `git grep -E` silently matches nothing; `File\.exists?` matches
the already-converted `File.exist?` because `?` quantifies the `s`; an indented
`order:` inside a constant hash is not an association option. When a count or an
absence is load-bearing, open the file.

## Where things live

`nodes.py` holds the loop's decisions — which failures route to the executor,
which to the planner, which to a human. `verify.py` is the layered gate, ordered
cheapest-first, short-circuiting. `config.py` is the safety story: the capability
partition and the command denylist. `prompts.py` is pure string building, kept
apart from the clients so it can be tested without a model. `state.py` documents
why `completed` and the two retry counters are shaped as they are.

The docstrings carry the reasoning, usually including the incident that produced
it. They are worth reading before changing the behaviour they describe.
