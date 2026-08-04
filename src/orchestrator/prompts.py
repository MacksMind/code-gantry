"""Prompt construction for the executor, the reviewer, and the planner.

Pure string building, kept apart from the clients that send it so it can be
tested without a model.

**Ordering is the caching strategy, not presentation.** Both paid models get the
plan snapshot and the completed-stage history first — large, append-only, and
byte-identical across the stages of a run — and the per-stage material last.
Providers cache on matching prompt prefixes, so reordering these for
readability would silently multiply the cost of every call.

Note what is *not* in the prefix: projected future stages. They do not exist
yet, and including them would break the prefix every time the planner derived
one.
"""

from __future__ import annotations

from orchestrator.config import ProjectConfig, Stage
from orchestrator.plandoc import PlanTree
from orchestrator.planner import cache_control
from orchestrator.state import FailureDetail, StageResult

REVIEW_SYSTEM_PROMPT = """\
You are the reviewer in an unattended refactoring loop. A local model makes the
edits; a planner decides what each stage should be; you decide whether a
finished stage may land on the project branch.

The stage's tests already pass — that is a precondition of you being called,
not something to confirm. Your job is what a test suite cannot check: whether
this diff does what the stage asked, stays inside the stage's constraints, and
remains consistent with the stages that came before it. The executor sees one
stage at a time and cannot see the plan, so cross-stage drift is yours to catch
and nobody else's.

Return one of three verdicts:

- "approved" — the diff does what the stage asked and honours its constraints.
  Minor stylistic preferences are not grounds for rework. Approval means it is
  squash-merged to the project branch, so hold it to the standard of a commit
  you would be content to find in the history later.
- "rework" — a specific, fixable defect in this diff. Say precisely what is
  wrong and why it matters, so the next attempt can act on it.
- "blocked" — the stage instruction itself is wrong, or the plan has a flaw
  that reworking this diff will not fix. This does not stop the run: it routes
  to the planner, which can revise the stage or insert a predecessor. Use it
  freely when the problem is upstream of the executor rather than grinding
  through rework attempts on an instruction that cannot be satisfied.

One change is never a scope violation: a file gaining a missing final newline.
The executor's editor normalises every file it writes, so this appears on any
file that was committed without one, no model chose it, and no instruction can
prevent it. Rejecting it does not stop it happening — it only sends correct
work back to an executor that will produce the same diff again. Ignore the
hunk and judge the rest. This covers exactly a `\\ No newline at end of file`
marker disappearing, in a file the stage was already permitted to edit.

Trailing whitespace at the end of an added line is likewise not yours. It is
removed from every added line before the stage is committed, so the diff you
are reading can show it and the landed commit will not. Judging it would reject
work over a character that is already gone.

Anything else about whitespace is yours to judge as usual.

Judge only the diff you are shown, against the stage you are given.\
"""

REVIEW_TOOLS_PROMPT = """\

## Looking at the repository

You can read the repository. Use it when the diff's safety depends on code the
diff does not contain — which is common, and is the case a diff alone cannot
settle.

The clearest example: a stage that deletes a declaration is safe exactly when
something elsewhere still covers what the declaration used to. That elsewhere
is not in the diff. Without reading it you are not judging the change, you are
restating the stage instruction in your own voice, and an approval that could
never have been a rejection is not a review.

So: before approving a diff whose correctness rests on a file you have not
seen, read the file. Before accepting a claim in the stage instruction about
what the rest of the codebase contains, check it. A count, a "nothing else
references this", a "the permit list already covers this" — those are claims,
and the code is the fact.

**What you find outside the diff is context, not a defect.** This is a legacy
codebase mid-migration and it has pre-existing problems that have nothing to do
with the stage in front of you. Finding one is not grounds for rework: the
executor cannot fix what the stage did not ask it to touch, and rejecting for
it burns attempts on work that will never be in scope. Judge whether *this
diff* is correct and complete for *this stage* — and unless the diff makes the
problem worse, approve.

Reading costs time on every stage, so read what you need and stop. If the diff
is self-evidently correct, return the verdict without looking at anything.

## Recording what the change was

`record` is the entry this stage leaves in the progress log — the project's
account of what has been done, which every later planning pass reads back as
history. Nothing else records it. The stage instruction says what was *asked
for*, and you are the only reader of what was actually written.

Write it for someone picking the work up in a year with no memory of this
stage: what the change does, and what a reader needs to know that the diff
alone would not tell them — a decision taken between two defensible options, a
constraint that forced the shape, something the change makes possible or rules
out next.

Not a verdict. `summary` already justifies the routing decision, so `record`
should not restate that the diff matched the stage, and should not list what
was avoided — no reader a year from now needs to know which constructs were not
introduced. Two or three sentences of substance beat a paragraph of compliance.

## Reporting what you found

`observations` is where a real problem outside this stage goes. It does not
affect the verdict and does not route anywhere — it is appended to the progress
log when the stage lands, which is what the next planning pass reads. That is
the only way something you notice survives; a finding left in your summary is
read once and lost.

Use it for something a maintainer would act on and that this stage did not
cause. `file` names where it lives, `finding` is the one-line claim, `detail`
is what you checked and why it matters.

**Use it, in particular, for a difference you cannot trace to a consequence.**
This is the common case and the easy one to get wrong. A diff can change how a
result is reached without changing the result, and the change then reads as not
strictly behaviour-preserving while nothing observable moves. That is worth
recording and it is not worth rejecting. If you are about to withhold approval
over a difference and cannot say what would actually differ for a caller,
approve it and write an observation instead. Rejecting costs a rework cycle and
returns the same diff; the observation reaches a human who can decide.

Withhold approval when there is a consequence you can name, or when the stage
cannot be done as written. Those are `rework` and `blocked` respectively.

Two things it is not for. Not for defects in this diff — those are `issues`,
and they route back to the executor. And not for anything you did not verify by
reading, or that the progress log already records; you are shown that log, and
re-reporting a known finding makes a reader unable to tell a duplicate from
independent confirmation.\
"""


