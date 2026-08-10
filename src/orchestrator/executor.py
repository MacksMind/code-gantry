"""What an attempt is given, before any of it reaches a model.

The edit cycle itself lives in `executorloop`; the provider call in
`executorclient`. What is here is everything that shapes an attempt before it
starts — the read budget, the excerpts, the conventions.

**File scoping is mandatory, not an optimisation.** An agent stage declares
what it may edit and what it needs to read, and `max_read_lines` bounds the
second: reference material is chosen by the planner, grows with every landed
stage, and reached 4,636 lines to change six on one run before it was capped.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator import gates
from orchestrator.cachekey import cache_key
from orchestrator.commands import (
    CommandResult,
    CommandRunner,
    collapse_progress_runs,
)
from orchestrator.config import ProjectConfig, Stage
from orchestrator.gitops import Git, GitError
from orchestrator.globs import matches_any
from orchestrator.repotools import number_lines






















@dataclass
class ExecutionResult:
    ok: bool
    log: str = ""
    timed_out: bool = False
    # Peak context the attempt held, or 0 if the provider reported none.
    context_tokens: int = 0
    # What the attempt spent, when the model was priced. Zero for a local
    # endpoint, which is the truth rather than a missing reading.
    cost_usd: float = 0.0
    # Reference files withheld to keep inside `max_read_lines`. Reported rather
    # than dropped quietly: a stage that behaves differently because it was
    # shown less than it declared must say so, or the next person debugging it
    # is reading a prompt the executor never received.
    dropped_reads: list[str] = field(default_factory=list)

    # --- the in-process executor ----------------------------------------
    #
    # Zero on the subprocess path, which is honest: it has no cycles and no
    # turns, and a zero here says "this executor does not work that way"
    # rather than "it did none".

    # Real provider counts, so cost is priced by `pricing.price_usage` — the
    # same function the planner and reviewer already use — instead of scraped
    # from a console line that omits reasoning tokens and cannot see OpenAI's
    # cache fields at all.
    usage: object | None = None
    cycles: int = 0
    model_turns: int = 0
    edits_applied: int = 0
    # Edits the tool refused. The instrument for the one claim this design
    # rests on and has not yet earned: that exact matching plus a read tool
    # beats fuzzy matching a diff out of prose. If this is high the answer is
    # not to add fuzzy matching back — it is that the refusal text is not
    # actionable enough.
    edit_refusals: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    # Why the repository refused to record the work, when it did — a commit
    # hook, in every case seen. Empty otherwise.
    #
    # It needs a field because "the executor committed before verify" is stated
    # as a guarantee in this loop's module docstring, on the reasoning that an
    # in-process loop cannot be killed mid-write. A hook falsifies it for a
    # reason the loop is entitled to know about and, until this existed, could
    # not report: the refusal was caught and dropped, so an attempt with
    # `commits: []` against 15 edits reported itself clean.
    commit_refused: str = ""
    # Which gate failed on which cycle, so a stage that used its whole budget
    # says what it kept failing rather than only that it ran out.
    in_loop_failures: list[str] = field(default_factory=list)
    # The opening turn of the attempt's first cycle. This is the only figure
    # that answers whether the prefix arranged to be shared across stages
    # actually is: everything after it in an attempt reads what it wrote.
    first_prompt_tokens: int = 0
    first_cached_tokens: int = 0
    # The model was still calling tools when the turn ceiling stopped it. A
    # different problem from a model that stopped having changed nothing, and
    # for a while they were reported identically: `ExecutorTurn.stopped` was
    # set in three places and read in none, so an attempt cut off mid-survey
    # arrived at the gate as "the attempt produced no changes".
    turns_exhausted: bool = False
    # What it asked for, by tool, and what was refused, by reason. Counts
    # rather than the rendered calls the other two loops log: the planner's
    # 40-call line is already hard to read, and this one makes sixty a cycle.
    # The calls themselves are in `executor-conversation.jsonl`, so a summary
    # that points at the detail beats one that repeats it.
    tool_counts: dict[str, int] = field(default_factory=dict)
    refusal_counts: dict[str, int] = field(default_factory=dict)
    # What the loop proved green, and against which tree: layer name ->
    # {"command", "head_sha"}. The gate reads this to decide whether running
    # the same command again would ask a question already answered. Not trust
    # — two facts compared, and any of them moving means it runs.
    gate_records: dict[str, dict] = field(default_factory=dict)














class Executor:
    def __init__(
        self,
        cfg: ProjectConfig,
        runner: CommandRunner,
        git: Git | None = None,
        log=None,
        tool_log=None,
    ):
        self.cfg = cfg
        self.runner = runner
        self.git = git
        # Both passed by `build_runtime`. This said "assigned by build_runtime"
        # and was not: the loop that binds the logger names the planner and the
        # reviewer, and the executor was constructed on its own line without
        # one. `self.log` was None for the whole of the executor's life, which
        # took "checks rewrote files; committed as" — zero emissions across
        # every run — and the transport-retry line with it.
        self.log = log
        # Reads, to the file the timeline is kept free of. See `RunPaths`.
        self.tool_log = tool_log

    def _tracked_paths(self) -> list[str] | None:
        """What the repository currently tracks, for the mention shield.

        Degrades to no shielding rather than failing the stage: this guard
        exists to save tokens, and a run whose git cannot list its own files
        has a larger problem than an over-attached prompt.
        """
        if self.git is None:
            return None
        try:
            return self.git.tracked_paths_now()
        except GitError:
            return None

    def gather_context(
        self, stage: Stage
    ) -> tuple[list[tuple[str, str]], list[CommandResult]]:
        """Run the stage's context commands and collect their stdout.

        These are declared by the operator, never produced by a model. Their
        output flows into the prompt; nothing flows the other way.
        """
        collected: list[tuple[str, str]] = []
        results: list[CommandResult] = []
        for command in stage.context_commands:
            result = self.runner.run(command)
            results.append(result)
            collected.append((command, result.output))
        return collected, results

    def run_agent_stage(
        self,
        stage: Stage,
        prompt: str,
        history_dir: Path | None = None,
        since_sha: str = "",
        agent_context: str | None = None,
        feedback: list[str] | None = None,
        failure_layer: str | None = None,
    ) -> ExecutionResult:
        """One attempt at a stage.

        `since_sha` is where the stage began, and the loop runs its gates as
        it goes — every one of them a question about what has changed *since
        the stage started*. It stays optional because `run_script_stage` and
        the resume paths call in without one.
        """
        return self._run_in_process(
            stage, prompt, history_dir, since_sha, agent_context, feedback,
            failure_layer,
        )

    def _run_in_process(
        self,
        stage: Stage,
        prompt: str,
        history_dir: Path | None = None,
        since_sha: str = "",
        agent_context: str | None = None,
        feedback: list[str] | None = None,
        failure_layer: str | None = None,
    ) -> ExecutionResult:
        """The in-process loop. See `executorloop.run_loop`.

        Assembled here rather than in `runtime.py` because every part of it is
        per-stage: the editor's allowlist is the stage's `edit_files`, and the
        reader's is the same list. A collaborator bound once for the run would
        have to be re-scoped on every stage, which is the same thing with a
        longer-lived object to get wrong.
        """
        from orchestrator.executorclient import OpenAIExecutorModel
        from orchestrator.executorloop import build_loop_parts, run_loop

        reader, editor, semantic = build_loop_parts(
            stage, self.cfg, self.cfg.target_repo
        )
        model = OpenAIExecutorModel(
            self.cfg.executor,
            log=self.log,
            tool_log=self.tool_log,
            # Project-declared tools and the runner that executes them. Bound
            # here with the rest of the per-stage assembly rather than in
            # `runtime`, so there is one place that knows what an attempt is
            # made of.
            project_tools=self.cfg.project_tools,
            runner=self.runner,
        )
        kept = set(_within_read_budget(stage.read_files, self.cfg))

        from orchestrator.prompts import build_executor_messages

        # Recording from here on, not from the way out. The opening messages
        # are complete before the first call, so the two artifacts that explain
        # why an attempt exists are on disk while it is still running rather
        # than only if it returns.
        conversation = Transcript(
            build_executor_messages(
                stage,
                self.cfg,
                prompt,
                agent_context=agent_context,
                feedback=feedback,
                failure_layer=failure_layer,
            ),
            history_dir,
        )
        if history_dir is not None:
            # The whole prompt, not the stage half. `nodes.execute` writes
            # `prompt.md` from `build_executor_prompt`, which no longer carries
            # the feedback on this path — so that artifact stopped explaining
            # why an attempt existed at all, and a reader opening the directory
            # after a rework saw a prompt identical to the previous attempt's.
            #
            # Written before the loop because everything in it is already
            # known: `_write_sent_prompt` stops at the first thing the model
            # said, so nothing the loop appends would ever have reached it.
            _write_sent_prompt(history_dir, conversation)
        out = run_loop(
            stage,
            self.cfg,
            self.git if self.git is not None else Git(self.cfg.target_repo),
            self.runner,
            model,
            reader,
            editor,
            semantic=semantic,
            since_sha=since_sha,
            conversation=conversation,
            # Run-level, not per stage. The provider caps this at 64
            # characters — a branch and a stage id together overran it and
            # every call 400'd — but the length is the smaller reason. The
            # cached prefix is the system prompt and the conventions, which
            # are identical across every stage of a run; keying per stage
            # would put each stage in its own cache and guarantee a miss on
            # the one region that was arranged to be shared.
            cache_key=cache_key("exec", self.cfg.project_branch),
            log=self.log,
        )
        out.dropped_reads = [p for p in stage.read_files if p not in kept]
        _count_tool_use(out, reader, editor)
        if history_dir is not None:
            _write_loop_record(history_dir, out)
        return out



def _read_lines(path: str, cfg: ProjectConfig) -> int | None:
    """Lines in a reference file, or None when it cannot be counted.

    None covers a glob, a path outside the repo, a binary blob — anything whose
    size is not a plain fact. Callers must not substitute a number for it.
    """
    if any(ch in path for ch in "*?["):
        return None
    try:
        target = cfg.target_repo / path
        if not target.is_file():
            return None
        return sum(1 for _ in target.open("rb"))
    except OSError:  # pragma: no cover - unreadable file behaves as uncountable
        return None


def _existing_agent_context(cfg: ProjectConfig) -> list[str]:
    """The agent-facing documents that are actually present.

    The defaults name two and most projects keep one, so an unconditional pass
    would name a path that does not resolve.

    Not deduplicated by content the way the planner's copy is. That dedup
    exists because both documents are rendered into one prompt; here they are
    passed as separate reads, where two paths naming the same bytes cost a
    duplicate read rather than a confused prompt.
    """
    root = Path(cfg.target_repo)
    return [p for p in cfg.effective_agent_context if (root / p).is_file()]


class ExcerptError(Exception):
    """A declared range could not be read.

    Loud, and it did not used to be. The old policy skipped an unreadable range
    on the reasoning that the instruction is the authority and an excerpt is
    only help — which held exactly as long as the instruction also carried the
    code. It no longer does: the planner authors none, so the excerpt *is* the
    code, and skipping one hands the executor an instruction referring to lines
    it was never shown. A payload that fails must fail the stage.
    """


def resolve_excerpts(
    stage,
    cfg: ProjectConfig,
    git=None,
    sha: str = "",
) -> list[tuple[str, str]]:
    """Read each declared range, returning (label, numbered text) pairs.

    Numbered, for the same reason the planner's own reads are: a line the
    executor is told to match is checkable against a number and not against a
    recollection.

    Read at `sha` when one is given, and the caller in the loop always gives
    one. Line numbers are the least stable identifier there is, and the state a
    range was chosen against is not the state it is read against: on a rework
    the executor's own prior attempt has already moved the lines. Reading at the
    stage's start sha puts the executor on the same baseline as the reviewer's
    diff and the planner's revision block, so all three describe one tree.

    Charged against `max_read_lines`, the same budget whole reference files
    come out of — this exists so a large file can contribute the part that
    matters, not so it can contribute more than a small one. Ranges are clipped
    rather than dropped, because a clipped range still carries its beginning,
    where a dropped file carries nothing.
    """
    budget = cfg.executor.max_read_lines
    remaining = None
    if budget is not None:
        # One budget for all reference material, not one each. Excerpts exist
        # so a file too large to send whole can still contribute the part that
        # matters — not so a stage can carry twice what the operator allowed by
        # splitting it across two fields. Reference files are counted first
        # because they were already chosen and trimmed by the time we get here.
        kept = _within_read_budget(stage.read_files, cfg)
        spent = sum(n for n in (_read_lines(p, cfg) for p in kept) if n)
        remaining = max(budget - spent, 0)
    out: list[tuple[str, str]] = []

    for ex in getattr(stage, "read_excerpts", []) or []:
        if remaining is not None and remaining <= 0:
            break
        if git is not None and sha:
            # `git show <sha>:<path>` on a symlink returns the link's *target* —
            # a path, not the file it names — so an excerpt of one would be a
            # numbered line of nonsense presented as the code to edit. Ask
            # before reading rather than guessing from the content.
            if git.is_symlink(sha, ex.path):
                raise ExcerptError(
                    f"{ex.path!r} is a symlink at {sha[:12]}; an excerpt of it "
                    "would carry the link's target, not the file. Point the "
                    "range at the file it resolves to."
                )
            try:
                body = git.show_file(sha, ex.path).splitlines()
            except GitError as exc:
                raise ExcerptError(
                    f"cannot read {ex.path!r} at {sha[:12]} for an excerpt of "
                    f"lines {ex.start}-{ex.end or 'end'}: {exc}"
                ) from exc
        else:
            target = Path(cfg.target_repo) / ex.path
            try:
                body = target.read_text(errors="replace").splitlines()
            except OSError as exc:
                raise ExcerptError(
                    f"cannot read {ex.path!r} for an excerpt of lines "
                    f"{ex.start}-{ex.end or 'end'}: {exc}"
                ) from exc
        first = max(ex.start or 1, 1)
        last = min(ex.end or len(body), len(body))
        if first > last:
            continue
        chosen = body[first - 1 : last]
        if remaining is not None and len(chosen) > remaining:
            chosen = chosen[:remaining]
        if not chosen:
            continue
        if remaining is not None:
            remaining -= len(chosen)
        label = f"{ex.path}:{first}-{first + len(chosen) - 1}"
        # Say so when the range is not the range that was asked for. The
        # executor is the participant that would otherwise act on a partial
        # quotation believing it whole, and since the planner no longer writes
        # code there is nothing else in the prompt to contradict it. Kept in
        # the label rather than a log line because the label travels with the
        # lines into the prompt, and the operator's copy is the artifact.
        if len(chosen) < last - first + 1:
            label += f" (clipped from {first}-{last} by max_read_lines)"
        if ex.note:
            label += f" — {ex.note}"
        out.append((label, number_lines(chosen, first)))
    return out


def _within_read_budget(read_files: list[str], cfg: ProjectConfig) -> list[str]:
    """Trim reference files to `max_read_lines`, largest first.

    Largest first because dropping the biggest recovers the most context per
    file lost, and because the small ones are likelier to be the base class or
    the routes file the stage actually needs — the big ones are the worked
    examples that accumulate as a run proceeds.

    Order is preserved among the survivors; only membership changes.

    A file whose size cannot be established is kept. The alternative is to
    invent a number for it, and inventing zero admits anything while inventing
    a large one drops the routes file that was declared as a glob. This is the
    same rule the auto-test paths follow: act on evidence, not on its absence.
    """
    budget = cfg.executor.max_read_lines
    if budget is None or not read_files:
        return list(read_files)

    sizes = {p: _read_lines(p, cfg) for p in read_files}
    total = sum(n for n in sizes.values() if n is not None)
    if total <= budget:
        return list(read_files)

    # Drop measurable files, biggest first, until the rest fit.
    dropped: set[str] = set()
    for path, _ in sorted(
        ((p, n) for p, n in sizes.items() if n is not None),
        key=lambda item: item[1],
        reverse=True,
    ):
        if total <= budget:
            break
        dropped.add(path)
        total -= sizes[path] or 0
    return [p for p in read_files if p not in dropped]


TRANSCRIPT_FILENAME = "executor-conversation.jsonl"


def _plain(item) -> dict:
    """One conversation item as a mapping the record can hold.

    The SDK's own output objects are not dicts and carry more than this, but
    what an operator opens the file for is which tool was asked for and with
    what — and a reasoning item's encrypted payload is bytes nobody reads.
    """
    if isinstance(item, dict):
        return item
    return {
        "type": getattr(item, "type", "?"),
        "name": getattr(item, "name", ""),
        "arguments": getattr(item, "arguments", ""),
    }


class Transcript(list):
    """The conversation, mirrored to disk one line at a time as it grows.

    A JSON array can only be written whole, so the record used to be produced
    on the way out of an attempt: a stage that spent forty minutes had nothing
    to read for thirty-nine of them, and an attempt that never returned left no
    record at all — which is the one case where the record is most wanted. One
    JSON object per line is the same content in a container that can be
    appended to, so the file is complete-so-far at every instant and
    `tail -f` works.

    A `list` subclass rather than a callback threaded through the four places
    that append. Those places are in two modules and a fifth is one refactor
    away; a record that has to be *remembered* at each of them is the shape of
    thing this codebase has already watched go quietly missing between two
    correct changes. Appending to the conversation is the only way to record
    it, so nothing can forget.

    Best effort, like every other artifact here: an attempt that worked must
    not be failed by a directory that could not be written.
    """

    def __init__(self, items: Iterable = (), history_dir: Path | None = None):
        super().__init__()
        self.path = history_dir / TRANSCRIPT_FILENAME if history_dir else None
        self.extend(items)

    def append(self, item) -> None:
        super().append(item)
        self._record(item)

    def extend(self, items) -> None:
        for item in items:
            self.append(item)

    def insert(self, index: int, item) -> None:
        super().insert(index, item)
        self._record(item)

    def __iadd__(self, items):
        self.extend(items)
        return self

    def _record(self, item) -> None:
        if self.path is None:
            return
        import json

        try:
            line = json.dumps(_plain(item), default=str)
        except Exception:  # noqa: BLE001 - a record is never worth an attempt
            line = json.dumps({"type": "?", "unserialisable": repr(item)[:2000]})
        try:
            with self.path.open("a") as fh:
                # Closed per item rather than held open, so what is on disk is
                # what has happened. A buffered handle would leave the last
                # several turns invisible to exactly the reader this exists
                # for, and the writes are a handful a minute against an HTTP
                # call apiece.
                fh.write(line + "\n")
        except OSError:
            return


def _write_loop_record(history_dir: Path, out: ExecutionResult) -> None:
    """What the cycle cost and how it went, for the attempt directory.

    Totals, so unlike the transcript this is written once and at the end. Best
    effort for the same reason.
    """
    import json

    try:
        (history_dir / "executor-loop.json").write_text(
            json.dumps(
                {
                    "cycles": out.cycles,
                    # This attempt's high-water mark. Recorded here as well as
                    # in the stage's total because the stage's is a sum across
                    # attempts, and a sum cannot be taken apart afterwards —
                    # the analysis that found the total was being assigned
                    # rather than accumulated had to infer per-attempt figures
                    # from cache writes, because this file did not carry the
                    # one number it is about.
                    "peak_prompt_tokens": out.context_tokens,
                    "model_turns": out.model_turns,
                    "edits_applied": out.edits_applied,
                    "edit_refusals": out.edit_refusals,
                    "commits": out.commits,
                    "in_loop_failures": out.in_loop_failures,
                    # Recorded here and not only rolled into the run total,
                    # because a cache whose hit rate cannot be seen per
                    # attempt cannot be tuned — the same argument that put
                    # refusals in the planner's ledger. The static prefix is
                    # arranged to be shared across every stage of a run, and
                    # this is the only place that claim can be checked.
                    "usage": {
                        "prompt_tokens": getattr(out.usage, "prompt_tokens", 0),
                        "cached_tokens": getattr(out.usage, "cached_tokens", 0),
                        "cache_write_tokens": getattr(
                            out.usage, "cache_write_tokens", 0
                        ),
                        "completion_tokens": getattr(
                            out.usage, "completion_tokens", 0
                        ),
                    },
                    "opening_turn": {
                        "prompt_tokens": out.first_prompt_tokens,
                        "cached_tokens": out.first_cached_tokens,
                    },
                    "cost_usd": out.cost_usd,
                },
                indent=2,
            )
        )
    except OSError:
        return


def _write_sent_prompt(history_dir: Path, conversation: list) -> None:
    """What the model was actually given, as one readable document.

    Separate from `executor-conversation.jsonl`, which is the whole exchange
    including every tool call and result and is the wrong thing to open first.
    This is the input: system prompt, conventions, stage, feedback — in order,
    and only the messages that were there before the model said anything.

    Best effort, like the transcript. An attempt that worked must not fail
    because a directory could not be written.
    """
    parts: list[str] = []
    for item in conversation:
        if not isinstance(item, dict):
            # Everything the model produced and everything answering it. The
            # exchange belongs in the transcript, not in the record of what it
            # was asked.
            break
        role = item.get("role")
        if role is None:
            break
        content = item.get("content")
        text = (
            "".join(p.get("text", "") for p in content)
            if isinstance(content, list)
            else str(content or "")
        )
        parts.append(f"<!-- {role} -->\n\n{text}")
    try:
        (history_dir / "sent-prompt.md").write_text("\n\n---\n\n".join(parts))
    except OSError:
        return


def _refusal_kind(reason: str) -> str:
    """One refusal, bucketed by what the caller should do about it.

    Deliberately about the reader's next move rather than about which function
    raised: "read budget" means stop asking, "not found" means read the file,
    "not unique" means widen the anchor. A count of `ToolError` would say
    nothing an operator could act on.
    """
    text = (reason or "").lower()
    if "too many tool calls" in text or "read budget spent" in text:
        return "budget"
    if "does not appear" in text:
        return "edit not found"
    if "appears" in text and "times" in text:
        return "edit not unique"
    if "scope" in text:
        return "out of scope"
    if "does not exist" in text or "not tracked" in text:
        return "no such path"
    return "other"


def _count_tool_use(out: ExecutionResult, reader, editor) -> None:
    """Summarise both ledgers onto the result.

    Two objects record calls — the reader and the editor — and an operator
    reading the log wants one answer, so they are merged here rather than at
    the log site. Same reason `_tool_log` merges the planner's two.
    """
    tools: dict[str, int] = {}
    refusals: dict[str, int] = {}
    for source in (reader, editor):
        for call in getattr(source, "calls", []) or []:
            tools[call.tool] = tools.get(call.tool, 0) + 1
            if getattr(call, "refusal", ""):
                # The raiser's own bucket wins where it set one. Deriving it
                # from the message is a fallback for refusals whose text and
                # cause are the same thing, and an edit refused by three
                # different routes to one sentence is the case where they are
                # not — no reading of that message could recover which fired.
                kind = getattr(call, "refusal_kind", "") or _refusal_kind(call.refusal)
                refusals[kind] = refusals.get(kind, 0) + 1
    out.tool_counts = tools
    out.refusal_counts = refusals
