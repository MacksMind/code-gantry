"""The chat-completions wire, and the roles speaking through whichever wire fits.

Measured 2026-09-11 against OpenRouter: an Anthropic model on the Messages
wire has its schema dropped in silence and any tools block refused with 404;
on chat completions the schema, strict tools, effort and a one-hour cache
marker all reach the model and the cache reads back on the second call.
"""

import inspect
from types import SimpleNamespace

import pytest

from code_gantry.config import PlannerConfig, ReviewerConfig
from code_gantry.dialects import (
    CHAT,
    MESSAGES,
    RESPONSES,
    dialect_for,
    request_extras,
    wire_for,
)

OPENROUTER = "https://openrouter.ai/api/v1"


class TestTheRouteDecidesTheWire:
    def test_an_anthropic_model_through_openrouter_goes_on_chat(self):
        assert dialect_for("anthropic/claude-fable-5.1", api_base=OPENROUTER) is CHAT

    def test_the_same_model_first_party_stays_on_messages(self):
        assert dialect_for("claude-fable-5-1") is MESSAGES
        assert dialect_for("claude-fable-5-1", api_base="https://api.anthropic.com") is MESSAGES

    def test_gemini_through_openrouter_stays_on_messages(self):
        assert dialect_for("google/gemini-3", api_base=OPENROUTER) is MESSAGES

    def test_openai_through_openrouter_stays_on_responses(self):
        assert dialect_for("openai/gpt-5.6-sol", api_base=OPENROUTER) is RESPONSES

    def test_an_operator_can_name_it(self):
        assert dialect_for("anything", "chat") is CHAT

    def test_wire_for_reads_the_config_without_raising_on_an_unset_env(self, monkeypatch):
        monkeypatch.delenv("NOT_SET_HERE", raising=False)
        cfg = PlannerConfig(model="anthropic/claude-fable-5.1", api_base_env="NOT_SET_HERE")
        assert wire_for(cfg, MESSAGES) is MESSAGES


