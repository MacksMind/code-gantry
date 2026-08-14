# Future work

Open items, with the evidence for each. This is the counterpart to
[docs/archive/rewrite-plan.md](archive/rewrite-plan.md), which is a closed
record and holds nothing outstanding — everything here is outstanding.

An item earns a place here by being a decision someone has to make, not a
task someone has to do. Where the reasoning is already written down beside the
code, this points at it rather than restating it.

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
