"""A model asking the same thing over and over.

Measured on one run: 9,337 tool calls, of which 699 — 7.5% — were consecutive
repeats with byte-identical arguments, in three bursts of 259, 239 and 190.
Every one of those calls *succeeded*; the model was handed the right answer
and asked again about three seconds later. Forty minutes of wall clock, and
each burst ended its attempt having edited nothing, which reaches the planner
as "the attempt produced no changes" — so it redraws a stage that was never
the problem.

The two thresholds do different jobs and neither substitutes for the other.
The nudge replaces the payload, because the model has already ignored that
payload twice and burying a warning under sixty lines of file is how it gets
ignored a third time. The abort exists so the attempt ends with a name on it
rather than running to the request timeout and arriving as silence.

Three is safe on the evidence rather than by taste: across those 9,337 calls
only five runs of consecutive-identical calls reached three at all, and every
one of the five was pathological.
"""

import json
import subprocess
from types import SimpleNamespace

import pytest

from code_gantry.config import ExecutorConfig
from code_gantry.edittools import FileEditor
from code_gantry.executorclient import (
    REPEAT_ABORT_AT,
    REPEAT_NUDGE_AT,
    OpenAIExecutorModel,
)
from code_gantry.gitops import Git
from code_gantry.repotools import ReadBudget, RepoReader

READ = '{"path": "app/a.rb"}'
OTHER = '{"path": "app/b.rb"}'


def call(name, args_json, call_id="c1"):
    return SimpleNamespace(
        type="function_call", name=name, arguments=args_json, call_id=call_id
    )


def finished():
    return SimpleNamespace(
        type="message", content=[SimpleNamespace(type="output_text", text="done")]
    )


def response(output):
    return SimpleNamespace(
        output=output,
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=10,
            input_tokens_details=SimpleNamespace(cached_tokens=0),
        ),
        status="completed",
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
        # Never StopIteration: a test that aborts early leaves responses
        # unused, and one that fails to abort must fail on the assertion
        # rather than on an exhausted fixture.
        return self._responses.pop(0) if self._responses else response([finished()])


@pytest.fixture
def parts(tmp_path):
    repo = tmp_path / "t"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.rb").write_text("class A\nend\n")
    (repo / "app" / "b.rb").write_text("class B\nend\n")
    (repo / ".gitignore").write_text(".code_gantry/\n")
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
    return editor, reader


def model(responses):
    cfg = ExecutorConfig(model="gpt-5.6-luna")
    return OpenAIExecutorModel(cfg, client=ScriptedClient(responses))


def outputs(conversation):
    return [
        x for x in conversation
        if isinstance(x, dict) and x.get("type") == "function_call_output"
    ]


def text_of(item):
    out = item["output"]
    if isinstance(out, list):
        return " ".join(str(b.get("text", "")) for b in out if isinstance(b, dict))
    return str(out)


class TestTheNudge:
    def test_the_first_two_identical_calls_are_answered_normally(self, parts):
        editor, reader = parts
        m = model([response([call("read_file", READ)]) for _ in range(2)]
                  + [response([finished()])])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        answers = outputs(conversation)
        assert len(answers) == 2
        for item in answers:
            assert "class A" in text_of(item)

    def test_the_third_replaces_the_answer_with_a_nudge(self, parts):
        editor, reader = parts
        m = model([response([call("read_file", READ)]) for _ in range(3)]
                  + [response([finished()])])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        third = text_of(outputs(conversation)[2])
        # The payload is gone, not annotated: re-sending it is both the cost
        # and the thing already proven not to land.
        assert "class A" not in third
        assert "read_file" in third
        assert str(REPEAT_NUDGE_AT) in third

    def test_a_different_call_resets_the_count(self, parts):
        editor, reader = parts
        script = [
            response([call("read_file", READ)]),
            response([call("read_file", READ)]),
            response([call("read_file", OTHER)]),
            response([call("read_file", READ)]),
            response([call("read_file", READ)]),
            response([finished()]),
        ]
        m = model(script)
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        answers = outputs(conversation)
        assert len(answers) == 5
        # Two, then a different one, then two more. Nothing reaches three in a
        # row, so every answer is the file.
        assert all("class" in text_of(item) for item in answers)

    def test_the_count_is_over_arguments_not_just_the_tool(self, parts):
        editor, reader = parts
        m = model([
            response([call("read_file", READ)]),
            response([call("read_file", OTHER)]),
            response([call("read_file", READ)]),
            response([finished()]),
        ])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        assert all("class" in text_of(item) for item in outputs(conversation))


