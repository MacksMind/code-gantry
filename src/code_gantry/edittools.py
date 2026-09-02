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

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from code_gantry.globs import matches_any
from code_gantry.repotools import ToolCall, ToolError, number_lines


@dataclass(frozen=True)
class Edit:
    """One replacement within one file."""

    old_string: str
    new_string: str
    replace_all: bool = False


def atomic_write(path: Path, text: str) -> None:
    """Put a *new* inode at `path` rather than rewriting the one already there.

    `Path.write_text` is `open(path, "w")`: truncate and rewrite in place, same
    inode, so nothing invalidates a stat cached elsewhere. Docker's file
    sharing keeps such a cache. Measured 2026-08-19 against a bind-mounted
    Rails repository: the editor changed one comment, the file grew 87 bytes,
    and the container went on reporting the *old* size while serving the *new*
    bytes — the host's `head -c 9007` and the container's whole-file digest
    were byte-identical. So every reader inside the container saw the file
    clipped back to its previous length, which cost it the last two `gem`
    declarations. Bundler announced "79 Gemfile dependencies" instead of 81,
    resolved without `redis` and `connection_pool`, and wrote that lockfile
    back to the host. Two runs died of it hours apart, and what armed it was a
    *comment*: the only thing that mattered was that the edit made the file
    longer.

    A sibling temp file plus `os.replace` cannot be answered from a stale
    stat, because the path comes to resolve to an inode that did not exist
    when the cache was filled. The temp file is a sibling so the rename stays
    on one filesystem, where `os.replace` is atomic; and the mode is carried
    over deliberately, because `mkstemp` creates 0600 and the file being
    replaced is usually not — a silent permission change on `bin/` scripts the
    pipeline itself executes is the way this fix would go wrong.
    """
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            # The rename is what fixes the stale stat; the fsync is so the
            # bytes are on disk before the name points at them.
            os.fsync(fh.fileno())
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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


# What a successful write hands back. Enough of the result to see whether the
# change landed where it was meant to, and no more: this is the file *after*
# the write, so it is authoritative in a way nothing else in the conversation
# is, and a model that trusts it instead of re-reading is right to.
ECHO_CONTEXT_LINES = 4
# A cap rather than a budget. It is not charged to the read budget — a
# consequence of a write is not a read, and an edit refused for having no
# reading budget left would be a ceiling nobody chose — so the cap is the only
# thing bounding it, and the loop re-sends every turn.
ECHO_MAX_LINES = 60


def changed_windows(before: str, after: str) -> str:
    """The regions a write changed, as the file's own bytes, numbered.

    **Why a write answers with text at all.** A tool result of "applied 1
    edit(s)" leaves the model holding a *prediction* of what the file now says,
    and this codebase already knows what that costs on the other side of the
    same seam: a check that rewrites a file after the model stops leaves its
    context stale, and the fix there was to hand back the diff and attribute
    it. This is the same fact one step earlier. The model's own edit is the
    first thing to make its picture of the file wrong, and it is the one change
    nobody was telling it about.

    Measured on the attempt that prompted it: an edit replaced the head of a
    multi-line block and left the tail — `).once` followed by the orphaned
    remains of a regex literal — and the model was told "applied 1 edit(s) to
    <path>". It spent 96 further calls discovering what four lines of context
    would have shown it immediately.

    Numbered through `number_lines`, so these bytes are quotable straight back
    into the next `old_string` without a `read_file` in between. That is the
    point: the shortest path from "I changed something" to "I can quote what is
    there now" should not leave the tool.

    Regions are found with `difflib` against the before-text rather than from
    what the caller *asked* for, because the two differ exactly when it
    matters. An edit that matched somewhere unintended reports where it
    actually landed.
    """
    import difflib

    old = before.split("\n")
    new = after.split("\n")
    spans: list[tuple[int, int]] = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
        None, old, new, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        # A deletion has j1 == j2, and a zero-width span would render nothing
        # at all — which is the one outcome that must not look like "no
        # change". Widen it to the seam so the join is visible.
        start = max(0, j1 - ECHO_CONTEXT_LINES)
        end = min(len(new), max(j2, j1 + 1) + ECHO_CONTEXT_LINES)
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))

    if not spans:
        return ""

    total = sum(end - start for start, end in spans)
    if total > ECHO_MAX_LINES:
        return (
            f"The change spans {total} lines, too many to quote back. "
            "`read_file` the range you need before quoting it."
        )

    parts = [number_lines(new[start:end], start + 1) for start, end in spans]
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Hunk:
    """One V4A hunk: where to look, what must be there, what replaces it.

    `scopes` are the `@@` headers above the hunk, outermost first. They are the
    format's answer to the ambiguity that `old_string` can only answer by
    quoting more — and the reason this tool exists beside `edit` rather than
    instead of it.
    """

    scopes: tuple[str, ...]
    before: tuple[str, ...]
    after: tuple[str, ...]


