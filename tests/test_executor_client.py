"""The executor's Responses client.

The facts worth pinning here were all learned from live calls, not from
documentation: that a reasoning model's tool call must be echoed back together
with the reasoning item it declares as required, and that a cache breakpoint on
every tool result is what stops cost growing with the square of the turn count.
A stub cannot show the first — it has no reasoning item to omit — so what these
tests assert is that *we* send the whole output list back, which is the part
under our control.
"""

from types import SimpleNamespace

import pytest

from code_gantry.config import ExecutorConfig
from code_gantry.edittools import FileEditor
from code_gantry.executorclient import OpenAIExecutorModel
from code_gantry.gitops import Git
from code_gantry.repotools import ReadBudget, RepoReader


def call(name, args_json, call_id="c1"):
    return SimpleNamespace(
        type="function_call", name=name, arguments=args_json, call_id=call_id
    )


def reasoning_item():
    # What a reasoning model emits alongside its calls, and what the API
    # rejects the call for arriving without.
    return SimpleNamespace(type="reasoning", id="r1", content=[])


def finished():
    """A model saying it is done.

    The terminator used to be a `message` with no content at all — which is
    the one shape the loop can no longer read as finishing, because it is
    exactly what a model that gives up returns. A fixture standing in for
    completion has to carry what completion carries, or every test in this
    file is exercising the branch production treats as a failure.
    """
    return SimpleNamespace(
        type="message", content=[SimpleNamespace(type="output_text", text="done")]
    )


def response(output, usage=None):
    return SimpleNamespace(output=output, usage=usage, status="completed")


def usage(prompt=100, completion=10, cached=0):
    return SimpleNamespace(
        input_tokens=prompt,
        output_tokens=completion,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


class ScriptedClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    @property
    def responses(self):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


@pytest.fixture
def parts(tmp_path):
    import subprocess

    repo = tmp_path / "t"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.rb").write_text("class A\nend\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "first"],
    ):
        subprocess.run(args, cwd=repo, check=True)

    editor = FileEditor(repo=repo, edit_files=["app/**"])
    reader = RepoReader(Git(repo), repo, ReadBudget())
    return editor, repo, reader


def model(responses, **cfg_over):
    cfg = ExecutorConfig(model="gpt-5.6-luna", **cfg_over)
    return OpenAIExecutorModel(cfg, client=ScriptedClient(responses)), cfg


