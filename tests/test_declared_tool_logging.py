"""A declared tool's command belongs in the tool log, not the timeline.

`CommandRunner` logs `$ <command>` and `  exit N in Xs` for everything it
spawns, which is right for the loop's own commands — lint, the suite, setup —
because those *are* the timeline. A declared tool is a model's tool call, and
the tool log is where every other tool call already goes.

Measured on one run: 27 `bundle install` / `bundle update` calls put 54 lines
into a 140-line `run.log`, and every one of them was already in `tools.log` as
`bundle_install() -> 11 line(s)`. The timeline was mostly a second copy of the
tool log, in a wider format, interleaved with the events it exists to show.

What the tool log needs from a declared call is that it happened and how it
ended. `11 line(s)` measures the answer's size, which is what a read tool's
entry means, and it is the wrong fact here — those eleven lines were bundler
saying it could not see its own gems. `exit 11` is the diagnosis; the output
itself is on the model's side of the conversation and in the artifacts.
"""

import pytest

from code_gantry.config import ProjectTool, ToolArgument


def a_tool(**overrides):
    fields = dict(
        name="bundle_install",
        description="Install what the manifest names.",
        command=["docker", "exec", "app", "bundle", "install"],
        roles=["executor"],
    )
    fields.update(overrides)
    return ProjectTool(**fields)


class Runner:
    """Records what it was asked to log, as `CommandRunner` would."""

    def __init__(self, exit_code=0, log=None):
        self.exit_code = exit_code
        self.logged: list[str] = []
        self._log = log or self.logged.append
        self.calls: list[list[str]] = []

    def run_argv(self, argv, timeout=None, log=...):
        self.calls.append(list(argv))
        sink = self._log if log is ... else log
        if sink:
            sink(f"$ {' '.join(argv)}\n  exit {self.exit_code} in 0.3s")

        class Result:
            command = " ".join(argv)
            exit_code = self.exit_code
            stdout = "Could not find mime-types-data-3.2026.0701"
            stderr = ""
            timed_out = False

        return Result()


class Reader:
    def __init__(self):
        self.recorded: list[tuple] = []

    def record_answer(self, tool, detail, text, exit_code=None):
        self.recorded.append((tool, detail, exit_code))
        return text


class TestTheTimelineStaysClean:
    def test_a_declared_call_writes_nothing_to_the_run_log(self):
        from code_gantry.projecttools import invoke

        runner = Runner(exit_code=11)
        invoke(a_tool(), {}, runner)
        assert runner.logged == [], "the declared command reached the run log"

    def test_the_command_still_runs(self):
        from code_gantry.projecttools import invoke

        runner = Runner()
        invoke(a_tool(), {}, runner)
        assert runner.calls == [["docker", "exec", "app", "bundle", "install"]]

    def test_the_model_still_sees_the_command_and_its_output(self):
        # Suppressing the log line must not narrow what the model is told: it
        # has to know what ran and what came back, or a failure is unreadable.
        from code_gantry.projecttools import invoke

        answer = invoke(a_tool(), {}, Runner(exit_code=11))
        assert "bundle install" in answer
        assert "exit 11" in answer
        assert "mime-types-data" in answer


class TestTheToolLogCarriesTheExitCode:
    def test_a_declared_entry_renders_its_exit_code(self):
        from code_gantry.planner import _render_call
        from code_gantry.repotools import ToolCall

        call = ToolCall(tool="bundle_install", detail="", lines=11, exit_code=11)
        assert _render_call(call) == "bundle_install() -> exit 11"

    def test_a_read_entry_still_renders_its_size(self):
        # `exit_code` is absent for everything that is not a spawned command,
        # and those entries must read exactly as they did.
        from code_gantry.planner import _render_call
        from code_gantry.repotools import ToolCall

        call = ToolCall(tool="read_file", detail="a.rb", lines=42)
        assert _render_call(call) == "read_file(a.rb) -> 42 line(s)"

    def test_a_refusal_still_wins(self):
        from code_gantry.planner import _render_call
        from code_gantry.repotools import ToolCall

        call = ToolCall(tool="gem_read", detail="x", lines=0, refusal="no such path")
        assert "refused: no such path" in _render_call(call)

    @pytest.mark.parametrize("code", [0, 1, 11])
    def test_the_exit_code_reaches_the_ledger(self, code):
        # Held is not recorded. This is the whole point of the change: the run
        # that spent fifteen minutes on `exit 11` had that number in the
        # timeline and nowhere a later reader would look.
        from code_gantry.plannertools import dispatch

        reader = Reader()
        dispatch(
            "bundle_install",
            {},
            reader=reader,
            semantic=None,
            project_tools=[a_tool(roles=["planner"])],
            runner=Runner(exit_code=code),
            role="planner",
        )
        assert reader.recorded == [("bundle_install", "bundle_install", code)]