_ENVELOPE_BARE = ("*** begin patch", "*** end patch", "*** end of file")
# A line of nothing but stars. Models truncate the named markers to their
# punctuation — a bare `***` closing a patch was three of ten refusals on the
# first run after the envelope was tolerated, all on one hunk retried verbatim.
# It cannot collide with content: a `***` line inside a hunk carries its space,
# `-` or `+` prefix and never reaches here.
_ENVELOPE_STARS = re.compile(r"^\*{3,}$")
_ENVELOPE_NAMED = (
    "*** update file:",
    "*** add file:",
    "*** create file:",
    "*** delete file:",
    "*** move to:",
)


def _envelope_line(raw: str, path: str) -> bool | None:
    """Is this `***` line the redundant envelope, and does it agree with `path`?

    `None` means "not envelope syntax at all", which is the caller's refusal.
    A named header whose path disagrees with the argument raises here rather
    than returning, because a call whose two halves name different files has
    no safe reading — see `parse_v4a`.
    """
    text = raw.strip()
    low = text.lower()
    if low in _ENVELOPE_BARE or _ENVELOPE_STARS.match(text):
        return True
    for marker in _ENVELOPE_NAMED:
        if low.startswith(marker):
            named = text[len(marker):].strip()
            if path and named and named != path:
                raise ToolError(
                    f"the patch envelope says {named!r} but the `path` "
                    f"argument says {path!r}. Send one file per call and "
                    "let `path` name it."
                )
            return True
    return None


def parse_v4a(diff: str, path: str = "") -> list[Hunk]:
    """A V4A patch body into hunks, or `ToolError`.

    The format is OpenAI's, and the shape is taken from the installed SDK
    rather than from a docs page: `ResponseApplyPatchToolCall.OperationUpdateFile`
    is `{type, path, diff}`, so `diff` is the hunk body alone and the file's
    name arrives beside it.

    **There are two canonical spellings of this format and models emit both.**
    The API form is the structured one above. The CLI and Agents-SDK form is a
    single freeform string fenced by `*** Begin Patch` / `*** End Patch` with
    `*** Update File: <path>` inside it. A model trained on the second closes
    the first with `*** End Patch` — measured at a quarter of all `apply_patch`
    calls on the first luna run, each refusal costing about three more calls
    while the model rebuilt a hunk that was already correct. The envelope is
    redundant here rather than wrong, so it is stripped rather than refused.
    That is tolerance at the payload boundary and nothing else: every hunk
    still has to match byte for byte, which is the tolerance that matters.

    The one envelope line that can carry a disagreement is the operation
    header, because it names a path. If it names a *different* file than the
    `path` argument, the two halves of the call contradict each other and
    guessing which one is meant is exactly the wrong-but-plausible write this
    tool exists to prevent — so that refuses.

    `*** End of File` marks a hunk running to EOF. Nothing here anchors on it:
    matching is exact and must succeed exactly once, so dropping the marker is
    strictly more permissive than refusing the patch that carries it.

    **A line's first character is its whole meaning, and an empty line has
    none.** Models routinely emit a bare `` for a context line that is blank,
    because trailing whitespace is invisible and editors strip it. Refusing
    those would make the format unusable for any file with a blank line in it,
    so an empty line is context. The cost is that a patch cannot express
    "remove a blank line" as its only change without a `-` and a space, which
    it can.
    """
    hunks: list[Hunk] = []
    scopes: list[str] = []
    before: list[str] = []
    after: list[str] = []

    def flush() -> None:
        if before or after:
            hunks.append(
                Hunk(tuple(scopes), tuple(before), tuple(after))
            )
            before.clear()
            after.clear()

    lines = diff.split("\n")
    # The terminator, not a line. A patch almost always ends with a newline,
    # and `split` turns that into a trailing empty element — which the rule
    # below would read as a blank *context* line and require the file to have
    # one in the same place. Every hunk would then fail to match for a reason
    # invisible in the payload. A genuine trailing blank context line arrives
    # as a space, so exactly one empty element goes.
    if lines and lines[-1] == "":
        lines.pop()

    for raw in lines:
        if raw.startswith("@@"):
            # A header after a body starts a new hunk; consecutive headers
            # nest, which is how the format expresses "the method inside this
            # class" without line numbers.
            if before or after:
                flush()
                scopes.clear()
            text = raw[2:].strip()
            if text:
                scopes.append(text)
            continue
        if raw.startswith("***"):
            envelope = _envelope_line(raw, path)
            if envelope is None:
                raise ToolError(
                    f"{raw.strip()!r} is not V4A. A line may begin with a "
                    "space, `-`, `+` or `@@`; the only `***` lines recognised "
                    "are the patch envelope's, and those are ignored."
                )
            continue
        if raw.startswith("-"):
            before.append(raw[1:])
        elif raw.startswith("+"):
            after.append(raw[1:])
        elif raw.startswith(" ") or not raw:
            line = raw[1:] if raw else raw
            before.append(line)
            after.append(line)
        else:
            raise ToolError(
                f"line {raw[:40]!r} starts with {raw[0]!r}. Every line in a "
                "hunk must begin with a space, `-` or `+`, or be a `@@` "
                "header — an unprefixed line cannot be told apart from "
                "context that lost its space."
            )
    flush()

    if not hunks:
        raise ToolError("the patch contains no hunks.")
    for index, hunk in enumerate(hunks, 1):
        if hunk.before == hunk.after:
            # Names the hunk and the mechanism, not only the state. The old
            # text said "a hunk has no `-` or `+` lines" and a model re-sent
            # the identical patch three times, because what it had written
            # was one hunk with a bare `@@` in the middle meaning "skip
            # ahead" — every one of the four no-op refusals on record is that
            # shape. The reference parser refuses it too; the message is
            # where the calls are saved.
            where = (
                f"hunk {index} of {len(hunks)}"
                + (f" (under `@@ {hunk.scopes[-1]}`)" if hunk.scopes else "")
                if len(hunks) > 1
                else "the hunk"
            )
            mechanism = (
                " A `@@` line starts a new hunk; it does not skip lines within "
                "one. Quote the lines between as context, or give each hunk "
                "its own `-` and `+` lines."
                if len(hunks) > 1
                else " Quote what must go with `-` and what replaces it with `+`."
            )
            raise ToolError(
                f"{where} is context only — {len(hunk.before)} line(s) and no "
                f"`-` or `+` — so it asks for no change.{mechanism}"
            )
    return hunks


