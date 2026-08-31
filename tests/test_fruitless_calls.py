"""A model whose calls all differ and none of which change anything.

The repeat guard beside this one keys on byte-identical consecutive arguments.
That is one shape a stalled attempt takes and it is not the shape that cost the
most: one measured attempt ran 2h04m, made 139 tool calls, applied 10 edit
batches and left the file it was working on syntactically broken — and its
longest run of byte-identical calls was **one**, because it was searching, so
every call differed by a character. The guard was unreachable for it by
construction.

What that attempt did have was refusals: 49 of 59 edits and 29 of 59 searches
came back with nothing. Measured across 879 recorded attempts, the longest run
of such calls per attempt is 0-3 for 97% of them, p95 is 3, and above 5 the
histogram is singletons. So the streak separates the classes cleanly, which is
what the identical-argument counter could not do here.

The thresholds are checked against where they *first fire*, not only against
how rare they are. On that attempt the nudge lands at call 40 — three calls
before the model began writing marker strings into the source file to find out
what was in it — and the abort at call 65 of 139. A backstop that only trips
near the end is one nobody would have felt.
"""

import subprocess
from types import SimpleNamespace

import pytest

from code_gantry.config import ExecutorConfig
from code_gantry.edittools import FileEditor
from code_gantry.executorclient import (
    FRUITLESS_ABORT_AT,
    FRUITLESS_NUDGE_AT,
    OpenAIExecutorModel,
    _is_fruitless,
)
from code_gantry.gitops import Git
from code_gantry.repotools import ReadBudget, RepoReader

# Every call differs, so the repeat guard never counts past one. That is the
# whole point of the fixture: these are the arguments that defeated it.
MISSES = [
    '{"pattern": "nothing_like_this_%d", "path_glob": "app/**"}' % i
    for i in range(FRUITLESS_ABORT_AT + 4)
]
HIT = '{"pattern": "class A", "path_glob": "app/**"}'


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
        return self._responses.pop(0) if self._responses else response([finished()])


@pytest.fixture
def parts(tmp_path):
    repo = tmp_path / "t"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.rb").write_text("class A\nend\n")
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
    return (
        FileEditor(repo=repo, edit_files=["app/**"]),
        RepoReader(Git(repo), repo, ReadBudget()),
    )


def model(responses):
    return OpenAIExecutorModel(
        ExecutorConfig(model="gpt-5.6-luna"), client=ScriptedClient(responses)
    )


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


def searches(n, start=0):
    return [response([call("search", MISSES[start + i])]) for i in range(n)]


class TestWhatCounts:
    def test_a_refusal_and_an_empty_search_count_and_nothing_else_does(self):
        # Read off the rendered result, which this codebase warns against — so
        # the two facts it depends on are pinned here rather than assumed. If a
        # tool ever refuses without `dispatch`'s prefix, or a second tool
        # learns to answer "nothing", this is the test that fails.
        assert _is_fruitless("edit", "cannot do that: that text does not appear")
        assert _is_fruitless("search", "(no matches)")
        assert not _is_fruitless("search", "app/a.rb:1:class A")
        assert not _is_fruitless("read_file", "    1 | class A")
        assert not _is_fruitless("edit", "applied 1 edit(s) to app/a.rb")
        # A read that *mentions* a refusal in its content is not one.
        assert not _is_fruitless("read_file", "    1 | # cannot do that: x")


class TestTheNudge:
    def test_calls_below_the_threshold_are_answered_plainly(self, parts):
        editor, reader = parts
        m = model(searches(FRUITLESS_NUDGE_AT - 1) + [response([finished()])])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        answers = outputs(conversation)
        assert len(answers) == FRUITLESS_NUDGE_AT - 1
        for item in answers:
            assert "changed nothing" not in text_of(item)

    def test_the_nudge_is_appended_to_the_answer_rather_than_replacing_it(
        self, parts
    ):
        # The opposite of the repeat guard, and the difference is the point.
        # There the payload is one the model already has. Here it is a refusal,
        # which is the only actionable thing in the exchange — withholding it
        # would remove the input most likely to end the streak.
        editor, reader = parts
        m = model(searches(FRUITLESS_NUDGE_AT) + [response([finished()])])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        last = text_of(outputs(conversation)[-1])
        assert last.startswith("(no matches)")
        assert "changed nothing" in last
        assert "read_file" in last

    def test_a_call_that_answered_resets_the_streak(self, parts):
        editor, reader = parts
        m = model(
            searches(FRUITLESS_NUDGE_AT - 1)
            + [response([call("search", HIT)])]
            + searches(FRUITLESS_NUDGE_AT - 1, start=FRUITLESS_NUDGE_AT)
            + [response([finished()])]
        )
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        for item in outputs(conversation):
            assert "changed nothing" not in text_of(item)


class TestTheAbort:
    def test_the_attempt_stops_and_says_why(self, parts):
        editor, reader = parts
        m = model(searches(FRUITLESS_ABORT_AT + 2) + [response([finished()])])
        conversation = []
        out = m.run(conversation, reader=reader, editor=editor)

        assert out.unproductive_stop
        assert "changed nothing" in out.unproductive_stop
        assert out.stopped is True
        # Fewer answers than the model asked for: the loop left at the abort.
        assert len(outputs(conversation)) == FRUITLESS_ABORT_AT

    def test_the_aborting_call_is_still_answered(self, parts):
        # A declared call with no result leaves the conversation malformed for
        # the provider, and there is a next request whenever the attempt had
        # already edited something.
        editor, reader = parts
        m = model(searches(FRUITLESS_ABORT_AT + 2) + [response([finished()])])
        conversation = []
        m.run(conversation, reader=reader, editor=editor)

        last = text_of(outputs(conversation)[-1])
        assert last.startswith("(no matches)")
        assert "the attempt was stopped" in last

    def test_the_stop_travels_on_the_field_nodes_reads(self, parts):
        # One field for both shapes, named for the meaning. `nodes.execute`
        # asks a single question of it, so a stop added later inherits the
        # answer instead of needing a reader nobody remembers to update.
        from code_gantry.executor import ExecutionResult

        assert hasattr(ExecutionResult, "unproductive_stop")
        editor, reader = parts
        m = model(searches(FRUITLESS_ABORT_AT + 2) + [response([finished()])])
        out = m.run([], reader=reader, editor=editor)
        assert isinstance(out.unproductive_stop, str) and out.unproductive_stop


class TestTheThresholds:
    def test_the_nudge_sits_above_the_measured_p95(self):
        # 97% of 879 recorded attempts never exceed 3. Set at or below that and
        # the guard fires on ordinary work, which is how a ceiling becomes a
        # policy nobody chose.
        assert FRUITLESS_NUDGE_AT == 4

    def test_the_abort_leaves_room_for_the_nudge_to_work(self):
        assert FRUITLESS_ABORT_AT > FRUITLESS_NUDGE_AT
        # Eight nudged calls before the attempt ends. Lower and the nudge never
        # gets a chance; higher and the incident this was written for would
        # have run past its own halfway point before stopping.
        assert FRUITLESS_ABORT_AT - FRUITLESS_NUDGE_AT >= 8