_RETRY_OPENING_REVIEW = (
    "A previous attempt at this task was rejected, and its work is on the "
    "branch — you will find it below, under what this stage has changed so "
    "far. Unlike a check that failed, this does not mean the work is "
    "unfinished: something in it is wrong and has to change.\n\n"
    "So read the feedback as naming something to *replace*, not something to "
    "add to. If it says an assertion or a block should be different, change "
    "the one that is there — leaving the original in place and putting the "
    "new form beside it satisfies nothing and is the common way this goes "
    "wrong. Everything the feedback does not name should come out unchanged."
)

_RETRY_OPENING_GATE = (
    "Your previous attempt is committed on this branch and did not pass a "
    "check. It is not being discarded, and it is not assumed to be wrong — the "
    "feedback below says what is still missing or failing. Read the files as "
    "they stand now rather than as the task describes them, fix what the "
    "feedback names, and change nothing else. If part of the task turns out to "
    "be done already, leave it exactly as it is."
)


def build_executor_prompt(
    stage: Stage,
    cfg: ProjectConfig,
    context: list[tuple[str, str]] | None = None,
    feedback: list[str] | None = None,
    failure_layer: str | None = None,
    cumulative_diff: str | None = None,
    excerpts: list[tuple[str, str]] | None = None,
    agent_context: str | None = None,
) -> str:
    """The message handed to the executor.

    A rework is a *fresh* invocation with no conversation history, so everything
    it needs is restated. It cannot see the plan document, the other stages, or
    the reviewer — only this.

    The opening depends on which gate sent it back, because the two cases want
    opposite things. A review rejection arrives with the branch already reset to
    the stage baseline, so nothing of the previous attempt survives and starting
    over is the point. A verify failure leaves the work committed on the branch,
    and `residue` in particular means the sweep was *incomplete* — repeating the
    approach on the sites that were missed is the fix.

    This said "rejected" on both for the whole of one 35-stage run in which the
    reviewer rejected nothing at all: the opening fired about a dozen times and
    was wrong every time, and on the seven `residue` failures it instructed the
    executor to do the opposite of what the feedback fifty lines below asked
    for.
    """
    parts: list[str] = []

    if feedback:
        parts.append(
            _RETRY_OPENING_REVIEW
            if failure_layer == "review"
            else _RETRY_OPENING_GATE
        )

    parts.append(f"## Task\n\n{stage.instruction}")

    if stage.constraints:
        parts.append(
            "## Hard constraints\n\n"
            f"{stage.constraints}\n"
            "A change that violates these will be rejected even if it is "
            "otherwise correct."
        )

    # After the stage's own constraints, because those are specific to this
    # work and these are standing. Obligations cluster rather than being split
    # by the reference material further down.
    #
    # The frame matters as much as the text. This document is written for
    # whoever works in the repository, human or otherwise, so it contains setup
    # steps, test commands and deploy procedure alongside the rules about how
    # code should look. The executor runs nothing, and an unframed list of
    # commands is how a model ends up narrating a command it never ran and
    # reasoning from the output it imagined.
    if agent_context and agent_context.strip():
        parts.append(
            "## How this repository is worked in\n\n"
            "Conventions its maintainers keep, read once at the commit this "
            "run started from. **These are facts about the repository, not "
            "work to do** — nothing here is part of your task, and where a "
            "passage describes running something, note that you cannot run "
            "commands and must not act as though you had. Follow the rules "
            "about how code in this repository is written; they apply on top "
            "of the stage's own constraints, and a change that breaks one will "
            "be rejected.\n\n"
            + agent_context.strip()
        )

    if stage.acceptance:
        parts.append(f"## Acceptance criteria\n\n{stage.acceptance}")

    if stage.require_new_tests:
        parts.append(
            "## Tests are required\n\n"
            "Write the tests for this behaviour first, then the implementation "
            "that satisfies them. A change with no tests will be rejected."
        )

    if stage.edit_files:
        listed = "\n".join(f"- {glob}" for glob in stage.edit_files)
        parts.append(
            "## Files you may change\n\n"
            f"{listed}\n\n"
            "Editing anything outside this list fails the stage. If the task "
            "appears to require a file that is not listed, stop and say so "
            "rather than editing it.\n\n"
            "**A file listed here that does not exist yet has already been "
            "created for you, empty.** So writing its contents as a quoted "
            "block rather than as an edit leaves that empty file behind, and "
            "the empty file is what gets committed. Create its contents the "
            "way this editor creates a file, and before you finish, confirm "
            "the file is not empty."
        )

    if stage.read_files:
        listed = "\n".join(f"- {glob}" for glob in stage.read_files)
        parts.append(f"## Context you may read but not change\n\n{listed}")

    if excerpts:
        blocks = [
            f"### `{label}`\n\n```\n{text}\n```" for label, text in excerpts
        ]
        parts.append(
            "## Lines from files you may read but not change\n\n"
            "Quoted from the repository as it stands, with line numbers, "
            "because whoever drew this stage had already read them. Treat them "
            "as current — you do not need to look them up again.\n\n"
            + "\n\n".join(blocks)
        )

    if stage.forbidden_patterns:
        listed = "\n".join(f"- /{p}/" for p in stage.forbidden_patterns)
        parts.append(
            "## Patterns you must not introduce\n\n"
            f"{listed}\n\n"
            "These are checked mechanically against the lines you add. They may "
            "be correct elsewhere in the project but are out of bounds here.\n\n"
            "Test files are exempt. A test asserting one of these is gone has "
            "to quote it, so write that assertion normally — the check skips "
            "test files and will not reject it."
        )

    if context:
        blocks = [
            f"### `{command}`\n\n```\n{output.strip()}\n```"
            for command, output in context
        ]
        parts.append(
            "## Context gathered from the repository\n\n" + "\n\n".join(blocks)
        )

    if cumulative_diff:
        parts.append(
            "## What this stage has changed so far\n\n"
            "Everything below is already committed on this branch. It is the "
            "work you are amending, not a description of what to do — read it "
            "before you edit, and leave the parts the feedback does not name "
            "alone.\n\n"
            f"```diff\n{cumulative_diff.strip()}\n```"
        )

    if feedback:
        listed = "\n\n".join(
            f"{i}. {item}" for i, item in enumerate(feedback, start=1)
        )
        parts.append(f"## Feedback on previous attempts\n\n{listed}")

    return "\n\n".join(parts)


