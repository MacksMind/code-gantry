"""The reviewer client.

Schema enforcement makes a malformed verdict unlikely, so most of the weight
here is on what happens when there is no usable verdict at all. "The reviewer
did not answer" must mean `blocked` — stop and ask a human — never a guess and
never a crash mid-run.
"""

from types import SimpleNamespace

import pytest

from orchestrator.config import parse_config
from orchestrator.reviewer import (
    Issue,
    OpenAIReviewer,
    ReviewVerdict,
    issues_as_feedback,
    make_reviewer,
)


def cfg_with(**reviewer_overrides):
    reviewer = {"model": "gpt-5.5"}
    reviewer.update(reviewer_overrides)
    return parse_config(
        {
            "target_repo": "/tmp/x",
            "project_branch": "work",
            "plan_root": "PLAN.md",
            "test_command": "pytest",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": reviewer,
        }
    )


def response(
    parsed=None,
    refusal=None,
    finish_reason="stop",
    usage=SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=900),
    ),
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(parsed=parsed, refusal=refusal),
            )
        ],
        usage=usage,
    )


class StubClient:
    """Stands in for `openai.OpenAI`."""

    def __init__(self, result):
        self._result = result
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


MESSAGES = [
    {"role": "system", "content": "you are a reviewer"},
    {"role": "user", "content": "stable prefix"},
    {"role": "user", "content": "the diff"},
]


class TestVerdicts:
    def test_approved(self):
        verdict = ReviewVerdict(verdict="approved", summary="Looks right.", issues=[])
        client = StubClient(response(parsed=verdict))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "approved"
        assert out.summary == "Looks right."

    def test_rework_carries_issues(self):
        verdict = ReviewVerdict(
            verdict="rework",
            summary="One problem.",
            issues=[Issue(severity="major", file="app/x.rb", description="Wrong verb.")],
        )
        client = StubClient(response(parsed=verdict))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "rework"
        assert out.issues[0].file == "app/x.rb"

    def test_blocked(self):
        verdict = ReviewVerdict(
            verdict="blocked", summary="Instruction is impossible.", issues=[]
        )
        client = StubClient(response(parsed=verdict))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "blocked"


class TestDefensiveHandling:
    def test_refusal_becomes_blocked(self):
        client = StubClient(response(parsed=None, refusal="I will not."))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "blocked"
        assert "refus" in out.summary.lower()

    def test_truncated_response_becomes_blocked(self):
        # A verdict cut off mid-JSON is not a verdict. Advancing on a partial
        # review is worse than stopping.
        verdict = ReviewVerdict(verdict="approved", summary="part", issues=[])
        client = StubClient(response(parsed=verdict, finish_reason="length"))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "blocked"
        assert "truncat" in out.summary.lower() or "length" in out.summary.lower()

    def test_missing_parsed_payload_becomes_blocked(self):
        client = StubClient(response(parsed=None))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "blocked"

    def test_api_exception_becomes_blocked_not_a_crash(self):
        # A transport failure on stage 4 of 6 must not lose the run.
        client = StubClient(RuntimeError("connection reset"))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "blocked"
        assert "connection reset" in out.summary

    def test_no_choices_becomes_blocked(self):
        client = StubClient(SimpleNamespace(choices=[], usage=None))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.verdict == "blocked"

    def test_a_blocked_fallback_is_marked_as_not_from_the_model(self):
        # The report should not imply the reviewer made a judgement it did not.
        client = StubClient(RuntimeError("boom"))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.failed is True

    def test_a_real_verdict_is_not_marked_failed(self):
        verdict = ReviewVerdict(verdict="blocked", summary="genuine", issues=[])
        client = StubClient(response(parsed=verdict))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.failed is False


class TestUsageAccounting:
    def test_records_token_counts(self):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.usage.prompt_tokens == 1000
        assert out.usage.completion_tokens == 50

    def test_records_cached_tokens(self):
        # The economic argument for the split depends on this being visible.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.usage.cached_tokens == 900

    def test_absent_usage_does_not_crash(self):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict, usage=None))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.usage.prompt_tokens == 0

    def test_absent_cached_token_detail_does_not_crash(self):
        # Not every provider or model reports this.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2)
        client = StubClient(response(parsed=verdict, usage=usage))
        out = OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert out.usage.cached_tokens == 0


class TestRequestShape:
    def test_messages_are_passed_through_in_order(self):
        # Reordering would break prefix caching.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert client.calls[0]["messages"] == MESSAGES

    def test_configured_model_is_used(self):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(cfg_with(model="gpt-5.4-mini").reviewer, client=client).review(
            MESSAGES
        )
        assert client.calls[0]["model"] == "gpt-5.4-mini"

    def test_response_format_requests_the_verdict_schema(self):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert client.calls[0]["response_format"] is ReviewVerdict


class TestFactory:
    def test_openai_provider_builds_an_openai_reviewer(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        assert isinstance(make_reviewer(cfg_with().reviewer), OpenAIReviewer)

    def test_missing_api_key_is_reported_clearly(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as e:
            make_reviewer(cfg_with().reviewer)
        assert "OPENAI_API_KEY" in str(e.value)


class TestIssuesAsFeedback:
    def test_formats_issues_for_the_executor(self):
        issues = [
            Issue(severity="major", file="app/x.rb", description="Wrong verb."),
            Issue(severity="minor", file="app/y.rb", description="Naming."),
        ]
        text = issues_as_feedback("Two problems.", issues)
        assert "app/x.rb" in text
        assert "Wrong verb." in text
        assert "Naming." in text

    def test_includes_severity(self):
        issues = [Issue(severity="major", file="a", description="d")]
        assert "major" in issues_as_feedback("s", issues)

    def test_summary_alone_when_there_are_no_issues(self):
        assert "Just wrong." in issues_as_feedback("Just wrong.", [])


class TestReviewerCacheControls:
    """OpenAI caches automatically, but not unconditionally.

    A cache key groups a run's reviews so they route to the same cache rather
    than competing for one, and retention decides whether the prefix survives
    the minutes a full Rails suite takes between two reviews. Verified against
    the live API before being wired in — both fields are accepted.
    """

    def test_a_cache_key_is_sent(self):
        client = StubClient(response(parsed=ReviewVerdict(verdict="approved", summary="ok", issues=[])))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(
            MESSAGES, cache_key="proj-slug"
        )
        assert client.calls[0]["prompt_cache_key"] == "proj-slug"

    def test_no_cache_key_sends_no_field(self):
        client = StubClient(response(parsed=ReviewVerdict(verdict="approved", summary="ok", issues=[])))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert "prompt_cache_key" not in client.calls[0]

    def test_retention_is_sent_when_configured(self):
        client = StubClient(response(parsed=ReviewVerdict(verdict="approved", summary="ok", issues=[])))
        OpenAIReviewer(
            cfg_with(prompt_cache_retention="24h").reviewer, client=client
        ).review(MESSAGES)
        assert client.calls[0]["prompt_cache_retention"] == "24h"

    def test_retention_is_absent_by_default(self):
        # Extended retention stores the prefix for longer, which is a data
        # policy decision the operator makes, not a default we impose.
        client = StubClient(response(parsed=ReviewVerdict(verdict="approved", summary="ok", issues=[])))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert "prompt_cache_retention" not in client.calls[0]
