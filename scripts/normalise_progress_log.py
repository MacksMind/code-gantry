"""One-shot: rewrite a progress log into one entry shape. Delete after use.

Not part of the package — it lives in `scripts/` and is excluded from the
wheel and from `testpaths`, so it costs the shipped tool nothing. It exists
because one project's log accumulated five entry shapes across three format
changes, and the fix is to migrate the file once rather than teach every
reader to interpret all five. A second project will not need it; if one does,
it will need a different one.

Run it during a pause, never against a live run — it rewrites a file
CodeGantry appends to, and `advance` writing an entry underneath it would
lose that entry.

    uv run python scripts/normalise_progress_log.py <project> --plan-sha <sha>
    uv run python scripts/normalise_progress_log.py <project> --plan-sha <sha> --write

Dry run by default: it prints the counts and a preview and touches nothing.
Verify it first with `uv run pytest scripts/` — it is destructive, one-shot,
and the file it rewrites is the record of everything the run has learned.



The format changed three times while the first long run was in flight, so the
file carries all of them at once. A consumer — the folding pass, a human, the
planner reading its own history — would otherwise have to interpret five
shapes, and interpreting five shapes is five chances to misread one.

Two rules govern every transformation here.

**Prose is never lost.** The observation is the part that cannot be recomputed;
the citation is only how you find what it is about. An entry whose citation
will not resolve becomes explicitly unattributed and keeps every word.

**A citation is derived, never trusted.** The old entries cite plan text by
line number, and model-written line numbers are exactly what the current format
exists to avoid — the first three under that scheme each named a real file and
a real in-range span pointing somewhere else entirely. Where an old entry also
quotes what it cites, that quote is an anchor and `locate` finds where the text
actually is. Where it does not, the entry says so rather than carrying a number
nobody checked.

Deliberately pure: parsing and rendering only, with plan documents supplied by
a `read_plan(sha, path)` callable. It can then be tested without a repository,
and run against a file without a run in flight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from code_gantry.addendum import heading_at, locate

_ENTRY = re.compile(r"(?m)^## ")
_BULLET = re.compile(r"(?m)^- \*\*([a-z ]+)\*\* (.*)$")
_REF_IN_HEADER = re.compile(r"#L\d+")
_SHA = re.compile(r"landed as `([0-9a-f]{7,40})`")
_STAGE = re.compile(r"`([^`]+)`")
# A path ending in .md, then optionally `:` and a line span, then optionally a
# parenthesised quotation. The quotation is the only part worth trusting.
_CITATION = re.compile(r"([\w./-]+\.md)(?::[\d\-,: ]+)?(?:\s*\(\"([^\"]+)\"\))?")

UNATTRIBUTED = "(unattributed)"


@dataclass
class Entry:
    header: str
    bullets: dict[str, str]
    prose: str

    @property
    def shape(self) -> str:
        """Which of the formats this entry was written in.

        `current` covers the unresolved-citation form too: an entry that
        records why its anchor could not be found is in the current shape and
        already says everything it can.
        """
        keys = set(self.bullets)
        has_ref = bool(_REF_IN_HEADER.search(self.header))
        if "the plan says" in keys or "citation" in keys:
            # An entry recording *why* it has no citation is in the
            # current shape and complete. Re-deriving it every pass would
            # make normalisation non-idempotent for exactly the entries
            # that already tried and failed.
            return "current"
        if "supersedes" in keys:
            return "v2" if has_ref else "v1"
        return "v0"


@dataclass
class Document:
    preamble: str
    entries: list[Entry] = field(default_factory=list)


@dataclass
class Report:
    total: int = 0
    unchanged: int = 0
    resolved: int = 0
    renamed: int = 0
    unattributed: int = 0


def parse_document(text: str) -> Document:
    """Split into a preamble and entries, on `## ` at the start of a line.

    Only at the start of a line: the prose of a migration log is full of Ruby
    comments, Markdown fragments and shell snippets, and a looser split turns
    one of them into a spurious entry that swallows the rest of the note.
    """
    parts = _ENTRY.split(text)
    return Document(preamble=parts[0], entries=[_parse_entry(p) for p in parts[1:]])


def _parse_entry(chunk: str) -> Entry:
    lines = chunk.split("\n")
    header = lines[0].strip()
    bullets = {key.strip(): value.strip() for key, value in _BULLET.findall(chunk)}
    # Prose is whatever follows the last bullet. An entry with no bullets at
    # all is malformed, but its text still has to survive.
    body = chunk.split("\n")[1:]
    last = max(
        (i for i, line in enumerate(body) if line.startswith("- **")), default=-1
    )
    return Entry(header=header, bullets=bullets, prose="\n".join(body[last + 1 :]))


def split_citations(text: str) -> list[tuple[str, str]]:
    """(path, quoted anchor) for each document a `supersedes` bullet names.

    The anchor is empty when the citation gave only a line number. That is not
    a failure to parse — it is the citation failing to say anything checkable,
    which is the whole reason these are re-derived.
    """
    out: list[tuple[str, str]] = []
    for path, quote in _CITATION.findall(text):
        pair = (path, quote.strip())
        if pair not in out:
            out.append(pair)
    return out


def _stage_of(entry: Entry) -> str:
    observed = entry.bullets.get("observed", "")
    found = _STAGE.search(observed)
    return found.group(1) if found else ""


def _sha_of(entry: Entry) -> str:
    found = _SHA.search(entry.bullets.get("observed", ""))
    return found.group(1) if found else ""


def _render(
    *,
    heading: str,
    ref: str,
    sha: str,
    stage: str,
    says: str,
    found: str,
    problem: str,
    prose: str,
) -> str:
    title = heading or UNATTRIBUTED
    if ref:
        title += f" — `{ref}`"
        if sha:
            title += f" @ `{sha}`"
    lines = [f"## {title}", ""]
    lines.append(f"- **observed** while landing `{stage}`" if stage else "- **observed**")
    if problem:
        lines.append(f"- **citation** unresolved: {problem}")
    if says:
        lines.append(f"- **the plan says** {says}")
    if found:
        # The planner's own one-line finding. The header is lifted from the
        # plan so that two stages working one section group together
        # necessarily rather than usually — which is right, and says nothing
        # about what was found. This is where that goes.
        lines.append(f"- **found** {found}")
    return "\n".join(lines) + "\n" + prose.rstrip() + "\n"


def normalise_entry(entry: Entry, read_plan) -> str:
    """One entry in the current shape. Returns it unchanged when it already is.

    `read_plan(sha, path)` returns a plan document's text, or None.
    """
    shape = entry.shape
    if shape == "current":
        return _reassemble(entry)
    if shape == "v2":
        # The reference was already derived and checked; only the bullet key
        # predates the rename. Re-deriving risks changing a citation that is
        # right, for nothing.
        moved = Entry(
            header=entry.header,
            bullets={
                ("the plan says" if k == "supersedes" else k): v
                for k, v in entry.bullets.items()
            },
            prose=entry.prose,
        )
        return _reassemble(moved)

    stage, sha = _stage_of(entry), _sha_of(entry)
    citations = split_citations(entry.bullets.get("supersedes", ""))

    missing: list[str] = []
    for path, anchor in citations:
        if not anchor:
            continue
        text = read_plan(sha, path)
        if text is None:
            missing.append(path)
            continue
        span = locate(anchor, text)
        if span is None:
            continue
        first, last = span
        ref = f"{path}#L{first}" + (f"-L{last}" if last != first else "")
        return _render(
            heading=heading_at(text, first),
            ref=ref,
            sha=sha,
            stage=stage,
            says=anchor,
            found=entry.header,
            problem="",
            prose=entry.prose,
        )

    return _render(
        heading="",
        ref="",
        sha="",
        stage=stage,
        says="",
        found=entry.header,
        problem=_why_not(citations, missing),
        prose=entry.prose,
    )


def _why_not(citations: list[tuple[str, str]], missing: list[str] = ()) -> str:
    if missing:
        named = ", ".join(f"`{path}`" for path in dict.fromkeys(missing))
        return (
            f"cited {named}, which does not exist at this revision. The plan "
            "was reorganised after this note was written, so the citation "
            "points at a document that is gone rather than at moved text"
        )
    if not citations:
        return (
            "this entry predates derived citations and quotes no plan text, so "
            "there is nothing to locate"
        )
    if not any(anchor for _, anchor in citations):
        named = ", ".join(f"`{path}`" for path, _ in citations)
        return (
            f"cited {named} by line number only. Line numbers written by a "
            "model are what derived citations replaced, so this one is not "
            "carried forward unchecked"
        )
    return (
        "the quoted text was not found in any document it cites; the note and "
        "the plan disagree about what the plan says"
    )


def _reassemble(entry: Entry) -> str:
    """Rebuild an entry that needs no change, byte for byte.

    Rendering it through `_render` instead would reorder bullets and reflow
    whitespace on 199 entries that are already correct, putting the whole file
    in the diff and hiding the entries that actually moved.
    """
    order = ["observed", "citation", "the plan says", "found"]
    keys = [k for k in order if k in entry.bullets]
    keys += [k for k in entry.bullets if k not in order]
    lines = [f"## {entry.header}", ""]
    lines += [f"- **{k}** {entry.bullets[k]}" for k in keys]
    return "\n".join(lines) + "\n" + entry.prose.rstrip() + "\n"


def normalise_document(text: str, read_plan) -> tuple[str, Report]:
    """The whole file, plus a count of what happened to it."""
    doc = parse_document(text)
    report = Report(total=len(doc.entries))
    out: list[str] = []
    for entry in doc.entries:
        shape = entry.shape
        rendered = normalise_entry(entry, read_plan)
        if shape == "current":
            report.unchanged += 1
        elif shape == "v2":
            report.renamed += 1
        elif rendered.startswith(f"## {UNATTRIBUTED}"):
            report.unattributed += 1
        else:
            report.resolved += 1
        out.append(rendered)
    return doc.preamble.rstrip("\n") + "\n\n" + "\n".join(out), report


# --- runner --------------------------------------------------------------
#
# Everything above is pure and testable without a repository. Everything below
# touches disk, and only when `--write` says so.


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from code_gantry.config import load_config  # noqa: PLC0415
    from code_gantry.gitops import Git, GitError  # noqa: PLC0415
    from code_gantry.runtime import ProjectPaths  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("slug", help="project slug")
    parser.add_argument(
        "--plan-sha",
        required=True,
        help=(
            "the revision to read plan documents at, for entries that name no "
            "sha of their own. Use the sha the current entries already pin — "
            "grep the log for '@ `' to find it."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the file. Without this, nothing is touched.",
    )
    args = parser.parse_args(argv)

    project = ProjectPaths(args.slug)
    cfg = load_config(project.config)
    if not cfg.plan_addendum_path:
        print("this project declares no plan_addendum_path", file=sys.stderr)
        return 1

    target = Path(cfg.target_repo) / cfg.plan_addendum_path
    git = Git(Path(cfg.target_repo))

    if args.write and not git.is_clean():
        # The file being rewritten is one a run appends to. A dirty tree means
        # either a run is in flight or a human is mid-edit, and both would lose
        # work — so this refuses rather than asks.
        print(
            "refusing to write: the target repo is not clean. Normalise during "
            "a pause, with nothing else in flight.",
            file=sys.stderr,
        )
        return 1

    def read_plan(sha: str, path: str) -> str | None:
        try:
            return git.show_file(sha or args.plan_sha, path)
        except GitError:
            return None

    original = target.read_text()
    rewritten, report = normalise_document(original, read_plan)

    print(f"{target}")
    print(f"  entries          {report.total}")
    print(f"  already current  {report.unchanged}")
    print(f"  bullet renamed   {report.renamed}")
    print(f"  citation derived {report.resolved}")
    print(f"  unattributed     {report.unattributed}")
    print(f"  bytes {len(original):,} -> {len(rewritten):,}")

    if not args.write:
        print("\ndry run; nothing written. Pass --write to apply.")
        return 0

    target.write_text(rewritten)
    print("\nwritten. Commit it on its own, before resuming.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
