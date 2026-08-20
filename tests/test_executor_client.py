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
            response([SimpleNamespace(type="message", content=[])], usage()),
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
            response([SimpleNamespace(type="message", content=[])], usage()),
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
            response([SimpleNamespace(type="message", content=[])], usage()),
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
        m, client = model([response([SimpleNamespace(type="message", content=[])], usage())]), None
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
            [response([SimpleNamespace(type="message", content=[])], usage())],
            reasoning_effort="max",
        )
        m.run([], reader=reader, editor=editor)
        assert m._client.requests[0]["reasoning"] == {"effort": "max"}

    def test_no_reasoning_parameter_is_sent_when_the_operator_chose_none(self, parts):
        # A model that does not take the parameter must not be sent it, and no
        # default of ours should override a provider's.
        editor, _, reader = parts
        m, _ = model([response([SimpleNamespace(type="message", content=[])], usage())])
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
            response([SimpleNamespace(type="message", content=[])], usage(150, 20)),
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
            response([SimpleNamespace(type="message", content=[])], usage(150)),
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
            response([SimpleNamespace(type="message", content=[])], usage()),
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
            response([SimpleNamespace(type="message", content=[])], usage(9000, 5, cached=8900)),
        ])
        out = m.run([], reader=reader, editor=editor)

        assert out.first_prompt_tokens == 1000
        assert out.first_cached_tokens == 800
        # The total is dominated by the later turn, which is the whole reason
        # the opening one has to be recorded on its own.
        assert out.usage.prompt_tokens == 10_000
        assert out.usage.cached_tokens == 9_700
