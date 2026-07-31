"""The append-only record of what the work turned out to be.

Plan documents are written before the work and go stale during it. A checklist
says twenty-four call sites across nine controllers; eight of those controllers
are now clean and the document has no way to know. On the first long run the
operator hand-wrote a paragraph of `planner.guidance` describing three landed
stages, because a new run starts with an empty history and would otherwise
re-derive work already done.

So the planner records what it observed, with a citation, and something else
folds those observations into the documents later. That separation is the whole
point: rewriting a plan is a judgement about what the work has become, and it
should not happen unattended in the middle of doing the work.

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


def _entry(note: dict, stage_id: str, when: str) -> str:
    # No commit sha: the entry travels inside the commit it describes, so
    # citing that commit from within it would be both circular and impossible
    # — the sha does not exist until the squash that includes this file.
    lines = [
        f"## {note.get('plan_step', '(unattributed)')}",
        "",
        f"- **observed** while landing `{stage_id}` ({when})",
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
    when: str,
) -> Path | None:
    """Append observations to the addendum. Returns the file written, if any.

    Silent no-op when the project has not configured a path or the planner had
    nothing to say, which is most of the time — a note is for when the plan and
    the repository disagree, not for narrating every stage.
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
            fh.write(_entry(note, stage_id, when))
            fh.write("\n")
    return target
