"""The reviewer client.

The provider sits behind a small protocol; OpenAI is the only implementation,
using the SDK's native structured-output parsing so the schema is enforced
server-side instead of hoped for.

Review used to need no tool access, and that was wrong. A diff does not always
carry the fact that decides it. Measured on one run of 31 stages: 8 of them
deleted an `attr_accessible` declaration, which is safe exactly when a permit
list elsewhere covers the same attributes — and that file is not in the diff.
The reviewer approved all 8, and could not have done anything else. A quarter
of that run's verdicts were approvals it had no way to withhold, which is not
a gate; it is the stage instruction restated in the reviewer's voice.

So it reads the repository now, through the same bounded, read-only view the
planner uses. Reading is not executing: `repotools` answers questions, every
command it runs is authored there, and nothing the reviewer can do changes a
file. The budget is its own — see `ReviewerConfig` — because the two roles ask
different questions and whoever retunes one should not silently retune the
other.

The defensive posture matters more than the happy path. If there is no usable
verdict — a refusal, a truncated response, a transport failure — the answer is
`blocked`. "The reviewer did not answer" means stop and ask a human; it never
means guess, and it never means crash four stages into a run.
"""

from __future__ import annotations

import os
import dataclasses
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel

from code_gantry.config import ReviewerConfig
from code_gantry.openaiclient import (
    TokenUsage,
    describe_call as _describe,
    extract_usage as _extract_usage,
    merge_usage as _merge_usage,
    refusal as _refusal,
    tool_request as _tool_request,
    transport_errors as _transport_errors,
)
from code_gantry.plannertools import dispatch, tool_schemas

Verdict = Literal["approved", "rework", "blocked"]




def _reasoning_param(cfg) -> dict:
    """The reasoning setting, or nothing at all.

    Nothing is the default and the important case: the reviewer has never sent
    this parameter, so it has always run at the provider's own choice. Emitting
    one unconditionally would change what the gate does on every project that
    never asked for it, which is not a change to make on the way past.
    """
    effort = getattr(cfg, "effort", None)
    return {"reasoning": {"effort": effort}} if effort else {}


class Issue(BaseModel):
    severity: Literal["major", "minor"]
    file: str
    description: str


class Observation(BaseModel):
    """Something real found nearby that this stage did not cause.

    Deliberately not a fourth verdict. A verdict is a routing decision — land,
    back to the executor, back to the planner — and a finding is information;
    the two are independent, and a reviewer that had to choose between
    reporting and routing would report only when it happened to be rejecting.
    """

    file: str
    finding: str
    detail: str


class ReviewVerdict(BaseModel):
    """The schema the reviewer is constrained to return."""

    verdict: Verdict
    summary: str
    # What the diff does, written for the progress log rather than for the
    # verdict. Required, and that is the whole difference from `observations`,
    # which is optional and has been returned empty 278 times out of 278: an
    # optional field invites nothing, and this one is always answerable.
    #
    # Separate from `summary` because `summary` justifies a routing decision
    # and reads like it — every one opens by confirming the diff matches the
    # stage and closes on what was not done. That is the right shape for a gate
    # and the wrong shape for a record someone reads a year later.
    record: str
    issues: list[Issue]
    observations: list[Observation] = []
    # Which of the stage's proposed `resolves` the diff actually settles, by
    # finding id. Required: the stage's list is a proposal and this is the
    # record, written by the only participant that saw the diff. Empty when
    # none of them are settled, which is an answer.
    resolved: list[str]


def _record_usage(outcome, usage, turns) -> None:
    """Both halves of the accounting, at every exit.

    Four paths leave this loop and each carries the totals out. The per-turn
    series has to travel with them, and a second assignment at four sites is
    the shape this codebase has already watched go quietly missing between two
    correct changes — so what "recording usage" means is decided once here
    rather than remembered four times.
    """
    outcome.usage = usage
    outcome.turn_usage = list(turns)


