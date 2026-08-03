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
    status="completed",
    usage=SimpleNamespace(
        input_tokens=1000,
        output_tokens=50,
        input_tokens_details=SimpleNamespace(cached_tokens=900, cache_write_tokens=0),
    ),
):
    """A finished Responses-API answer.

    `output_parsed` is where the SDK puts the validated model; `output` still
    carries the message, because a refusal lives in its content parts rather
    than in a field of its own.
    """
    content = []
    if refusal is not None:
        content.append(SimpleNamespace(type="refusal", refusal=refusal))
    return SimpleNamespace(
        output_parsed=parsed,
        output=[SimpleNamespace(type="message", content=content)],
        status=status,
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        usage=usage,
    )


class StubClient:
    """Stands in for `openai.OpenAI`."""

    def __init__(self, result):
        self._result = result
        self.calls = []
        self.responses = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class SequenceClient(StubClient):
    """One scripted result per call, so a retry can be observed."""

    def __init__(self, results):
        super().__init__(None)
        self._results = list(results)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


MESSAGES = [
    {"role": "system", "content": "you are a reviewer"},
    {"role": "user", "content": "stable prefix"},
    {"role": "user", "content": "the diff"},
]


def tool_response(name, arguments, call_id="call_1"):
    """A turn that asks for one tool instead of answering.

    On the Responses API a tool request is an item in `output` with type
    `function_call`, carrying `name` and `arguments` flat rather than nested.
    """
    call = SimpleNamespace(
        type="function_call",
        id="fc_1",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )
    return SimpleNamespace(
        output_parsed=None,
        output=[call],
        status="completed",
        incomplete_details=None,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=10,
            input_tokens_details=SimpleNamespace(cached_tokens=90, cache_write_tokens=0),
        ),
    )


class StubReader:
    """Stands in for `RepoReader`. Records what it was asked for."""

    def __init__(self, text="file contents"):
        self.text = text
        self.asked = []
        self.calls = []

    def read_file(self, path, start=None, end=None):
        self.asked.append(path)
        return self.text


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
        client = StubClient(response(parsed=verdict, status="incomplete"))
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

    def test_an_empty_response_becomes_blocked(self):
        # Nothing parsed and nothing in output. Whatever produced it, there is
        # no verdict, and a run must stop rather than infer one.
        client = StubClient(
            SimpleNamespace(
                output_parsed=None, output=[], status="completed",
                incomplete_details=None, usage=None,
            )
        )
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
        # Verbatim. The breakpoints are placed once by `build_review_messages`;
        # the client must not rewrite what it was handed, because a marker that
        # moved between turns would be a different prefix every time.
        assert client.calls[0]["input"] == MESSAGES

    def test_no_tools_are_sent_without_repo_access(self):
        # The default. A reviewer with no reader must make the same request it
        # always did, or every project without repo access pays for a shape it
        # cannot use.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert "tools" not in client.calls[0]

    def test_configured_model_is_used(self):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(cfg_with(model="gpt-5.4-mini").reviewer, client=client).review(
            MESSAGES
        )
        assert client.calls[0]["model"] == "gpt-5.4-mini"

    def test_the_verdict_schema_is_requested(self):
        # Server-side enforcement, rather than hoping the shape comes back.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert client.calls[0]["text_format"] is ReviewVerdict


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

    def test_retention_is_never_sent(self):
        # `prompt_cache_retention` is a chat-completions field. On the
        # Responses API the lifetime comes from prompt_cache_options.ttl,
        # which is fixed at 30m and is currently the only supported value —
        # so there is nothing here for an operator to choose.
        client = StubClient(response(parsed=ReviewVerdict(verdict="approved", summary="ok", issues=[])))
        OpenAIReviewer(cfg_with().reviewer, client=client).review(MESSAGES)
        assert "prompt_cache_retention" not in client.calls[0]


