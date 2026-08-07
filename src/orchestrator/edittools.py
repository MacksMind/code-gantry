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
from orchestrator.repotools import ToolCall, ToolError, number_lines


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


# Enough of the file to correct from, and no more. An `old_string` is
# typically two to five lines, so this leaves room for the surrounding block
# without turning every refusal into a free file read.
NEAREST_CONTEXT_LINES = 6
NEAREST_MAX_LINES = 40
# Below this, a suggestion is a guess. A confidently wrong location is worse
# than none: it invites the model to edit somewhere it never meant to.
NEAREST_MIN_RATIO = 0.6
# A line has to carry this much to be worth anchoring on. Shorter ones — `end`,
# a brace, a blank — appear everywhere and would place the window at random.
NEAREST_ANCHOR_MIN_CHARS = 12
# Lower, because the location came from text known to have been real rather
# than from the ratio alone.
NEAREST_LOCATED_MIN_RATIO = 0.45


@dataclass(frozen=True)
class Nearest:
    """A window for the model, and which route produced it.

    Always returned, including when there is no window — `text` is empty then
    and `route` says why. That is the point of the type: an outcome with no
    window is still an outcome to count, and "the anchor was ambiguous" and
    "nothing came close" are opposite facts that a `None` collapses into one.

    The route exists because three mechanisms now sit between a bad
    `old_string` and a stalled attempt, and they do different jobs. The
    line-number separator stops refusals happening at all; these windows make a
    refusal recoverable in the turn it occurs. A single refusal rate blends
    them, so the next run could improve and leave us unable to say what
    improved. `_refusal_kind` buckets by matching words in the message and
    every window renders with the same words — so this distinction is invisible
    to it by construction, which is exactly the shape of a mechanism that gets
    reported as *available* forever and never shown to have *fired*.
    """

    text: str
    route: str


def nearest_text(
    text: str,
    old_string: str,
    *,
    locate: Callable[[str], list[str]] | None = None,
) -> Nearest:
    """Where the file most resembles what was asked for, as its real bytes.

    A not-found refusal used to cost a `read_file` call — against a budget that
    twelve of thirty-six attempts exhausted — before the model could try again.
    This folds that read into the refusal that made it necessary.

    Measured on the misses that prompted it, both from a 1,700-line routes
    file. One sent a `scope` block at four-space indent where the file has two
    *and* omitted a line the block actually contains. Another quoted a list
    entry with the wrong indentation and put the closing token where it is not.
    So these are not near-misses to be repaired by normalising whitespace —
    the model is writing the file as it believes it to be. What it needs back
    is the bytes.

    Two fallbacks, in order, and worth naming apart because the words are
    already spoken for: `search` and `semantic_search` are tools the model
    calls, while neither of these is reachable by it.

    **The anchor fallback** works from the model's own text — the first
    non-blank line, stripped, then a `difflib` scan. That line matched exactly
    modulo indentation in both real misses, and an anchor gives a location
    rather than a similarity score.

    **The semantic fallback** runs only when the anchor one has failed, and
    works from text the index holds instead. See below for why that is the
    stronger key at that point.

    **This never repairs the edit.** It returns text for the model to read; the
    edit is still refused and the next `old_string` must match exactly. The
    property that made structured edits worth having — that an applied change
    is one the model actually specified — is the thing not being traded away
    here.
    """
    lines = text.split("\n")
    want = [ln for ln in old_string.split("\n")]
    while want and not want[0].strip():
        want.pop(0)
    while want and not want[-1].strip():
        want.pop()
    if not want or not lines:
        return Nearest("", "none")

    anchor = want[0].strip()
    span = min(len(want) + NEAREST_CONTEXT_LINES, NEAREST_MAX_LINES)

    hits = [i for i, ln in enumerate(lines) if ln.strip() == anchor]
    if len(hits) == 1:
        return Nearest(_window(lines, hits[0], span), "anchor")
    if len(hits) > 1:
        # Several places look like the anchor, so a single window would be a
        # guess about which. Say so and let the model narrow it itself.
        return Nearest("", "ambiguous")

    at, ratio = _best_window(lines, want, 0, len(lines))
    if at is not None and ratio >= NEAREST_MIN_RATIO:
        return Nearest(_window(lines, at, span), "window")

    # The semantic fallback, when an index is configured.
    #
    # It returns text an index believes belongs to this file. That text is
    # stale — the index is rebuilt on a commit hook, so it can be several edits
    # and several commits behind — but it is *known to have been real*, which
    # the `old_string` is not: that has already failed to match, so it is known
    # wrong. Lines of the chunk are therefore the better anchors.
    #
    # Walked a line at a time and stopped at the first that still exists here.
    # An earlier version scored spans instead, which was the wrong shape twice:
    # a span is a chunk boundary rather than the start of what was wanted, and
    # narrowing the same matcher to a region it had already scanned buys only a
    # lower threshold.
    #
    # Nothing indexed reaches the model. The stale text is a search key; every
    # byte returned is cut from the buffer above. That is what makes consulting
    # a lagging index safe at all.
    at = _anchor_from_chunks(lines, locate(old_string) if locate else [])
    if at is not None:
        lo = max(at - len(want), 0)
        hi = min(at + 2 * len(want) + 1, len(lines))
        found, ratio = _best_window(lines, want, lo, hi)
        if found is not None and ratio >= NEAREST_LOCATED_MIN_RATIO:
            return Nearest(_window(lines, found, span), "semantic")
    return Nearest("", "none")