@dataclass
class ReviewOutcome:
    verdict: Verdict
    summary: str
    # What the diff does, for the progress log. See `ReviewVerdict.record`.
    record: str = ""
    issues: list[Issue] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    # True when the `blocked` verdict is ours rather than the model's, so the
    # report does not imply a judgement the reviewer never made.
    failed: bool = False
    # What it looked at, in order. Recorded for the same reason the planner's
    # is: a verdict reached without reading is worth less than one reached
    # after it, and the two are indistinguishable from the verdict alone.
    tool_calls: list[str] = field(default_factory=list)
    # The same ledger counted by tool. `tool_calls` is the record and goes
    # to the artifact; this is what the run log prints, because a large
    # review's rendered calls are thousands of characters on one line of a
    # timeline meant to be skimmed -- and they are already in `tools.log`,
    # one per line, and in `review.json` in order.
    tool_counts: dict[str, int] = field(default_factory=dict)
    # The questions put to the semantic index and what came back. Kept for
    # these alone because they are the only reads whose answer cannot be
    # fetched from the repository again — see `ToolCall.result`.
    semantic_results: list[dict] = field(default_factory=list)
    # Real problems found nearby that this stage did not cause. Carried
    # separately from `issues`, which are defects in this diff and route it
    # back to the executor; these route nowhere and are written to the
    # progress log when the stage lands.
    observations: list[Observation] = field(default_factory=list)
    # Finding ids the reviewer confirmed the diff settles. See
    # `ReviewVerdict.resolved`.
    resolved: list[str] = field(default_factory=list)
    # One reading per turn, in order. `usage` sums the loop and cannot say
    # where a cached prefix stops matching: measured on a live run, cached
    # tokens per turn sat pinned near 200,000 against a peak prompt of
    # ~230,000, so 22-35k was re-sent uncached on every turn — far more than
    # this role's own reads, which came to under 10k tokens across a whole
    # review. A flat cache against small growth and a growing cache produce
    # the same aggregate rate, so only the series separates them. Same reason
    # `executor-loop.json` keeps the attempt's peak: a sum cannot be
    # decomposed afterwards.
    turn_usage: list[dict] = field(default_factory=list)
    # How the model's last turn ended, facts first. Same key, same shape, in
    # all three roles' artifacts: one event that used to present as
    # `parsed_output is None` here, an empty content list in the executor, and
    # nothing at all in between.
    turn_end: dict | None = None

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "record": self.record,
            "issues": [i.model_dump() for i in self.issues],
            "observations": [o.model_dump() for o in self.observations],
            "resolved": list(self.resolved),
            "tool_calls": list(self.tool_calls),
            # The same ledger by tool. It reaches `run.log` as a summary
            # line and reached no structured artifact, so every count
            # measured from this project so far came from parsing that
            # line back out of a log.
            "tool_counts": dict(self.tool_counts),
            # Always present, empty included. An absent key and an empty
            # one read the same to anyone measuring later, and this
            # project has already answered a question wrongly that way.
            "semantic_results": list(self.semantic_results),
            # The series, so a later reader can see where a cached prefix
            # stopped matching. `usage` below is the sum and cannot answer it.
            "turn_usage": list(self.turn_usage),
            # Always present, null included: an absent key and "it ended
            # normally" are different answers and must not render alike.
            "turn_end": self.turn_end,
            # Every field, walked off the dataclass. The hand-written list
            # this replaces named four of five and dropped
            # `peak_prompt_tokens`, so eleven records on one run carried a
            # tool-loop *total* and no context figure — and the total was
            # twice read as one. Same defect as `executor-loop.json`'s ten
            # fields of twenty: wherever a subset is written out by hand, the
            # hand is the defect, and the next field added goes missing too.
            "usage": dataclasses.asdict(self.usage),
            "client_failure": self.failed,
        }


class ReviewerClient(Protocol):
    def review(
        self, messages: list[dict[str, str]], cache_key: str | None = None
    ) -> ReviewOutcome: ...


def _blocked(reason: str) -> ReviewOutcome:
    return ReviewOutcome(verdict="blocked", summary=reason, failed=True)