def _scope_bounds(lines: list[str], scopes: tuple[str, ...]) -> list[tuple[int, int]]:
    """Where each `@@` header could be pointing, narrowing as they nest.

    Returns every surviving candidate rather than picking one. A header that
    matches twice is not an error here: the hunk body may still be unique
    inside exactly one of them, and refusing early would make the model widen
    a scope that was doing its job.
    """
    windows = [(0, len(lines))]
    for scope in scopes:
        nxt: list[tuple[int, int]] = []
        for start, end in windows:
            for i in range(start, end):
                if lines[i].strip() == scope:
                    nxt.append((i + 1, end))
        if not nxt:
            raise ToolError(
                f"no line in the file reads {scope!r}, so the `@@ {scope}` "
                "header names a place that is not there. Read the file and "
                "quote a line that exists, or drop the header."
            )
        windows = nxt
    return windows


def apply_v4a(text: str, hunks: list[Hunk]) -> str:
    """Every hunk, in order, against one buffer — or none of them.

    Same all-or-nothing contract as `apply_edits`, for the same reason, and
    matched **exactly**: every context and `-` line must equal the file's line
    byte for byte. No whitespace tolerance, no fuzz, no nearest-match fallback.

    That is the whole reason this is worth having and it is the thing to
    protect. A context diff's ordinary failure mode is a hunk landing somewhere
    plausible and wrong, which commits, tests, and sometimes passes — the class
    of failure the rest of this system spends its gates on. Refusing loudly
    costs one tool call. The format's advantage over `old_string` is not
    tolerance; it is that **every removed line is named**, so a replacement
    cannot silently strand the tail of the construct it was replacing, which
    is the corruption this was written after.
    """
    for index, hunk in enumerate(hunks, start=1):
        lines = text.split("\n")
        windows = _scope_bounds(lines, hunk.scopes)
        want = list(hunk.before)

        hits: list[int] = []
        for start, end in windows:
            for i in range(start, max(start, end - len(want) + 1)):
                if lines[i : i + len(want)] == want:
                    if i not in hits:
                        hits.append(i)

        if not hits:
            raise ToolError(
                f"hunk {index}: its context and `-` lines do not appear in the "
                "file as written. Every one of them must match byte for byte, "
                "indentation included. `read_file` the range and rebuild the "
                "hunk from what is there.",
                kind="patch not found",
            )
        if len(hits) > 1:
            raise ToolError(
                f"hunk {index}: its lines appear in {len(hits)} places, so it "
                "does not identify one. Add a `@@` header naming the enclosing "
                "definition, or include more context lines — nothing has been "
                "changed.",
                kind="patch ambiguous",
            )

        at = hits[0]
        text = "\n".join(lines[:at] + list(hunk.after) + lines[at + len(want) :])
    return text