def _addendum(cfg: ProjectConfig | None) -> str | None:
    """The configured progress log, if the project keeps one.

    `cfg` is optional on these builders and several tests pass None, so this
    must not be the thing that raises on a path every planner call takes.
    """
    return getattr(cfg, "plan_addendum_path", None) if cfg else None


def _history_limit(cfg: ProjectConfig | None) -> int | None:
    """How many landed stages the reviewer is shown, if it is bounded.

    Defensive for the same reason as `_addendum`: `cfg` is optional on these
    builders and several tests pass None, so this must not be the thing that
    raises on a path every review takes.
    """
    reviewer = getattr(cfg, "reviewer", None) if cfg else None
    return getattr(reviewer, "history_stages", None) if reviewer else None


def _without_addendum(plan: PlanTree, addendum_path: str | None) -> PlanTree:
    """The plan tree with the progress log taken out.

    The log is reachable by a markdown link from the plan root, so it arrives
    as one more child of the frozen snapshot — frozen at run start, which for
    the one document whose whole job is to be current means wrong. Readers that
    want it get the live copy handed to them separately.
    """
    if not addendum_path:
        return plan
    return PlanTree(
        root=plan.root,
        children=[d for d in plan.children if d.path != addendum_path],
        problems=list(plan.problems),
        skipped=list(plan.skipped),
    )


def _plan_block(plan: PlanTree, addendum_path: str | None = None) -> str:
    """The plan documents, with the progress log identified among them.

    Once the plan links its log, the log arrives as one more child among
    several and nothing in the content marks it out. But its role is different
    in kind: every other document says what the work *is*, and it alone says
    what the work has *become*. Naming it is driven by `plan_addendum_path`, so
    it stays a property of the project's configuration rather than prose an
    operator has to remember to keep writing.
    """
    intro = (
        "## The plan\n\n"
        "This is the authority for the project. A stage instruction is a "
        "pointer into it, not a substitute for it."
    )
    if addendum_path:
        intro += (
            "\n\nThese documents say what the work **is**. They do not say what "
            "has been done — they were written before it, and nothing edits "
            f"them as it happens. `{addendum_path}` is where that is recorded, "
            "appended as each stage lands. When the two disagree about whether "
            "something is outstanding, the log is later.\n\n"
            "The other documents here were read once, when this run started, "
            "and are the plan as it stood then. The log is not: it is included "
            "as it stands now, with every entry written up to this call. There "
            "is no later version to go and fetch.\n\n"
            "It is still not a substitute for looking at the code. A count in "
            "a document is a claim about when someone wrote it down."
        )
    # The log goes last among the documents. It is the only one that grows, and
    # in a concatenated cache prefix a document that grows re-bills everything
    # after it — here, seven static runbooks that happened to be linked below
    # it in the plan's opening paragraph.
    return intro + "\n\n" + plan.as_prompt_payload(last=addendum_path)


