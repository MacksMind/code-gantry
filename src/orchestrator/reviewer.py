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
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel

from orchestrator.config import ReviewerConfig
from orchestrator.openaiclient import (
    TokenUsage,
    describe_call as _describe,
    extract_usage as _extract_usage,
    merge_usage as _merge_usage,
    refusal as _refusal,
    tool_request as _tool_request,
    transport_errors as _transport_errors,
)
from orchestrator.plannertools import dispatch, openai_tool_schemas
from orchestrator.retry import Backoff, with_provider_retry

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
    # Real problems found nearby that this stage did not cause. Carried
    # separately from `issues`, which are defects in this diff and route it
    # back to the executor; these route nowhere and are written to the
    # progress log when the stage lands.
    observations: list[Observation] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "record": self.record,
            "issues": [i.model_dump() for i in self.issues],
            "observations": [o.model_dump() for o in self.observations],
            "tool_calls": list(self.tool_calls),
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "cached_tokens": self.usage.cached_tokens,
                "cache_write_tokens": self.usage.cache_write_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
            "client_failure": self.failed,
        }


class ReviewerClient(Protocol):
    def review(
        self, messages: list[dict[str, str]], cache_key: str | None = None
    ) -> ReviewOutcome: ...


def _blocked(reason: str) -> ReviewOutcome:
    return ReviewOutcome(verdict="blocked", summary=reason, failed=True)


