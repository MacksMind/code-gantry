"""Plan document resolution and snapshotting.

A project is defined by a plan document, which may link to children.

**The plan root is a document, not a directory.** Pointing it at `docs/` would
sweep runbooks, ADRs, and onboarding notes into every review and planner
prompt — a correctness problem and a cost problem at once.

**Children resolve relative to the root document's directory and may not
escape it.** Without that, a link in a plan document could pull arbitrary files
off disk into a payload pasted verbatim into every paid call.

**The tree is read at the run's base sha, not the working tree**, so a
concurrent edit on `main` cannot change what a run thinks it was asked to do.
And it is snapshotted once per run, so the reviewer judges against the plan as
it stood when the run began while the planner's revisions land in the live
documents and show up as divergence in `status.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from code_gantry.gitops import Git, GitError

# Markdown inline links. Deliberately only markdown: a plan document is prose,
# and anything cleverer becomes a way to pull in files nobody reviewed.
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Only these are followed. A link to a .rb file is a code reference, not a plan
# child, and inlining it would be both wrong and expensive.
_FOLLOWED_SUFFIXES = (".md", ".markdown")

SNAPSHOT_INDEX = "index.txt"


@dataclass
class PlanDocument:
    path: str  # repo-relative, POSIX
    content: str


@dataclass
class PlanTree:
    root: PlanDocument | None = None
    children: list[PlanDocument] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def documents(self) -> list[PlanDocument]:
        return ([self.root] if self.root else []) + list(self.children)

    @property
    def ok(self) -> bool:
        return self.root is not None and not self.problems

    def as_prompt_payload(self, last: str | None = None) -> str:
        """One byte-stable block, roots first, for the cacheable prompt prefix.

        `last` names a document to sink to the end — the progress log, in
        practice. A cached prefix is matched as a prefix, so a document that
        grows invalidates everything concatenated *after* it, and only what
        comes after it. The log is the one plan document that grows, and
        children are ordered by where the root links them: this project links
        the log in its opening paragraph, which put a file gaining ~2KB per
        landed stage ahead of seven static runbooks totalling ~168KB. Every
        one of those was re-billed whenever the log moved.

        Sinking it costs nothing — the documents are labelled by path and the
        planner is told which one records progress, so order carries no meaning
        to the reader. It is purely where the growth is allowed to happen.
        """
        docs = self.documents
        if last:
            docs = [d for d in docs if d.path != last] + [
                d for d in docs if d.path == last
            ]
        blocks = [f"### {d.path}\n\n{d.content.strip()}" for d in docs]
        return "\n\n".join(blocks)


def extract_links(content: str) -> list[str]:
    """Markdown links worth following, in document order, deduplicated."""
    found: list[str] = []
    for target in _LINK.findall(content):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        # Strip an anchor: docs/plan.md#section refers to the same document.
        target = target.split("#", 1)[0]
        if not target:
            continue
        if not target.lower().endswith(_FOLLOWED_SUFFIXES):
            continue
        if target not in found:
            found.append(target)
    return found


def resolve_plan_tree(git: Git, plan_root: str, at_sha: str) -> PlanTree:
    """Resolve the root document and its explicitly linked children.

    One level deep, deliberately. Recursive resolution would make the payload
    size a property of the documents rather than of the config, and a plan that
    links a plan that links a plan is not a bounded prompt.
    """
    tree = PlanTree()
    root_path = PurePosixPath(plan_root)

    try:
        root_content = git.show_file(at_sha, plan_root)
    except GitError as e:
        tree.problems.append(
            f"plan_root {plan_root!r} could not be read at {at_sha[:12]}: {e}. "
            "The plan document must be committed in the target repo — the repo "
            "copy is what CodeGantry operates against and what the "
            "planner revises."
        )
        return tree

    tree.root = PlanDocument(path=plan_root, content=root_content)
    root_dir = root_path.parent

    for link in extract_links(root_content):
        if PurePosixPath(link).is_absolute():
            tree.problems.append(
                f"plan child {link!r} is an absolute path; children must resolve "
                "beside the root document"
            )
            continue

        resolved = _normalise(root_dir / link)
        if resolved is None or not _within(resolved, root_dir):
            tree.problems.append(
                f"plan child {link!r} resolves outside the plan root's directory "
                f"({root_dir}/). A link that escapes would pull arbitrary files "
                "into every review prompt."
            )
            continue

        resolved_str = str(resolved)
        if resolved_str == plan_root:
            continue

        if not git.file_exists_at(at_sha, resolved_str):
            # A plan that links a document it has not written yet is normal
            # during authoring, and not worth failing a run over.
            tree.skipped.append(resolved_str)
            continue

        tree.children.append(
            PlanDocument(path=resolved_str, content=git.show_file(at_sha, resolved_str))
        )

    return tree


def snapshot_tree(tree: PlanTree, dest: Path) -> None:
    """Write the resolved tree to `dest`, flat, with an index.

    Flat rather than mirroring the source layout: the snapshot is a prompt
    payload, not a browsable copy, and a flat directory cannot itself contain a
    traversal.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for existing in dest.iterdir():
        if existing.is_file():
            existing.unlink()

    lines = []
    for document in tree.documents:
        flat = document.path.replace("/", "__")
        (dest / flat).write_text(document.content)
        lines.append(f"{flat}\t{document.path}")
    (dest / SNAPSHOT_INDEX).write_text("\n".join(lines) + "\n" if lines else "")


def load_snapshot(dest: Path) -> PlanTree:
    """Read back a snapshot, preserving original paths and order."""
    tree = PlanTree()
    index = dest / SNAPSHOT_INDEX
    if not index.is_file():
        tree.problems.append(f"no plan snapshot at {dest}")
        return tree

    documents = []
    for line in index.read_text().splitlines():
        if not line.strip():
            continue
        flat, _, original = line.partition("\t")
        body = (dest / flat)
        if not body.is_file():
            tree.problems.append(f"plan snapshot is missing {flat}")
            continue
        documents.append(PlanDocument(path=original or flat, content=body.read_text()))

    if documents:
        tree.root = documents[0]
        tree.children = documents[1:]
    return tree


def _normalise(path: PurePosixPath) -> PurePosixPath | None:
    """Collapse `.` and `..` without touching the filesystem.

    Resolving against the real filesystem would follow symlinks, which is
    exactly the escape this check exists to prevent.
    """
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return PurePosixPath(*parts) if parts else None


def _within(candidate: PurePosixPath, directory: PurePosixPath) -> bool:
    if str(directory) in ("", "."):
        return True
    try:
        candidate.relative_to(directory)
    except ValueError:
        return False
    return True