def _deferred_block(deferred: list[dict] | None) -> str:
    """Plan steps taken out of order, carried for the planner.

    In the cached prefix with the history, and for the same reason: it changes
    only when a deferral is added or resolved, not on every call. Rendering it
    at all is the point — the planner does not have to remember, and cannot
    quietly stop mentioning one.
    """
    outstanding = [d for d in (deferred or []) if not d.get("resolved")]
    resolved = [d for d in (deferred or []) if d.get("resolved")]

    if not outstanding and not resolved:
        return (
            "## Deferred plan steps\n\nNone. You have taken the plan in order "
            "so far."
        )

    lines = ["## Deferred plan steps", ""]
    if outstanding:
        lines.append(
            "Still outstanding. You must not return `project_complete` without "
            "listing these in `reasoning`; take one on as a stage whenever it "
            "becomes possible, and mark it resolved when it lands."
        )
        lines.append("")
        for entry in outstanding:
            lines.append(f"- **{entry.get('plan_step')}**")
            if entry.get("reason"):
                lines.append(f"  - deferred because: {entry['reason']}")
            if entry.get("blocked_on"):
                lines.append(f"  - blocked on: {entry['blocked_on']}")
            if entry.get("safe_because"):
                lines.append(f"  - judged safe because: {entry['safe_because']}")
    if resolved:
        lines.append("")
        lines.append("Already resolved: " + ", ".join(
            str(e.get("plan_step")) for e in resolved
        ))
    return "\n".join(lines)


def _costs_block(costs: list[dict] | None) -> str:
    """What stages have cost the executor, across every run of this project.

    The per-stage figures in the history above cover this run only, and a run
    begins with none — so the very first derivation, which is where batch size
    gets decided, would have nothing to calibrate against. These persist.

    Keyed by merge sha because that is what survives the squash: the stage
    branch is deleted and the executor's commits are folded away, so `git show`
    on this sha is the only way back to what those files actually were.
    """
    if not costs:
        return ""
    lines = "\n".join(
        f"- `{c['merge_sha'][:12]}` {c['stage_id']} — {c['files']} file(s), "
        f"{c['context_tokens']:,} tokens"
        for c in costs
    )
    return (
        "\n\n## What stages have cost the executor\n\n"
        "Measured, across every run of this project. Size a batch against "
        "these rather than against a file count — the figure is dominated by "
        "fixed overhead, so a stage's cost tracks the size of the files far "
        "more than their number.\n\n" + lines
    )


