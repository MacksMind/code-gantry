"""Prompt construction for the executor and the reviewer.

Pure string building, kept separate from the clients that send it so it can
be tested without a model.

Two design points carry weight:

- The executor is told the stage's constraints and forbidden patterns, not
  just the reviewer. The executor sees one stage at a time and cannot
  otherwise know that a technically-correct edit is illegal in this one;
  finding out at review time wastes a whole attempt.
- The review messages are ordered stable-first. OpenAI caches on matching
  prompt prefixes, so the reference documents and stage list lead and the
  per-stage diff comes last. Reordering for readability would silently
  double the cost of every review.
"""

from __future__ import annotations

from orchestrator.config import RunConfig, Stage

REVIEW_SYSTEM_PROMPT = """\
You are the reviewer in a two-model refactoring loop. A local model makes the
edits; you inspect the resulting diff at each stage boundary and decide
whether the work may advance.

The tests already pass — that is a precondition of you being called, not
something you need to confirm. Your job is what a test suite cannot check:
whether this diff does what the stage asked, stays inside the stage's
constraints, and remains consistent with decisions made in earlier stages.
The executor sees only one stage at a time, so cross-stage drift is yours to
catch and nobody else's.

Return one of three verdicts:

- "approved" — the diff does what the stage asked and honours its
  constraints. Minor stylistic preferences are not grounds for rework.
- "rework" — there is a specific, fixable defect in this diff. Say precisely
  what is wrong and why it matters, so the next attempt can act on it.
- "blocked" — the stage instruction itself is wrong, or the plan has a flaw
  that reworking this diff will not fix. Use this when the problem is
  upstream of the executor. It stops the run for a human decision, which is
  the correct outcome when the instruction cannot be satisfied as written.
  Do not grind through rework attempts on an impossible instruction.

Judge only the diff you are shown against the stage you are given.\
"""


def build_executor_prompt(
    stage: Stage,
    cfg: RunConfig,
    context: list[tuple[str, str]] | None = None,
    feedback: list[str] | None = None,
) -> str:
    """The message handed to the executor.

    A rework attempt is a *fresh* invocation, so everything the executor
    needs must be restated — no conversation history carries over.
    """
    parts: list[str] = []

    if feedback:
        parts.append(
            "A previous attempt at this task was rejected. Start again from "
            "the current state of the repository and address the feedback "
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
            "Write the tests for this behaviour first, then the "
            "implementation that satisfies them. A change with no tests will "
            "be rejected."
        )

    if stage.edit_files:
        listed = "\n".join(f"- {glob}" for glob in stage.edit_files)
        parts.append(
            "## Files you may change\n\n"
            f"{listed}\n\n"
            "Editing anything outside this list will fail the stage. If the "
            "task appears to require a file that is not listed, stop and say "
            "so rather than editing it."
        )

    if stage.forbidden_patterns:
        listed = "\n".join(f"- /{p}/" for p in stage.forbidden_patterns)
        parts.append(
            "## Patterns you must not introduce\n\n"
            f"{listed}\n\n"
            "These are checked mechanically against the lines you add. They "
            "may be correct elsewhere in the project but are out of bounds "
            "for this stage."
        )

    if context:
        blocks = [
            f"### `{command}`\n\n```\n{output.strip()}\n```" for command, output in context
        ]
        parts.append("## Context gathered from the repository\n\n" + "\n\n".join(blocks))

    if feedback:
        listed = "\n\n".join(f"{i}. {item}" for i, item in enumerate(feedback, start=1))
        parts.append(f"## Feedback on previous attempts\n\n{listed}")

    return "\n\n".join(parts)


def build_review_messages(
    stage: Stage,
    cfg: RunConfig,
    diff: str,
    documents: list[tuple[str, str]],
) -> list[dict[str, str]]:
    """Chat messages for the reviewer, ordered stable payload first.

    Messages before the last are byte-identical across every stage of a run.
    That is what makes prefix caching hit, and it is why the diff is last.
    """
    messages = [{"role": "system", "content": REVIEW_SYSTEM_PROMPT}]

    stable: list[str] = []

    if documents:
        blocks = [
            f"### {name}\n\n{body.strip()}" for name, body in documents
        ]
        stable.append(
            "## Reference documents\n\n"
            "These are the authority for this refactor. A stage instruction "
            "is a pointer into them, not a substitute for them.\n\n"
            + "\n\n".join(blocks)
        )

    stage_list = "\n\n".join(
        f"### Stage {i}: {s.id}"
        + (f" ({s.kind})" if s.kind != "agent" else "")
        + "\n\n"
        + (s.instruction or s.command or s.human_steps or "")
        + (f"\n\nConstraints: {s.constraints}" if s.constraints else "")
        for i, s in enumerate(cfg.stages, start=1)
    )
    stable.append(
        "## Every stage in this run, in order\n\n"
        "You are shown all of them so you can catch drift from decisions made "
        "in earlier stages, and changes that belong to a later stage leaking "
        "into this one.\n\n" + stage_list
    )

    messages.append({"role": "user", "content": "\n\n".join(stable)})

    current: list[str] = [
        f"## The stage under review: {stage.id}\n\n{stage.instruction or stage.command or stage.human_steps or ''}"
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
            "This stage creates new code, so there is no prior behaviour to "
            "compare against. Judge it against these criteria."
        )

    current.append(f"## The diff\n\n```diff\n{diff.strip()}\n```")
    current.append(
        "Return your verdict now. If the stage instruction itself cannot be "
        "satisfied as written, return \"blocked\" rather than \"rework\"."
    )

    messages.append({"role": "user", "content": "\n\n".join(current)})
    return messages
