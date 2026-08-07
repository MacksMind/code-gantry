"""The write side of the repository, with the same posture as the read side.

`repotools.RepoReader` answers questions within bounds, holds no model, and
records what it did whether it answered or refused. This is its counterpart for
changes: it applies an edit or refuses one, and every refusal is a tool result
the executor reads and can act on inside the same turn.

**Why exact match rather than a fuzzy one.** The editor this replaces matched
loosely because it was compensating for a lossy channel, not for a model that
cannot count spaces: a SEARCH/REPLACE block has to be reproduced byte-exactly
inside free-form prose, where a stray fence, a smart quote or a truncated reply
destroys the whole reply. A tool call removes the channel — the string arrives
as provider-escaped JSON against a schema the provider enforces — so what is
left is only whether the model reproduced real bytes. And that is a question it
can now *check*, because it has a read tool and the file is in front of it. The
old editor's model could not check; it emitted a block and learned the answer
from an exit code one reflection later.

That argument is bounded and not yet established for this project. It is why
refusals are counted and reported: if they are common, the answer is not to
reintroduce fuzzy matching but that the refusal text is not actionable enough.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.globs import matches_any
from orchestrator.repotools import ToolCall, ToolError


@dataclass(frozen=True)
class Edit:
    """One replacement within one file."""

    old_string: str
    new_string: str
    replace_all: bool = False


def normalise(text: str) -> str:
    """What every write leaves behind, stated once.

    The editor normalises line endings and the final newline on every file it
    writes. No model chose it and no instruction prevents it, so the machinery
    declares it — in the reviewer's prompt, and by hiding line-ending churn from
    the diff it judges — rather than letting every planner rediscover it by
    burning a rework budget.
    """
    if not text:
        return text
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def apply_edits(text: str, edits: list[Edit]) -> str:
    """Every edit, in order, against one buffer — or none of them.

    Raises rather than returning a partial result. A half-applied batch is the
    worst outcome available: the model then reasons against a file that neither
    it nor the tool has seen, and its next `old_string` is drawn from a version
    that no longer exists.

    Edits apply in sequence, so a later one may legitimately match text an
    earlier one produced. That is a feature and it is why the buffer is threaded
    through rather than each edit being checked against the original.
    """
    for index, edit in enumerate(edits, start=1):
        if not edit.old_string:
            raise ToolError(
                f"edit {index} has an empty old_string. To create a file use "
                "create_file; to remove one use delete_file."
            )

        count = text.count(edit.old_string)

        # Two refusals, kept distinct on purpose — the same distinction the
        # reader makes between "it is not there" and "it is there and you may
        # not have it", and for the same reason: they lead to different next
        # moves. Not found means re-read; found twice means widen the anchor.
        if count == 0:
            raise ToolError(
                f"edit {index}: that text does not appear in the file. Read it "
                "again and quote the exact bytes, including indentation — "
                "nothing has been changed."
            )
        if count > 1 and not edit.replace_all:
            raise ToolError(
                f"edit {index}: that text appears {count} times, so it does not "
                "identify one place. Include more surrounding lines to make it "
                "unique, or set replace_all to change every occurrence — "
                "nothing has been changed."
            )

        text = text.replace(
            edit.old_string, edit.new_string, -1 if edit.replace_all else 1
        )
    return text


@dataclass
class FileEditor:
    """Applies changes within the stage's declared scope, or refuses.

    Scope is enforced here rather than only at the verify gate. The gate stays
    — a check may rewrite a file this never saw, a script stage runs operator
    shell, and a resume can carry a human's work — but a write refused at
    source costs one tool result, where the same write caught at the gate costs
    a whole attempt and routes to the planner.

    `protected` is the plan-document guard, injected rather than imported so
    this module knows nothing about plan trees. Whatever the planner draws from,
    the executor may not edit: a stage able to change its own instructions could
    move the goalposts it is judged against, with a green suite behind it.
    """

    repo: Path
    edit_files: list[str]
    protected: Callable[[str], bool] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    touched: set[str] = field(default_factory=set)

    # --- boundaries -----------------------------------------------------

    def _root(self) -> Path:
        return Path(self.repo).resolve()

    def _resolve_writable(self, path: str) -> tuple[str, Path]:
        """A repo-relative path this stage may write, or `ToolError`.

        Three separate refusals, because they fail for three different reasons
        and a model that conflates them makes the wrong correction.
        """
        raw = (path or "").strip()
        if not raw:
            raise ToolError("no path given")

        root = self._root()
        candidate = (root / raw).resolve()
        # `resolve()` follows symlinks, so a link pointing outside is caught
        # here rather than becoming a write primitive aimed at the host.
        if candidate != root and root not in candidate.parents:
            raise ToolError(
                f"{raw!r} is outside the repository. Only paths within it can "
                "be written."
            )

        rel = str(candidate.relative_to(root))

        if self.protected is not None and self.protected(rel):
            raise ToolError(
                f"{rel!r} is a document this run plans from, so no stage may "
                "edit it. If it is wrong, say so in your reply and stop — "
                "changing it here would edit the instructions this work is "
                "judged against."
            )

        if not matches_any(rel, self.edit_files):
            raise ToolError(
                f"{rel!r} is not in this stage's scope. In scope: "
                f"{', '.join(self.edit_files)}. If the task cannot be done "
                "without it, say so in your reply and stop rather than "
                "editing it."
            )

        return rel, candidate

    def _record(self, tool: str, detail: str, changed: int) -> None:
        self.calls.append(ToolCall(tool=tool, detail=detail, lines=changed))

    def record_refusal(self, tool: str, detail: str, reason: str) -> None:
        """Note a change that was denied.

        Same reason the reader records its refusals: a loop that stopped
        because it was finished and one that stopped because every edit was
        refused produce the same artifact otherwise, and only one of them is a
        working executor.
        """
        self.calls.append(
            ToolCall(tool=tool, detail=detail, lines=0, refusal=reason)
        )

    # --- tools ----------------------------------------------------------

    def edit(self, path: str, edits: list[Edit]) -> str:
        rel, full = self._resolve_writable(path)
        if not full.is_file():
            raise ToolError(
                f"{rel!r} does not exist. Use create_file to write a new file."
            )
        try:
            before = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ToolError(f"{rel!r} could not be read as text: {e}") from e

        after = normalise(apply_edits(before, edits))
        full.write_text(after, encoding="utf-8")
        self.touched.add(rel)
        self._record("edit", rel, len(edits))
        return f"applied {len(edits)} edit(s) to {rel}"

    def create_file(self, path: str, content: str) -> str:
        rel, full = self._resolve_writable(path)
        if full.is_file() and full.read_text(encoding="utf-8", errors="replace").strip():
            raise ToolError(
                f"{rel!r} already exists and is not empty. Use edit to change it."
            )
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(normalise(content), encoding="utf-8")
        self.touched.add(rel)
        self._record("create_file", rel, content.count("\n") + 1)
        return f"created {rel}"

    def delete_file(self, path: str) -> str:
        """Removal is its own tool, not an edit with an empty replacement.

        An `old_string` the model got slightly wrong should refuse, never
        silently empty a file — so emptying is something it has to ask for by
        name.
        """
        rel, full = self._resolve_writable(path)
        if not full.is_file():
            raise ToolError(f"{rel!r} does not exist, so there is nothing to delete.")
        full.unlink()
        self.touched.add(rel)
        self._record("delete_file", rel, 0)
        return f"deleted {rel}"