class TestTheLoop:
    def test_a_turn_with_no_calls_ends_it(self, parts):
        editor, _, reader = parts
        m, _ = model([response([SimpleNamespace(
            type="message", content=[SimpleNamespace(type="output_text", text="done")]
        )], usage())])
        out = m.run([], reader=reader, editor=editor)
        assert out.stopped is True
        assert out.turns == 1
        assert "done" in out.text

    def test_a_tool_call_is_dispatched_and_answered(self, parts):
        editor, repo, reader = parts
        m, _ = model([
            response([reasoning_item(), call(
                "edit",
                '{"path": "app/a.rb", "edits": [{"old_string": "class A", '
                '"new_string": "class B", "replace_all": false}]}',
            )], usage()),
            response([finished()], usage()),
        ])
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert out.stopped is True
        assert "class B" in (repo / "app" / "a.rb").read_text()
        assert out.calls == ["edit(app/a.rb)"]

    def test_the_whole_output_list_is_echoed_not_just_the_calls(self, parts):
        # The reasoning item comes back too. Sending the call alone is
        # rejected — "was provided without its required 'reasoning' item" —
        # and no stub can raise that, so what is pinned is that we send it.
        editor, _, reader = parts
        m, _ = model([
            response([reasoning_item(), call(
                "edit",
                '{"path": "app/a.rb", "edits": [{"old_string": "class A", '
                '"new_string": "class B", "replace_all": false}]}',
            )], usage()),
            response([finished()], usage()),
        ])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        kinds = [getattr(x, "type", x.get("type") if isinstance(x, dict) else None)
                 for x in conversation]
        assert "reasoning" in kinds
        assert "function_call" in kinds
        assert "function_call_output" in kinds

    def test_every_tool_result_carries_a_cache_breakpoint(self, parts):
        # Marks accumulate rather than move, so marking each result extends the
        # cached prefix. Without this the loop re-sends every earlier result at
        # full price and cost grows with the square of the turn count.
        editor, _, reader = parts
        m, _ = model([
            response([call("read_file", '{"path": "app/a.rb"}')], usage()),
            response([finished()], usage()),
        ])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        outputs = [x for x in conversation
                   if isinstance(x, dict) and x.get("type") == "function_call_output"]
        assert outputs
        for item in outputs:
            assert item["output"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}

    def test_explicit_cache_mode_is_requested(self, parts):
        # GPT-5.6 caches at breakpoints and does not fall back to the longest
        # matching prefix, so the opt-in is required rather than helpful.
        editor, _, reader = parts
        m, client = model([response([finished()], usage())]), None
        m[0].run([], reader=reader, editor=editor)
        sent = m[0]._client.requests[0]
        assert sent["prompt_cache_options"] == {"mode": "explicit"}

    def test_the_reasoning_effort_reaches_the_request(self, parts):
        """A value that crosses config into a request needs the journey tested.

        The endpoint is what limits this, not the model: chat/completions
        refuses `max` for these models and /v1/responses accepts it. The whole
        `openai/responses/` routing prefix existed to get a request onto that
        endpoint; calling it directly is what replaces the prefix, and this is
        the assertion that the setting still arrives.
        """
        editor, _, reader = parts
        m, _ = model(
            [response([finished()], usage())],
            reasoning_effort="max",
        )
        m.run([], reader=reader, editor=editor)
        assert m._client.requests[0]["reasoning"] == {"effort": "max"}

    def test_no_reasoning_parameter_is_sent_when_the_operator_chose_none(self, parts):
        # A model that does not take the parameter must not be sent it, and no
        # default of ours should override a provider's.
        editor, _, reader = parts
        m, _ = model([response([finished()], usage())])
        m.run([], reader=reader, editor=editor)
        assert "reasoning" not in m._client.requests[0]

    def test_running_out_of_turns_does_not_report_stopped(self, parts):
        # The work it committed stands and the gates judge it, but the loop
        # must not read an exhausted budget as "the model finished".
        editor, _, reader = parts
        answer = response([call("read_file", '{"path": "app/a.rb"}')], usage())
        m, _ = model([answer, answer], max_model_turns=2)
        out = m.run([], reader=reader, editor=editor)
        assert out.stopped is False
        assert out.turns == 2


class TestAccounting:
    def test_usage_is_summed_across_turns(self, parts):
        editor, _, reader = parts
        m, _ = model([
            response([call("read_file", '{"path": "app/a.rb"}')], usage(100, 10)),
            response([finished()], usage(150, 20)),
        ])
        out = m.run([], reader=reader, editor=editor)
        assert out.usage.prompt_tokens == 250
        assert out.usage.completion_tokens == 30

    def test_context_is_the_peak_not_the_sum(self, parts):
        # What the operator needs for stage sizing is how much the model
        # actually held at once.
        editor, _, reader = parts
        m, _ = model([
            response([call("read_file", '{"path": "app/a.rb"}')], usage(100)),
            response([finished()], usage(150)),
        ])
        out = m.run([], reader=reader, editor=editor)
        assert out.usage.peak_prompt_tokens == 150

    def test_the_peak_is_computed_once(self):
        # `ExecutorTurn` kept its own `peak_prompt_tokens` and maxed it by hand,
        # because the executor tracked a peak before `TokenUsage` had one. When
        # the reviewer's missing peak was fixed by adding the field to
        # `TokenUsage`, that hand-written max became a second copy of the same
        # number — correct on the day and free to drift afterwards, which is
        # how this codebase has lost a value more than once.
        from code_gantry.executorclient import ExecutorTurn

        assert not hasattr(ExecutorTurn(), "peak_prompt_tokens"), (
            "the peak lives on `usage`; a second copy here is one to keep in step"
        )

    def test_a_turn_that_never_ran_peaks_at_zero(self, parts):
        # The failure path returns early, before any usage is merged.
        from code_gantry.executorclient import ExecutorTurn

        assert ExecutorTurn().usage.peak_prompt_tokens == 0