def _history_block(
    completed: list[StageResult],
    addendum_path: str | None = None,
    limit: int | None = None,
) -> str:
    """What *this run* has landed — which is not what the project has landed.

    The empty case used to read "this is the first stage of the project". True
    once, and false on every restart after: a run begins with an empty list
    however much work the branch already carries, so a month-old project was
    told it was starting from nothing. That is the one thing the planner most
    needs to be right about, stated as a fact and wrong by default.

    It cannot be fixed by guessing in the other direction either. This run
    genuinely does not know the project's history; it knows where the history
    is written. So it says so, and names the file.
    """
    if not completed:
        record = (
            f"`{addendum_path}` records what earlier runs landed, and is one of "
            "the plan documents above."
            if addendum_path
            else "The plan documents above are the only record."
        )
        return (
            "## Completed stages\n\n"
            "None **in this run**. That is a fact about this run, not about the "
            "project: a run starts with an empty history however much work the "
            "branch already carries, so this is equally what a restart half way "
            "through a long project looks like.\n\n"
            f"{record} Read it, and check the repository, before concluding that "
            "anything in the plan is still outstanding."
        )

    shown = completed
    dropped = 0
    if limit is not None and len(completed) > limit:
        shown = completed[-limit:]
        dropped = len(completed) - limit

    entries = []
    for entry in shown:
        line = f"### Stage {entry.get('index')}: {entry.get('id')}"
        if entry.get("revisions"):
            line += f" (took {entry['revisions'] + 1} revisions)"
        if entry.get("merge_sha"):
            line += f"\n\nLanded as `{entry['merge_sha'][:12]}`."
        # No instruction, no reviewer summary, no context cost — each of those
        # is carried better somewhere else, and this block was reproducing all
        # three. Measured at 45 stages: 74,000 tokens, ~1,650 an entry, resent
        # on each of ~15 tool iterations per call, and growing by ~6,600
        # characters per landing.
        #
        # The instruction is the planner's own prior output, echoed back — and
        # git already has it, as the squash commit's subject and the stage
        # artifact. What the stage *did* now goes to the progress log, written
        # by the reviewer after reading the diff, and the log is fed live on
        # every call. The context cost goes to `stage-costs.md`, which
        # `_costs_block` renders and which spans every run rather than only
        # this one.
        #
        # What is left is what has no other home: which stages this run landed,
        # what they cost in revisions, and the ids that let the planner tie the
        # other three channels together.
        #
        # The stage asked for these and the executor never saw them: they did
        # not fit the read budget, so the tail of the list was cut. Said here
        # because the choice of what to drop is the planner's to make — it
        # knows which reference it can do without, and truncation does not.
        if entry.get("withheld_reads"):
            line += (
                "\nRead budget: the executor was not given "
                f"{', '.join(entry['withheld_reads'])} — over `max_read_lines`. "
                "Declare fewer or smaller `read_files` and the rest arrive."
            )
        entries.append(line)

    # A truncated list that does not say so reads as the whole record, and
    # anything judging completeness against it would judge against a fifth of
    # one. Said in the heading rather than a footnote, because the heading is
    # what orients a reader who skims.
    if dropped:
        head = (
            f"## Completed stages: the last {len(shown)} of {len(shown) + dropped}\n\n"
            f"The {dropped} earlier ones are not shown. What the project has "
            "done is recorded above; these are here for the shape of recent "
            "work, not as the record of it.\n\n"
        )
    else:
        head = (
            "## Completed stages, in order\n\n"
            "Each landed as one commit on the project branch after passing "
            "review and the full suite.\n\n"
        )
    return head + "\n\n".join(entries)


def _review_system_prompt(cfg: ProjectConfig | None) -> str:
    """The contract, plus the tool section when there are tools.

    Told it can read when it cannot, the reviewer either hallucinates a lookup
    or hedges a verdict it should have given outright — so the section is
    conditional rather than always present. It rides in the cached prefix
    either way: `repo_access` is fixed for a run.
    """
    reviewer = getattr(cfg, "reviewer", None)
    if reviewer is not None and getattr(reviewer, "repo_access", False):
        return REVIEW_SYSTEM_PROMPT + "\n" + REVIEW_TOOLS_PROMPT
    return REVIEW_SYSTEM_PROMPT


def _conventions_block(agent_context: str | None) -> str:
    """The repository's own agent-facing documents, for the reviewer.

    Framed as what the repository requires rather than as background, because
    this is a gate: a convention it is shown but not told to enforce buys
    nothing. Empty string when there is none, so a project without one gets no
    heading rather than an empty promise.
    """
    if not agent_context or not agent_context.strip():
        return ""
    return (
        "## How this repository is worked in\n\n"
        "Conventions its maintainers keep, read at the commit this run started "
        "from. They bind the diff you are judging as firmly as the stage's own "
        "constraints do: a change that breaks one is a defect even where the "
        "stage said nothing about it. Where a passage describes procedure "
        "rather than how code should be written, it is context and not a "
        "criterion.\n\n" + agent_context.strip() + "\n\n"
    )


