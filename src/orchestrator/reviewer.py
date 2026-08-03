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

import json
import os
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel

from orchestrator.config import ReviewerConfig
from orchestrator.plannertools import dispatch, openai_tool_schemas
from orchestrator.retry import Backoff, with_transport_retry

Verdict = Literal["approved", "rework", "blocked"]


class Issue(BaseModel):
    severity: Literal["major", "minor"]
    file: str
    description: str


class ReviewVerdict(BaseModel):
    """The schema the reviewer is constrained to return."""

    verdict: Verdict
    summary: str
    issues: list[Issue]


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    # Last, so positional construction keeps working. Written to cache but not
    # read back: billed above base rate, so a run that writes on every call and
    # reads on none is paying a premium for nothing — which is exactly what
    # gpt-5.6-sol was measured doing, six calls, ~55k written each, zero read.
    cache_write_tokens: int = 0

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(self.prompt_tokens - self.cached_tokens, 0)


@dataclass
class ReviewOutcome:
    verdict: Verdict
    summary: str
    issues: list[Issue] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    # True when the `blocked` verdict is ours rather than the model's, so the
    # report does not imply a judgement the reviewer never made.
    failed: bool = False
    # What it looked at, in order. Recorded for the same reason the planner's
    # is: a verdict reached without reading is worth less than one reached
    # after it, and the two are indistinguishable from the verdict alone.
    tool_calls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "issues": [i.model_dump() for i in self.issues],
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


def _transport_errors() -> tuple[type[BaseException], ...]:
    """Exception types meaning the request never arrived.

    `APITimeoutError` subclasses `APIConnectionError`, so one entry covers
    both. Resolved lazily and degrading to no retrying, matching how the
    SDK is imported everywhere else here.
    """
    try:
        from openai import APIConnectionError
    except ImportError:  # pragma: no cover - the SDK is a hard dependency
        return ()
    return (APIConnectionError,)


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
        self._client = client if client is not None else _build_openai_client(cfg)

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
        extra: dict = {"prompt_cache_options": {"mode": "explicit"}}
        if cache_key:
            extra["prompt_cache_key"] = cache_key
        if self.cfg.prompt_cache_retention:
            extra["prompt_cache_retention"] = self.cfg.prompt_cache_retention

        tools = openai_tool_schemas(self.semantic) if self.reader else []
        conversation = list(messages)
        usage = TokenUsage()
        looked_at: list[str] = []
        completion = None

        # One turn per tool round trip, plus one for the answer.
        for _ in range(self._max_tool_turns() + 1):
            try:
                completion = with_transport_retry(
                    lambda: self._client.chat.completions.parse(
                        model=self.cfg.model,
                        # No moving breakpoint. `build_review_messages` marks
                        # the end of the diff, which does not move during the
                        # loop, so from the second turn everything up to it
                        # reads from cache and only the accumulating tool
                        # results are fresh. Marking the newest message instead
                        # would mean attaching a breakpoint to a `tool` message,
                        # a shape this provider is not known to accept — and
                        # documented behaviour here has been wrong twice.
                        messages=conversation,
                        response_format=ReviewVerdict,
                        **({"tools": tools} if tools else {}),
                        **extra,
                    ),
                    retry_on=_transport_errors(),
                    backoff=Backoff(
                        budget_seconds=self.cfg.transport_retry_seconds,
                        max_delay_seconds=self.cfg.transport_retry_max_delay_seconds,
                    ),
                    log=self.log,
                )
            except Exception as e:  # noqa: BLE001 - any failure means "no verdict"
                outcome = _blocked(f"The reviewer call failed: {e}")
                outcome.usage = usage
                outcome.tool_calls = looked_at
                return outcome

            usage = _merge_usage(usage, _extract_usage(getattr(completion, "usage", None)))

            choices = getattr(completion, "choices", None) or []
            if not choices:
                outcome = _blocked("The reviewer returned no choices.")
                outcome.usage = usage
                outcome.tool_calls = looked_at
                return outcome

            requests = getattr(choices[0].message, "tool_calls", None) or []
            if not requests:
                break

            # The assistant turn verbatim, then one `tool` message per request.
            # The API requires every tool call to be answered before the next
            # assistant turn, keyed by id, or the conversation is malformed.
            conversation = conversation + [choices[0].message]
            for req in requests:
                name, args = _tool_request(req)
                looked_at.append(_describe(name, args))
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": req.id,
                        "content": dispatch(name, args, self.reader, self.semantic),
                    }
                )

        if completion is None:  # pragma: no cover - the loop always runs once
            return _blocked("The reviewer produced no response.")

        choices = getattr(completion, "choices", None) or []
        if not choices:
            outcome = _blocked("The reviewer returned no choices.")
            outcome.usage = usage
            outcome.tool_calls = looked_at
            return outcome

        choice = choices[0]
        message = choice.message

        if getattr(message, "refusal", None):
            outcome = _blocked(f"The reviewer refused to answer: {message.refusal}")
            outcome.usage = usage
            outcome.tool_calls = looked_at
            return outcome

        if getattr(choice, "finish_reason", None) == "length":
            # A verdict cut off mid-JSON is not a verdict, even if the parsed
            # fragment happens to validate.
            outcome = _blocked(
                "The reviewer's response was truncated (finish_reason=length), "
                "so its verdict cannot be trusted."
            )
            outcome.usage = usage
            outcome.tool_calls = looked_at
            return outcome

        parsed = getattr(message, "parsed", None)
        if parsed is None:
            # Reached the turn ceiling still asking for tools, or answered with
            # nothing parsable. Either way there is no verdict, and a review
            # that ran out of turns must say so rather than look like a refusal.
            outcome = _blocked("The reviewer returned no parsable verdict.")
            outcome.usage = usage
            outcome.tool_calls = looked_at
            return outcome

        return ReviewOutcome(
            verdict=parsed.verdict,
            summary=parsed.summary,
            issues=list(parsed.issues),
            usage=usage,
            failed=False,
            tool_calls=looked_at,
        )