@dataclass
class FileEditor:
    """Applies changes within the stage's declared scope, or refuses.

    Scope is enforced here rather than only at the verify gate. The gate stays
    — a check may rewrite a file this never saw, and a resume can carry a
    human's work — but a write refused at
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
    # `(path_glob, reason)` pairs: paths no model may write by hand, whatever
    # the stage's scope says. Scope answers "may this stage touch this file";
    # this answers a question scope cannot, which is that the file is
    # *generated* and belongs to the tool that produces it.
    #
    # The reason is the operator's sentence and is rendered verbatim. What a
    # generated file is and what to run instead are project knowledge — a
    # message written here would ship one project's vocabulary to every
    # other project's executor.
    no_direct_edit: list[tuple[str, str]] = field(default_factory=list)
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

        # Ahead of scope, because a generated file is usually *in* scope and
        # the scope refusal would then name the wrong cause: a model told the
        # path is out of bounds asks the planner to widen a list that is
        # already wide enough, and the planner cannot see why that failed.
        for pattern, reason in self.no_direct_edit:
            if matches_any(rel, [pattern]):
                raise ToolError(
                    f"{rel!r} may not be edited directly: {reason}",
                    kind="no direct edit",
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

    def _applied(self, headline: str, before: str, after: str) -> str:
        """A successful write, with what the file now says at the places it
        changed.

        On both writing tools rather than one, deliberately. They are two ways
        to state the same operation and a model choosing between them should
        not also be choosing between two success contracts — the pair is
        already the thing most likely to be confused, which is why their
        descriptions now point at each other.
        """
        window = changed_windows(before, after)
        if not window:
            return headline
        return f"{headline}. It now reads:\n\n{window}"

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
        atomic_write(full, after)
        self.touched.add(rel)
        self._record("edit", rel, len(edits))
        return self._applied(f"applied {len(edits)} edit(s) to {rel}", before, after)

    def apply_patch(self, path: str, kind: str, diff: str) -> str:
        """The same three operations, stated as a patch instead of a quote.

        Shares `_resolve_writable`, `normalise` and `atomic_write` with `edit`,
        so scope, the plan-document guard, `no_direct_edit` and the stale-stat
        fix are one implementation rather than two. The safety story guards the
        *path*, and the payload's shape does not reach it.

        `kind` mirrors the operation names in the installed SDK
        (`create_file` / `update_file` / `delete_file`) so a model that has seen
        the hosted tool emits what it already knows. It is not the hosted tool:
        that one is `{"type": "apply_patch"}` on the Responses wire only, and
        this executor's model is a routing policy whose wire is not knowable
        until it resolves — 85% of measured turns came back on Messages, where
        the type does not exist.
        """
        if kind == "delete_file":
            return self.delete_file(path)

        rel, full = self._resolve_writable(path)
        hunks = parse_v4a(diff, rel)

        if kind == "create_file":
            if full.is_file() and full.read_text(
                encoding="utf-8", errors="replace"
            ).strip():
                raise ToolError(
                    f"{rel!r} already exists and is not empty. Use "
                    "`update_file` to change it."
                )
            for hunk in hunks:
                if hunk.before:
                    raise ToolError(
                        "a create_file patch may only add lines. Every line "
                        "must begin with `+`; there is nothing yet for a "
                        "context or `-` line to match."
                    )
            before = ""
            after = normalise("\n".join(l for h in hunks for l in h.after))
            full.parent.mkdir(parents=True, exist_ok=True)
        elif kind == "update_file":
            if not full.is_file():
                raise ToolError(
                    f"{rel!r} does not exist. Use `create_file` to write a new "
                    "file."
                )
            try:
                before = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                raise ToolError(f"{rel!r} could not be read as text: {e}") from e
            after = normalise(apply_v4a(before, hunks))
        else:
            raise ToolError(
                f"{kind!r} is not an operation. Use `create_file`, "
                "`update_file` or `delete_file`."
            )

        atomic_write(full, after)
        self.touched.add(rel)
        self._record("apply_patch", rel, len(hunks))
        return self._applied(
            f"applied {len(hunks)} hunk(s) to {rel}", before, after
        )

    def create_file(self, path: str, content: str) -> str:
        rel, full = self._resolve_writable(path)
        if full.is_file() and full.read_text(encoding="utf-8", errors="replace").strip():
            raise ToolError(
                f"{rel!r} already exists and is not empty. Use edit to change it."
            )
        full.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(full, normalise(content))
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