def build_review_messages(
    stage: Stage,
    cfg: ProjectConfig,
    diff: str,
    plan: PlanTree,
    completed: list[StageResult],
    progress_log: str | None = None,
    agent_context: str | None = None,
) -> list[dict[str, str]]:
    """Chat messages for the reviewer, stable payload first.

    Everything before the breakpoint is byte-identical across the stages of a
    run — that is what makes prefix caching hit, and why the diff is last.

    `progress_log` is the addendum as it stands now, and it goes *after* the
    breakpoint. The snapshot's copy is whatever existed at run start — 6,680
    bytes against 480,867 on the branch, measured on one long run — so the
    reviewer's only account of what had been done was the completed-stage
    history. Passing the live one fixes that; putting it in the cached prefix
    would break the one thing that caches, because this model does not fall
    back to the longest matching prefix and every landing would miss.
    """
    # The Responses API shape: message items whose content is a list of
    # `input_text` parts. The reviewer moved off chat/completions because
    # gpt-5.6-sol refuses function tools together with reasoning there, and a
    # reviewer that can look but cannot think is the wrong trade for a gate.
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "input_text", "text": _review_system_prompt(cfg)}
            ],
        }
    ]
    # A content-block list rather than a string, so it can carry the cache
    # breakpoint. GPT-5.6 caches at an explicit breakpoint and does not fall
    # back to the longest matching prefix; its default `implicit` mode puts one
    # on the *latest* message, which here is the diff. Marking the end of the
    # stable payload is what lets the diff vary without invalidating everything
    # before it — measured as read=0/write=55,498 before, read=55,489/write=0
    # after. Ordering the stable payload first was necessary and, on this model
    # family, not sufficient.
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    # The plan alone. The completed-stage history used to live
                    # here too, and every landing changed it — taking ~50,000
                    # tokens of unchanged plan documents out of cache with a few
                    # hundred tokens of history. Measured on one run: three
                    # reviewer calls, three full-price writes, one of them only
                    # 13 minutes after its predecessor and well inside the
                    # retention window. History now follows the breakpoint.
                    # The addendum is dropped from this tree rather than
                    # rendered here: the snapshot's copy is stale, and two
                    # copies of one document with one of them wrong is worse
                    # than either alone. The live one follows the breakpoint.
                    # Conventions lead the plan, and sit inside the breakpoint
                    # with it. Both are fixed for the run, so this is the one
                    # placement that is paid for once rather than per stage —
                    # and this model does not fall back to the longest matching
                    # prefix, so static content after the mark misses every
                    # time. Before the plan because it describes the repository
                    # the plan is about, which is the order the planner reads
                    # them in too.
                    "text": _conventions_block(agent_context)
                    + _plan_block(_without_addendum(plan, _addendum(cfg)), None),
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    )

    current: list[str] = []
    addendum = _addendum(cfg)
    if progress_log and progress_log.strip():
        current.append(
            "## What has been done\n\n"
            f"`{addendum}`, as it stands now — an entry appended as each stage "
            "lands. The plan above says what the work **is**; this says what it "
            "has **become**. Where the two disagree about whether something is "
            "outstanding, this is later.\n\n" + progress_log.strip()
        )

    current += [
        _history_block(completed, addendum, limit=_history_limit(cfg)),
        f"## The stage under review: {stage.id}\n\n{stage.instruction or ''}",
    ]

    if stage.constraints:
        current.append(
            "## Reject criteria for this stage\n\n"
            f"{stage.constraints}\n\n"
            "Treat these as grounds for rejection, not as background."
        )

    if stage.acceptance:
        current.append(
            "## Acceptance criteria\n\n"
            f"{stage.acceptance}\n\n"
            "This stage creates new behaviour, so there is no prior behaviour "
            "to compare against. Judge it against these criteria."
        )

    current.append(
        "## The cumulative stage diff\n\n"
        "This is the whole stage, not the delta since any earlier rejection — "
        "the same way a pull-request re-review shows the whole diff.\n\n"
        f"```diff\n{diff.strip()}\n```"
    )
    current.append(
        "Return your verdict now. If the stage instruction itself cannot be "
        'satisfied as written, return "blocked" rather than "rework" — that '
        "routes to the planner, not to a human."
    )

    # The second breakpoint, and the last thing that does not move during a
    # review. With tools the reviewer takes several turns, and without a mark
    # here every one of them re-sends this whole block — the live progress log,
    # the history, the stage, the diff — at full price. The plan block above is
    # already cached and stays cached; this marks the end of the per-stage
    # payload so that from the second turn only the accumulating tool results
    # are fresh.
    #
    # Deliberately not a marker that moves onto the newest message. That would
    # mean attaching a breakpoint to a `tool` message, and the only shape
    # measured working on this provider is a text block inside a user message.
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "\n\n".join(current),
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    )
    return messages