class TestSpellings:
    def test_structured_output_is_response_format(self):
        assert list(CHAT.structured(object)) == ["response_format"]

    def test_first_party_effort_is_reasoning_effort(self):
        assert CHAT.effort("low") == {"reasoning_effort": "low"}

    def test_through_the_gateway_effort_rides_in_the_body(self):
        cfg = PlannerConfig(model="anthropic/claude-fable-5.1", api_base=OPENROUTER, effort="low")
        built = request_extras(cfg, session_id="s")
        assert "reasoning_effort" not in built
        assert built["extra_body"]["reasoning"] == {"effort": "low"}
        assert built["extra_body"]["provider"] == {"require_parameters": True}

    def test_a_text_block_carries_cache_control_with_its_ttl(self):
        block = CHAT.text_block("hi", cache=True, ttl="1h")
        assert block == {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral", "ttl": "1h"}}

    def test_a_responses_shaped_block_is_translated_with_its_mark(self):
        [block] = CHAT.normalise([{"role": "user", "content": [
            {"type": "input_text", "text": "x", "prompt_cache_breakpoint": {"mode": "explicit"}}
        ]}])[0]["content"]
        assert block == {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}

    def test_a_messages_mark_keeps_its_ttl_on_this_wire(self):
        [block] = CHAT.normalise([{"role": "system", "content": [
            {"type": "text", "text": "x", "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ]}])[0]["content"]
        assert block["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_a_ttl_is_dropped_on_the_wire_that_has_none(self):
        # RESPONSES leaves its own vocabulary alone; MESSAGES keeps a TTL.
        [block] = MESSAGES.normalise([{"role": "user", "content": [
            {"type": "input_text", "text": "x", "prompt_cache_breakpoint": {"mode": "explicit"}}
        ]}])[0]["content"]
        assert block["cache_control"] == {"type": "ephemeral"}

    def test_the_client_base_is_v1_on_openrouter(self):
        cfg = PlannerConfig(model="anthropic/claude-fable-5.1", api_base="https://openrouter.ai/api", api_key_env=None)
        assert str(CHAT.client(cfg).base_url).rstrip("/") == OPENROUTER

    def test_tool_schemas_wrap_the_strict_function(self):
        [tool] = CHAT.tool_schemas([{"name": "read_file", "description": "Read.",
                                     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}])
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "read_file" and tool["function"]["strict"] is True
        assert tool["function"]["parameters"]["additionalProperties"] is False

    def test_no_kwarg_is_unknown_to_the_sdk(self):
        from openai import OpenAI

        cfg = PlannerConfig(model="anthropic/claude-fable-5.1", api_base=OPENROUTER, effort="high")
        built = request_extras(cfg, session_id="sess", cache_key="k")
        method = OpenAI(api_key="x").chat.completions.create
        unknown = sorted(set(built) - set(inspect.signature(method).parameters))
        assert not unknown, unknown


def _chat_reply(finish="stop", content="", tool_calls=(), parsed=None, usage=None):
    calls = [
        SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))
        for cid, name, args in tool_calls
    ]
    message = SimpleNamespace(content=content, tool_calls=calls or None, parsed=parsed,
                              refusal=None, reasoning_details=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=message)],
        usage=usage or SimpleNamespace(
            prompt_tokens=4469, completion_tokens=40, cost=0.0044,
            prompt_tokens_details=SimpleNamespace(cached_tokens=4465, cache_write_tokens=0),
        ),
    )


class TestReadingTheWire:
    def test_usage_is_read_in_its_own_shape(self):
        usage = CHAT.usage(_chat_reply().usage)
        assert (usage.prompt_tokens, usage.cached_tokens, usage.cache_write_tokens) == (4469, 4465, 0)
        assert usage.provider_cost_usd == 0.0044
        assert usage.cache_write_1h_tokens == 0, "no TTL breakdown here; zero, not the total"

    def test_a_turn_end_reads_finish_reason(self):
        end = CHAT.turn_end(_chat_reply("length", content="partial"))
        assert end.label == "max_tokens" and end.abnormal
        assert CHAT.turn_end(_chat_reply("stop", content="done")).label == "finished"
        assert CHAT.turn_end(_chat_reply("tool_calls", tool_calls=[("c1", "read_file", '{"path": "a"}')])).label == "tool_use"

    def test_tool_calls_are_read_and_echoed_in_function_shape(self):
        reply = _chat_reply("tool_calls", tool_calls=[("c1", "read_file", '{"path": "a"}')])
        assert CHAT.tool_calls(reply) == [{"id": "c1", "name": "read_file", "args": {"path": "a"}}]
        conversation = []
        CHAT.append_model_turn(conversation, reply)
        assert conversation[0]["role"] == "assistant"
        assert conversation[0]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "a"}'}

    def test_results_go_back_as_tool_messages_marking_only_the_last(self):
        conversation = []
        CHAT.append_tool_results(conversation, [("c1", "one"), ("c2", "two")], cache=True)
        assert [m["role"] for m in conversation] == ["tool", "tool"]
        assert "cache_control" not in conversation[0]["content"][0]
        assert conversation[1]["content"][0]["cache_control"] == {"type": "ephemeral"}

    def test_the_parsed_answer_is_on_the_message(self):
        assert CHAT.parsed(_chat_reply(parsed="THE VERDICT")) == "THE VERDICT"


class StubChatClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


class TestThePlannerSpeaksIt:
    def _cfg(self):
        return PlannerConfig(
            model="anthropic/claude-fable-5.1", api_base=OPENROUTER, api_key_env=None,
            effort="low", cache_ttl="1h", repo_access=False,
        )

    def test_a_chat_routed_planner_calls_chat_completions_parse(self):
        from code_gantry.planner import Planner, PlannerResponse

        parsed = PlannerResponse(verdict="project_complete", reasoning="done", status_entry="e")
        client = StubChatClient([_chat_reply("stop", content="{}", parsed=parsed)])
        outcome = Planner(self._cfg(), client=client).plan(
            [{"role": "user", "content": [{"type": "text", "text": "plan", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]}]
        )
        assert outcome.verdict == "project_complete" and not outcome.failed
        (call,) = client.calls
        assert call["response_format"] is PlannerResponse
        assert call["messages"][0]["role"] == "system"
        assert call["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        # The newest block carries the moving five-minute mark, as on Messages.
        assert call["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert call["extra_body"]["reasoning"] == {"effort": "low"}
        assert call["max_tokens"] == self._cfg().max_tokens
        assert "tools" not in call
        assert outcome.usage.cached_tokens == 4465

    def test_the_planner_on_a_first_party_model_still_calls_messages(self):
        from code_gantry.planner import Planner

        cfg = PlannerConfig(model="claude-opus-5", api_key_env=None)
        assert Planner(cfg, client=object()).wire is MESSAGES


class TestTheReviewerSpeaksIt:
    def test_a_chat_routed_reviewer_reads_the_verdict_off_the_message(self):
        from code_gantry.reviewer import Reviewer, ReviewVerdict

        cfg = ReviewerConfig(model="anthropic/claude-fable-5.1", api_base=OPENROUTER, api_key_env=None)
        verdict = ReviewVerdict(verdict="approved", summary="fine", record="did it", issues=[], resolved=[])
        client = StubChatClient([_chat_reply("stop", content="{}", parsed=verdict)])
        outcome = Reviewer(cfg, client=client).review(
            [{"role": "system", "content": [{"type": "input_text", "text": "judge"}]},
             {"role": "user", "content": [{"type": "input_text", "text": "diff"}]}],
            cache_key="k",
        )
        assert outcome.verdict == "approved" and not outcome.failed
        (call,) = client.calls
        assert call["response_format"] is ReviewVerdict
        assert call["messages"][0]["content"][0]["type"] == "text", "translated to this wire's vocabulary"
        assert outcome.turn_usage[0]["cached_tokens"] == 4465

    def test_the_reviewer_on_an_openai_model_still_speaks_responses(self):
        from code_gantry.reviewer import Reviewer

        assert Reviewer(ReviewerConfig(model="gpt-5.5", api_key_env=None), client=object()).wire is RESPONSES


class TestMarksMoveOrAccumulateByWire:
    def test_a_messages_loop_marks_only_the_outgoing_copy(self):
        from code_gantry.roleloop import run_structured_loop

        class Client:
            def __init__(self):
                self.calls = []
                self.messages = SimpleNamespace(parse=self._parse)

            def _parse(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return SimpleNamespace(
                        content=[SimpleNamespace(type="tool_use", id="t1", name="read_file", input={"path": "a"})],
                        stop_reason="tool_use", usage=None,
                    )
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                       stop_reason="end_turn", usage=None, parsed_output="P")

        cfg = PlannerConfig(model="claude-opus-5", api_key_env=None)
        client = Client()
        result = run_structured_loop(
            wire=MESSAGES, client=client, cfg=cfg,
            conversation=[{"role": "user", "content": [{"type": "text", "text": "go"}]}],
            tools=[], schema=object, extra={}, dispatch=lambda n, a: "contents", max_turns=3,
        )
        assert result.parsed == "P"
        second = client.calls[1]["messages"]
        marked = [b for m in second for b in (m["content"] if isinstance(m["content"], list) else []) if "cache_control" in b]
        assert len(marked) == 1, "one moving mark on the outgoing copy"

    def test_a_responses_loop_marks_every_result(self):
        from code_gantry.roleloop import run_structured_loop

        class Client:
            def __init__(self):
                self.calls = []
                self.responses = SimpleNamespace(parse=self._parse)

            def _parse(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return SimpleNamespace(
                        output=[SimpleNamespace(type="function_call", call_id="c1", name="read_file", arguments='{"path": "a"}')],
                        status="completed", incomplete_details=None, usage=None,
                    )
                return SimpleNamespace(output=[], status="completed", incomplete_details=None, usage=None, output_parsed="P")

        cfg = ReviewerConfig(model="gpt-5.5", api_key_env=None)
        client = Client()
        result = run_structured_loop(
            wire=RESPONSES, client=client, cfg=cfg,
            conversation=[{"role": "user", "content": [{"type": "input_text", "text": "go"}]}],
            tools=[], schema=object, extra={}, dispatch=lambda n, a: "contents", max_turns=3,
        )
        assert result.parsed == "P"
        outputs = [i for i in client.calls[1]["input"] if isinstance(i, dict) and i.get("type") == "function_call_output"]
        assert outputs and all("prompt_cache_breakpoint" in o["output"][0] for o in outputs)


class TestARefusalIsReportedAsOne:
    def test_the_sdks_finish_reason_error_becomes_a_refusal(self):
        from openai import ContentFilterFinishReasonError

        from code_gantry.planner import Planner

        refused = _chat_reply("content_filter")
        refused.choices[0].message.refusal = "blocked by policy"

        class Client:
            def __init__(self):
                self.parse_calls = 0
                self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse, create=self._create))

            def _parse(self, **kwargs):
                self.parse_calls += 1
                raise ContentFilterFinishReasonError()

            def _create(self, **kwargs):
                assert isinstance(kwargs["response_format"], dict), "re-sent with the schema as a plain parameter"
                return refused

        cfg = PlannerConfig(model="anthropic/claude-fable-5.1", api_base=OPENROUTER, api_key_env=None, repo_access=False)
        outcome = Planner(cfg, client=Client()).plan([{"role": "user", "content": "plan"}])
        assert outcome.failed
        assert "refused" in outcome.reasoning
        assert outcome.turn_end["reason"] == "refusal"
