"""Which wire a call goes out on is a property of the model, not of the role.

The three roles each hardcode an API surface, and none of them chose it: the
planner speaks Anthropic Messages because it talks to Anthropic, and the
executor and reviewer speak OpenAI Responses because of what they replaced.
Three weeks of that reads as a design and is an accident of arrival order.

The evidence for each row is a measurement, not a preference:

- `openai/*` -> Responses. gpt-5.6-sol refuses function tools together with
  reasoning on chat/completions outright, and on Responses marks accumulate
  rather than move, so each tool turn extends the cached prefix — 98.1%,
  99.996% and 97.2% across three replayed stages.
- `anthropic/*` -> Messages. Structured output, `cache_control` with a TTL,
  and the longest-matching-prefix extension the plan block depends on.
- `google/*` -> Messages. Measured 2026-08-21: on Responses our OpenAI-dialect
  marks are ignored and the cached region pins at one figure however the
  conversation grows; through Messages with a moving `cache_control`
  breakpoint the same model over four tool turns read 13,427 -> 16,325 ->
  28,579 -> 36,751 with no rewrite after the first.

Unknown families get a defined answer rather than an exception. A model
appearing that we have not classified must not end a run — the router can
resolve to anything on its shortlist, and the majority wire caches
automatically on every provider OpenRouter lists as automatic.
"""

import pytest

from code_gantry.dialects import MESSAGES, RESPONSES, dialect_for


class TestTheFamilyMap:
    @pytest.mark.parametrize(
        "model",
        ["claude-opus-5", "anthropic/claude-opus-5", "us.anthropic.claude-opus-5",
         "anthropic/claude-opus-5:batch", "~anthropic/claude-opus-latest"],
    )
    def test_anthropic_however_it_is_spelled(self, model):
        """Route prefixes, region prefixes and suffixes name one model.

        A fixed list of exact strings meeting a set an operator and a router
        can both extend is a bet, not a specification.
        """
        assert dialect_for(model) is MESSAGES

    @pytest.mark.parametrize(
        "model",
        ["gpt-5.6-luna", "openai/gpt-5.6-sol", "us.openai.gpt-5.6-luna",
         "openai/gpt-5.6-sol:batch"],
    )
    def test_openai_however_it_is_spelled(self, model):
        assert dialect_for(model) is RESPONSES

    @pytest.mark.parametrize(
        "model", ["google/gemini-3.7-flash", "gemini-3.7-flash", "~google/gemini-pro-latest"]
    )
    def test_gemini_goes_to_messages(self, model):
        assert dialect_for(model) is MESSAGES

    def test_an_unknown_family_still_answers(self):
        """A run must not end because a router picked something new."""
        assert dialect_for("acme/brand-new-model-9") is RESPONSES

    def test_an_operator_can_override(self):
        """The map is provider knowledge, but a deployment may know better."""
        assert dialect_for("acme/brand-new-9", override="messages") is MESSAGES
        assert dialect_for("anthropic/claude-opus-5", override="responses") is RESPONSES

    def test_the_router_itself_is_not_a_family(self):
        """`openrouter/pareto-code` names a policy. Resolving it is the caller's
        job — a dialect chosen for a policy would be a guess about what it
        resolves to, and it resolved to three different families in one day."""
        with pytest.raises(ValueError):
            dialect_for("openrouter/pareto-code")


class TestStructuredOutput:
    def test_each_wire_names_it_differently(self):
        class Schema: ...
        assert RESPONSES.structured(Schema) == {"text_format": Schema}
        assert MESSAGES.structured(Schema) == {"output_format": Schema}

    def test_no_schema_sends_nothing(self):
        assert RESPONSES.structured(None) == {}
        assert MESSAGES.structured(None) == {}


class TestEffort:
    def test_each_wire_names_it_differently(self):
        assert RESPONSES.effort("max") == {"reasoning": {"effort": "max"}}
        assert MESSAGES.effort("high") == {"output_config": {"effort": "high"}}

    def test_absent_when_the_operator_chose_none(self):
        """A model that does not take the parameter must not be sent one, and
        no default of ours should override a provider's."""
        assert RESPONSES.effort(None) == {}
        assert MESSAGES.effort("") == {}