class OpenAIReviewer:
    def __init__(self, cfg: ReviewerConfig, client=None, log=None, reader=None,
                 semantic=None):
        self.cfg = cfg
        # Assigned by `build_runtime`; the client predates the run log.
        self.log = log
        # Absent unless the project turned repo access on, in which case the
        # reviewer judges from the diff alone as it always did.
        self.reader = reader
        self.semantic = semantic
        # Bound by `build_runtime`; see the planner's.
        self.tool_log = None
        self._client = client if client is not None else _build_openai_client(cfg)

    def _reset_reads(self) -> None:
        """Forget the previous review's reads. See `RepoReader.reset`."""
        if self.reader is not None:
            self.reader.reset()
        if self.semantic is not None:
            self.semantic.calls.clear()

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
        from orchestrator.planner import _render_call

        return [_render_call(c) for c in getattr(self.reader, "calls", []) or []]

    def _tool_counts(self) -> dict[str, int]:
        """The same ledger, counted by tool, for the run log's one-line summary.

        From `call.tool` rather than from the rendered strings. Taking the name
        off the front of `_render_call`'s output would work today and is the
        move this codebase has been burned by twice — a value derived from
        rendered text stops being derivable the moment the rendering changes,
        and nothing fails when it does.
        """
        from orchestrator.repotools import count_calls

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
            from orchestrator.planner import _render_call

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

        extra: dict = {
            "prompt_cache_options": {"mode": "explicit"},
            **_reasoning_param(self.cfg),
        }
        if cache_key:
            extra["prompt_cache_key"] = cache_key

        tools = openai_tool_schemas(self.semantic) if self.reader else []
        conversation = list(messages)
        usage = TokenUsage()
        response = None
        # The ledger is cumulative, so each turn reports only what it added.
        logged = len(getattr(self.reader, "calls", []) or [])

        # One turn per tool round trip, plus one for the answer.
        for _ in range(self._max_tool_turns() + 1):
            try:
                response = with_provider_retry(
                    lambda: self._client.responses.parse(
                        model=self.cfg.model,
                        input=conversation,
                        text_format=ReviewVerdict,
                        **({"tools": tools} if tools else {}),
                        **extra,
                    ),
                    retry_on=_transport_errors(),
                    transient=Backoff(
                        budget_seconds=self.cfg.transport_retry_seconds,
                        max_delay_seconds=self.cfg.transport_retry_max_delay_seconds,
                    ),
                    spurious=Backoff(
                        budget_seconds=self.cfg.invalid_request_retry_seconds,
                        initial_seconds=self.cfg.invalid_request_initial_seconds,
                        factor=self.cfg.invalid_request_factor,
                    ),
                    log=self.log,
                )
            except Exception as e:  # noqa: BLE001 - any failure means "no verdict"
                outcome = _blocked(f"The reviewer call failed: {e}")
                outcome.usage = usage
                outcome.tool_calls = self._looked_at()
                outcome.tool_counts = self._tool_counts()
                return outcome

            usage = _merge_usage(usage, _extract_usage(getattr(response, "usage", None)))

            requests = [
                item
                for item in (getattr(response, "output", None) or [])
                if getattr(item, "type", "") == "function_call"
            ]
            if not requests:
                break

            # The whole turn back, then its results. Every output item, not
            # just the calls: a reasoning model emits a `reasoning` item that
            # each `function_call` declares as required, and echoing the call
            # without it is rejected — "was provided without its required
            # 'reasoning' item". The API then requires each call to be answered
            # by a `function_call_output` with the same `call_id` before the
            # next turn.
            conversation.extend(getattr(response, "output", None) or [])
            for item in requests:
                name, args = _tool_request(item)
                conversation.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        # A content list rather than a bare string, so the
                        # result can carry a cache breakpoint. Marks accumulate
                        # rather than move: a request writes only its latest
                        # four, but matching considers up to the latest eighty
                        # in the conversation, so every turn extends the cached
                        # prefix instead of restarting it. Without this the
                        # loop re-sends every earlier result at full price and
                        # cost grows with the square of the turn count — the
                        # planner measured 48k uncached tokens for a decision
                        # making no tool calls against 1.4M for one making
                        # sixteen.
                        "output": [
                            {
                                "type": "input_text",
                                "text": dispatch(
                                    name, args, self.reader, self.semantic
                                ),
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                )
            # After the batch has run, before the next request goes out.
            logged = self._log_new_calls(logged)

        if response is None:  # pragma: no cover - the loop always runs once
            return _blocked("The reviewer produced no response.")

        refusal = _refusal(response)
        if refusal:
            outcome = _blocked(f"The reviewer refused to answer: {refusal}")
            outcome.usage = usage
            outcome.tool_calls = self._looked_at()
            outcome.tool_counts = self._tool_counts()
            return outcome

        if getattr(response, "status", None) == "incomplete":
            # A verdict cut off mid-JSON is not a verdict, even if the parsed
            # fragment happens to validate.
            reason = getattr(
                getattr(response, "incomplete_details", None), "reason", "unknown"
            )
            outcome = _blocked(
                f"The reviewer's response was truncated ({reason}), so its "
                "verdict cannot be trusted."
            )
            outcome.usage = usage
            outcome.tool_calls = self._looked_at()
            outcome.tool_counts = self._tool_counts()
            return outcome

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            # Reached the turn ceiling still asking for tools, or answered with
            # nothing parsable. Either way there is no verdict, and a review
            # that ran out of turns must say so rather than look like a refusal.
            outcome = _blocked("The reviewer returned no parsable verdict.")
            outcome.usage = usage
            outcome.tool_calls = self._looked_at()
            outcome.tool_counts = self._tool_counts()
            return outcome

        return ReviewOutcome(
            verdict=parsed.verdict,
            summary=parsed.summary,
            record=getattr(parsed, "record", "") or "",
            issues=list(parsed.issues),
            observations=list(getattr(parsed, "observations", None) or []),
            usage=usage,
            failed=False,
            tool_calls=self._looked_at(),
            tool_counts=self._tool_counts(),
        )


def make_reviewer(
    cfg: ReviewerConfig, target_repo=None, log=None
) -> ReviewerClient:
    """The pluggable seam. Config validation already restricts the provider,
    so this only has to map it.

    `target_repo` is optional so preflight can build a client just to prove the
    credentials work, without needing a repo on hand — the same reason
    `make_planner` takes it that way.
    """
    if cfg.provider != "openai":
        raise RuntimeError(f"unsupported reviewer provider {cfg.provider!r}")

    reader = semantic = None
    if cfg.repo_access and target_repo is not None:
        from orchestrator.gitops import Git
        from orchestrator.repotools import ReadBudget, RepoReader
        from orchestrator.semantic import SemanticSearch, SemanticSearchConfig

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
            semantic = SemanticSearch(search_cfg, calls=reader.calls)

    return OpenAIReviewer(cfg, log=log, reader=reader, semantic=semantic)


def _build_openai_client(cfg: ReviewerConfig):
    from openai import OpenAI

    if cfg.api_key_env not in os.environ:
        raise RuntimeError(
            f"reviewer.api_key_env names {cfg.api_key_env}, which is not set "
            "in the environment"
        )

    return OpenAI(
        api_key=os.environ[cfg.api_key_env],
        base_url=cfg.resolve_api_base(),
        timeout=cfg.request_timeout_seconds,
        max_retries=cfg.max_retries,
    )


def issues_as_feedback(summary: str, issues: list[Issue]) -> str:
    """Render a verdict as one feedback item for the next executor prompt."""
    if not issues:
        return summary
    listed = "\n".join(
        f"- [{i.severity}] {i.file}: {i.description}" for i in issues
    )
    return f"{summary}\n{listed}"
