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

Judge only the diff you are shown, against the stage you are given.\
"""


def build_executor_prompt(
    stage: Stage,
    cfg: ProjectConfig,
    context: list[tuple[str, str]] | None = None,
    feedback: list[str] | None = None,
) -> str:
    """The message handed to the executor.

    A rework is a *fresh* invocation with no conversation history, so everything
    it needs is restated. It cannot see the plan document, the other stages, or
    the reviewer — only this.
    """
    parts: list[str] = []

    if feedback:
        parts.append(
            "A previous attempt at this task was rejected. Address the feedback "
            "below. Do not repeat the rejected approach."
        )

    parts.append(f"## Task\n\n{stage.instruction}")

    if stage.constraints:
        parts.append(
            "## Hard constraints\n\n"
            f"{stage.constraints}\n"
            "A change that violates these will be rejected even if it is "
            "otherwise correct."
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
            "rather than editing it."
        )

    if stage.read_files:
        listed = "\n".join(f"- {glob}" for glob in stage.read_files)
        parts.append(f"## Context you may read but not change\n\n{listed}")

    if stage.forbidden_patterns:
        listed = "\n".join(f"- /{p}/" for p in stage.forbidden_patterns)
        parts.append(
            "## Patterns you must not introduce\n\n"
            f"{listed}\n\n"
            "These are checked mechanically against the lines you add. They may "
            "be correct elsewhere in the project but are out of bounds here."
        )

    if context:
        blocks = [
            f"### `{command}`\n\n```\n{output.strip()}\n```"
            for command, output in context
        ]
        parts.append(
            "## Context gathered from the repository\n\n" + "\n\n".join(blocks)
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
            "Every document here was read once, when this run started. The log "
            "on disk is appended to as stages land, so by mid-run it is ahead "
            "of the copy above; reading it with `read_file` gives you the "
            "later version. And neither is a substitute for looking at the "
            "code — a count in a document is a claim about when someone wrote "
            "it down."
        )
    return intro + "\n\n" + plan.as_prompt_payload()


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


def _history_block(completed: list[StageResult], addendum_path: str | None = None) -> str:
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

    entries = []
    for entry in completed:
        line = f"### Stage {entry.get('index')}: {entry.get('id')}"
        if entry.get("revisions"):
            line += f" (took {entry['revisions'] + 1} revisions)"
        line += "\n\n" + (entry.get("instruction") or "").strip()
        if entry.get("merge_sha"):
            line += f"\n\nLanded as `{entry['merge_sha'][:12]}`."
        # What the executor actually had to hold. The only honest basis for
        # sizing the next stage: a file count says nothing, since two stages
        # that each edited one file have differed here by more than threefold.
        # Absent for script stages and for runs that predate the measurement,
        # and omitted rather than rendered as a zero the planner might read as
        # free.
        if entry.get("executor_context_tokens"):
            line += (
                f"\nExecutor context: {entry['executor_context_tokens']:,} tokens."
            )
        if entry.get("review_summary"):
            line += f"\nReviewer: {entry['review_summary']}"
        entries.append(line)

    return (
        "## Completed stages, in order\n\n"
        "Each landed as one commit on the project branch after passing review "
        "and the full suite.\n\n" + "\n\n".join(entries)
    )


def build_review_messages(
    stage: Stage,
    cfg: ProjectConfig,
    diff: str,
    plan: PlanTree,
    completed: list[StageResult],
) -> list[dict[str, str]]:
    """Chat messages for the reviewer, stable payload first.

    Everything before the last message is byte-identical across the stages of a
    run — that is what makes prefix caching hit, and why the diff is last.
    """
    messages = [{"role": "system", "content": REVIEW_SYSTEM_PROMPT}]
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
                    "type": "text",
                    # The plan alone. The completed-stage history used to live
                    # here too, and every landing changed it — taking ~50,000
                    # tokens of unchanged plan documents out of cache with a few
                    # hundred tokens of history. Measured on one run: three
                    # reviewer calls, three full-price writes, one of them only
                    # 13 minutes after its predecessor and well inside the
                    # retention window. History now follows the breakpoint.
                    "text": _plan_block(plan, _addendum(cfg)),
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    )

    current: list[str] = [
        _history_block(completed, _addendum(cfg)),
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

    messages.append({"role": "user", "content": "\n\n".join(current)})
    return messages


def build_planner_messages(
    cfg: ProjectConfig,
    plan: PlanTree,
    completed: list[StageResult],
    current_stage: Stage | None = None,
    failure: FailureDetail | None = None,
    revision: int = 0,
    interventions_used: int = 0,
    interventions_max: int = 0,
    status_tail: str | None = None,
    layout: str | None = None,
    deferred: list[dict] | None = None,
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
    leading = _plan_block(plan, _addendum(cfg))
    if layout:
        leading += "\n\n## What the repository contains\n\n" + layout

    # The completed history and the deferred list used to live in here too, and
    # both change as the run proceeds — so every landed stage and every deferral
    # re-billed the plan and the layout along with them. Measured: two planner
    # calls a minute apart, each writing ~91,000 tokens and reading back 4,051,
    # which was the system block, the only part that had not changed. They now
    # follow the breakpoint, costing full price for their own few hundred
    # tokens rather than taking ninety thousand down with them.
    situational = (
        _history_block(completed, _addendum(cfg))
        + "\n\n"
        + _deferred_block(deferred)
    )

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
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": leading,
                    "cache_control": cache_control(
                        getattr(cfg, "cache_ttl", None)
                    ),
                }
            ],
        }
    ]

    # History and deferrals lead the situational half: they are the run's state
    # rather than its instructions, and the planner reads them before deciding.
    current: list[str] = [situational]

    if status_tail:
        current.append(
            "## Recent entries from status.md\n\n"
            "Your own record of what was expected versus what happened.\n\n"
            + status_tail.strip()
        )

    if current_stage is None:
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

        if failure:
            current.append(_failure_block(failure))

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

    messages.append({"role": "user", "content": "\n\n".join(current)})
    return messages


def _failure_block(failure: FailureDetail) -> str:
    """What the planner needs to tell "widen this stage" from "insert a
    predecessor" — the specific damage, not an exit code."""
    parts = [
        "### How it failed\n\n"
        f"Gate: **{failure.get('layer')}**\n"
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