class TestTextBlocks:
    def test_the_block_type_differs(self):
        assert RESPONSES.text_block("hi")["type"] == "input_text"
        assert MESSAGES.text_block("hi")["type"] == "text"

    def test_a_cache_mark_uses_each_wire_s_vocabulary(self):
        r = RESPONSES.text_block("hi", cache=True)
        m = MESSAGES.text_block("hi", cache=True)
        assert r["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert m["cache_control"] == {"type": "ephemeral"}

    def test_a_ttl_reaches_only_the_wire_that_has_one(self):
        """Anthropic's default ephemeral window is about five minutes, and a
        stage outlasts it — the plan block shipped with a bare marker once and
        the reports showed 3% cached. Responses fixes its own lifetime."""
        assert MESSAGES.text_block("hi", cache=True, ttl="1h")["cache_control"]["ttl"] == "1h"
        assert "ttl" not in RESPONSES.text_block("hi", cache=True)["prompt_cache_breakpoint"]

    def test_no_mark_leaves_the_block_clean(self):
        assert "cache_control" not in MESSAGES.text_block("hi")
        assert "prompt_cache_breakpoint" not in RESPONSES.text_block("hi")


class TestRequestLevelCacheOptions:
    def test_responses_must_opt_in(self):
        """GPT-5.6 caches at explicit breakpoints and does not fall back to the
        longest matching prefix, so this is required rather than helpful."""
        assert RESPONSES.cache_options() == {"prompt_cache_options": {"mode": "explicit"}}

    def test_messages_has_no_request_level_switch(self):
        assert MESSAGES.cache_options() == {}


class TestTheCacheKeyParameter:
    """`prompt_cache_key` is a Responses parameter and only a Responses one.

    It ended a run 16 landings deep. `cache_options()` and `effort()` beside
    it were both moved onto the dialect; this one stayed hardcoded at the call
    site, so a model whose family maps to Messages sent it to
    `messages.create()`, which rejected it client-side before a request was
    ever made — four attempts in seconds, the coding model never invoked, and
    a planner correctly concluding the failure was not the stage's.

    Anthropic has no request-level equivalent: caching there is the
    `cache_control` markers the dialect already emits, so the answer for that
    wire is nothing at all rather than a differently-spelled key.
    """

    def test_responses_spells_it(self):
        assert RESPONSES.cache_key_param("k") == {"prompt_cache_key": "k"}

    def test_messages_has_no_such_parameter(self):
        assert MESSAGES.cache_key_param("k") == {}

    def test_an_empty_key_adds_nothing_on_either_wire(self):
        """The call sites guarded on the key before this existed; the guard
        belongs here so neither can forget it."""
        for wire in (RESPONSES, MESSAGES):
            assert wire.cache_key_param("") == {}
            assert wire.cache_key_param(None) == {}


class TestUsage:
    def test_each_wire_is_read_in_its_own_shape(self):
        class RUsage:
            input_tokens = 100
            output_tokens = 10
            input_tokens_details = type("D", (), {"cached_tokens": 60, "cache_write_tokens": 5})()
        class MUsage:
            input_tokens = 40
            output_tokens = 10
            cache_read_input_tokens = 60
            cache_creation_input_tokens = 5

        r = RESPONSES.usage(RUsage())
        m = MESSAGES.usage(MUsage())
        # Anthropic reports three orthogonal numbers; read as OpenAI's shape it
        # produced a 251% hit rate in a real report. Both normalise to "total
        # input" and "the part of it that was a cache read".
        assert (r.prompt_tokens, r.cached_tokens) == (100, 60)
        assert (m.prompt_tokens, m.cached_tokens) == (105, 60)


class TestEveryKwargIsOneItsOwnSdkAccepts:
    """The seam the last two outages crossed, checked on both wires.

    `session_id` went to `responses.create` as a top-level keyword and killed
    a run; the test written for it validated against `responses.create` alone,
    so when `prompt_cache_key` did the same thing to `messages.create` there
    was nothing to catch it. A seam test that knows about one wire is not a
    seam test once there are two.

    The kwargs come from `request_extras`, which is what the loop calls — not
    from a list copied out of it. The first version of this test did copy the
    list, which is the defect it exists to catch, one level up: a fifth
    contribution added at the call site would have been invisible to it in
    exactly the way `prompt_cache_key` was invisible to the `session_id` test.

    Signatures come from the installed packages rather than recall, for the
    reason every provider fact here does.
    """

    @pytest.mark.parametrize("wire_name", ["responses", "messages"])
    def test_no_kwarg_is_unknown_to_the_sdk(self, wire_name):
        import inspect

        from anthropic import Anthropic
        from openai import OpenAI

        from code_gantry.config import ExecutorConfig
        from code_gantry.executorclient import request_extras

        method = (
            OpenAI(api_key="x").responses.create
            if wire_name == "responses"
            else Anthropic(api_key="x").messages.create
        )
        cfg = ExecutorConfig(
            model="openai/gpt-5.6-sol" if wire_name == "responses" else "anthropic/claude-opus-5",
            api_base="https://openrouter.ai/api" + ("/v1" if wire_name == "responses" else ""),
            reasoning_effort="high",
        )
        built = request_extras(cfg, session_id="sess-1", cache_key="k")
        assert built, "the assembly returned nothing, so this proves nothing"
        unknown = sorted(set(built) - set(inspect.signature(method).parameters))
        assert not unknown, f"{wire_name}: not parameters of the SDK call: {unknown}"

    def test_the_loop_assembles_nothing_of_its_own(self):
        """`run` must call `request_extras` and add no keys beside it.

        Otherwise the test above checks a function the production path has
        quietly grown past — which is how both outages happened.
        """
        import ast
        import inspect

        from code_gantry.executorclient import OpenAIExecutorModel

        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(OpenAIExecutorModel.run)))
        assigned = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "extra"
        ]
        assert len(assigned) == 1, "expected one `extra:` assignment in run()"
        value = assigned[0].value
        assert isinstance(value, ast.Call), "extra should be one call, not a literal"
        assert getattr(value.func, "id", "") == "request_extras", (
            "run() builds its own kwargs again; they will not be checked"
        )