class TestCacheWriteAccounting:
    """Writes are billed above base rate and were not being counted.

    Measured on gpt-5.6-sol: six calls with an identical ~55k-token prefix,
    every one reporting cache_write_tokens ~55,500 and cached_tokens 0. The
    report showed "0 cached, 0%", which reads as "caching is not helping" when
    the truth is "caching is actively costing extra".
    """

    def test_the_write_count_is_read_from_the_details(self):
        from orchestrator.reviewer import _extract_usage

        class Details:
            cached_tokens = 0
            cache_write_tokens = 55_500

        class Usage:
            prompt_tokens = 55_503
            completion_tokens = 200
            prompt_tokens_details = Details()

        usage = _extract_usage(Usage())
        assert usage.cache_write_tokens == 55_500
        assert usage.cached_tokens == 0

    def test_a_provider_that_omits_it_is_zero(self):
        from orchestrator.reviewer import _extract_usage

        class Details:
            cached_tokens = 100

        class Usage:
            prompt_tokens = 1000
            completion_tokens = 10
            prompt_tokens_details = Details()

        assert _extract_usage(Usage()).cache_write_tokens == 0


class TestExplicitCacheMode:
    """The breakpoint in the prompt only counts if the request opts in.

    GPT-5.6's default is `implicit`, which places a breakpoint on the latest
    message and ignores ours. Both halves are needed: the marked block, and
    mode="explicit" on the request.
    """

    def call(self, **over):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(cfg_with(**over.pop("cfg", {})).reviewer, client=client).review(
            MESSAGES, **over
        )
        return client.calls[0]

    def test_explicit_mode_is_requested(self):
        assert self.call(cache_key="k")["prompt_cache_options"] == {"mode": "explicit"}

    def test_the_cache_key_is_still_sent(self):
        # GPT-5.6 needs it for reliable matching, not merely as a hint.
        sent = self.call(cache_key="orchestrator:proj")
        assert sent["prompt_cache_key"] == "orchestrator:proj"

    def test_retention_is_not_sent_by_default(self):
        # Deprecated on GPT-5.6 in favour of prompt_cache_options.ttl, and
        # sending a deprecated parameter alongside its replacement invites the
        # kind of silent misbehaviour this whole area just cost us.
        assert "prompt_cache_retention" not in self.call()


class TestOutagesAreWaitedOutNotEscalated:
    """The reviewer died to the same disconnection, two seconds apart.

    Same reasoning as the planner's: bounded by wall clock rather than by a
    retry count, and out loud, because SDK retries log at DEBUG where `run.log`
    never sees them and a silent fifteen-minute wait is indistinguishable from
    a hang.
    """

    def _connection_error(self):
        import httpx
        from openai import APIConnectionError

        return APIConnectionError(request=httpx.Request("POST", "https://x/y"))

    def test_a_connection_error_is_retried(self):
        verdict = ReviewVerdict(verdict="approved", summary="Fine.", issues=[])
        client = SequenceClient([self._connection_error(), response(parsed=verdict)])
        out = OpenAIReviewer(
            cfg_with(transport_retry_seconds=0.01).reviewer, client=client
        ).review(MESSAGES)
        assert out.verdict == "approved"
        assert len(client.calls) == 2

    def test_it_blocks_once_the_budget_is_spent(self):
        client = StubClient(self._connection_error())
        out = OpenAIReviewer(
            cfg_with(transport_retry_seconds=0.01).reviewer, client=client
        ).review(MESSAGES)
        assert out.verdict == "blocked"
        assert "Connection error" in out.summary

    def test_a_refusal_is_not_retried(self):
        client = StubClient(response(parsed=None, refusal="I will not."))
        out = OpenAIReviewer(
            cfg_with(transport_retry_seconds=900).reviewer, client=client
        ).review(MESSAGES)
        assert out.verdict == "blocked"
        assert len(client.calls) == 1


