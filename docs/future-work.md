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

## Whether an edit refusal should hand back a window more often

`nearest_text` folds a read into the refusal that made it necessary: on a
not-found `old_string` it returns the file's real bytes around where the model
seems to have meant, numbered as `read_file` numbers them, and the model
re-quotes from there. When it fires it works — five times in one measured
attempt, and each time the next quote was correct.

**It fired five times out of twenty-eight.** The other twenty-three refusals
carried no window at all.

The cause is in its anchor. It takes the first non-blank line of `old_string`,
strips it, and looks for a file line that is *equal* to it; failing that it
falls back to a `difflib` similarity scan and then to the semantic index. Its
docstring says it was measured on two real misses, and both were whole-line
quotes from a routes file — which the whole-line anchor is exactly right for.
The misses that arrive in bulk are not that shape. They are sub-line fragments:
`"raised\sat"`, `"StandardError"`, `").once\s\(raised"` — a model narrowing
its quote after an ambiguity refusal, which is the move the refusal text asks
for. No whole-line equality can match a fragment, and the similarity fallback
then fails its ratio.

Two things have to be established before this is a decision, and one of them is
an instrument problem.

**The route is unrecoverable from a killed attempt.** `ToolError.kind` carries
which fallback answered — `anchor`, `ambiguous`, `semantic`, `none` — and it
reaches `refusal_counts` in `executor-loop.json`, which is one write at the end.
So on the attempt that prompted this, the counts do not exist. `tools.log` is
live and appends, but it records the rendered *message*, and every route renders
the same sentence: a classifier over rendered text cannot separate classes the
text renders identically, which is the fault `kind` exists to avoid. **The
distinction is being made and then thrown away at the only place it could be
read.** Whatever else changes here, the live ledger should carry the kind.

**And the fragment case may not want a window at all.** A fragment that appears
nowhere is usually a fragment the model invented from memory, and the right
answer to it might be the ambiguity refusal's answer — quote more, not less —
rather than a window somewhere plausible. `NEAREST_MIN_RATIO` exists because a
confidently wrong location invites an edit the model never meant. Widening the
anchor to substrings makes that failure more likely, not less.

Candidate answers:

- Do nothing. `apply_patch` now takes the case that produced most of these:
  a model narrowing a quote to a fragment is working around a limit of
  `old_string`, and a hunk with a `@@` header expresses what it was reaching
  for. Re-measure the refusal mix after a run or two before changing anything
  here — the distribution that produced "23 of 28" was measured under a tool
  set that no longer exists.
- Record `refusal_kind` on `tools.log` so the question is answerable at all,
  and change nothing else yet. Cheap, and it is a precondition for every other
  option.
- Anchor on the longest line of `old_string` rather than the first, which is
  the one most likely to be a whole line even when the quote is a fragment.
- Let a fragment anchor on a *substring* match, and raise the ratio it must
  clear, so the window is only offered where the location is not a guess.

What decides it is not the miss rate. It is whether a window offered for a
fragment lands somewhere the model then edits — and that is measurable only
once the route reaches a ledger that survives the attempt.

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

## Whether `git grep` should come back as a fallback for `rg`

`search` shells to ripgrep, and `ripgrep is installed` is a *blocking* preflight
check — its own docstring says why: "the only external command the pipeline
itself requires that the operator did not name in config, so it is the only one
that has to be checked rather than simply run." A host without `rg` cannot start
a run at all, while `git` is a hard dependency already present everywhere the
tool can work. A fallback would remove the one install step that is ours rather
than the project's, which matters most on a machine nobody has set up yet.

**The reasons for the swap are all still true, and a fallback has to carry every
one of them.** A pathspec is not a glob: git lets `*` cross `/` and needs `**/`
to consume a directory component, so `app/**/*` never sees a file sitting
directly in `app`. Replayed over one run's tool log at the sha it was taken at —
337 searches, 76 empty, and **27 of the 76 had matches**. That is 8% of every
search and 36% of every empty answer, wrong. Beyond globs: `\s` is not valid in
POSIX ERE, so `git grep -E` silently matches nothing where ripgrep's
`--engine auto` retries under PCRE2; ripgrep skips dotfiles and git grep does
not, so the dialects disagree about whether a `.rubocop.yml` exists; and the
tracked-only boundary is ripgrep's ignore handling in one and git's index in the
other, which land in the same place for the case that matters but are not the
same rule.

**Why this is a decision rather than a task.** A fallback is by definition
invisible to its caller, and these two tools disagree in exactly the way that
this codebase has already paid for: *an empty answer gets believed*. A model
treats "no results" as a fact about the repository and reasons forward from it —
the executor that hit a run of false empties abandoned `search` and asked
`semantic_search` the same question ten times in 74 seconds. So a fallback that
quietly answers differently does not degrade gracefully; it reintroduces the
original defect on precisely the hosts nobody is watching.

Four shapes, and they are genuinely different products:

- **Translate.** Convert globset to pathspec and dialect-shift the pattern.
  Closest to a real fallback and the only one where the caller need not know —
  and the translation is not total, so the residue is silent.
- **Answer, and say which tool answered.** Degraded behaviour, declared in the
  tool result, on the standing rule that anything the machinery does is the
  tool's to state rather than the operator's to work around.
- **Refuse rather than differ.** `search` reports itself unavailable and the
  model falls back to `read_file`, `list_files` and semantic search. This is the
  only option that cannot produce a false empty, because it produces no answer
  at all.
- **Remove the dependency instead.** Ship or fetch a ripgrep binary, so the
  question does not arise. Trades a config-time install for a supply-chain
  decision.

What would settle it is a measurement nobody has taken: replay one run's
searches through a `git grep` translation and count the disagreements. If the
answer is that translated globs and dialect-shifted patterns agree on all but a
handful, the first option is real. If it is another 8%, only the third is honest.
