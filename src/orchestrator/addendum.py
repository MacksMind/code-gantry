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


_NOISE = re.compile(r"[`*_>#\[\]]")


def _normalise(text: str) -> str:
    """Markdown punctuation and whitespace removed, for comparing quotes."""
    return " ".join(_NOISE.sub("", text or "").split()).casefold()


def locate(anchor: str, text: str) -> tuple[int, int] | None:
    """The line span of `anchor` within `text`, 1-based and inclusive.

    The counting belongs here. Asking the planner for a line range meant
    asking a model to count lines in a document the prompt shows it as
    unnumbered prose — the first three citations under that scheme were each a
    real file and a real in-range span pointing at the wrong place entirely.
    Character offsets would be worse, not better; nothing that reads text as
    tokens can count either.

    What the planner is reliably good at is quoting. Every anchor it produced
    while the field was called `supersedes` was real text from a real plan
    document; only the arithmetic was wrong. So it quotes and this counts.
    """
    needle = _normalise(anchor)
    if len(needle) < 12:
        return None
    lines = text.splitlines()

    # The tightest window, not the first one found. Windows are tested from the
    # top of the document, so the earliest `first` that reaches the quote at
    # all is line 1 — returning that would cite the whole document up to the
    # passage rather than the passage.
    best: tuple[int, int] | None = None
    for first in range(len(lines)):
        for last in range(first, len(lines)):
            joined = _normalise("\n".join(lines[first : last + 1]))
            if needle in joined:
                if best is None or (last - first) < (best[1] - best[0]):
                    best = (first, last)
                break
            if len(joined) > len(needle) * 3:
                break
    return (best[0] + 1, best[1] + 1) if best else None


def resolve_anchor(path: str, anchor: str, read_plan) -> tuple[str, str, str]:
    """(header, `path#Lx-Ly`, problem) — never discarding the note.

    A quotation that cannot be found costs its location. It must not cost the
    observation: the progress record is what the next run reads to know what is
    done, and three separate defects on this project were notes computed
    correctly and lost on the way to disk.
    """
    if not path:
        return "", "", "no plan document named"
    try:
        text = read_plan(path)
    except Exception:
        text = None
    if text is None:
        return "", "", f"`{path}` is not readable in the plan at this commit"
    if not anchor.strip():
        return "", "", "no quotation given, so there is nothing to locate"

    span = locate(anchor, text)
    if span is None:
        return "", "", (
            f"the quoted text was not found in `{path}`; the note and the "
            "document disagree about what the plan says"
        )
    first, last = span
    ref = f"{path}#L{first}" + (f"-L{last}" if last != first else "")
    return heading_at(text, first), ref, ""


_ESCAPE = re.compile(r"(?<!\\)\\u([0-9a-fA-F]{4})")


def decode_escapes(text: str) -> str:
    """`\\u2014` written literally becomes the character it meant.

    The planner returns JSON, and a model that double-escapes a non-ASCII
    character emits `\\\\u2014` — which decodes correctly to a backslash
    followed by `u2014`, and lands in a committed Markdown document looking
    like a bug in this tool. Seen twice in one stage out of fifty-two.

    Narrow on purpose: a sequence already escaped by a preceding backslash is
    left alone, so a document that legitimately discusses escape syntax keeps
    saying what it said.
    """
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text or "")


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
    anchor = decode_escapes((note.get("anchor") or "").strip())
    header, ref, problem = ("", "", "")
    if read_plan is not None:
        header, ref, problem = resolve_anchor(
            (note.get("plan_path") or "").strip(), anchor, read_plan
        )

    title = header or "(unattributed)"
    if ref:
        title += f" — `{ref}`"
        if plan_sha:
            # Pinned for the same reason a GitHub permalink is: the folding
            # pass edits these documents, so a line range means one thing at
            # the commit it was read from and something else afterwards.
            title += f" @ `{plan_sha[:12]}`"

    # "while planning", not "while landing", and the distinction is the whole
    # point. The planner produces these when it *derives* the stage — before
    # the executor has run — and `advance` holds them until the stage lands.
    # They are what reading the plan against the code revealed, which is
    # genuinely worth recording, and they are not an account of what the work
    # did. Labelling them "while landing" made a prediction read as a report:
    # one entry said a controller "now permits" two fields "with a two-shop
    # controller-spec example reading the values back", written before a line
    # of it existed. The stage happened to deliver it. The log had no way to
    # know that, and the next planner call read it back as history.
    lines = [f"## {title}", "", f"- **observed** while planning `{stage_id}`"]
    if problem:
        lines.append(f"- **citation** unresolved: {problem}")
    if anchor:
        lines.append(f"- **the plan says** {anchor}")
    # The planner's own one-line finding, after what the plan claims and before
    # the prose. The heading is lifted from the plan so that two stages working
    # one section group together necessarily rather than usually — which is
    # right, and says nothing about what was found. Without this, an entry has
    # a filing label and a paragraph and nothing in between.
    finding = decode_escapes(note.get("finding") or "").strip()
    if finding:
        lines.append(f"- **found** {finding}")
    lines += ["", decode_escapes(note.get("observation", "")).strip(), ""]
    return "\n".join(lines)