class TestFailures:
    def test_a_transport_failure_ends_the_cycle_with_a_reason(self, parts):
        editor, _, reader = parts

        class Boom:
            @property
            def responses(self):
                return self

            def create(self, **kwargs):
                raise RuntimeError("connection reset")

        cfg = ExecutorConfig(model="m", transport_retry_seconds=0.0)
        out = OpenAIExecutorModel(cfg, client=Boom()).run([], reader=reader, editor=editor)
        assert out.stopped is False
        assert "connection reset" in out.failure

    def test_unparsable_arguments_become_a_refusal_not_a_crash(self, parts):
        editor, _, reader = parts
        m, _ = model([
            response([call("edit", "{not json")], usage()),
            response([finished()], usage()),
        ])
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert out.stopped is True
        answered = [x for x in conversation
                    if isinstance(x, dict) and x.get("type") == "function_call_output"]
        assert "cannot do that" in answered[0]["output"][0]["text"]

    def test_a_model_refusal_is_reported(self, parts):
        editor, _, reader = parts
        refused = SimpleNamespace(
            output=[SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="refusal", refusal="no")],
            )],
            usage=usage(),
            status="completed",
        )
        m, _ = model([refused])
        out = m.run([], reader=reader, editor=editor)
        assert "refused to answer" in out.failure


class TestTheOpeningTurnIsRecordedSeparately:
    """Summed usage cannot answer the cross-stage question.

    Within one attempt the conversation grows and every later turn re-reads
    what the first one wrote, so a run with no reuse between stages at all
    still reports 90%-plus cached. Whether the prefix arranged to be shared
    across stages *is* shared is a fact about the opening turn alone.
    """

    def test_the_first_turns_figures_are_kept_apart(self, parts):
        editor, _, reader = parts
        m, _ = model([
            response([call("read_file", '{"path": "app/a.rb"}')], usage(1000, 5, cached=800)),
            response([finished()], usage(9000, 5, cached=8900)),
        ])
        out = m.run([], reader=reader, editor=editor)

        assert out.first_prompt_tokens == 1000
        assert out.first_cached_tokens == 800
        # The total is dominated by the later turn, which is the whole reason
        # the opening one has to be recorded on its own.
        assert out.usage.prompt_tokens == 10_000
        assert out.usage.cached_tokens == 9_700


class TestUsageIsReadInTheWireItArrivedOn:
    """The loop normalised every response with the *Responses* extractor.

    `Dialect.usage` exists for this and `test_each_wire_is_read_in_its_own_shape`
    proves it reads both shapes — but the loop called
    `extract_usage(response.usage)` directly, so a Messages-wire attempt was
    parsed by OpenAI's reader. The field names line up just well enough to hide
    it: `input_tokens` and `output_tokens` are spelled the same on both wires
    and came through, `cost` is top-level on the gateway and came through, and
    the two that have no OpenAI counterpart — `cache_read_input_tokens` and
    `cache_creation_input_tokens` — silently read zero.

    So every executor attempt of a Messages-wire run reported **0% cached**
    while the provider's own logs showed the cache working. A categorical zero
    from an instrument nobody had driven end to end, which is the whole of
    "check the instrument before the world": the operator's dashboard was
    right and this reading was wrong.

    Two green tests stating opposite things, with nothing exercising the seam
    between them. Same shape as `execute` routing to an edge `EDGES` did not
    list.
    """

    @staticmethod
    def _messages_response():
        """One turn in the Anthropic shape, ending the loop."""
        return SimpleNamespace(
            id="msg_1",
            type="message",
            role="assistant",
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="done")],
            usage=SimpleNamespace(
                input_tokens=40,
                output_tokens=10,
                cache_read_input_tokens=60,
                cache_creation_input_tokens=5,
            ),
        )

    class _MessagesClient:
        def __init__(self, response):
            self._response = response
            self.requests = []

        @property
        def messages(self):
            return self

        def create(self, **kwargs):
            self.requests.append(kwargs)
            return self._response

    def test_a_messages_attempt_records_the_cache_it_actually_got(self, parts):
        editor, _, reader = parts
        cfg = ExecutorConfig(model="anthropic/claude-opus-5")
        client = self._MessagesClient(self._messages_response())
        out = OpenAIExecutorModel(cfg, client=client).run(
            [], reader=reader, editor=editor
        )
        assert out.usage.cached_tokens == 60, "read with the wrong wire's extractor"
        assert out.usage.cache_write_tokens == 5
        # Anthropic reports these orthogonally, so total input is the sum.
        assert out.usage.prompt_tokens == 105

    def test_the_responses_wire_is_still_read_correctly(self, parts):
        """The fix must not change the wire that was already right."""
        editor, _, reader = parts
        m, _ = model([response([SimpleNamespace(
            type="message", content=[SimpleNamespace(type="output_text", text="done")]
        )], usage())])
        out = m.run([], reader=reader, editor=editor)
        assert out.usage.prompt_tokens > 0