def _tool_request(req) -> tuple[str, dict]:
    """Name and arguments from one OpenAI tool call.

    Arguments arrive as a JSON *string* rather than an object, and a model can
    emit one that does not parse. That is a bad request, not a dead review — an
    empty dict reaches `dispatch`, which answers with a readable refusal the
    reviewer can act on.
    """
    fn = getattr(req, "function", None)
    name = getattr(fn, "name", "") or ""
    raw = getattr(fn, "arguments", "") or "{}"
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return name, {}
    return name, args if isinstance(args, dict) else {}


def _describe(name: str, args: dict) -> str:
    """One tool call, rendered for the log and the artifact."""
    detail = args.get("path") or args.get("pattern") or args.get("glob") or args.get(
        "question"
    ) or args.get("ref") or ""
    return f"{name}({detail})" if detail else name


def _merge_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """Totals across the turns of one review.

    A tool loop bills once per turn, so the single-call reading understates what
    a review cost by however many times it looked at something. Summing here is
    what keeps `report.md` honest — the economic argument for splitting the
    models depends on that number staying true.
    """
    return TokenUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        cached_tokens=left.cached_tokens + right.cached_tokens,
        cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
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


def _extract_usage(usage) -> TokenUsage:
    """Read what the provider reported, tolerating absent fields.

    Cached-token reporting is not guaranteed across models or providers, and a
    missing field must not take down a run.
    """
    if usage is None:
        return TokenUsage()

    details = getattr(usage, "prompt_tokens_details", None)
    return TokenUsage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        cached_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
        cache_write_tokens=(
            (getattr(details, "cache_write_tokens", 0) or 0) if details else 0
        ),
    )


def issues_as_feedback(summary: str, issues: list[Issue]) -> str:
    """Render a verdict as one feedback item for the next executor prompt."""
    if not issues:
        return summary
    listed = "\n".join(
        f"- [{i.severity}] {i.file}: {i.description}" for i in issues
    )
    return f"{summary}\n{listed}"
