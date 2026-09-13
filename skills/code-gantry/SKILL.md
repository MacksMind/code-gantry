# Work the CodeGantry ledger from this session

<!-- Lives in code-gantry/skills/code-gantry; a target repository links it
     from .claude/skills/code-gantry so one copy serves every repository. -->

CodeGantry runs a fleet of bays against this repository's projects. Each
project is a directory with a `code_gantry.yaml` beside its plan, and its
record — the plan tree, what landed, what is claimed, and every finding —
is a *ledger*: one append-only sequence of events in a shared table, read
and written only through the `code-gantry` CLI. **This session never reads
the table itself and never edits plan documents to change the record.**
The plan the fleet reads is rendered from the ledger, so a change written
here reaches the next planner call without a restart.

What this session has that the fleet does not: a shell, the containers, the
dev database dumps, a browser — and the person. The things waiting on a
person are of two kinds, and the session's job differs for each: something
that needs to be *checked* with those tools, which the session does itself;
and something that needs to be *decided* — delete the columns or land them,
is this behaviour a defect or a rule, does this ship before the bump — which
nobody in the pipeline may decide and neither may this session. The second
kind is put to the person, one at a time, and their answer is what gets
written.

## The one command

Every verb below is spelled `cg …` for brevity and means, in full:

```sh
uv run --project ~/projects/code-gantry code-gantry … --config docs/rails_5_migration_project/code_gantry.yaml
```

Run it that way every time, from this repository's root: an alias or an
exported variable set in one shell does not survive to the next command a
tool runs. The config names the project; the other project here is
`docs/technical_debt/code_gantry.yaml`. `--json` on any verb gives the whole
record.

**The CLI is the only interface.** It loads the ledger's table name and the
credentials itself, from the file the config points at. If a command fails,
show its error verbatim and stop. Do not look for credentials, do not read or
configure AWS, do not import `code_gantry` from Python, do not open the
table: none of that is this session's, and a failure of the command is
something to report, not to route around.

## Read what is waiting

```sh
cg ledger waiting            # findings that need a human, and open human-owned items
cg ledger waiting --json     # every field: text, keys, the card, the thread
cg ledger show --open        # every open item, whoever owns it
cg ledger findings           # open findings, the pipeline's included
```

Each thing waiting is a *finding* (`f-…`, something a planner or reviewer
observed) or an *item* (a key like `r5.034`, an entry in a plan document a
person owns). It may carry a *card* — the recommendation an investigation
attached: what it says, what it anchors to, what was checked, what to do —
and a *thread* of cards and questions.

## Act on one thing

Investigate first, with the shell. Verify any claim that would park work —
"cannot be done", "needs a human", "nobody owns this" — and any claim that
creates work. Then write one of these, and nothing else:

```sh
# a finding
cg ledger answer <id> discard --text "why"
cg ledger answer <id> amend --text "the sentence the item should carry" --target <key>
cg ledger answer <id> debt --text "the entry" --target <section key>
cg ledger answer <id> raise --text "what would settle it, and who decides"

# an item
cg ledger land <key> <sha>           # already done in the tree: the commit that did it
cg ledger strike <key> "why"         # nothing to do: zero population, wrong premise
cg plan edit <key> --owner pipeline  # the fleet can have it after all

# either
cg ledger move <id> --to docs/technical_debt/code_gantry.yaml [--under <section key there>]
                                     # belongs to another project; general debt is a project
cg ledger accept <id>                # apply whatever the card recommends, as the events above
```

## When it needs a decision

Do not decide it, and do not park it as "needs a human" — that is how it got
here. Put it to the person, in the session, one at a time: what the thing
says, what you checked, the options, and what each option would write. Name
who decides what; if you cannot name both, it is work rather than a decision
and goes back to the fleet. Then write their answer as the event it is:

```sh
cg ledger unblock <key> "the decision, as a fact the planner can act on"
                                     # a key held on a question (`cg ledger block <key> "…"` is how one is held)
cg ledger answer <id> amend --text "the constraint the decision sets" --target <key>
                                     # the decision changes what an item says
cg plan edit <key> --owner pipeline  # and now the fleet can do it
cg ledger strike <key> "decided: not doing it"
cg plan add --under <section key> --title "…" --body-file notes.md
                                     # the decision is new work
```

Write facts, never instructions: "the columns are deleted, not landed" is a
fact the planner draws a stage from; "delete the columns" is an instruction
the plan should not carry. A decision the person defers is `raise` on a
finding, with what would settle it, and it stays on the dashboard.

Or leave it decided-later: `cg ledger recommend <id> --card '<json>'` attaches
a card (the shape is in `cg ledger recommend --help`), and
`cg ledger ask <id> --text "…"` puts a question on the thread for the next
investigation. Both show on the dashboard.

What each disposition means: **discard** — the ledger keeps it, the plan
never sees it; the right answer for most observations. **amend** — change
something already in the plan: a count, a scope, a constraint it got wrong;
written on the item at the next fold. **debt** — a defect this project's own
work created, filed as an item under a section of this project. **move** —
belongs to another project. **raise** — you cannot settle it; say what would.
An absence ("nothing covers this") is not a finding: discard unless something
concrete waits on it. Raise anything security- or data-integrity-shaped.

## Add work the ledger does not know about

New work — from a Jira ticket, a conversation, something found while
checking something else — becomes an item in a plan document's section, and
the fleet draws against it at its next derivation:

```sh
cg ledger show                        # the tree: documents, sections, items and their keys
cg plan show <section key>            # one node: where it sits, what hangs on it
cg plan add --under <section key> --title "One line naming the outcome" --body-file notes.md
cg plan add --under <section key> --title "…" --body-file notes.md --owner human
cg plan add --under <document key> --kind section --title "A new section"   # when none fits
```

The title is the outcome; the body is what the planner needs to draw a stage
and nothing it does not: the defect or the change, where it is (paths, the
ticket), what done looks like, and any constraint the work must respect. Facts,
never instructions to the planner. `--owner human` keeps it off the fleet's
list and on the dashboard's, for work that needs a decision or a person's
hands first; `plan edit <key> --owner pipeline` hands it over later. The
project is the branch the fix should land on, and the section is where in
that project's documents it belongs. In this repository that is the Rails 5
project for everything now: its `technical_debt.md` has a section for defects
this project's work created and one for inherited defects the branch has to
carry (`r5.021`); the plan document is for the work the project is for. The
`technical_debt` project is closed to new work; nothing goes there.

## When you are done

```sh
cg ledger fold                                          # once; a cache matter, see below
~/projects/code-gantry/bin/daemon wake rails_5_migration_project   # if anything is now drawable
```

**Everything written here reaches the planner at its next derivation, with no
fold.** An item added, amended, unblocked or handed over is drawable the
moment it is written; the planner is sent the plan text and everything that
changed since together, every call. The fold only decides which of the two
the planner is sent it in: the plan text is the block the model caches for an
hour, so the fold rewrites it, and doing it once at the end of a session
rather than after every answer is what keeps that cache warm. `wake` is the
one thing that is not automatic: it clears the project's "nothing to draw"
mark on every host and starts the idle bays, and without it a project the
fleet found complete stays idle, because the daemon cannot see the ledger
change.

## Rules

- The record is the ledger. Do not edit `PLAN.md`, `technical_debt.md`,
  `human_in_the_loop.md` or `progress_log.md` to change what is done or open;
  they are rendered from the ledger for the fleet and imported from it only once.
- Never write to a bay's checkout (`../acme_app-bay*`); a run may be live in it.
- One thing at a time, and the recommendation is a recommendation: a person
  decides, and a discard is not re-raised.
- The dashboard at `http://localhost:4040/` (on a host running the daemon)
  shows the same queue and takes the same actions.
