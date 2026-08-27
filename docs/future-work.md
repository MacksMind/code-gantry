# Future work

Open items, with the evidence for each. This is the counterpart to
[docs/archive/rewrite-plan.md](archive/rewrite-plan.md), which is a closed
record and holds nothing outstanding — everything here is outstanding.

An item earns a place here by being a decision someone has to make, not a
task someone has to do. Where the reasoning is already written down beside the
code, this points at it rather than restating it.

## A resume routes on where the run stopped, not on what went wrong

`resume_entry_point` reads `failure_layer` and sends a resumed run to `plan` or
`verify` from it. That field is written unconditionally — `_escalate`,
`_planner_failure` and `_retry_or_plan` all set it — so it holds the *last*
failure of a sequence. Next to it sits `opening_failure`, which `_opening` claims
write-once per stage or revision, under a docstring saying exactly why: "whichever
failure got here first is the diagnosis; everything after it is what that failure
caused, and overwriting is precisely the defect this exists to fix."

Two fields, one of them deliberately protected against being overwritten, and the
routing decision consults the other one.

**Measured, twice.** A run's full suite went red because the Chromium containers
had exhausted their sessions, which fails every example type through the global
reset hook. `failure_layer` became `full_suite`, a repository-state failure that
re-enters at `verify`. The planner then diagnosed it correctly and blocked,
because the remedy needs a shell it does not have — and blocking wrote `planner`
over the top, which is a *planning* failure and re-enters at `plan`. So after the
containers were restarted, the resume would have gone back to the planner to
re-derive against a world that was already fixed, rather than to the gates to
re-judge it. It took a surgical edit of the checkpoint to send it to the right
node, and the same shape cost time again later the same day.

**Why this is a decision and not a patch.** Routing on `opening_failure` is not
obviously right either. `failure_layer` answers "where did this run stop", which
is what the escalation text describes and therefore what a human reads; and after
a planner block the stage spec may genuinely be stale, so re-entering at `verify`
would judge an unrevised stage against a diff the planner has already rejected.
The honest description of the bug is narrower than "it reads the wrong field": it
is that **a repository-state failure escalated *through* the planner loses the
fact that the repository was the problem**, and that is the case a human is most
likely to fix by hand before resuming.

Candidate answers, in increasing order of how much they change:

- Route on `opening_failure` when it is a repository-state failure and the last
  layer is `planner`, on the grounds that the planner blocking on a repo problem
  does not make it a plan problem.
- Ask the escalation what the human is expected to fix, and record *that* rather
  than deriving it from either field.
- Leave the routing alone and make `code-gantry resume` take an explicit entry
  point, so the operator asserts it the way `--reset-progress-budget` is asserted.

The third is the cheapest and the most honest about who knows the answer; the
first is the one that would have saved both incidents unattended. What decides it
is whether an unattended run should ever re-enter at `verify` after the planner
has spoken, and that is a question about trust rather than about code.

## `discover` should be pluggable

`init` drafts a config by inspecting a repository. The knowledge of how each
ecosystem spells its test command, its linter and its runtime is spread across
four places in `discover.py` as inline `if` chains, and their coverage is
uneven in ways the draft never mentions:

| where | what it knows |
|---|---|
| `_discover_test` | `Gemfile` → `bundle exec rspec`; `pyproject.toml` → `pytest`; `package.json` → `npm test` |
| `_discover_lint` | `.rubocop.yml` → RuboCop; a ruff config → ruff. Nothing for Node, Go, or anything else |
| `_stack_notes` | version files for four runtimes, but manifest parsing only for `Gemfile`, and within it only `ruby` and `rails` lines |
| `_TEST_SCRIPT_CANDIDATES` | `bin/rspec` sitting beside the generic `bin/test` and `Makefile` |

A Node repository therefore drafts with a test command, **no linter, and no
stack notes** — and nothing distinguishes "this project has no linter" from
"nobody taught discovery to look". That is the failure this codebase meets in
every other form: an absence and a negative answer rendering identically, and
the empty one getting believed.

Pluggable means one entry per ecosystem — the manifest that identifies it, and
the test, lint and version facts that follow — so that adding Go is adding a
row, and a missing row is visible *as* a missing row rather than as a confident
partial answer. The check for whether it worked is not that a new ecosystem
drafts correctly, but that an unknown one says so.

**Why this is not urgent, which is worth recording so it is not mistaken for
neglect.** `init` drafts; the draft is headed "Read every line before approving"
and an operator edits it before it runs anything. A wrong guess costs a line of
typing once per project and cannot reach a run. The argument for doing it is
that the gaps are silent, not that they are expensive — and silence is the
thing that stops being true when someone else's repository is the one being
drafted against.