class TestAnEmptyFinishIsNotAFinish:
    """A model that says nothing and asks for nothing has not finished.

    Measured over one run's 76 attempts: 6 ended with `ok: True`, `log: ""`
    and no edits at all, one of them after 96 searches and $0.35. The wire
    renders finishing and giving up identically — `stop_reason` is the same
    either way — and the only thing separating them is that one carries
    content and the other carries none. Nothing looked, so the scope gate
    reported "the attempt produced no changes", which reads as a badly drawn
    stage and sends the planner to redraw one that was never the problem.

    Asked once rather than adjudicated: a turn is cheap against a wasted
    attempt, and the answer settles which of the two it was.
    """

    def test_an_empty_close_is_asked_about_once(self, parts):
        editor, _, reader = parts
        m, _ = model([
            response([SimpleNamespace(type="message", content=[])], usage()),
            response([SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="done, actually")],
            )], usage()),
        ])
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert out.stopped is True
        assert out.empty_finishes == 1
        assert out.turns == 2
        assert "done, actually" in out.text
        # And the question is in the record, because the response that
        # provoked it carries no content and cannot be appended itself.
        asked = [x for x in conversation
                 if isinstance(x, dict) and x.get("role") == "user"]
        assert len(asked) == 1
        assert "request_replan" in asked[0]["content"][0]["text"]

    def test_a_second_empty_close_is_taken_as_the_answer(self, parts):
        editor, _, reader = parts
        m, _ = model([
            response([SimpleNamespace(type="message", content=[])], usage()),
            response([SimpleNamespace(type="message", content=[])], usage()),
        ])
        out = m.run([], reader=reader, editor=editor)

        assert out.stopped is True
        assert out.empty_finishes == 2
        assert out.turns == 2
        assert out.text == ""

    def test_a_close_that_says_something_is_left_alone(self, parts):
        editor, _, reader = parts
        client_responses = [response([SimpleNamespace(
            type="message", content=[SimpleNamespace(type="output_text", text="done")]
        )], usage())]
        m, _ = model(client_responses)
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert out.empty_finishes == 0
        assert out.turns == 1
        # No nudge — it said something. The closing turn itself is recorded,
        # which is `TestTheClosingTurnIsRecorded` below.
        assert not [x for x in conversation
                    if isinstance(x, dict) and x.get("role") == "user"]


