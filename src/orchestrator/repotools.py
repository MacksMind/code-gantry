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
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.gitops import Git, GitError
from orchestrator.globs import matches_any


class ToolError(Exception):
    """A refusal the planner is allowed to see and work around."""


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


@dataclass
class RepoReader:
    """Answers questions about a repository, within bounds.

    Holds no model and makes no decisions. The planner asks; this replies or
    refuses, and records what it did either way.
    """

    git: Git
    repo: Path
    budget: ReadBudget = field(default_factory=ReadBudget)
    calls: list[ToolCall] = field(default_factory=list)
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
    _lines_used: int = 0

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

    def _spend(self, tool: str, detail: str, text: str) -> str:
        used = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        self._lines_used += used
        self.calls.append(ToolCall(tool=tool, detail=detail, lines=used))
        return text

    def record_refusal(self, tool: str, detail: str, reason: str) -> None:
        """Note a call that was denied.

        Called by the dispatcher rather than at the raise sites: `_resolve`
        and `_require_readable` know neither the tool's name nor what was
        asked for, and the detail of a refused call is the question, because
        there is no content to describe it by.
        """
        self.calls.append(ToolCall(tool=tool, detail=detail, lines=0, refusal=reason))

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
        if self._lines_used >= self.budget.max_total_lines:
            raise ToolError(
                f"read budget spent for this step "
                f"({self.budget.max_total_lines} lines). Work with what you have."
            )

    def _clip(self, lines: list[str]) -> tuple[list[str], bool]:
        cap = self.budget.max_lines_per_call
        remaining = max(self.budget.max_total_lines - self._lines_used, 0)
        allowed = min(cap, remaining)
        if len(lines) <= allowed:
            return lines, False
        return lines[:allowed], True

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
        return self._spend("read_file", rel, numbered)

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

    def search(self, pattern: str, path_glob: str | None = None) -> list[str]:
        """Matching lines, as `path:line:text`.

        `git grep` rather than a walk: it is tracked-only by construction, it is
        fast on a large repository, and its runtime is bounded in a way a
        model-supplied regex evaluated in-process is not.

        The pattern is data. It is passed after `-e`, and `--` closes the option
        list, so a leading dash is a pattern rather than a flag. There is no
        shell anywhere in `Git._run`.
        """
        self._charge_call("search")
        if not (pattern or "").strip():
            raise ToolError("no search pattern given")

        target = path_glob.strip() if path_glob else "."

        # `-P` because a model writes Perl-flavoured regex. Measured on the
        # first live run of this tool: six of twenty-four searches returned
        # nothing because `\s`, `\b` and `(:|=>)` mean nothing to git's default
        # basic-regex engine, and every one of those was a wasted call against
        # a budget of twenty-five. The pattern that found the real answer,
        # `render[^_]*\btext:`, matches 7 sites with `-P` and 0 without.
        #
        # Not every git is built with PCRE, so fall back rather than fail: `-E`
        # at least gives alternation and quantifiers.
        # Searching a commit rather than the tree puts the ref before `--`, and
        # git then prefixes every hit with `<ref>:`. Stripped below, so a
        # pinned reader and a live one return the same `path:line:text`.
        ref = [self.at_sha] if self.at_sha else []
        # Live, so include files not yet committed. `git grep` reads the
        # working tree but only for *tracked* paths, so an edit is found
        # and a newly created file is not — and the ones that matter here
        # are precisely the new ones: a spec this attempt just wrote, or a
        # file a human left before resuming.
        #
        # `--untracked` excludes ignored paths by default, which is the
        # boundary that matters and the only one: this repository has
        # 617,485 untracked files and 0 untracked-but-not-ignored, so the
        # flag never walks the ignored tree. Measured, it is *faster* than
        # the plain search — 0.078s against 0.157s.
        untracked = [] if self.at_sha else ["--untracked"]
        for flavour in ("-P", "-E"):
            proc = self.git._run(
                "grep", "-n", "-I", "--no-color", *untracked, flavour,
                "-e", pattern, *ref, "--", target,
                check=False,
            )
            if proc.returncode in (0, 1):
                break
            if "-P" not in (proc.stderr or "") and flavour == "-P":
                # A real error — a bad pattern, a bad path — not a missing
                # engine. Retrying in another dialect would only confuse it.
                break

        # git grep exits 1 for "no matches", which is an answer.
        if proc.returncode not in (0, 1):
            raise ToolError(
                f"search failed: {proc.stderr.strip() or 'invalid pattern'}"
            )
        hits = [line for line in proc.stdout.splitlines() if line.strip()]
        if self.at_sha:
            prefix = self.at_sha + ":"
            hits = [h[len(prefix):] if h.startswith(prefix) else h for h in hits]
        hits, clipped = self._clip(hits)
        if clipped:
            hits = hits + ["... truncated; narrow the pattern or the path"]
        self._spend("search", f"{pattern} in {path_glob or '.'}", "\n".join(hits))
        return hits

    def git_show(self, ref: str, path: str) -> str:
        """A file as it stood at a ref."""
        self._charge_call("git_show")
        resolved = self._resolve(path)
        rel = self._relative(resolved)
        try:
            body = self.git.show_file(ref, rel)
        except GitError as e:
            raise ToolError(str(e)) from e
        lines, clipped = self._clip(body.splitlines())
        text = "\n".join(lines) + ("\n... truncated" if clipped else "")
        return self._spend("git_show", f"{ref}:{rel}", text)

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