class Reviewer:
    """The reviewer, on whichever wire its configured endpoint wants."""

    def __init__(self, cfg: ReviewerConfig, client=None, log=None, reader=None,
                 semantic=None, project_tools=None):
        self.cfg = cfg
        # Assigned by `build_runtime`; the client predates the run log.
        self.log = log
        # Absent unless the project turned repo access on, in which case the
        # reviewer judges from the diff alone as it always did.
        self.reader = reader
        self.semantic = semantic
        # The project's whole declared menu, scoped per use. The reviewer had
        # no route to it at all — `make_reviewer` never took one — so a project
        # could declare a tool the gate needed and the gate could not reach it.
        # That is the same shape as the reviewer having had no tools for most
        # of this project's life: a checkpoint that cannot reach its evidence
        # produces verdicts indistinguishable from judgement.
        self.project_tools = list(project_tools or [])
        # Bound by `build_runtime`; see the planner's.
        self.runner = None
        self.tool_log = None
        from code_gantry.dialects import RESPONSES, wire_for

        self.wire = wire_for(cfg, RESPONSES)
        self._client = client if client is not None else self.wire.client(cfg)

    def _reset_reads(self) -> None:
        """Forget the previous review's reads. See `RepoReader.reset`."""
        from code_gantry.repotools import Spend

        if self.reader is not None:
            self.reader.spend = Spend()

    def _looked_at(self) -> list[str]:
        """What the reviewer read, from the ledger rather than the request.

        This was built at the call site from the arguments, before dispatch
        ran, so the artifact recorded that a call was *made* and nothing about
        what came back — neither the size of the answer nor whether there was
        one. A reviewer cut off by its read budget produced a `review.json`
        identical to one that stopped because it was satisfied, so asked how
        often the 25-call cap bound across 331 reviews, the artifact could not
        say.

        That is exactly what `ToolCall.refusal` exists for one layer down, and
        its docstring already argues it: "a cap whose binding cannot be
        observed cannot be tuned."

        `_render_call` is the planner's, imported rather than restated. Two
        renderings of one ledger is how they drift, and these two had — the
        planner's carried the line count and the refusal, the reviewer's
        carried neither.
        """
        from code_gantry.planner import _render_call

        return [_render_call(c) for c in getattr(self.reader, "calls", []) or []]

    def _semantic_results(self) -> list[dict]:
        """What the index was asked, and what it answered."""
        from code_gantry.repotools import semantic_results

        return semantic_results(self.reader)

    def _tool_counts(self) -> dict[str, int]:
        """The same ledger, counted by tool, for the run log's one-line summary.

        From `call.tool` rather than from the rendered strings. Taking the name
        off the front of `_render_call`'s output would work today and is the
        move this codebase has been burned by twice — a value derived from
        rendered text stops being derivable the moment the rendering changes,
        and nothing fails when it does.
        """
        from code_gantry.repotools import count_calls

        return count_calls(self.reader)

    def _log_new_calls(self, seen: int) -> int:
        """Emit the reads made since `seen`; return the new watermark.

        The planner's, one role over, and for the same reason: until the
        verdict came back there was no way to tell a gate reading seven files
        from one reading none. Shorter here — reviews ran 29 to 66 seconds
        against derivations of 3 to 20 minutes — and nearly free, because
        `_looked_at` already renders the ledger.
        """
        calls = list(getattr(self.reader, "calls", []) or [])
        sink = getattr(self, "tool_log", None) or self.log
        if sink:
            from code_gantry.planner import _render_call

            for call in calls[seen:]:
                sink(f"[review] {_render_call(call)}")
        return len(calls)

    def _max_tool_turns(self) -> int:
        """The reader's own call budget is the real ceiling.

        Past it every tool refuses, the reviewer reads the refusal and answers
        with what it has. This bound is the backstop for a model that ignores
        the refusal and keeps asking — without it a loop that never converges
        would hold a stage open until the request timeout.
        """
        return max(self.cfg.max_read_calls, 1)

    def review(
        self, messages: list[dict[str, str]], cache_key: str | None = None
    ) -> ReviewOutcome:
        """One review.

        `cache_key` groups a run's reviews so they route to the same cache
        rather than competing for one. On GPT-5.6 it is required for reliable
        matching rather than merely helpful.

        `mode: explicit` is the other half of the caching fix. GPT-5.6 caches at
        breakpoints and does not fall back to the longest matching prefix; its
        default `implicit` mode puts a breakpoint on the latest message, which
        here is the diff. Left at the default, every review wrote its whole
        prefix to cache — billed above the uncached rate — and read none of it
        back. The breakpoint itself is placed in `prompts.build_review_messages`;
        this is the request-side opt-in that makes it count.
        """
        # Per review, not per run. Without this the reader was shared across
        # every review a process made: `review.json` recorded 685 calls for a
        # review that made a handful, the line and call ceilings named "for
        # this step" were really for the whole run, and refusals climbed from
        # zero to 58 as late reviews were starved by reads their predecessors
        # had done. The planner had always reset; this site was written without
        # one and nothing compared the two.
        self._reset_reads()

        specs = (
            tool_schemas(
                self.semantic,
                self.project_tools,
                "reviewer",
                getattr(self.reader, "budget", None),
            )
            if self.reader
            else []
        )
        wire = self.wire
        conversation = list(messages)
        # The ledger is cumulative, so each turn reports only what it added.
        logged = len(getattr(self.reader, "calls", []) or [])

        def dispatch_one(name: str, args: dict) -> str:
            return dispatch(
                name, args, self.reader, self.semantic,
                project_tools=self.project_tools, runner=self.runner, role="reviewer",
            )

        def after_batch() -> None:
            nonlocal logged
            logged = self._log_new_calls(logged)

        from code_gantry.dialects import describe_end, request_extras
        from code_gantry.roleloop import run_structured_loop

        result = run_structured_loop(
            wire=wire,
            client=self._client,
            cfg=self.cfg,
            conversation=conversation,
            tools=wire.tool_schemas(specs) if specs else [],
            schema=ReviewVerdict,
            extra=request_extras(
                self.cfg, getattr(self, "session_id", "") or "", cache_key=cache_key
            ),
            dispatch=dispatch_one,
            max_turns=self._max_tool_turns(),
            log=self.log,
            after_batch=after_batch,
        )

        def finish(outcome: ReviewOutcome) -> ReviewOutcome:
            _record_usage(outcome, result.usage, result.turns)
            outcome.tool_calls = self._looked_at()
            outcome.tool_counts = self._tool_counts()
            outcome.semantic_results = self._semantic_results()
            return outcome

        if result.failure is not None:
            return finish(_blocked(f"The reviewer call failed: {result.failure}"))
        if result.response is None:  # pragma: no cover - the loop always runs once
            return finish(_blocked("The reviewer produced no response."))
        if result.refusal:
            return finish(_blocked(f"The reviewer refused to answer: {result.refusal}"))
        end = result.end
        if end.label == "max_tokens":
            # A verdict cut off mid-JSON is not a verdict, even if the parsed
            # fragment happens to validate.
            outcome = _blocked(
                f"The reviewer's response was truncated ({end.reason}), so its "
                "verdict cannot be trusted."
            )
            outcome.turn_end = end.as_record()
            return finish(outcome)
        parsed = result.parsed
        if parsed is None:
            outcome = _blocked(describe_end("reviewer", end) + ".")
            outcome.turn_end = end.as_record()
            return finish(outcome)

        return ReviewOutcome(
            verdict=parsed.verdict,
            summary=parsed.summary,
            record=getattr(parsed, "record", "") or "",
            issues=list(parsed.issues),
            observations=list(getattr(parsed, "observations", None) or []),
            resolved=list(getattr(parsed, "resolved", None) or []),
            usage=result.usage,
            turn_usage=list(result.turns),
            failed=False,
            tool_calls=self._looked_at(),
            tool_counts=self._tool_counts(),
            semantic_results=self._semantic_results(),
        )