class TestTheClosingTurnIsRecorded:
    """The turn that ends an attempt belongs in the record of the attempt.

    `append_model_turn` only ran on the branch where the model asked for
    something, so the closing message — the one where it says what it did, or
    gives up — was never appended. The conversation *is* the transcript: a
    `list` subclass whose docstring says appending is the only way to record,
    "so nothing can forget". There was a hole at exactly the last item, and it
    is why six silent attempts could not be told apart from a broken text
    extractor: the one turn that would have distinguished them was the one not
    written down.

    Appending it has a second effect, which is why it is deliberate rather
    than incidental: cycles within an attempt share one conversation, so the
    model's own summary is now in front of it on a rework. That is right —
    a model reworking its own work should see what it said it did.

    An empty close is still not appended. A message with no content is not
    something the next request can carry, and the nudge already leaves a
    record of that case.
    """

    def test_a_closing_message_is_appended(self, parts):
        editor, _, reader = parts
        m, _ = model([response([SimpleNamespace(
            type="message",
            content=[SimpleNamespace(type="output_text", text="I changed the thing.")],
        )], usage())])
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert "I changed the thing." in out.text
        assert conversation, "the closing turn never reached the record"

    def test_an_empty_close_appends_no_model_turn(self, parts):
        editor, _, reader = parts
        m, _ = model([
            response([SimpleNamespace(type="message", content=[])], usage()),
            response([SimpleNamespace(type="message", content=[])], usage()),
        ])
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert out.empty_finishes == 2
        # Only the nudge, which is a user turn. Nothing with empty content.
        roles = [x.get("role") for x in conversation if isinstance(x, dict)]
        assert roles == ["user"], roles

    def test_the_next_cycle_sees_what_the_model_said(self, parts):
        """A rework opens with the model's own account in context."""
        editor, _, reader = parts
        m, _ = model([
            response([SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="done, I think")],
            )], usage()),
            response([SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text="fixed it")],
            )], usage()),
        ])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)
        before = len(conversation)
        conversation.append({"role": "user", "content": [
            {"type": "input_text", "text": "the tests failed"}]})
        m.run(conversation, reader=reader, editor=editor)

        assert before > 0
        assert len(conversation) > before + 1


class _MessagesClient:
    """A Messages-wire client that can be scripted to raise before answering.

    Its own stub rather than `ScriptedClient` because the executor is the
    wire-polymorphic role and the two wires are reached through different
    attributes — `client.messages.create` against `client.responses.create`.
    A test that only ever drives the Responses attribute cannot see anything
    that goes wrong on the other one, which is how the missing retry survived.
    """

    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _refused(status, message, wire="messages"):
    """The real SDK exception, built the way the SDK builds it.

    A stand-in with a `status_code` attribute would pass `retry_on` for the
    wrong reason: the whole defect was that the class hierarchy did not match,
    and only a real one can show that.
    """
    import anthropic
    import httpx
    import openai

    request = httpx.Request("POST", f"https://example.invalid/v1/{wire}")
    response = httpx.Response(status, request=request)
    family = anthropic if wire == "messages" else openai
    kind = family.BadRequestError if status == 400 else family.RateLimitError
    return kind(f"Error code: {status} - {message}", response=response, body=None)


