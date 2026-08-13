"""Read access to the target repository, for the planner.

The planner draws stages for a repository it has never seen. It was given a
directory-level layout and the plan documents, and asked questions only the
files can answer: how many call sites are in this controller, does a spec for
it exist, which specs cover this class. It answered them from plausibility, and
plausibility is wrong often enough to be expensive — a phantom spec path cost a
fifteen-minute stall, a stale call-site count cost a blocked stage and an
intervention, and not knowing which specs covered a controller meant running
the whole suite on every verify.

Reading is not executing. The partition that keeps `command`, `checks` and
`test_command` out of the planner's schema is untouched: this module answers
questions, and every command it runs is authored here, never by a model. A
search pattern arrives as data and reaches git as a single argv element behind
`-e` and `--`, which is the same shape as `test_paths` feeding an
operator-authored test template.

Three boundaries, each with a reason:

**Inside the repository.** Paths are resolved and checked against the real
repository root, so `..`, an absolute path, and a symlink pointing out all fail
the same way.

**Tracked files only.** This is a security boundary rather than housekeeping.
The convention on these projects is that identifiable infrastructure values —
account ids, hosted zone ids, ARNs, real domains — live in environment
variables or gitignored files specifically so they are never committed. Planner
context is sent to a cloud API. Tracked-only means `.env`, `.agent.env` and
`cdk.context.json` are unreadable even when asked for by name.

**Bounded.** Per-call, per-run and call-count ceilings. The lesson of the first
long run was that unbounded context degrades latency *and* accuracy — an
executor handed 69,000 tokens of reference material converted four of six call
sites, then three of six. Handing the planner an unlimited way to fill its own
window would repeat that mistake one layer up.

Exhaustion raises `ToolError`, which the caller turns into a tool result the
model can read. It must be able to finish its answer with what it has; killing
the call would discard the reasoning already done.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.gitops import Git, GitError
from orchestrator.globs import matches_any


class ToolError(Exception):
    """A refusal the planner is allowed to see and work around.

    `kind` is for the record, not for the model: a bucket the raiser already
    knows and the reader would otherwise have to recover by matching words in
    the message. Most refusals leave it empty and are bucketed from their text,
    which is fine where the text and the cause are the same thing. It is set
    where they are not — an edit refused after three different fallbacks tried
    to place it renders the same sentence every time.
    """

    def __init__(self, message: str, *, kind: str = "") -> None:
        super().__init__(message)
        self.kind = kind


SEPARATOR = " | "
"""What divides a line number from the line.

It may not be whitespace, and for most of this project's life it was: two
spaces, against indentation that is also spaces. A line indented by two arrived
as four with nothing marking where ours stopped and the file's began, so a model
quoting it back into an `edit` quoted our padding as code and the edit was
refused for text the file does not contain.

