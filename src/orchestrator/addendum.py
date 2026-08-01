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

import re
from pathlib import Path


_REF = re.compile(r"^(?P<path>[^#\s]+)#L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")


def parse_ref(ref: str) -> tuple[str, int, int] | None:
    """`path#L23-L45`, the shape a GitHub permalink uses.

    Returns None for anything that is not one, which is how a note whose
    citation cannot be trusted stops being treated as a citation.
    """
    match = _REF.match((ref or "").strip())
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    return match.group("path"), start, min_max(start, end)


def min_max(start: int, end: int) -> int:
    return end if end >= start else start


def heading_at(text: str, line: int) -> str:
    """The nearest Markdown heading at or above `line`.

    Derived rather than written. A header the planner composes is only as
    consistent as the model's memory of how it phrased the same section last
    time, and grouping entries is the entire job — two stages working the same
    plan section have to produce the same string *necessarily*, not usually.
    Lifting it from the document makes that structural.
    """
    body = text.splitlines()
    for index in range(min(line, len(body)) - 1, -1, -1):
        candidate = body[index].strip()
        if candidate.startswith("#"):
            return candidate.lstrip("#").strip()
    return ""


def resolve_header(ref: str, read_plan) -> tuple[str, str]:
    """(header, problem) for a citation, without ever discarding the note.

    A bad citation costs a header. It must not cost the observation: the
    progress record is the thing the next run reads to know what is done, and
    three separate defects this project has already shipped were notes computed
    correctly and lost on the way to disk. So an unresolvable reference is
    reported in the entry and the entry is still written.
    """
    parsed = parse_ref(ref)
    if not parsed:
        return "", "citation is not a `path#Lstart-Lend` reference"
    path, start, end = parsed
    try:
        text = read_plan(path)
    except Exception:
        return "", f"`{path}` is not readable in the plan at this commit"
    if text is None:
        return "", f"`{path}` is not readable in the plan at this commit"
    total = len(text.splitlines())
    if start > total:
        return "", f"`{path}` has {total} lines; L{start} is past the end"
    return heading_at(text, start), ""


def _entry(note: dict, stage_id: str, read_plan=None, plan_sha: str = "") -> str:
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
    ref = (note.get("plan_ref") or "").strip()
    header, problem = ("", "")
    if ref and read_plan is not None:
        header, problem = resolve_header(ref, read_plan)

    title = header or note.get("plan_step") or "(unattributed)"
    if ref:
        title += f" — `{ref}`"
        if plan_sha:
            # Pinned for the same reason a GitHub permalink is: the folding
            # pass edits these documents, so a line range means one thing at
            # the commit it was read from and something else afterwards.
            title += f" @ `{plan_sha[:12]}`"

    lines = [f"## {title}", "", f"- **observed** while landing `{stage_id}`"]
    if problem:
        lines.append(f"- **citation** unresolved: {problem}")
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
    read_plan=None,
    plan_sha: str = "",
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
            fh.write(_entry(note, stage_id, read_plan, plan_sha))
            fh.write("\n")
    return target
