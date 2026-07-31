"""The progress log: how the plan learns what has been done.

A plan document lists work. Nothing in it knows which of that work has already
happened, and a run starts with an empty history however much the branch
carries — so without a record, the next run reads the same plan and derives a
stage that already landed. On the first long run the operator patched that by
hand, writing a paragraph of `planner.guidance` describing three landed stages.
This is that paragraph, written by the work itself.

So the usual entry is progress: a stage landed, and here is where its plan step
now stands. A correction — the plan was wrong when written — is the same
mechanism pointed at a different cause and belongs here too. Both answer one
question: what does the plan not yet know?

The planner writes the entries and something else folds them into the
documents later. That separation is the whole point: rewriting a plan is a
judgement about what the work has become, and it should not happen unattended
in the middle of doing the work.

Two properties this file exists to guarantee.

**Append-only.** Entries are added; nothing is edited or removed. A note that
turns out to be wrong is corrected by a later note, in the open, not by
rewriting the record.

**Written outside the stage's diff.** The orchestrator appends after a stage has
landed, from the planner's structured output. No executor ever writes here — the
scope guard treats this path as a plan document precisely so an attempt to is
caught. A stage that could edit the record of its own work is a stage that can
launder its own history.
"""

from __future__ import annotations

from pathlib import Path


def _entry(note: dict, stage_id: str) -> str:
    """One observation, carrying only what git cannot tell you.

    No commit sha and no timestamp. Both belong to the commit this entry is
    written into, and `git log` or `git blame` on the file answers either —
    correctly after a rebase, where the same facts embedded in append-only
    prose would become claims about history that history had invalidated.

    The stage id stays, and is not the same kind of duplication. It is the
    semantic thread back to the work that produced the insight, and this file
    is meant to be read as a document by whoever folds it into the plan. Making
    them blame forty lines to see which piece of work each came from would be a
    poor trade for one line of redundancy.
    """
    lines = [
        f"## {note.get('plan_step', '(unattributed)')}",
        "",
        f"- **observed** while landing `{stage_id}`",
    ]
    if note.get("supersedes"):
        lines.append(f"- **supersedes** {note['supersedes']}")
    lines += ["", note.get("observation", "").strip(), ""]
    return "\n".join(lines)


def append_notes(
    repo: Path,
    addendum_path: str | None,
    notes: list[dict],
    *,
    stage_id: str,
) -> Path | None:
    """Append entries to the progress log. Returns the file written, if any.

    Silent no-op when the project has not configured a path, or when the stage
    mapped to nothing in the plan worth recording. A stage that advances a plan
    step should carry an entry — that is what keeps the next run from deriving
    it again — but not every stage does, and an empty note is worse than none.
    """
    if not addendum_path or not notes:
        return None

    target = Path(repo) / addendum_path
    # A path may name a directory to collect notes in, or the file itself. A
    # directory is the better default for a long project: one file per run
    # keeps a fourteen-hour session from producing one unreadable document.
    if target.suffix != ".md":
        target = target / "plan-addendum.md"
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        target.write_text(
            "# Plan addendum\n\n"
            "Observations recorded during automated runs, each citing what was "
            "read to support it. Append-only and written by the orchestrator; "
            "no stage may edit this file.\n\n"
            "These are notes for a later pass, not changes to the plan. The "
            "plan documents still say what they said.\n\n"
        )

    with target.open("a") as fh:
        for note in notes:
            fh.write(_entry(note, stage_id))
            fh.write("\n")
    return target