def build_planner_messages(
    cfg: ProjectConfig,
    plan: PlanTree,
    completed: list[StageResult],
    current_stage: Stage | None = None,
    failure: FailureDetail | None = None,
    opening_failure: FailureDetail | None = None,
    revision: int = 0,
    interventions_used: int = 0,
    interventions_max: int = 0,
    status_tail: str | None = None,
    layout: str | None = None,
    deferred: list[dict] | None = None,
    stage_costs: list[dict] | None = None,
    agent_context: str | None = None,
    stage_diff: str | None = None,
) -> list[dict[str, str]]:
    """Chat messages for the planner.

    Same prefix as the reviewer for the same reason: the plan and the completed
    history lead, the situation-specific material follows.

    The repository layout leads too, and belongs in the cached prefix: it is
    read once at the run's base sha and does not change. Without it the planner
    writes `edit_files` globs from imagination — the first live run guessed
    `src/calculator.py` at a repository containing `src/calc.py`, took a scope
    violation on stage one, and spent half its intervention budget recovering.
    """
    # Only what is fixed for the whole run. The plan is read once at base_ref
    # and the layout once at base_sha; neither changes while the run does.
    # Layout first, then the plan — the reverse of how they read, and for the
    # same reason the log goes last among the plan documents. The layout is
    # fixed for the run; the log is now live and gains a couple of KB per
    # landing. Behind the plan block it was re-billed every time the log moved,
    # which is the defect the document ordering fixed one level down and this
    # reintroduced one level up the moment the log stopped being frozen.
    leading = ""
    if agent_context:
        # Before the plan, because it describes the machine the plan runs on.
        # A planner that does not know a Gemfile edit reinstalls the bundle
        # reads five items as blocked and draws none of them — which happened.
        leading += (
            "## How this repository works\n\n"
            "Conventions its maintainers keep for whoever works in it, read "
            "once at the plan's commit. **These are facts about the "
            "repository, not work to do** — nothing here is a plan item, and "
            "no stage is drawn from it. Where it contradicts a plan document "
            "about what is possible, it is describing the machine and the "
            "plan is describing intent; say so in `reasoning`.\n\n"
            + agent_context
            + "\n\n"
        )
    if layout:
        leading += "## What the repository contains\n\n" + layout + "\n\n"
    leading += _plan_block(plan, _addendum(cfg))

    # The completed history and the deferred list used to live in here too, and
    # both change as the run proceeds — so every landed stage and every deferral
    # re-billed the plan and the layout along with them. Measured: two planner
    # calls a minute apart, each writing ~91,000 tokens and reading back 4,051,
    # which was the system block, the only part that had not changed. They now
    # follow the breakpoint, costing full price for their own few hundred
    # tokens rather than taking ninety thousand down with them.
    # Split, so the append-only half can be cached separately from the half
    # that churns. The completed history only ever grows at the end, so a
    # breakpoint after it lets Anthropic extend the cached prefix between
    # stages rather than rebuild it. The cost table is a sliding window of the
    # last twelve and the deferral list mutates in place, so both sit outside
    # it — a breakpoint after *those* would miss on every stage and cost more
    # than not caching at all.
    history = _history_block(completed, _addendum(cfg))
    volatile = _costs_block(stage_costs) + "\n\n" + _deferred_block(deferred)

    # The breakpoint, and the reason the ordering above exists. Anthropic
    # caching is explicit: without this marker the plan snapshot and the
    # repository layout are re-billed in full on every planner call, which is
    # most of the prompt and the entire economic argument for calling a paid
    # model at checkpoints.
    #
    # It goes here and nowhere else. A breakpoint after content that changes
    # between calls would invalidate the cache every time, which costs more than
    # not caching at all.
    #
    # The TTL is not decoration. Anthropic's default ephemeral window is about
    # five minutes; between two planner calls sits a whole stage — an executor
    # attempt, a suite, a review, a merge-gate suite — which on a real project
    # is comfortably longer. This block shipped with a bare marker and so
    # expired every time, while the system block one file over carried the
    # configured lifetime and survived. That is precisely what the reports
    # showed across two runs: 3% cached, the 3% being the system block.
    blocks = [
        {
            "type": "text",
            "text": leading,
            "cache_control": cache_control(getattr(cfg, "cache_ttl", None)),
        },
        # The second breakpoint, and the reason this is worth the complexity.
        # The planner is an agentic loop: every turn re-sends the whole prompt,
        # and a derivation runs ten to twenty-five turns. This block is
        # byte-identical across all of them and was being re-sent uncached each
        # time. Measured before the change: 118M prompt tokens across 72 calls
        # at a 50% hit rate, with per-call volume risen from ~670k to ~2.96M as
        # the history grew.
        {
            "type": "text",
            "text": history,
            "cache_control": cache_control(getattr(cfg, "cache_ttl", None)),
        },
    ]

    # History and deferrals lead the situational half: they are the run's state
    # rather than its instructions, and the planner reads them before deciding.
    current: list[str] = [volatile]

    if status_tail:
        current.append(
            "## Recent entries from status.md\n\n"
            "Your own record of what was expected versus what happened.\n\n"
            + status_tail.strip()
        )

    if current_stage is None:
        # A rejected spec leaves no stage behind, so this is the only place the
        # planner can be told about one. Without it the redraw is another
        # derivation with no knowledge of what was wrong — three identical
        # attempts and then the escalation this replaced, at three times the
        # cost of escalating immediately.
        if failure and (failure.get("layer") if isinstance(failure, dict) else None) == "validation":
            current.append(
                _failure_block(
                    failure,
                    heading="Your last stage spec was rejected before it ran",
                    preamble=(
                        "This is a check on the spec itself, not on any work — "
                        "nothing was attempted and nothing was cut. Draw the "
                        "stage again with these fixed. They are mechanical, so "
                        "a redraw that does not address them will be rejected "
                        "the same way."
                    ),
                )
            )
        current.append(
            "## Your task now: derive the next stage\n\n"
            "Given the plan and the stages already completed, produce the next "
            "stage — or return `project_complete` if the plan has been "
            "executed."
        )
    else:
        current.append(
            f"## Your task now: the current stage failed\n\n"
            f"Stage `{current_stage.id}` (revision {revision}) did not land.\n\n"
            "### What it was asked to do\n\n"
            f"{current_stage.instruction or ''}\n\n"
            "### Its declared scope\n\n"
            + "\n".join(f"- {g}" for g in current_stage.edit_files)
        )

        if current_stage.constraints:
            current.append(
                f"### Its constraints\n\n{current_stage.constraints}"
            )

        # The diagnosis first, then what it went on to cause. Read the other
        # way round the planner acts on the consequence: "the attempt
        # reproduced the previous diff" tells it retrying is pointless and
        # nothing about what to draw instead. Both sit here, after the
        # breakpoint, with the rest of the situational material.
        if opening_failure and opening_failure != failure:
            current.append(
                _failure_block(
                    opening_failure,
                    heading="How it first failed",
                    preamble=(
                        "This is what went wrong before the retries. The "
                        "attempts after it were responses to this, so this is "
                        "the failure to draw against."
                    ),
                )
            )

        if failure:
            current.append(
                _failure_block(
                    failure,
                    heading=(
                        "Where it ended up"
                        if opening_failure and opening_failure != failure
                        else "How it failed"
                    ),
                )
            )

        # After the diagnosis, because it is evidence for the choice rather
        # than the choice itself, and a diff placed ahead of the failure pushes
        # the failure down behind material the planner reads second.
        #
        # Here because the planner's own reads cannot supply it. Its tools show
        # the working tree, in which a failed `extend` attempt's work is
        # indistinguishable from code that was always there — so it wrote a
        # revision saying a header and two examples "are already present and
        # must remain byte-identical", which is true of the tree and false of
        # the diff. The reviewer judges the cumulative diff from the stage's
        # start, where all three are additions by this stage, and blocked it
        # for an instruction that contradicts its own scope. Both were reading
        # correctly from different baselines. The executor has had this diff
        # since rework stopped resetting the tree; this is the participant that
        # writes the instruction the other two are held to.
        if stage_diff and stage_diff.strip():
            current.append(
                "## What this stage has already put on its branch\n\n"
                "This is the whole of what the stage has changed since it "
                "started, and it is what the reviewer is shown — not the delta "
                "since the last attempt. **None of it is baseline.** Reading a "
                "file will show you these lines as ordinary existing code; "
                "they are this stage's own doing, and an instruction that "
                "calls them pre-existing describes a tree the reviewer cannot "
                "see and will be blocked for contradicting the diff.\n\n"
                "Write the revision against this, and let its scope cover "
                "everything below that you intend to keep.\n\n"
                f"```diff\n{stage_diff.strip()}\n```"
            )

        current.append(
            "Decide whether to revise this stage, insert a predecessor stage "
            "before it, or stop. A revision keeps the stage's identity, so the "
            "report reads as one stage that took two attempts rather than "
            "pretending they were different work.\n\n"
            "If you revise, `revision_mode` decides the fate of the work "
            "already on the branch: `extend` keeps it (right when the scope was "
            "merely too narrow), `restart` discards it (right when the approach "
            "was wrong)."
        )

    if interventions_max:
        remaining = max(interventions_max - interventions_used, 0)
        current.append(
            f"## Budget\n\n"
            f"You have {remaining} intervention(s) left out of "
            f"{interventions_max} for this run. When they are gone the run "
            "escalates to a human. Spend them on stages drawn wrongly, not on "
            "restating the same instruction."
        )

    # One user message, three blocks. Two consecutive user messages would be
    # rejected by the API, and splitting across messages would put the
    # breakpoints in the wrong place anyway.
    blocks.append({"type": "text", "text": "\n\n".join(current)})
    return [{"role": "user", "content": blocks}]