class TestTheAbort:
    def test_ten_identical_calls_end_the_turn(self, parts):
        editor, reader = parts
        m = model([response([call("read_file", READ)]) for _ in range(20)])
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert out.turns == REPEAT_ABORT_AT
        assert out.stopped is True
        assert "read_file" in out.unproductive_stop
        assert str(REPEAT_ABORT_AT) in out.unproductive_stop

    def test_the_abandoned_turn_leaves_a_well_formed_conversation(self, parts):
        # Every declared call needs an answer or the next request is malformed
        # — and there is a next request whenever the attempt had already
        # edited something, because the loop appends gate feedback and runs
        # another cycle over the same conversation.
        editor, reader = parts
        m = model([response([call("read_file", READ)]) for _ in range(20)])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        declared = [
            x for x in conversation
            if getattr(x, "type", None) == "function_call"
        ]
        assert len(declared) == len(outputs(conversation))

    def test_the_reason_is_not_an_empty_finish(self, parts):
        # `empty_finishes` is the other way an attempt ends with nothing, and
        # the two want different answers from the planner.
        editor, reader = parts
        m = model([response([call("read_file", READ)]) for _ in range(20)])
        out = m.run([], reader=reader, editor=editor)

        assert out.empty_finishes == 0
        assert out.failure == ""


class TestItReachesTheResult:
    """The journey, not the endpoints.

    `unproductive_stop` is computed in the client and read by whoever reads
    `ExecutionResult`. Four defects in this codebase have been values that
    were correct on both sides and lost in transit, so the test drives the
    loop rather than asserting the field exists.
    """

    def test_the_loop_records_why_the_attempt_stopped(self, tmp_path, parts):
        from code_gantry.commands import CommandRunner
        from code_gantry.config import Stage, parse_config
        from code_gantry.executorloop import run_loop

        editor, reader = parts
        repo = editor.repo
        cfg = parse_config({
            "target_repo": str(repo),
            "base_ref": "main",
            "project_branch": "proj",
            "plan_root": "PLAN.md",
            "test_command": "true",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.5"},
        })
        stage = Stage(id="s1", instruction="do it", edit_files=["app/**"])
        git = Git(repo)
        m = model([response([call("read_file", READ)]) for _ in range(20)])

        out = run_loop(
            stage, cfg, git, CommandRunner(cwd=repo, timeout=60), m, reader, editor,
            since_sha=git.rev_parse("HEAD"),
        )

        assert "read_file" in out.unproductive_stop
        # And in `log`, which `nodes.execute` clips into `executor_note` — for
        # this stop even on a first attempt, which is the exception
        # `test_a_repeat_abort_is_reported_even_on_a_first_attempt` pins.
        assert "read_file" in out.log


class TestItStaysVisible:
    """A withheld call must still reach a ledger.

    `tools.log`, `tool_counts` and `refusal_counts` are all built from the
    reader's and editor's ledgers, and only `dispatch` writes to those. So a
    call answered by the nudge would appear in none of them, and the guard
    would make a burst *less* legible than it was before it existed — two
    calls, then silence, in the one record an operator watches live.

    `record_refusal` is the right instrument and says so in its own docstring:
    a cap whose binding cannot be observed cannot be tuned. `refusal_kind`
    rather than the message, because a bucket recovered by matching prose is a
    classifier over rendered text.
    """

    def _burst(self, parts, n):
        editor, reader = parts
        m = model([response([call("read_file", READ)]) for _ in range(n)]
                  + [response([finished()])])
        m.run([], reader=reader, editor=editor)
        return reader

    def test_a_nudged_call_is_recorded(self, parts):
        reader = self._burst(parts, REPEAT_NUDGE_AT)
        withheld = [c for c in reader.calls if c.refusal_kind == "repeated"]
        assert len(withheld) == 1
        assert withheld[0].tool == "read_file"
        # The question, not the content: there is no content to describe it by.
        assert "app/a.rb" in withheld[0].detail

    def test_every_withheld_call_of_a_burst_is_recorded(self, parts):
        reader = self._burst(parts, REPEAT_ABORT_AT)
        withheld = [c for c in reader.calls if c.refusal_kind == "repeated"]
        answered = [c for c in reader.calls if not c.refusal]
        # Two answered before the threshold, then one per turn to the abort.
        assert len(answered) == REPEAT_NUDGE_AT - 1
        assert len(withheld) == REPEAT_ABORT_AT - REPEAT_NUDGE_AT + 1

    def test_the_counts_and_the_log_see_them(self, parts):
        from code_gantry.repotools import count_calls, count_refusals

        editor, reader = parts
        reader = self._burst(parts, REPEAT_ABORT_AT)
        assert count_calls(reader)["read_file"] == REPEAT_ABORT_AT
        assert count_refusals(reader)["repeated"] == (
            REPEAT_ABORT_AT - REPEAT_NUDGE_AT + 1
        )