class TestBlocksTheEndpointWillActuallyAccept:
    """The conversation is built in Responses vocabulary; Messages must translate.

    `prompt_cache_key` was a Responses-only *parameter* that reached
    `messages.create()`. These are Responses-only *content fields* that
    reached it the same way, one layer in: `build_executor_messages` hardcodes
    `{"type": "input_text"}` on every block and embeds
    `prompt_cache_breakpoint` inside one of them. The endpoint answers 400
    `invalid_request_error` naming the offending messages, so the coding model
    never sees the instruction — twelve attempts across three stages, none of
    which reached a model.

    `split_system` already translated the system block, which is why the 400
    named `messages[0]` and `messages[1]` and not the system, and why this
    looked for a while like a problem with the prompt rather than with the
    wire.

    The accepted discriminators come from the installed SDK's own union rather
    than a list written here, for the reason every provider fact in this file
    does: a hand-written copy is a bet on a schema somebody else owns.
    """

    @staticmethod
    def _accepted() -> set:
        import typing

        from anthropic.types import ContentBlockParam

        names = set()
        for arm in typing.get_args(ContentBlockParam):
            hints = typing.get_type_hints(arm) if hasattr(arm, "__annotations__") else {}
            names.update(typing.get_args(hints.get("type")) or ())
        return names

    def test_input_text_is_not_a_thing_on_this_wire(self):
        """The premise. If this ever fails the translation is unnecessary."""
        assert "input_text" not in self._accepted()
        assert "text" in self._accepted()

    def test_messages_translates_the_text_type(self):
        conv = [{"role": "user", "content": [{"type": "input_text", "text": "GO"}]}]
        out = MESSAGES.normalise(conv)
        assert out[0]["content"][0] == {"type": "text", "text": "GO"}

    def test_messages_translates_the_cache_marker(self):
        """A breakpoint has to survive the translation or the Messages arm
        silently loses the caching the Responses arm has."""
        conv = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "CONVENTIONS",
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }
                ],
            }
        ]
        block = MESSAGES.normalise(conv)[0]["content"][0]
        assert block["type"] == "text"
        assert "prompt_cache_breakpoint" not in block
        assert block["cache_control"]["type"] == "ephemeral"

    def test_responses_leaves_its_own_vocabulary_alone(self):
        conv = [{"role": "user", "content": [{"type": "input_text", "text": "GO"}]}]
        assert RESPONSES.normalise(conv) == conv

    def test_neither_touches_tool_traffic(self):
        """`append_tool_results` already builds these per wire; normalising
        must not reach into what the dialect got right."""
        conv = [
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1",
                                          "content": "ok"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "1",
                                               "name": "edit", "input": {}}]},
        ]
        assert MESSAGES.normalise(conv) == conv

    def test_it_does_not_mutate_what_it_was_given(self):
        """The caller holds this list across turns and mirrors it to disk."""
        conv = [{"role": "user", "content": [{"type": "input_text", "text": "GO"}]}]
        MESSAGES.normalise(conv)
        assert conv[0]["content"][0]["type"] == "input_text"

    def test_a_real_executor_conversation_survives_the_endpoint(self, tmp_path):
        """The one that would have caught it: the actual builder, not a fixture."""
        from code_gantry.config import Stage, parse_config
        from code_gantry.prompts import build_executor_messages

        cfg = parse_config({
            "target_repo": str(tmp_path),
            "base_ref": "main",
            "project_branch": "proj",
            "plan_root": "PLAN.md",
            "full_test_command": "true",
            "executor": {"model": "anthropic/claude-opus-5"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        stage = Stage(id="s", instruction="do the thing", edit_files=["src/**"])
        conv = build_executor_messages(
            stage, cfg, "PROMPT", agent_context="CONVENTIONS",
            feedback=["a gate failed"], failure_layer="tests",
        )
        _system, rest = MESSAGES.split_system(MESSAGES.normalise(conv))
        seen = {
            b.get("type")
            for message in rest
            for b in (message.get("content") or [])
            if isinstance(b, dict)
        }
        unknown = sorted(seen - self._accepted())
        assert not unknown, f"the endpoint rejects these block types: {unknown}"


class TestEffortIsSpelledForTheRouteNotTheModel:
    """Measured against all three live endpoints on 2026-08-22.

    | spelling                | Anthropic direct | OR -> claude | OR -> gemini |
    | `output_config`         | OK               | OK           | **404**      |
    | `extra_body.reasoning`  | **400**          | OK           | OK           |

    `output_config` is Anthropic-native, so it survives OpenRouter only where
    the upstream *is* Anthropic. OpenRouter's own model listing bears this out:
    no model on it declares `output_config`, and every one declares `reasoning`.
    With `require_parameters` on — which is there to stop a provider silently
    dropping structured output — a parameter no provider can honour excludes
    every provider, and the gateway answers 404 `No endpoints found that can
    handle the requested parameters`. Four attempts a second, three stages, no
    model ever reached.

    So the axis is the *route*, not the model family: the same
    `anthropic/claude-opus-5` takes `output_config` through the gateway and
    `reasoning` through it too, while direct Anthropic takes only the first.
    That is `gateway.py`'s question rather than the dialect's, which is why
    the spelling moves here and `request_extras` stops asking the wire for it.
    """

    @staticmethod
    def _cfg(base):
        from code_gantry.config import ExecutorConfig

        return ExecutorConfig(
            model="anthropic/claude-opus-5", api_base=base, reasoning_effort="high"
        )

    def test_through_the_gateway_it_rides_in_extra_body(self):
        from code_gantry.executorclient import request_extras

        built = request_extras(self._cfg("https://openrouter.ai/api"), cache_key=None)
        assert built["extra_body"]["reasoning"] == {"effort": "high"}
        assert "output_config" not in built

    def test_first_party_keeps_the_native_parameter(self):
        from code_gantry.executorclient import request_extras

        built = request_extras(self._cfg("https://api.anthropic.com"), cache_key=None)
        assert built["output_config"] == {"effort": "high"}
        assert "reasoning" not in built.get("extra_body", {})

    def test_no_effort_configured_sends_neither(self):
        from code_gantry.config import ExecutorConfig
        from code_gantry.executorclient import request_extras

        cfg = ExecutorConfig(model="anthropic/claude-opus-5",
                             api_base="https://openrouter.ai/api")
        built = request_extras(cfg, cache_key=None)
        assert "output_config" not in built
        assert "reasoning" not in built.get("extra_body", {})

    def test_the_responses_wire_is_untouched(self):
        """It already spells effort `reasoning` at the top level, which both
        OpenAI and the gateway accept. Nothing measured says to change it."""
        from code_gantry.config import ExecutorConfig
        from code_gantry.executorclient import request_extras

        cfg = ExecutorConfig(model="openai/gpt-5.6-sol",
                             api_base="https://openrouter.ai/api/v1",
                             reasoning_effort="high")
        built = request_extras(cfg, cache_key=None)
        assert built["reasoning"] == {"effort": "high"}
        assert "reasoning" not in built.get("extra_body", {})


class TestEachWireNamesItsOwnSdksFailures:
    """The retry types belong to the SDK the call actually goes out on.

    `executorclient` asked `openaiclient.transport_errors()` for them, which
    names OpenAI's classes — correct while every executor call was a Responses
    call, and silently wrong the day the executor became the wire-polymorphic
    role. An `anthropic.APIStatusError` is not an `openai.APIStatusError`, so
    `retry_on` matched nothing on the Messages wire and every failure
    propagated on its first raise.

    Measured on a live run: four attempts, four seconds, no backoff logged,
    and the stage's whole rework allowance spent by requests that never
    reached a model. The same hole covered 429s and dropped sockets — nothing
    on that wire had ever been retried.

    So the tuple is a property of the dialect, beside `client`, which is the
    thing that decides which SDK raises in the first place.
    """

    def test_responses_names_openais(self):
        import openai

        assert set(RESPONSES.transport_errors()) == {
            openai.APIConnectionError,
            openai.APIStatusError,
        }

    def test_messages_names_anthropics(self):
        import anthropic

        assert set(MESSAGES.transport_errors()) == {
            anthropic.APIConnectionError,
            anthropic.APIStatusError,
        }

    def test_the_wires_do_not_cover_each_other(self):
        # The defect itself, stated as a fact about the SDKs rather than about
        # our code: neither tuple would have caught the other's failure, so
        # asking the wrong one is the same as not retrying.
        import anthropic
        import httpx
        import openai

        request = httpx.Request("POST", "https://example.invalid/v1/messages")
        refused = anthropic.BadRequestError(
            "Error code: 400 - Cache content 1 is expired.",
            response=httpx.Response(400, request=request),
            body=None,
        )
        assert not isinstance(refused, tuple(RESPONSES.transport_errors()))
        assert isinstance(refused, tuple(MESSAGES.transport_errors()))
        assert not isinstance(refused, openai.APIStatusError)

    def test_every_dialect_answers(self):
        # A dialect added later inherits the question rather than the default,
        # which is how the executor came to be asking the wrong module.
        from code_gantry.dialects import _BY_NAME

        for name, wire in _BY_NAME.items():
            assert wire.transport_errors(), f"{name} names no failures to retry"


class _Obj(dict):
    """Read by attribute like an SDK object, built like a dict. Both happen."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


def _reply(stop_reason="end_turn", text="", tool_calls=0):
    """A Messages response, in the shape the SDK returns."""
    content = [
        _Obj(type="tool_use", id=f"toolu_{i}", name="read_file", input={})
        for i in range(tool_calls)
    ]
    if text:
        content.append(_Obj(type="text", text=text))
    return _Obj(content=content, stop_reason=stop_reason)


def _responses_reply(status="completed", text="", tool_calls=0, incomplete=None):
    """A Responses response, carrying the two fields nothing used to read."""
    output = [
        _Obj(type="function_call", call_id=f"call_{i}", name="read_file", arguments="{}")
        for i in range(tool_calls)
    ]
    if text:
        output.append(_Obj(type="message", content=[_Obj(type="output_text", text=text)]))
    return _Obj(
        output=output,
        status=status,
        incomplete_details=_Obj(reason=incomplete) if incomplete else None,
    )


class TestHowATurnEnded:
    """One question, asked the same way on both wires.

    Every role needs to know how a turn ended and no two asked it alike.
    `_messages_stopped` consulted `stop_reason`; `_responses_stopped` consulted
    *nothing* — it inferred stopping from the absence of tool calls, so on that
    wire a turn that ended abnormally was indistinguishable from one that
    finished, by construction, while `status` and `incomplete_details` sat on
    the response unread. The planner compared `stop_reason` against two string
    literals and discarded it; the reviewer never looked.

    Measured: a planner call returned no verdict after 27 reads over 426
    seconds and blocked on a message covering at least three different bugs,
    with nothing in the artifact able to narrow it.

    The facts are recorded and the label is derived from them, never the other
    way round: `empty` is not a provider's word, it is a reason plus no
    content, and a record holding only the label cannot answer a question
    nobody has thought of yet. An unrecognised reason is kept verbatim rather
    than bucketed, which is the half that would have made that block legible.
    """

    def test_messages_reads_the_stop_reason(self):
        end = MESSAGES.turn_end(_reply(stop_reason="end_turn", text="done"))
        assert end.reason == "end_turn"
        assert end.has_content is True
        assert end.abnormal is False
        assert end.label == "finished"

    def test_messages_tool_use_is_not_an_ending(self):
        end = MESSAGES.turn_end(_reply(stop_reason="tool_use", tool_calls=1))
        assert end.label == "tool_use"
        assert end.abnormal is False

    def test_an_end_turn_carrying_nothing_is_the_shape_that_reads_as_success(self):
        # The failure the executor already counts as `empty_finishes`, named
        # here so every role can see it. Finishing and giving up render
        # identically on the wire; only the absence of content separates them.
        end = MESSAGES.turn_end(_reply(stop_reason="end_turn"))
        assert end.label == "empty"
        assert end.abnormal is True

    def test_a_truncation_is_abnormal(self):
        end = MESSAGES.turn_end(_reply(stop_reason="max_tokens", text="half an ans"))
        assert end.label == "max_tokens"
        assert end.abnormal is True

    def test_responses_reads_the_status_it_had_been_ignoring(self):
        end = RESPONSES.turn_end(_responses_reply(status="completed", text="done"))
        assert end.reason == "completed"
        assert end.abnormal is False

    def test_responses_reads_incomplete_details(self):
        end = RESPONSES.turn_end(
            _responses_reply(status="incomplete", incomplete="max_output_tokens")
        )
        assert end.reason == "max_output_tokens"
        assert end.abnormal is True

    def test_an_unrecognised_reason_is_kept_verbatim(self):
        # The half that makes a new failure legible instead of bucketed. A
        # reason nobody has classified is abnormal *and* still says its name.
        end = MESSAGES.turn_end(_reply(stop_reason="some_new_thing", text="x"))
        assert end.reason == "some_new_thing"
        assert end.label == "unknown"
        assert end.abnormal is True

    def test_the_record_carries_the_facts_not_only_the_label(self):
        end = MESSAGES.turn_end(_reply(stop_reason="end_turn"))
        record = end.as_record()
        assert record["reason"] == "end_turn"
        assert record["has_content"] is False
        assert record["has_tool_calls"] is False
        assert record["label"] == "empty"

    def test_stopped_is_the_same_reading(self):
        # Not a second, independent look at the response: both wires' existing
        # rules are preserved exactly, computed from the one captured answer.
        assert MESSAGES.stopped(_reply(stop_reason="tool_use", tool_calls=1)) is False
        assert MESSAGES.stopped(_reply(stop_reason="end_turn", text="done")) is True
        assert RESPONSES.stopped(_responses_reply(status="completed", text="d")) is True
        assert RESPONSES.stopped(_responses_reply(status="completed", tool_calls=1)) is False


class TestAllThreeRolesRecordItTheSameWay:
    """One event, one key, one shape — in every role's artifact.

    The value of doing this at all is uniformity: a turn ending abnormally
    used to present as `parsed_output is None` in the planner, an empty
    content list in the executor, and nothing at all in the reviewer, so
    nobody comparing two runs could tell they were looking at the same thing.
    A shared reader is only half of that; the other half is that all three
    write it down under the same name.
    """

    FIELDS = {"reason", "has_content", "has_tool_calls", "label", "abnormal"}

    def test_the_record_shape_is_fixed(self):
        from code_gantry.dialects import TurnEnd

        assert set(TurnEnd("end_turn", False, False).as_record()) == self.FIELDS

    def test_every_role_declares_the_field(self):
        # Named on each role's own record rather than bolted on by whoever
        # writes the artifact, so a role cannot quietly stop carrying it.
        from dataclasses import fields

        from code_gantry.executor import ExecutionResult
        from code_gantry.planner import PlannerOutcome
        from code_gantry.reviewer import ReviewOutcome

        for kind in (PlannerOutcome, ReviewOutcome, ExecutionResult):
            names = {f.name for f in fields(kind)}
            assert "turn_end" in names, f"{kind.__name__} does not record how a turn ended"

    def test_the_reviewer_writes_it_even_when_null(self):
        # An absent key and "it ended normally" are different answers. This
        # project has already reported a zero that was really a missing field.
        from code_gantry.reviewer import ReviewOutcome

        assert "turn_end" in ReviewOutcome(verdict="approved", summary="s").as_dict()

    def test_one_describer_serves_every_role(self):
        from code_gantry.dialects import TurnEnd, describe_end

        empty = TurnEnd("end_turn", False, False)
        for role in ("planner", "reviewer", "executor"):
            said = describe_end(role, empty)
            assert said.startswith(f"the {role} ")
            assert "ended its turn without answering" in said
            assert "'end_turn'" in said, "the raw reason must survive the label"