def _failure_block(
    failure: FailureDetail,
    heading: str = "How it failed",
    preamble: str | None = None,
) -> str:
    """What the planner needs to tell "widen this stage" from "insert a
    predecessor" — the specific damage, not an exit code.

    `heading` is a parameter because a stage can arrive here having failed
    twice for unrelated reasons, and two blocks both titled "How it failed"
    would read as a contradiction rather than a sequence.
    """
    parts = [
        f"### {heading}\n\n"
        + (f"{preamble}\n\n" if preamble else "")
        + f"Gate: **{failure.get('layer')}**\n"
        f"Summary: {failure.get('summary')}"
    ]

    if failure.get("out_of_scope_paths"):
        listed = "\n".join(f"- {p}" for p in failure["out_of_scope_paths"])
        parts.append(
            "### Files touched outside the declared scope\n\n"
            f"{listed}\n\n"
            "If these legitimately belong to this stage, widen `edit_files` to "
            "include them and the work already done stands. If they do not, "
            "leave them out — they will be reverted and the rest of the stage's "
            "work is kept."
        )

    if failure.get("failing_paths"):
        listed = "\n".join(f"- {p}" for p in failure["failing_paths"])
        parts.append(f"### Paths implicated in the failure\n\n{listed}")

    if failure.get("detail"):
        parts.append(f"### Detail\n\n```\n{failure['detail'].strip()}\n```")

    return "\n\n".join(parts)