### The alternative: draft by prompt rather than by inspection

A registry is one answer to "how do we know about Go". The other is to stop
knowing: have `init` emit a **config template inside a prompt** and let a model
fill it in against the repository, which removes the ecosystem table rather
than growing it.

**The drafting model is not a role.** It is not the planner, it is not
configured anywhere, and it cannot be — the config that would name it is the
artifact being written. The operator opens whatever assistant they already use,
hands it the prompt or tells it to run `init` itself, and reads what comes
back. That is an ad hoc act by a person outside the system, not a fourth
participant in it, which is why the capability partition is untouched rather
than widened: the invariant governs what the planner may author mid-run and
unattended, and nothing here runs.

`docs/architecture.md` currently argues the other way on purpose — "Discovery
splits along the same line as the planner's write permissions", executable
fields "from deterministic repo inspection, never a model". Doing this means
replacing that reason, not quietly deleting it.

**The prompt's real payload is how to check the answer, not how to write it.**
Drafting a config is the easy half and any competent assistant will do it; what
makes the draft trustworthy is that `code-gantry validate` runs `setup_command`,
runs the suite, runs the `checks` and calls both model endpoints, against this
host, before anything is committed to. So the prompt has to say that, say how
to read the result, and expect the assistant to iterate — draft, validate, fix
what failed, validate again — rather than hand a plausible YAML file to a
person who then discovers on stage 3 that a command was wrong.

**Which means one earlier claim here was half wrong.** It said a model makes
the "I do not know" problem harder, because an `if` chain lacking a Go branch
returns nothing while a model returns a confident `go test ./...`. Under a
validate loop that is backwards for every field validate executes: the
confident wrong guess fails in seconds and the assistant sees exactly why,
which is a better outcome than a silent omission a human has to notice.

It stays true, and becomes the whole of the problem, for the fields validate
**cannot** reach on a healthy repository:

- `failed_file_pattern` and `seed_pattern` are regexes over a *failing* run's
  output. A green suite never matches them, so validate passing says nothing
  at all about them — and a pattern that matches nothing reads downstream as
  "this runner prints no locators", which is believed. This has already cost
  one outage.
- `test_file_patterns` answers a question no gate asks until a stage derives
  its test selection.
- `scope_exempt_globs`, `no_direct_edit` and `forbidden_patterns` only bite
  once a stage is editing.

So the template should mark those fields as unverified-by-construction and ask
for the evidence behind each — the line of real runner output a regex was
written against, not a regex that looks right. Everything else, validate
settles.

**It is also the cheap experiment**, which is the argument for trying it before
deciding. The block can ship beside the inspection rather than replacing it,
and the two drafts compared on ecosystems the chains already cover. If the
model wins there, that is evidence; the version where it obviously wins on Go
is a story, because nothing currently competes.

## Whether the executor should edit through `apply_patch` and V4A diffs

OpenAI ships a first-class editing tool. It is declared as
`{"type": "apply_patch"}` on Responses, Chat Completions and Assistants; the
model emits an `apply_patch_call` naming one of `create_file`, `update_file`
or `delete_file`, and for an update the payload is a **V4A diff** — a unified
diff with context lines and `@@` hunk headers. The host applies it and returns
an `apply_patch_call_output` carrying the `call_id` and a `completed` or
`failed` status. So this is not a hosted editor: OpenAI defines the schema and
the format, and `edittools.py` would still do the writing.

Our `edit` is exact-string replacement — the model quotes an `old_string` that
must appear once. **Measured over the two most recent runs: 412 edit calls, 17
refused, 4.1%.** Thirteen were "that text does not appear in the file" and four
were "that text appears N times, so it does not identify one place." The second
class is precisely what a `@@` header exists for, and there is no way to
express it in our schema — a model that has found the right line inside the
wrong-shaped file has nothing to say except quote more text and hope.

**The reason to distrust that 4.1% is the history behind it.** It used to be
worse and the cause was ours: `read_file` numbered with a two-space separator
that indentation could not be told apart from, and 74 of 117 refused
`old_string`s — 63% — matched the file exactly once two spaces were stripped
from every line. The rate is what it is now *because* an instrument bug was
fixed, which is exactly the state in which adopting somebody else's format
looks more attractive than it is. Before treating 4.1% as a defect, read the
seventeen: an executor that quotes badly is a prompt problem, and this
repository has already mistaken one for a format problem once.

**The real trade is which way the failures fail.** Exact matching refuses
loudly and cheaply — the model is told the text is absent or ambiguous, and the
next cycle costs one tool call. A context diff is applied by *matching*, and
its failure mode is a hunk that lands somewhere plausible and wrong. That is a
silent bad edit inside a stage that then commits, tests and possibly passes,
which is the class of failure this codebase spends most of its gates on. The
fallbacks we already carry are the same hazard in miniature and were built
deliberately narrow: `Nearest` tries an anchor derived from the model's own
first line and then a semantic window, and both hand back a *suggestion* the
model must re-quote rather than writing anything.