def _observation(note: dict, stage_id: str) -> str:
    """One reviewer finding, filed under the file it is about.

    No plan citation, and that is the difference from `_entry`. A planner note
    says the plan is out of date and quotes the passage it contradicts; a
    reviewer finding says the *code* has a problem the stage did not cause, and
    usually corresponds to nothing written in the plan at all. Requiring an
    anchor would mean asking for a quotation that does not exist, and the
    planner's own citations went wrong in exactly that direction — a real file
    and a real span pointing at the wrong place.

    So it is filed under the file. Two findings about one file group together
    for the same structural reason headings are lifted from the plan rather
    than composed.
    """
    path = decode_escapes((note.get("file") or "").strip()) or "(unattributed)"
    finding = decode_escapes((note.get("finding") or "").strip())
    detail = decode_escapes((note.get("detail") or "").strip())

    lines = [
        f"## `{path}`",
        "",
        f"- **observed** by the reviewer while landing `{stage_id}`",
    ]
    if finding:
        lines.append(f"- **found** {finding}")
    lines += ["", detail, ""]
    return "\n".join(lines)


def append_observations(
    repo: Path,
    addendum_path: str | None,
    observations: list[dict],
    *,
    stage_id: str,
) -> Path | None:
    """Append reviewer findings to the progress log.

    Same file and same append-only guarantee as the planner's notes, and the
    same reason for existing: something learned while doing the work that the
    plan does not know. The difference is who noticed and what about.

    Written only when a stage lands. A stage can be reviewed several times
    across rework attempts, and writing on each would report one finding three
    times — so `review` replaces what it holds rather than accumulating, and
    this runs once, from whatever the last review saw.

    Reaching the planner needs no further plumbing: the log is spliced live into
    the planner's prompt, so a finding written here is in its context on the
    next derivation. The reviewer is shown the same live log, which is also how
    it avoids reporting the same thing on every subsequent stage.
    """
    if not addendum_path or not observations:
        return None

    target = _target(repo, addendum_path)
    _ensure_header(target)

    with target.open("a") as fh:
        for note in observations:
            fh.write(_observation(note, stage_id))
            fh.write("\n")
    return target


def append_outcome(
    repo: Path,
    addendum_path: str | None,
    *,
    stage_id: str,
    summary: str,
) -> Path | None:
    """Record what the stage actually did, in the reviewer's words.

    This exists because everything else in this file is written before the
    work. The planner's notes are drafted while the stage is derived; the
    reviewer's observations are about code the stage did not touch. Nothing
    recorded what landed — and the planner reads this log back as history on
    every subsequent derivation, so an intention published on landing became
    the account of record.

    The reviewer's summary is the right source and needs no new model call: it
    is written after reading the diff, by the only participant that saw it,
    and it already describes the change rather than the request. It is
    unavailable on a stage that never landed, which is correct — there is
    nothing to report.

    First in the entry order, ahead of the planner's notes, because it is the
    only part that is a claim about the past.
    """
    if not addendum_path or not (summary or "").strip():
        return None

    target = _target(repo, addendum_path)
    _ensure_header(target)

    body = decode_escapes(summary.strip())
    with target.open("a") as fh:
        fh.write(f"## What `{stage_id}` landed\n\n")
        fh.write("- **reviewed** after the diff was written\n\n")
        fh.write(f"{body}\n\n")
    return target


def _target(repo: Path, addendum_path: str) -> Path:
    """The file to append to, creating the directory if needed."""
    target = Path(repo) / addendum_path
    # A path may name a directory to collect notes in, or the file itself. A
    # directory is the better default for a long project: one file per run
    # keeps a fourteen-hour session from producing one unreadable document.
    if target.suffix != ".md":
        target = target / "plan-addendum.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _ensure_header(target: Path) -> None:
    if target.exists():
        return
    target.write_text(
        "# Plan addendum\n\n"
        "Observations recorded during automated runs, each citing what was "
        "read to support it. Append-only and written by the orchestrator; "
        "no stage may edit this file.\n\n"
        "These are notes for a later pass, not changes to the plan. The "
        "plan documents still say what they said.\n\n"
    )


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

    target = _target(repo, addendum_path)
    _ensure_header(target)

    with target.open("a") as fh:
        for note in notes:
            fh.write(_entry(note, stage_id, read_plan, plan_sha))
            fh.write("\n")
    return target