def _anchor_from_chunks(lines: list[str], chunks: list[str]) -> int | None:
    """The first line of any chunk that still exists here, and where.

    The chunks arrive ranked, and a nested walk visits their lines in that
    order — first match wins, no intermediate list. Several chunks commonly
    come back for one file, and scoring each separately would either take the
    best of several guesses or stop at whichever chunk happened to contain a
    survivor. Walking them in rank order leaves the index's own ordering as the
    only preference expressed.

    Compared stripped, because indentation shifts most readily and is not what
    identifies a line. Short lines are skipped: `end`, `}` and their kind occur
    everywhere. A line occurring more than once is skipped for the same reason
    — it names no single place, and a wrong place is worse than none.
    """
    for chunk in chunks:
        for raw in chunk.split("\n"):
            needle = raw.strip()
            if len(needle) < NEAREST_ANCHOR_MIN_CHARS:
                continue
            hits = [i for i, line in enumerate(lines) if line.strip() == needle]
            if len(hits) == 1:
                return hits[0]
    return None


def _best_window(
    lines: list[str], want: list[str], lo: int, hi: int
) -> tuple[int | None, float]:
    """The offset in [lo, hi) whose lines most resemble `want`, and how much.

    Compared with leading whitespace stripped, because indentation is the thing
    most often wrong and it should not dominate the score — the point is to
    find the place, and `_window` then returns the real bytes including the
    indentation that was got wrong.
    """
    import difflib

    width = max(len(want), 1)
    target = "\n".join(ln.strip() for ln in want)
    best, best_ratio = None, 0.0
    for i in range(lo, max(hi - width + 1, lo + 1)):
        window = "\n".join(ln.strip() for ln in lines[i : i + width])
        matcher = difflib.SequenceMatcher(None, target, window)
        if matcher.quick_ratio() <= best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = i, ratio
    return best, best_ratio


def _window(lines: list[str], at: int, span: int) -> str:
    """Numbered exactly as `read_file` numbers, so it reads the same way.

    Which is now enforced rather than promised — the refusal message says these
    bytes are numbered the way `read_file` numbers them, and it says so to a
    model that is about to quote them back.
    """
    start = max(at - 1, 0)
    chosen = lines[start : start + span]
    return number_lines(chosen, start + 1)


def apply_edits(
    text: str,
    edits: list[Edit],
    *,
    locate: Callable[[str], list[str]] | None = None,
) -> str:
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
            near = nearest_text(text, edit.old_string, locate=locate)
            where = (
                f"\n\nThe closest place in the file is:\n\n{near.text}\n\n"
                "Those are its actual bytes, numbered as `read_file` numbers "
                "them. Quote from there."
                if near.text
                else " Read it again and quote the exact bytes, including "
                "indentation."
            )
            # The route rides on the error and not in the message. It is an
            # instrument for us; to the model it would be noise about how we
            # found a window, and the bytes are cut from the live buffer
            # whichever route found them, so it changes nothing it should do.
            raise ToolError(
                f"edit {index}: that text does not appear in the file."
                + where
                + "\n\nNothing has been changed.",
                kind=f"edit not found ({near.route})",
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
    # Given a repo-relative path and the text that was not found, returns
    # candidate line numbers in that file. Optional: the editor works
    # without one and every project that has no index gets today's
    # behaviour unchanged.
    locator: Callable[[str, str], list[int]] | None = None
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

    def record_refusal(
        self, tool: str, detail: str, reason: str, kind: str = ""
    ) -> None:
        """Note a change that was denied.

        Same reason the reader records its refusals: a loop that stopped
        because it was finished and one that stopped because every edit was
        refused produce the same artifact otherwise, and only one of them is a
        working executor.
        """
        self.calls.append(
            ToolCall(
                tool=tool, detail=detail, lines=0, refusal=reason, refusal_kind=kind
            )
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

        locate = (
            (lambda want: self.locator(rel, want)) if self.locator else None
        )
        after = normalise(apply_edits(before, edits, locate=locate))
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
