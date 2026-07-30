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


def _plan_block(plan: PlanTree) -> str:
    return (
        "## The plan\n\n"
        "This is the authority for the project. A stage instruction is a "
        "pointer into it, not a substitute for it.\n\n"
        + plan.as_prompt_payload()
    )


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


def _history_block(completed: list[StageResult]) -> str:
    if not completed:
        return (
            "## Completed stages\n\nNone yet — this is the first stage of the "
            "project."
        )

    entries = []
    for entry in completed:
        line = f"### Stage {entry.get('index')}: {entry.get('id')}"
        if entry.get("revisions"):
            line += f" (took {entry['revisions'] + 1} revisions)"
        line += "\n\n" + (entry.get("instruction") or "").strip()
        if entry.get("merge_sha"):
            line += f"\n\nLanded as `{entry['merge_sha'][:12]}`."
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
    messages.append(
        {
            "role": "user",
            "content": _plan_block(plan) + "\n\n" + _history_block(completed),
        }
    )

    current: list[str] = [
        f"## The stage under review: {stage.id}\n\n{stage.instruction or ''}"
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
    leading = _plan_block(plan)
    if layout:
        leading += "\n\n## What the repository contains\n\n" + layout
    leading += "\n\n" + _history_block(completed)
    leading += "\n\n" + _deferred_block(deferred)

    # The breakpoint, and the reason the ordering above exists. Anthropic
    # caching is explicit: without this marker the plan snapshot, the repository
    # layout and the completed history are re-billed in full on every planner
    # call, which is most of the prompt and the entire economic argument for
    # calling a paid model at checkpoints.
    #
    # It goes here and nowhere else. A breakpoint after content that changes
    # between calls would invalidate the cache every time, which costs more than
    # not caching at all.
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": leading,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]

    current: list[str] = []

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