class _Block(dict):
    """Read by attribute like an SDK object, built like a dict. Both happen."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


def _messages_done(text="done"):
    """A Messages-wire model saying it has finished.

    Carrying text and `end_turn` together, because an `end_turn` with no
    content is the shape the loop reads as giving up rather than finishing.
    """
    return _Block(
        content=[_Block(type="text", text=text)],
        stop_reason="end_turn",
        usage=None,
    )


def _markers(payload):
    """Every cache marker anywhere in a request, however deeply nested."""
    found = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("cache_control", "prompt_cache_breakpoint"):
                found.append(key)
            found += _markers(value)
    elif isinstance(payload, list):
        for item in payload:
            found += _markers(item)
    return found


class TestTheExecutorRetriesOnTheWireItIsCallingOn:
    """The retry types came from the OpenAI SDK whatever wire was in use.

    Correct while every executor call was a Responses call, and silently wrong
    once the executor became the wire-polymorphic role. Measured on a live
    run: a Gemini model resolves to Messages, a gateway returned 400, and
    `anthropic.BadRequestError` is not an `openai.APIStatusError` — so
    `retry_on` matched nothing, the exception propagated on its first raise,
    and four attempts burned four seconds and the stage's whole rework
    allowance without one request reaching a model.
    """

    def test_a_rate_limit_on_the_messages_wire_is_waited_out(self, parts):
        editor, _, reader = parts
        client = _MessagesClient([
            _refused(429, "rate-limited upstream"),
            _messages_done(),
        ])
        cfg = ExecutorConfig(model="google/gemini-3.7-flash", transport_retry_seconds=0.05)
        out = OpenAIExecutorModel(cfg, client=client).run([], reader=reader, editor=editor)

        assert out.failure == ""
        assert len(client.requests) == 2, "the failure was not retried on this wire"

    def test_a_persistent_failure_still_ends_the_attempt(self, parts):
        # The budget is what bounds it. Retrying must not turn a dead provider
        # into a hang, which is the failure the wall-clock bound exists for.
        editor, _, reader = parts
        client = _MessagesClient([_refused(429, "still unwell") for _ in range(4)])
        cfg = ExecutorConfig(model="google/gemini-3.7-flash", transport_retry_seconds=0.02)
        out = OpenAIExecutorModel(cfg, client=client).run([], reader=reader, editor=editor)

        assert "still unwell" in out.failure


class TestAStaleCacheHandleIsResentWithoutTheCache:
    """The conversation was never the problem, so it goes back out whole.

    Four attempts died on the identical `Cache content 590015763578880000 is
    expired.`, with no generation recorded at the gateway for any of them. A
    handle built for our marked prefix had died; every resend named the same
    dead object. Replaying it unchanged is the one thing that cannot work.

    So the same conversation is sent again with nothing pointing at a cache —
    the full context, as if this were the first turn. The markers are stripped
    in place rather than for one request, because a handle that is dead for
    this turn is dead for the next one too, and flapping between marked and
    unmarked would pay the rejection once per turn.
    """

    def test_the_resend_carries_no_markers(self, parts):
        editor, _, reader = parts
        client = _MessagesClient([
            _refused(400, "Cache content 590015763578880000 is expired."),
            _messages_done(),
        ])
        cfg = ExecutorConfig(model="google/gemini-3.7-flash")
        conversation = [{
            "role": "user",
            "content": [{
                "type": "text",
                "text": "do the thing",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }],
        }]
        out = OpenAIExecutorModel(cfg, client=client).run(
            conversation, reader=reader, editor=editor
        )

        assert out.failure == ""
        assert len(client.requests) == 2
        assert _markers(client.requests[0]), "the first request should have been marked"
        assert _markers(client.requests[1]) == [], "the resend still pointed at a cache"

    def test_the_conversation_stays_cold_for_the_rest_of_the_attempt(self, parts):
        editor, _, reader = parts
        client = _MessagesClient([
            _refused(400, "Cache content 1 is expired."),
            _messages_done(),
        ])
        cfg = ExecutorConfig(model="google/gemini-3.7-flash")
        conversation = [{
            "role": "user",
            "content": [{
                "type": "text",
                "text": "hello",
                "cache_control": {"type": "ephemeral"},
            }],
        }]
        OpenAIExecutorModel(cfg, client=client).run(
            conversation, reader=reader, editor=editor
        )

        assert _markers(conversation) == []

    def test_the_content_itself_is_unchanged(self, parts):
        # Cold, not truncated. What was rejected was a pointer to a cache, and
        # dropping any of the conversation would answer a different problem.
        editor, _, reader = parts
        client = _MessagesClient([
            _refused(400, "Cache content 1 is expired."),
            _messages_done(),
        ])
        cfg = ExecutorConfig(model="google/gemini-3.7-flash")
        conversation = [{
            "role": "user",
            "content": [{
                "type": "text",
                "text": "the whole context",
                "cache_control": {"type": "ephemeral"},
            }],
        }]
        OpenAIExecutorModel(cfg, client=client).run(
            conversation, reader=reader, editor=editor
        )

        sent = client.requests[1]["messages"]
        assert sent[0]["content"][0]["text"] == "the whole context"

    def test_a_second_stale_answer_is_not_retried_forever(self, parts):
        # One cold resend. If the provider says it again with nothing marked,
        # the diagnosis belongs to whoever reads the failure.
        editor, _, reader = parts
        client = _MessagesClient([
            _refused(400, "Cache content 1 is expired."),
            _refused(400, "Cache content 1 is expired."),
            _messages_done(),
        ])
        cfg = ExecutorConfig(model="google/gemini-3.7-flash")
        out = OpenAIExecutorModel(cfg, client=client).run(
            [], reader=reader, editor=editor
        )

        assert "is expired" in out.failure
        assert len(client.requests) == 2
