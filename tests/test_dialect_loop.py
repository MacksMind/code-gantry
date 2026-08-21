"""The tool-loop mechanics, once, per wire rather than per role.

Both clients grew their own: the planner's on Messages, the executor's on
Responses, neither aware the other existed. They do the same four things —
read the tool calls out of a response, echo the model's turn back, answer with
results, decide whether the model has stopped — in two shapes.

Shape belongs to the *endpoint*, not the vendor. The same Google model
returned `function_call` items on Responses and `tool_use` blocks on Messages,
so these are two wires rather than two providers, and a role pointed at either
should work without knowing which.
"""

import json

import pytest

from code_gantry.dialects import MESSAGES, RESPONSES


class _Block(dict):
    """An SDK object is read by attribute; a stub here is a dict. Both happen."""
    def __getattr__(self, k):
        try: return self[k]
        except KeyError: raise AttributeError(k)


def _responses_reply(calls=(), text=""):
    out = [_Block(type="reasoning", id="rs_1", summary=[])]
    for i, (name, args) in enumerate(calls):
        out.append(_Block(type="function_call", call_id=f"call_{i}",
                          name=name, arguments=json.dumps(args)))
    if text:
        out.append(_Block(type="message", content=[_Block(type="output_text", text=text)]))
    return _Block(output=out)


def _messages_reply(calls=(), text="", stop=None):
    content = [_Block(type="thinking", thinking="hmm", signature="sig"),
               _Block(type="redacted_thinking", data="opaque")]
    for i, (name, args) in enumerate(calls):
        content.append(_Block(type="tool_use", id=f"toolu_{i}", name=name, input=args))
    if text:
        content.append(_Block(type="text", text=text))
    return _Block(content=content,
                  stop_reason=stop or ("tool_use" if calls else "end_turn"))


class TestReadingToolCalls:
    def test_responses_reads_function_call_items(self):
        r = _responses_reply([("read_file", {"path": "a.rb"})])
        assert RESPONSES.tool_calls(r) == [
            {"id": "call_0", "name": "read_file", "args": {"path": "a.rb"}}]

    def test_messages_reads_tool_use_blocks(self):
        r = _messages_reply([("read_file", {"path": "a.rb"})])
        assert MESSAGES.tool_calls(r) == [
            {"id": "toolu_0", "name": "read_file", "args": {"path": "a.rb"}}]

    def test_responses_arguments_arrive_as_a_json_string(self):
        """And a model can emit one that does not parse. That is a bad request,
        not a dead turn — an empty dict reaches dispatch, which answers with a
        readable refusal."""
        r = _Block(output=[_Block(type="function_call", call_id="c", name="x", arguments="{oops")])
        assert RESPONSES.tool_calls(r) == [{"id": "c", "name": "x", "args": {}}]

    def test_neither_crashes_on_an_unexpected_shape(self):
        """Fourteen-hour runs should not end on an attribute error."""
        assert RESPONSES.tool_calls(_Block(output=None)) == []
        assert MESSAGES.tool_calls(_Block(content=None)) == []


class TestStopping:
    def test_responses_stops_when_no_calls_remain(self):
        assert RESPONSES.stopped(_responses_reply(text="done")) is True
        assert RESPONSES.stopped(_responses_reply([("x", {})])) is False

    def test_messages_reads_the_stop_reason(self):
        assert MESSAGES.stopped(_messages_reply(text="done")) is True
        assert MESSAGES.stopped(_messages_reply([("x", {})])) is False


class TestEchoingTheModelTurn:
    def test_responses_echoes_the_whole_output_list(self):
        """A function_call declares its reasoning item as required; sending the
        call alone is rejected outright."""
        r = _responses_reply([("x", {})])
        conv = []
        RESPONSES.append_model_turn(conv, r)
        assert [i["type"] for i in conv] == ["reasoning", "function_call"]

    def test_messages_echoes_one_assistant_message(self):
        r = _messages_reply([("x", {})])
        conv = []
        MESSAGES.append_model_turn(conv, r)
        assert len(conv) == 1 and conv[0]["role"] == "assistant"
        kinds = [b["type"] for b in conv[0]["content"]]
        assert kinds == ["thinking", "redacted_thinking", "tool_use"]

    def test_messages_keeps_redacted_thinking(self):
        """Gemini emits it on this wire, and a thinking block dropped from the
        echo breaks the turn it belongs to."""
        conv = []
        MESSAGES.append_model_turn(conv, _messages_reply([("x", {})]))
        assert any(b["type"] == "redacted_thinking" for b in conv[0]["content"])


class TestAnsweringWithResults:
    def test_responses_appends_one_item_per_call(self):
        conv = []
        RESPONSES.append_tool_results(conv, [("call_0", "1 | line")], cache=True)
        assert conv[0]["type"] == "function_call_output"
        assert conv[0]["call_id"] == "call_0"
        assert conv[0]["output"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}

    def test_messages_appends_one_user_message_holding_all_of_them(self):
        """Every tool_use must be answered in the next message."""
        conv = []
        MESSAGES.append_tool_results(conv, [("toolu_0", "a"), ("toolu_1", "b")], cache=True)
        assert len(conv) == 1 and conv[0]["role"] == "user"
        blocks = conv[0]["content"]
        assert [b["tool_use_id"] for b in blocks] == ["toolu_0", "toolu_1"]

    def test_messages_marks_the_last_result_only(self):
        """It *moves* on this wire: only the last one counts for Gemini, and
        four is a hard ceiling that marking every result would pass by the
        fifth turn."""
        conv = []
        MESSAGES.append_tool_results(conv, [("a", "x"), ("b", "y")], cache=True)
        blocks = conv[0]["content"]
        assert "cache_control" not in blocks[0]
        assert blocks[-1]["cache_control"] == {"type": "ephemeral"}

    def test_responses_marks_every_result(self):
        """It *accumulates* on this wire — a request writes its latest four but
        matching considers up to eighty, so each mark extends the cached prefix
        rather than restarting it. Pinned because collapsing the two wires onto
        one rule silently changed the path measured at 97-99% cached."""
        conv = []
        RESPONSES.append_tool_results(conv, [("a", "x"), ("b", "y")], cache=True)
        assert all(
            item["output"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
            for item in conv
        )

    def test_no_mark_when_not_asked(self):
        conv = []
        MESSAGES.append_tool_results(conv, [("a", "x")], cache=False)
        assert "cache_control" not in conv[0]["content"][0]


class TestToolSchemas:
    SPEC = [{"name": "read_file", "description": "Read it.",
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}}]

    def test_responses_is_flat_and_strict(self):
        """Strict is not a preference: the SDK will not auto-parse a structured
        response beside a non-strict tool."""
        t = RESPONSES.tool_schemas(self.SPEC)[0]
        assert t["type"] == "function" and t["strict"] is True
        assert t["name"] == "read_file" and "parameters" in t

    def test_messages_takes_the_neutral_spec_as_it_stands(self):
        """The shape this codebase already builds *is* this wire's shape."""
        t = MESSAGES.tool_schemas(self.SPEC)[0]
        assert set(t) == {"name", "description", "input_schema"}
        assert t["input_schema"]["properties"]["path"]["type"] == "string"

    def test_neither_mutates_what_it_was_handed(self):
        before = json.dumps(self.SPEC)
        RESPONSES.tool_schemas(self.SPEC)
        MESSAGES.tool_schemas(self.SPEC)
        assert json.dumps(self.SPEC) == before