class TestToolLoop:
    """A reviewer that can look at the repository.

    The reason this exists: a stage that deletes an `attr_accessible`
    declaration is safe exactly when a permit list elsewhere covers the same
    attributes, and that file is not in the diff. Measured on one run, 8 of 31
    stages had that shape and every one was approved by a reviewer with no way
    to check. These tests pin the loop that fixed it.
    """

    def test_a_tool_request_is_answered_and_the_loop_continues(self):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        reader = StubReader("def discount_params\n  permit(:title)\nend")
        client = SequenceClient(
            [
                tool_response("read_file", '{"path": "app/x.rb"}'),
                response(parsed=verdict),
            ]
        )
        out = OpenAIReviewer(
            cfg_with().reviewer, client=client, reader=reader
        ).review(MESSAGES)

        assert out.verdict == "approved"
        assert reader.asked == ["app/x.rb"]
        # The second request carries the assistant turn and the tool reply.
        second = client.calls[1]["input"]
        assert second[-1]["type"] == "function_call_output"
        assert second[-1]["call_id"] == "call_1"
        assert "discount_params" in second[-1]["output"][0]["text"]

    def test_a_tool_result_carries_a_cache_breakpoint(self):
        # Marks accumulate rather than move: a request writes only its latest
        # four, but matching considers up to eighty in the conversation, so
        # every turn extends the cached prefix instead of restarting it.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = SequenceClient(
            [
                tool_response("read_file", '{"path": "a.rb"}'),
                response(parsed=verdict),
            ]
        )
        OpenAIReviewer(
            cfg_with().reviewer, client=client, reader=StubReader()
        ).review(MESSAGES)
        result = client.calls[1]["input"][-1]
        assert result["output"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}

    def test_tools_are_offered_when_a_reader_is_present(self):
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = StubClient(response(parsed=verdict))
        OpenAIReviewer(
            cfg_with().reviewer, client=client, reader=StubReader()
        ).review(MESSAGES)
        names = [t["name"] for t in client.calls[0]["tools"]]
        assert "read_file" in names
        assert all(t["type"] == "function" for t in client.calls[0]["tools"])
        # Strict is not optional: the SDK refuses to auto-parse a
        # structured response alongside non-strict function tools.
        assert all(t["strict"] for t in client.calls[0]["tools"])

    def test_what_it_looked_at_is_recorded(self):
        # A verdict reached without reading is worth less than one reached
        # after it, and the two are indistinguishable from the verdict alone.
        verdict = ReviewVerdict(verdict="rework", summary="no", issues=[])
        client = SequenceClient(
            [
                tool_response("read_file", '{"path": "app/models/cart.rb"}'),
                response(parsed=verdict),
            ]
        )
        out = OpenAIReviewer(
            cfg_with().reviewer, client=client, reader=StubReader()
        ).review(MESSAGES)
        assert out.tool_calls == ["read_file(app/models/cart.rb)"]
        assert out.as_dict()["tool_calls"] == ["read_file(app/models/cart.rb)"]

    def test_usage_is_summed_across_turns(self):
        # A tool loop bills once per turn. Reporting only the last one
        # understates what a review cost by however many times it looked.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = SequenceClient(
            [
                tool_response("read_file", '{"path": "a.rb"}'),
                response(parsed=verdict),
            ]
        )
        out = OpenAIReviewer(
            cfg_with().reviewer, client=client, reader=StubReader()
        ).review(MESSAGES)
        assert out.usage.prompt_tokens == 1100  # 100 from the tool turn + 1000
        assert out.usage.cached_tokens == 990  # 90 + 900

    def test_unparsable_tool_arguments_do_not_end_the_review(self):
        # A bad request is not a dead review: the refusal reaches the model as
        # a readable result and it can answer with what it has.
        verdict = ReviewVerdict(verdict="approved", summary="ok", issues=[])
        client = SequenceClient(
            [
                tool_response("read_file", "{not json"),
                response(parsed=verdict),
            ]
        )
        out = OpenAIReviewer(
            cfg_with().reviewer, client=client, reader=StubReader()
        ).review(MESSAGES)
        assert out.verdict == "approved"
        assert not out.failed

    def test_a_loop_that_never_answers_blocks_rather_than_hangs(self):
        # The turn ceiling is the backstop for a model that ignores the
        # reader's refusal and keeps asking.
        cfg = cfg_with(max_read_calls=2).reviewer
        client = SequenceClient(
            [tool_response("read_file", '{"path": "a.rb"}') for _ in range(6)]
        )
        out = OpenAIReviewer(cfg, client=client, reader=StubReader()).review(MESSAGES)
        assert out.verdict == "blocked"
        assert out.failed
        # Bounded: turns + 1, not until the transport gives up.
        assert len(client.calls) == 3

    def test_a_transport_failure_mid_loop_keeps_what_it_saw(self):
        client = SequenceClient(
            [
                tool_response("read_file", '{"path": "a.rb"}'),
                RuntimeError("connection reset"),
            ]
        )
        out = OpenAIReviewer(
            cfg_with().reviewer, client=client, reader=StubReader()
        ).review(MESSAGES)
        assert out.verdict == "blocked"
        assert out.failed
        assert out.tool_calls == ["read_file(a.rb)"]
        # The first turn's tokens were still spent and must still be reported.
        assert out.usage.prompt_tokens == 100