**It also welds the executor to a provider.** `edit` is our schema, and the
role's client could be pointed anywhere. `apply_patch` is an OpenAI tool type,
so adopting it makes the executor's editing capability a property of who is
serving it — and that is a live question this week rather than a hypothetical,
with Bedrock evaluated and rejected on structured outputs and OpenRouter still
open. The safety story survives either way, which is the first thing anyone
will ask: a host-applied patch still resolves through `_resolve_writable` and
still meets `no_direct_edit`, because those guard the path rather than the
payload.

One fact has to be established before any of this is a decision. The tool is
documented as supported on **GPT-5.1 through GPT-5.5**, and the executor runs
`gpt-5.6-luna`. That is either a stale docs page or a real gap, and it is
answerable with one call rather than by reading.

Candidate answers, in increasing order of how much they change:

- Do nothing. 4.1% is not a cost anyone has felt, and no stage has been traced
  to it.
- Give our own `edit` the thing V4A has and we lack: an optional enclosing
  context — a `within` argument naming a surrounding line or block — so an
  ambiguous quote can be scoped without adopting a diff format. This targets
  four of the seventeen and nothing else.
- Offer `apply_patch` alongside `edit` and let the model choose, then count
  which it reaches for and what each costs. Note that this cannot be read as a
  preference between formats until the descriptions are comparable, because a
  tool's description decides how often it is called and its results decide
  nothing.
- Adopt it as the executor's only editing tool.

What decides it is not the refusal rate. It is whether a wrong-place patch is
worse than a refused edit — and given that a refusal is visible in the tool log
while a misapplied hunk is visible only if a test happens to cover the line, the
answer is probably yes, which argues for the second option over the fourth.

## A response is born in three places, and each one reads it differently

Every role ends up asking the same question — *how did this turn end* — and no
two of them ask it the same way:

```
planner.py         self._client.messages.parse(...)      Anthropic SDK, Messages
reviewer.py        self._client.responses.parse(...)     OpenAI SDK, Responses
executorclient.py  wire.send(...) -> create()            either wire, via the dialect
```

The planner and the reviewer call `.parse()`, which populates `parsed_output`;
the executor calls `.create()` and reads content blocks. So one event — a model
ending a turn without producing what was asked for — presents as
`parsed_output is None` in one role and as an empty content list in another,
and there is nothing in the code that says those are the same thing.

The readings underneath are worse than merely duplicated. `_messages_stopped`
consults `stop_reason`. `_responses_stopped` consults *nothing*: it infers that
the model stopped from the absence of tool calls, so on that wire a turn that
ended abnormally is indistinguishable from one that finished, by construction —
`status` and `incomplete_details` are on the response and no code path reads
them. The planner compares `stop_reason` against two string literals and then
discards the value. The reviewer never looks at it.

**Measured.** A planner call returned no structured verdict after 27 reads over
426 seconds, and the run blocked on `the planner returned no parsable verdict`
— a message that covers at least three different bugs. The artifact could not
narrow it: `planner.json` carries a `rejected_answer` key whose only writer is
the *success* path, so it read `null` on the one branch its name describes.
Whether the model had returned prose instead of a verdict, returned an empty
turn, or stopped for a reason the code does not check was unrecoverable an hour
later.

The narrow half of that has been fixed — the dialect now reads its wire's own
answer, the classification is derived from the recorded facts rather than
replacing them, and all three roles record the same thing under the same key.
That leaves the call sites: three of them, one line each.

**The decision is whether to go further and put the three producers behind one
send.** The argument for is the one this codebase has already paid for
elsewhere: a record that has to be *remembered* at each call site is the shape
of thing that goes quietly missing between two correct changes, and the answer
that worked for the executor transcript was to make the recording a property of
the only operation that can produce the thing. Three producers means three
chances to forget, and a fourth role would inherit the omission rather than the
behaviour.

The argument against is that it is not a tidy-up. Unifying means reconciling
`.parse()` against `.create()` across two SDKs, and the structured-output path
is where two separate outages have come from — a keyword one endpoint does not
take, and a parameter a gateway accepts and ignores. Against that, the value on
offer is preventive: it stops a future omission rather than fixing a present
defect, and the present defect is already closed.

What would settle it is a count nobody has taken: how many *other* facts about a
response are read in one role and dropped in the others. If the answer is one,
this is not worth a refactor. If turn usage, refusals and stop reasons are all
in the same state, the seam is missing rather than the fields.
