"""The reviewer client.

Review needs no tool access, so this is a direct SDK call rather than an
agentic wrapper. The provider sits behind a small protocol; OpenAI is the
only implementation, using the SDK's native structured-output parsing so the
schema is enforced server-side instead of hoped for.

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

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "summary": self.summary,
            "issues": [i.model_dump() for i in self.issues],
            "usage": {
                "prompt_tokens": self.usage.prompt_tokens,
                "cached_tokens": self.usage.cached_tokens,
                "completion_tokens": self.usage.completion_tokens,
            },
            "client_failure": self.failed,
        }


class ReviewerClient(Protocol):
    def review(self, messages: list[dict[str, str]]) -> ReviewOutcome: ...


def _blocked(reason: str) -> ReviewOutcome:
    return ReviewOutcome(verdict="blocked", summary=reason, failed=True)


class OpenAIReviewer:
    def __init__(self, cfg: ReviewerConfig, client=None):
        self.cfg = cfg
        self._client = client if client is not None else _build_openai_client(cfg)

    def review(self, messages: list[dict[str, str]]) -> ReviewOutcome:
        try:
            completion = self._client.chat.completions.parse(
                model=self.cfg.model,
                messages=messages,
                response_format=ReviewVerdict,
            )
        except Exception as e:  # noqa: BLE001 - any failure means "no verdict"
            return _blocked(f"The reviewer call failed: {e}")

        usage = _extract_usage(getattr(completion, "usage", None))

        choices = getattr(completion, "choices", None) or []
        if not choices:
            outcome = _blocked("The reviewer returned no choices.")
            outcome.usage = usage
            return outcome

        choice = choices[0]
        message = choice.message

        if getattr(message, "refusal", None):
            outcome = _blocked(f"The reviewer refused to answer: {message.refusal}")
            outcome.usage = usage
            return outcome

        if getattr(choice, "finish_reason", None) == "length":
            # A verdict cut off mid-JSON is not a verdict, even if the parsed
            # fragment happens to validate.
            outcome = _blocked(
                "The reviewer's response was truncated (finish_reason=length), "
                "so its verdict cannot be trusted."
            )
            outcome.usage = usage
            return outcome

        parsed = getattr(message, "parsed", None)
        if parsed is None:
            outcome = _blocked("The reviewer returned no parsable verdict.")
            outcome.usage = usage
            return outcome

        return ReviewOutcome(
            verdict=parsed.verdict,
            summary=parsed.summary,
            issues=list(parsed.issues),
            usage=usage,
            failed=False,
        )


def make_reviewer(cfg: ReviewerConfig) -> ReviewerClient:
    """The pluggable seam. Config validation already restricts the provider,
    so this only has to map it."""
    if cfg.provider == "openai":
        return OpenAIReviewer(cfg)
    raise RuntimeError(f"unsupported reviewer provider {cfg.provider!r}")


def _build_openai_client(cfg: ReviewerConfig):
    from openai import OpenAI

    if cfg.api_key_env not in os.environ:
        raise RuntimeError(
            f"reviewer.api_key_env names {cfg.api_key_env}, which is not set "
            "in the environment"
        )

    return OpenAI(
        api_key=os.environ[cfg.api_key_env],
        base_url=cfg.api_base,
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
    )


def issues_as_feedback(summary: str, issues: list[Issue]) -> str:
    """Render a verdict as one feedback item for the next executor prompt."""
    if not issues:
        return summary
    listed = "\n".join(
        f"- [{i.severity}] {i.file}: {i.description}" for i in issues
    )
    return f"{summary}\n{listed}"