Measured over 117 refused `old_string`s on one run: 74 — 63% — matched the file
exactly once two spaces were stripped from every line. That is not a model
misremembering what it read. The median gap between reading a file and failing
to edit it was zero conversation items; it read what we sent and reproduced it
faithfully, and what we sent was ambiguous.
"""


def number_lines(lines: list[str], first: int = 1) -> str:
    """Lines, numbered, in the one format every part of this system uses.

    Three places rendered this and each held its own copy of the format string —
    here, the nearest-match window in an edit refusal, and the planner's
    excerpts. The refusal's own message tells the model those bytes are
    "numbered as `read_file` numbers them", which was a promise nothing enforced.
    A format that appears in three literals drifts while every test stays green,
    so it appears in one.

    An empty line drops the trailing pad. Nothing else is trimmed: a line whose
    content is whitespace still has that content, and a rendering that lied in
    that direction would be the same class of defect as the one it replaces.
    """
    out = []
    for i, line in enumerate(lines):
        numbered = f"{first + i:>5}{SEPARATOR}{line}"
        out.append(numbered if line else numbered.rstrip())
    return "\n".join(out)


@dataclass
class ReadBudget:
    """Ceilings on what one planning call may pull into context.

    Defaults are deliberately generous enough to answer real questions — read a
    controller, list a spec directory, count call sites — and far too small to
    inhale a repository.
    """

    max_lines_per_call: int = 400
    max_total_lines: int = 3000
    max_calls: int = 25
    # And a ceiling in characters, because a line is not a unit of size.
    #
    # Every limit above assumes a line is roughly a line's worth of text. That
    # holds for source and fails completely for a minified bundle, a vendored
    # asset, a `structure.sql`, or a fixture with one enormous row — and a
    # repository of any age has some. Measured on this project's target: a
    # derivation whose searches returned 106 and 160 "lines" was followed by a
    # call the provider rejected at `1103000 tokens > 1000000 maximum`, with a
    # recorded initial prompt of 611,008 characters. Essentially the whole
    # million arrived through results that every line-based ceiling called
    # small.
    #
    # 12,000 is about 3,000 tokens — comfortably more than 400 ordinary source
    # lines, so nothing that was already reasonable changes, and three orders
    # of magnitude below what one minified line can carry.
    max_chars_per_call: int = 12_000
    # And the same dimension applied to the step, which the ceiling above did
    # not cover. Bounding each call at 12,000 characters says nothing about a
    # hundred of them, and `max_total_lines` is the proxy the char ceiling was
    # added to replace — so the total was still counted in the unit that had
    # already been shown not to measure anything.
    #
    # It bites on the cheap tools. `search` returns few lines per call and each
    # of those lines can be a whole minified file, so a sweep spends almost no
    # line budget while spending arbitrary context. Under this project's target
    # config the executor may make 200 calls against a 20,000-line total: a
    # search-heavy attempt exhausts neither and can still pull megabytes.
    #
    # The default is the line budget at eighty characters a line, which is the
    # argument `max_chars_per_call` was chosen by — nothing already reasonable
    # changes, and only the pathological case is caught.
    max_total_chars: int = 3000 * 80


def _read_detail(
    rel: str,
    chosen: list[str],
    first: int,
    start: int | None,
    end: int | None,
    clipped: bool,
) -> str:
    """How one read is named in the ledger: the path, and which lines of it.

    The path alone cannot answer whether two reads of a file saw the same
    bytes, and that question is load-bearing. Measured over 97 planner
    decisions: 188 paths were read in more than one decision and 73 of those
    were never modified by the run, ~11.8% of all read output returning
    content the model had already been shown. Whether a run-fixed excerpt
    would remove that depends on whether those were the same lines or
    different windows, and the ledger could not say.

    The range recorded is the one **served**. A request for 1-999 against a
    40-line file saw 1-40, and one cut short by the read budget saw less than
    it asked for — what a later reading needs is which bytes reached the
    model, which is the same choice `resolve_excerpts` makes when it labels a
    clipped excerpt with what arrived rather than what was wanted.

    A whole file keeps the bare path. It is the common case and already
    unambiguous, and appending `:1-40` to every one of them would churn the
    ledger to say nothing.
    """
    if not chosen:
        # `lines: 0` already carries this, and there is no range to name.
        return rel
    if start is None and end is None and not clipped:
        return rel
    return f"{rel}:{first}-{first + len(chosen) - 1}"


@dataclass
class ToolCall:
    """One question put to the repository, answered or not.

    `refusal` is empty for an answered call and carries the reason for a
    denied one. It exists because the ledger used to be written only by
    `_spend`, which runs after a tool succeeds: a call that was refused —
    for an exhausted budget, a path that is not there, a pattern that is
    not a pattern — left no trace at all. Measured over one run of 65
    planning steps, 24 stopped at exactly the 25-call ceiling and not one
    refusal was recorded, so a step that stopped because it had finished
    and a step that stopped because it had been cut off produced the same
    artifact. A cap whose binding cannot be observed cannot be tuned.

    `lines: 0` is deliberately not the marker. It already means "the search
    ran and matched nothing", which is a different fact that the planner
    acts on differently.
    """

    tool: str
    detail: str
    lines: int
    refusal: str = ""
    # The raiser's own bucket, when the message alone does not carry it.
    # Empty means "derive it from the text", which is what every refusal did
    # before an edit could be refused by three different routes to the same
    # sentence.
    refusal_kind: str = ""
    # What the call returned, kept for semantic search alone.
    #
    # Every other read here is reproducible: the ledger records the path
    # and range, the sha is known, and the same bytes can be fetched back.
    # Copying them would be storing a second copy of the repository.
    #
    # A semantic hit cannot be reconstructed. It depends on an index built
    # from whatever commits had been ingested, on a similarity cutoff and
    # on an embedding model, so the same question later returns different
    # chunks. For the one tool whose output is unrecoverable the record
    # kept the least: a question and a line count. A verdict resting on
    # eighteen lines nobody can retrieve is what tool access was granted
    # to prevent.
    result: str = ""


@dataclass
class Spend:
    """What one step has already used up.

    Everything mutable about a read budget, in one object, so that clearing it
    is replacing it. The counters used to be three fields on `RepoReader` and
    the clearing was three statements at a call site — which went stale the
    moment `max_total_chars` was added underneath `max_total_lines`, because
    nothing taught the clearing about the new one. `_chars_used` then
    accumulated for the life of the process and every planner call past the
    ceiling was refused on its first read: 14 of 31 calls on one measured run,
    each drawing a stage with no way to check a premise against the code.

    A tidier `reset()` would have fixed that instance and kept the shape. This
    changes the shape: whatever fields `Spend` grows, a fresh one starts empty,
    and there is no list for anyone to maintain.
    """

    calls: list[ToolCall] = field(default_factory=list)
    lines: int = 0
    chars: int = 0


@dataclass
class RepoReader:
    """Answers questions about a repository, within bounds.

    Holds no model and makes no decisions. The planner asks; this replies or
    refuses, and records what it did either way.
    """

    git: Git
    repo: Path
    budget: ReadBudget = field(default_factory=ReadBudget)
    spend: Spend = field(default_factory=Spend)
    # Pin every read to one commit instead of the working tree.
    #
    # Empty is right for a live run: the reviewer is called with the stage's
    # work committed on the stage branch, so the tree already *is* the state
    # being judged, and reading it needs no git at all.
    #
    # It is wrong for anything that reads after the fact. Replaying a review
    # against a tree that has moved on by thirty stages would let it approve a
    # deletion because a permit list landed later — the right answer for the
    # wrong reason, and indistinguishable from judgement.
    at_sha: str = ""
    # Globs a search must never read, from operator config. Which paths are
    # vendored, minified or generated is project knowledge, so there is no
    # default — one here would ship this repository's layout to every project.
    #
    # Search only. `read_file` is the planner naming a path deliberately, and an
    # exclusion is about what a sweep pulls in by accident; conflating them
    # would make a file the operator can see unreadable to the pipeline.
    search_exclude_globs: list[str] = field(default_factory=list)

    # --- boundaries -----------------------------------------------------

    def _root(self) -> Path:
        return Path(self.repo).resolve()

    def _resolve(self, path: str) -> Path:
        """A repo-relative path, or `ToolError`.

        `resolve()` follows symlinks, so a tracked link pointing outside is
        caught here rather than becoming an exfiltration route.
        """
        raw = (path or "").strip()
        if not raw:
            raise ToolError("no path given")
        root = self._root()
        candidate = (root / raw).resolve()
        if candidate != root and root not in candidate.parents:
            raise ToolError(
                f"{raw!r} is outside the repository; only paths within it can be read"
            )
        return candidate

    def _tracked(self) -> set[str]:
        try:
            if self.at_sha:
                return set(self.git.tracked_paths(self.at_sha))
            out = self.git._out("ls-files")
        except GitError as e:  # pragma: no cover - a broken repo fails louder elsewhere
            raise ToolError(f"cannot list tracked files: {e}") from e
        return {line for line in out.splitlines() if line.strip()}

    def _require_readable(self, rel: str, resolved: Path) -> None:
        """Two different refusals, kept distinct on purpose.

        "It is not there" and "it is there and you may not have it" lead the
        planner to different next moves: correct the path, or stop asking. The
        first is the answer that would have prevented an invented spec path
        turning into a fifteen-minute stall; collapsing them into one message
        would leave it guessing which it had hit.
        """
        tracked = self._tracked()

        # Pinned to a commit, the tree is the authority and the working copy is
        # irrelevant: a file may be absent from disk because a later stage
        # deleted it, and present at this sha. Testing disk would refuse a file
        # that demonstrably existed, and admit one that did not.
        if self.at_sha:
            if rel not in tracked:
                raise ToolError(
                    f"{rel!r} is not in the tree at {self.at_sha[:12]}. It may "
                    "have been added later, or never existed. Do not assume a "
                    "path from a naming convention — list the directory instead."
                )
            return

        if not resolved.exists():
            raise ToolError(
                f"{rel!r} does not exist in this repository. Do not assume a "
                "path from a naming convention — list the directory instead."
            )
        if rel not in tracked:
            # One exception, and git draws it rather than we do: a file
            # that is untracked but *not ignored* is part of the working
            # project and simply not committed yet. The executor's own new
            # spec is one; so is a file a human left in the tree before
            # resuming, which the agent must be able to see or the fix that
            # prompted the resume is invisible to it.
            #
            # The reason tracked-only exists survives untouched, because it
            # was always really about `.gitignore`: `.env`, `.agent.env` and
            # `cdk.context.json` are ignored precisely so they are never
            # committed, and ignored is exactly what this does not admit.
            #
            # An earlier version scoped this to the stage's `edit_files`,
            # reasoning that an untracked file in scope must be the executor's
            # own work. That is only true for a fresh stage on a normal run —
            # precheck's clean-tree guard exempts both resumes and revisions —
            # and it answered the wrong question anyway.
            if rel in self._untracked_but_not_ignored():
                return
            raise ToolError(
                f"{rel!r} exists but is not tracked by git, so it cannot be read. "
                "Untracked and ignored files hold credentials and generated "
                "output, and this context is sent to a third-party API."
            )

    def _untracked_but_not_ignored(self) -> set[str]:
        """Files git considers part of the project but does not yet track."""
        try:
            out = self.git._out("ls-files", "--others", "--exclude-standard")
        except GitError:  # pragma: no cover - a broken repo fails louder elsewhere
            return set()
        return {line for line in out.splitlines() if line.strip()}

    def _relative(self, resolved: Path) -> str:
        return str(resolved.relative_to(self._root()))

    # --- budget ---------------------------------------------------------

    @property
    def calls(self) -> list[ToolCall]:
        """The ledger, through the container.

        A property rather than a rename, because `calls` is read by the
        planner, the reviewer, the executor's counters and every test that
        asserts what a role looked at. What matters is that it resolves to
        whichever `Spend` is current — a caller holding the list itself would
        keep appending to the previous step's after a replacement, and nothing
        would say so.
        """
        return self.spend.calls

    def _spend(self, tool: str, detail: str, text: str) -> str:
        used = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        self.spend.lines += used
        # Charged where the lines are, and only on an answered call — a refusal
        # costs nothing here for the same reason it costs no call: recording a
        # denial must not make the next one more likely.
        self.spend.chars += len(text)
        self.calls.append(ToolCall(tool=tool, detail=detail, lines=used))
        return text

    def record_answer(self, tool: str, detail: str, text: str) -> str:
        """Charge text this reader did not produce to the same budget.

        An operator-declared tool answers with bytes that land in the same
        context window as a `read_file`, and the ceilings are there to bound
        that window — `max_total_chars` exists because a line is not a unit of
        size, and a container's output is no more measured in lines than a
        minified bundle is. Left uncharged, the one channel that can return a
        whole vendored directory would be the only one that is free, and the
        `(120k/800k chars)` line an operator reads would stop describing what
        was actually spent.

        It also puts the call in the ledger, which is what makes it visible in
        `tools.log` and in the per-step counts. A tool whose use is invisible
        cannot be judged worth its cost.
        """
        return self._spend(tool, detail, text)

    def record_refusal(
        self, tool: str, detail: str, reason: str, kind: str = ""
    ) -> None:
        """Note a call that was denied.

        Called by the dispatcher rather than at the raise sites: `_resolve`
        and `_require_readable` know neither the tool's name nor what was
        asked for, and the detail of a refused call is the question, because
        there is no content to describe it by.
        """
        self.calls.append(
            ToolCall(
                tool=tool, detail=detail, lines=0, refusal=reason, refusal_kind=kind
            )
        )

    def reset(self) -> None:
        """Forget everything spent, for the next step.

        A method rather than three assignments at each call site, because a
        reset that has to be *remembered* field by field is what failed here
        twice. `plan()` cleared `calls` and `_lines_used` and was written
        before `max_total_chars` existed; when that ceiling was added
        underneath the line ceiling, nothing taught the reset about it, so
        `_chars_used` accumulated for the life of the process and every planner
        call past the ceiling was refused on its first read — 14 of 31 calls on
        one measured run, permanently blind from the moment it crossed.

        And `review()` had no reset at all, so one run's reviews shared a
        budget: `review.json` recorded 685 calls for a review that made a
        handful, and late reviews were starved by reads their predecessors had
        done.

        The list is cleared in place. `SemanticSearch` is constructed with
        `calls=reader.calls` so the two share one list and the log stays
        chronological; rebinding the attribute would hand them separate lists
        and nothing would say so.
        """
        self.spend = Spend()

    def _answered(self) -> int:
        """Calls the budget is actually spent on.

        Recording a denial must not make the next one more likely, so the
        ceiling counts answered calls and refusals ride along free.
        `_max_tool_turns` is what bounds a model that ignores a refusal and
        keeps asking.
        """
        return sum(1 for c in self.calls if not c.refusal)

    def _charge_call(self, tool: str) -> None:
        if self._answered() >= self.budget.max_calls:
            raise ToolError(
                f"too many tool calls in one step "
                f"(limit {self.budget.max_calls}). Work with what you have."
            )
        if self.spend.lines >= self.budget.max_total_lines:
            raise ToolError(
                f"read budget spent for this step "
                f"({self.budget.max_total_lines} lines). Work with what you have."
            )
        if self.spend.chars >= getattr(self.budget, "max_total_chars", 0) > 0:
            raise ToolError(
                f"read budget spent for this step "
                f"({self.budget.max_total_chars} characters). Work with what "
                "you have."
            )

    def _clip(self, lines: list[str]) -> tuple[list[str], bool]:
        cap = self.budget.max_lines_per_call
        remaining = max(self.budget.max_total_lines - self.spend.lines, 0)
        allowed = min(cap, remaining)
        clipped = False
        if len(lines) > allowed:
            lines, clipped = lines[:allowed], True
        return self._clip_chars(lines, clipped)

    def _clip_chars(self, lines: list[str], clipped: bool) -> tuple[list[str], bool]:
        """The same ceiling in characters — see `ReadBudget.max_chars_per_call`.

        Applied after the line cap rather than instead of it, because the two
        answer different questions and a caller wants whichever binds first.
        A line longer than the whole budget is truncated rather than dropped:
        the first characters of a minified bundle still identify it, and
        returning nothing for a file that plainly matched reads as "not there",
        which is the failure the search work was about.
        """
        budget = getattr(self.budget, "max_chars_per_call", 0)
        if budget <= 0:
            return lines, clipped
        out: list[str] = []
        spent = 0
        for line in lines:
            if spent + len(line) > budget:
                room = budget - spent
                if room > 0:
                    out.append(line[:room] + " … line truncated")
                return out, True
            out.append(line)
            spent += len(line) + 1
        return out, clipped

    # --- tools ----------------------------------------------------------

    def read_file(self, path: str, start: int | None = None, end: int | None = None) -> str:
        """A tracked file, or a numbered slice of one.

        Numbered because the planner cites what it read. A `forbidden_patterns`
        entry or a `test_paths` claim traced to a line is checkable; one traced
        to a recollection is not.
        """
        self._charge_call("read_file")
        resolved = self._resolve(path)
        rel = self._relative(resolved)
        self._require_readable(rel, resolved)

        if self.at_sha:
            try:
                body = self.git.show_file(self.at_sha, rel).splitlines()
            except GitError as e:
                raise ToolError(str(e)) from e
        else:
            body = resolved.read_text(errors="replace").splitlines()
        first = max(start or 1, 1)
        last = min(end or len(body), len(body))
        chosen = body[first - 1 : last] if first <= last else []
        chosen, clipped = self._clip(chosen)

        numbered = number_lines(chosen, first)
        if clipped:
            numbered += f"\n... truncated at {len(chosen)} lines; ask for a narrower range"
        return self._spend("read_file", _read_detail(rel, chosen, first, start, end, clipped), numbered)

    def list_files(self, glob: str | None = None) -> list[str]:
        """Tracked paths, optionally filtered.

        An empty list is an answer, not a failure: "does a spec for this exist"
        is a question the planner needs to be able to hear *no* to.
        """
        self._charge_call("list_files")
        paths = sorted(self._tracked())
        if glob:
            pattern = glob.strip()
            paths = [
                p
                for p in paths
                if fnmatch.fnmatch(p, pattern)
                # `spec/**/*.rb` should match `spec/a_spec.rb` too, which
                # fnmatch's single-star semantics would otherwise miss.
                or fnmatch.fnmatch(p, pattern.replace("/**/", "/"))
            ]
        self._spend("list_files", glob or "(all)", "\n".join(paths))
        return paths

    def _search_globs(self, path_glob: str | None) -> list[str]:
        """The `-g` filters for one `path_glob`, in ripgrep's dialect.

        Two normalisations, both measured against what models actually wrote
        rather than what the field description asks for.

        `|` separates alternatives. The `pattern` argument beside this one is
        alternated that way, so the path gets alternated the same way — seven
        times in one run's log. git read the pipe as a literal character in a
        single pathspec and matched nothing.

        A bare directory name means everything under it. It was 91 of the 332
        globs in that log, and as a ripgrep glob it matches the directory entry
        and none of its contents — which would have swapped one silent empty
        answer for another while looking like a fix.
        """
        raw = (path_glob or "").strip()
        if not raw or raw == ".":
            return []
        globs = []
        for part in raw.split("|"):
            part = part.strip().strip("/")
            if not part:
                continue
            # `..` never reaches the filesystem here. It cannot escape the
            # search either — a glob is a filter over a walk rooted at the
            # repository, not a path to walk.
            if (
                ".." not in part
                and not any(c in part for c in "*?[")
                and (self._root() / part).is_dir()
            ):
                part = f"{part}/**"
            globs.append(part)
        return globs

    def _drop_ignored(self, lines: list[str]) -> list[str]:
        """The same lines, minus any whose file the repository ignores.

        ripgrep's `-g` is a filter over the walk rather than within it, so a
        glob **overrides** the ignore rules: `rg --hidden -g '**/*'` on the
        target repository returned 41 hits out of the executor's own
        conversation transcript, 18 out of `tools.log`, and the planner's
        `planner.json` for the stage being executed — none of which the same
        search without a glob could see. So the tracked-only boundary this
        tool documents held for exactly the calls that did not ask for a path.

        Asked of the matched paths rather than of the walk, which is one
        subprocess on an answer that is already small instead of a second pass
        over the tree. `git check-ignore` is asked without `--no-index`, so a
        *tracked* file matching an ignore rule is not reported and survives.
        That is git's boundary rather than ripgrep's, and it is the better of
        the two here: ripgrep knows nothing of the index, so it hides a tracked
        file the stage is allowed to edit, and a glob then makes it visible
        again. The rule this settles on is the one the caller can act on —
        what the repository ignores is out, what it tracks is in.

        Failing open here would restore the leak silently, so a
        `check-ignore` that cannot answer is an error the caller sees.
        """
        paths = []
        for line in lines:
            body = line[2:] if line.startswith("./") else line
            paths.append(body.split(":", 1)[0])
        wanted = sorted({p for p in paths if p})
        if not wanted:
            return list(lines)
        proc = subprocess.run(
            ["git", "check-ignore", "-z", "--stdin"],
            cwd=str(self._root()),
            input="\0".join(wanted),
            capture_output=True,
            text=True,
            errors="replace",
        )
        # 0 is "some are ignored", 1 is "none are" — both are answers, and 1 is
        # the common one. Anything else is git declining to say, and a filter
        # that guards a boundary must not treat silence as permission.
        if proc.returncode not in (0, 1):
            raise ToolError(
                "search could not check the repository's ignore rules: "
                f"{proc.stderr.strip() or 'git check-ignore failed'}"
            )
        ignored = {p for p in proc.stdout.split("\0") if p}
        if not ignored:
            return list(lines)
        return [line for line, path in zip(lines, paths) if path not in ignored]

    def search(self, pattern: str, path_glob: str | None = None) -> list[str]:
        """Matching lines, as `path:line:text`.

        `rg` rather than `git grep`, and the reason is the glob rather than the
        speed. A pathspec is not a glob: git's default matching lets `*` cross
        `/` and requires `**/` to consume a directory component, so
        `app/controllers/**/*` matches only what is two levels down and never
        sees the controller sitting directly in `app/controllers`. Measured
        over one run's tool log: of 337 searches, 76 came back empty, and **27
        of them had matches** the model was never shown — 8% of every search it
        made, and 36% of every empty answer it was given. The cost is not the
        wasted call. An empty result is evidence, and these were false
        evidence: the executor that hit a run of them went off to
        `semantic_search` and asked the same question ten times.

        ripgrep's globset is what the models are already writing for, `-g`
        repeats so alternatives need no encoding, and `--engine auto` retries
        under PCRE2 rather than failing on the lookaround a model reaches for.

        The tracked-only boundary is now ripgrep's ignore handling rather than
        git's index, and it lands in the same place for the case that matters:
        this operator keeps identifiable infrastructure values in gitignored
        files, and ripgrep skips those by default. It is in fact the old
        `--untracked` behaviour exactly — ignored files out, untracked-but-not
        -ignored in, which is what a spec this attempt just wrote needs.

        `--hidden` because ripgrep skips dotfiles and git grep does not. Left
        off, the swap would have silently removed every `.rubocop.yml`,
        `.ruby-version` and CI config from view — a new blind spot in the same
        change that closed one. It does not reopen the boundary: those files
        are hidden, not ignored.

        `.git` has to be excluded by hand, and finding that out is the whole
        argument for testing the layer you call. A scratch repository said
        `--hidden` was safe; it only said so because no hook sample happened to
        match the probe pattern. The suite searched for `version` and got seven
        hits out of `.git/hooks/fsmonitor-watchman.sample`. Nothing about the
        flag's description suggests it, and no amount of reading would have
        produced it.

        The pattern is still data, passed after `-e`, so a leading dash is a
        pattern rather than a flag. There is no shell here.
        """
        self._charge_call("search")
        if not (pattern or "").strip():
            raise ToolError("no search pattern given")
        if self.at_sha:
            # ripgrep walks a filesystem and cannot read a commit. Nothing in
            # `src/` has ever pinned a search — the capability existed for the
            # tests alone — and answering from the working tree would make a
            # pinned reader report unpinned results, which is worse than
            # refusing.
            raise ToolError(
                "search cannot be pinned to a commit; this reader is pinned at "
                f"{self.at_sha[:12]}. read_file and list_files are pinned and "
                "answer from that tree."
            )

        argv = [
            "rg", "--line-number", "--no-heading", "--with-filename",
            "--color", "never", "--hidden", "--engine", "auto",
        ]
        globs = self._search_globs(path_glob)
        for glob in globs:
            argv += ["-g", glob]
        # Last, and that is the whole of it: ripgrep resolves overlapping
        # globs in order and the last match wins, so `-g '!.git'` placed
        # before a model's `-g '**/*'` is silently overridden and the walk
        # goes back into `.git`. Measured on a fixture: eight hits out of
        # `.git/logs` and `.git/COMMIT_EDITMSG` — reflog lines and commit
        # messages returned as if they were source. The exclusion has to
        # outrank anything the model can write, which means going after it.
        # Exclusions last, all of them, for the reason `!.git` is last: ripgrep
        # resolves overlapping globs in order and the last match wins, so an
        # operator's exclusion placed before a model's `-g '**/*'` is silently
        # overridden.
        argv += ["-g", "!.git"]
        for excluded in self.search_exclude_globs:
            cleaned = (excluded or "").strip()
            if cleaned:
                argv += ["-g", f"!{cleaned.lstrip('!')}"]
        # The path is not decoration. ripgrep given no path and a stdin that is
        # not a terminal searches **stdin**, so this tool worked at a shell and
        # returned nothing from `subprocess.run` with an inherited pipe — every
        # search silently empty, which reads as "not in this repository". That is
        # the pathspec bug's failure arriving by a different route, and it would
        # have struck whichever way a run happened to be launched: cron, CI, or a
        # shell with stdin redirected. Given a path, ripgrep never consults
        # stdin.
        argv += ["-e", pattern, "."]

        proc = subprocess.run(
            argv,
            cwd=str(self._root()),
            capture_output=True,
            text=True,
            # Belt and braces with the path above. It costs nothing and removes
            # any dependence on how ripgrep classifies the stream.
            stdin=subprocess.DEVNULL,
            # A repository is not obliged to be UTF-8, and `Git._run` carries
            # this same guard for the same reason: one Windows-1252 quote in
            # one tracked file used to crash the process mid-search.
            errors="replace",
        )
        # 1 is "no matches", which is an answer.
        if proc.returncode not in (0, 1):
            # ripgrep folds "your glob selected nothing" into the same exit
            # code as a bad pattern, and the two want opposite next moves —
            # widen the path, or fix the regex. Left merged, a model reads
            # `search failed` and re-runs the same glob with a different
            # pattern. It is the same distinction `read_file` already draws
            # between a path that is absent and one that is forbidden.
            raise ToolError(
                f"search failed: {proc.stderr.strip() or 'invalid pattern'}"
            )
        # "Your glob selected nothing" and "the pattern is not there" want
        # opposite fixes — widen the path, or fix the regex — so they are
        # separate answers.
        #
        # Derived rather than read off an exit code. ripgrep folds the two into
        # status 2 only when it is given *no* path; with `.` supplied it searches
        # normally and exits 1 either way, which made the check that read stderr
        # unreachable the moment the stdin bug was fixed. Asking which files the
        # globs select is one extra call at 0.02s on a 4,423-file repository, and
        # it is true regardless of how ripgrep classifies the run.
        # Searching `.` prefixes every hit with `./`. Stripped here so the
        # `path:line:text` contract every caller parses is unchanged.
        hits = [
            line[2:] if line.startswith("./") else line
            for line in proc.stdout.splitlines()
            if line.strip()
        ]
        hits = self._drop_ignored(hits)

        # After the filter, not before it. A glob selecting only ignored files
        # produces hits from ripgrep and none from this tool, which is exactly
        # the case the message below is for — and the shape most likely to be
        # believed, since an empty answer reads as a fact about the repository.
        if not hits and globs:
            listing = subprocess.run(
                ["rg", "--files", "--hidden", *sum((["-g", g] for g in globs), []),
                 "-g", "!.git", "."],
                cwd=str(self._root()), capture_output=True, text=True,
                errors="replace", stdin=subprocess.DEVNULL,
            )
            # The listing is subject to the same override, so it is filtered
            # too. Untouched, a glob aimed straight at an ignored directory
            # would list its files, decline to raise, and return nothing.
            if not self._drop_ignored(
                [line for line in listing.stdout.splitlines() if line.strip()]
            ):
                raise ToolError(
                    f"no files matched the path {path_glob!r}; the pattern was "
                    "never tried. Check the path with list_files, or drop it to "
                    "search the whole repository."
                )

        hits, clipped = self._clip(hits)
        if clipped:
            hits = hits + ["... truncated; narrow the pattern or the path"]
        self._spend("search", f"{pattern} in {path_glob or '.'}", "\n".join(hits))
        return hits

    def git_show(self, ref: str, path: str | None = None) -> str:
        """A file as it stood at a ref, or a commit's message and stat.

        The pathless form is how a merge sha becomes evidence rather than a
        number: `stage-costs.md` keys every line by one, and nothing could
        dereference it until now. It answers with `--stat`, which is what the
        question behind it needs — how large was that stage, and what did it
        touch. Confinement does not apply: there is no path to escape with.
        """
        self._charge_call("git_show")
        rel = None
        if path is not None:
            rel = self._relative(self._resolve(path))
        try:
            body = self.git.show_file(ref, rel)
        except GitError as e:
            raise ToolError(str(e)) from e
        lines, clipped = self._clip(body.splitlines())
        text = "\n".join(lines) + ("\n... truncated" if clipped else "")
        # The ledger reads back as the argv that produced it, so a pathless
        # call must not render as `sha:None` — the log is what a limit is
        # tuned from, and an entry nobody can reproduce is not evidence.
        return self._spend(
            "git_show", f"{ref}:{rel}" if rel is not None else ref, text
        )

    def git_diff(
        self, ref: str, other: str | None = None, path: str | None = None
    ) -> str:
        """What changed between two points, optionally for one path."""
        self._charge_call("git_diff")
        args = ["diff", ref]
        if other:
            args.append(other)
        if path:
            resolved = self._resolve(path)
            args += ["--", self._relative(resolved)]
        proc = self.git._run(*args, check=False)
        if proc.returncode not in (0, 1):
            raise ToolError(f"diff failed: {proc.stderr.strip()}")
        lines, clipped = self._clip(proc.stdout.splitlines())
        text = "\n".join(lines) + ("\n... truncated" if clipped else "")
        return self._spend("git_diff", " ".join(args[1:]), text)


def count_calls(*ledgers) -> dict[str, int]:
    """One ledger's calls, counted by tool. Several ledgers, merged.

    Every agentic role reports what it looked at, and every one of them was
    counting its own way: the executor merged a reader and an editor and also
    bucketed refusals, the reviewer counted a single reader, and the planner
    reported a bare total with no breakdown at all. Three shapes of the same
    arithmetic, which is how `number_lines` drifted into three copies of one
    format string while every test stayed green.

    Variadic because the executor genuinely has two ledgers — reads and edits
    are recorded by different objects — and an operator reading a log wants one
    answer rather than two lines to add up.

    A refused call still counts. It is a thing the role asked for, and a role
    that spent its budget being refused must not read like one that asked for
    nothing.
    """
    counts: dict[str, int] = {}
    for ledger in ledgers:
        for call in getattr(ledger, "calls", ledger) or []:
            counts[call.tool] = counts.get(call.tool, 0) + 1
    return counts


def count_refusals(*ledgers) -> dict[str, int]:
    """The same calls, bucketed by why they were denied.

    `refusal_kind` where the raiser set one, and only then the message. An edit
    can be refused by three separate routes to one sentence, and no reading of
    that sentence recovers which fired — a classifier over rendered text cannot
    separate classes the text renders identically.
    """
    from orchestrator.executor import _refusal_kind

    counts: dict[str, int] = {}
    for ledger in ledgers:
        for call in getattr(ledger, "calls", ledger) or []:
            if not getattr(call, "refusal", ""):
                continue
            kind = getattr(call, "refusal_kind", "") or _refusal_kind(call.refusal)
            counts[kind] = counts.get(kind, 0) + 1
    return counts


# The tools whose answers cannot be fetched back from the repository.
SEMANTIC_TOOLS = frozenset({"semantic_search", "locate"})


def semantic_results(*ledgers) -> list[dict]:
    """The questions asked of the index, and what came back.

    Filtered by tool rather than by "has a result". The policy is that only
    the semantic tools fill `result` — see `ToolCall.result` — and this is what
    makes that a property instead of a convention: a later caller that sets it
    on a `read_file` does not thereby copy a repository file into an artifact.
    """
    out = []
    for ledger in ledgers:
        for call in getattr(ledger, "calls", ledger) or []:
            if call.tool in SEMANTIC_TOOLS and call.result:
                out.append({"question": call.detail, "returned": call.result})
    return out


def render_counts(counts: dict[str, int]) -> str:
    """`12 read_file, 6 search, 1 semantic_search` — busiest first.

    The one renderer, for the same reason `_render_call` is shared across the
    three roles rather than restated: two renderings of one ledger is how they
    drift, and these had.
    """
    return ", ".join(
        f"{n} {name}" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