OpenAIReviewer = Reviewer


def make_reviewer(
    cfg: ReviewerConfig, target_repo=None, log=None, project_tools=None
) -> ReviewerClient:
    """The pluggable seam. Config validation already restricts the provider,
    so this only has to map it.

    `target_repo` is optional so preflight can build a client just to prove the
    credentials work, without needing a repo on hand — the same reason
    `make_planner` takes it that way.
    """
    reader = semantic = None
    if cfg.repo_access and target_repo is not None:
        from code_gantry.gitops import Git
        from code_gantry.repotools import ReadBudget, RepoReader
        from code_gantry.semantic import SemanticSearch, SemanticSearchConfig

        reader = RepoReader(
            Git(target_repo),
            target_repo,
            ReadBudget(
                max_lines_per_call=cfg.max_read_lines_per_call,
                max_total_lines=cfg.max_read_lines_total,
                max_total_chars=cfg.max_read_chars_total,
                max_calls=cfg.max_read_calls,
            ),
        )
        search_cfg = SemanticSearchConfig.from_mapping(cfg.semantic_search)
        if search_cfg is not None:
            # One list, shared with the reader, so the log is chronological.
            # Two lists concatenated say what was looked at but not in what
            # order, and the order is most of how a conclusion was reached.
            semantic = SemanticSearch(search_cfg, reader=reader)

    return Reviewer(
        cfg, log=log, reader=reader, semantic=semantic, project_tools=project_tools
    )


def issues_as_feedback(summary: str, issues: list[Issue]) -> str:
    """Render a verdict as one feedback item for the next executor prompt."""
    if not issues:
        return summary
    listed = "\n".join(
        f"- [{i.severity}] {i.file}: {i.description}" for i in issues
    )
    return f"{summary}\n{listed}"
