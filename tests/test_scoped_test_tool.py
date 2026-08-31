"""A ceiling on a repeated argument, and why withholding a tool needs one.

The executor is never offered the whole suite. The reason is written down in
`resolve_test_command`: it has no notion of `edit_files`, so faced with a red
spec outside the stage it will edit that spec, and a full run gives it minutes
per pass in which to do so.

Offering it a *scoped* runner is a different thing and a good one. It reads
`rspec ./spec/a_spec.rb:500` in a failure and can run that one example — the
cleanest feedback the runner produces, with none of the parallel runner's
container churn around it. Measured on this project's own scripts:
`bin/parallel_rspec` tears down and recreates four Chromium containers per
invocation, which is the 37 lines and 1,749 characters of `Container ...
Started` that took 42% of one recorded feedback budget. `bin/rspec` starts one.

**But an uncapped repeated argument hands the withheld tool straight back.**
Enumerating every spec file is running everything, spelled differently. So the
boundary has to be made of something: a cap, enforced where the call is
dispatched rather than where the schema is advertised, because a model can name
a value the schema said was too long.

Five, from the tail rather than the middle. Over 375 recorded suite runs that
named failing examples: median 2, p95 4, **97.1% at five or fewer** — then a
near-empty band at 6-10 before the mass-failure runs of 21 and up, which are
exactly the ones nobody should be answering one path at a time.
"""

import pytest

from code_gantry.config import ConfigError, ProjectTool, ToolArgument, parse_config
from code_gantry.projecttools import build_argv, tool_schema
from code_gantry.repotools import ToolError

from test_config import minimal
from test_project_tools import Recorder

CAP = 5


def scoped_runner(**over):
    argument = {
        "name": "paths",
        "description": "Spec files, or single examples.",
        "repeated": True,
        "max_values": CAP,
    }
    argument.update(over.pop("argument", {}))
    tool = {
        "name": "run_tests",
        "description": "Run the suite against a selection.",
        "command": ["bin/rspec", "{paths}"],
        "arguments": [argument],
        "roles": ["executor"],
    }
    tool.update(over)
    return tool


def a_tool(**over):
    return ProjectTool(**scoped_runner(**over))


class TestTheCapIsEnforcedWhereItIsDispatched:
    def test_a_call_at_the_cap_runs(self):
        argv = build_argv(a_tool(), {"paths": [f"spec/s{i}_spec.rb" for i in range(CAP)]})
        assert argv[0] == "bin/rspec"
        assert len(argv) == CAP + 1

    def test_one_over_the_cap_runs_nothing(self):
        with pytest.raises(ToolError) as e:
            build_argv(a_tool(), {"paths": [f"spec/s{i}_spec.rb" for i in range(CAP + 1)]})
        assert "at most 5" in str(e.value)
        # Says what happened to the call, because "refused" and "ran a subset"
        # are the two readings and only one of them is true.
        assert "Nothing was run" in str(e.value)

    def test_the_schema_is_not_the_constraint(self):
        """A filter over what is advertised is not a limit on what is run.

        `maxItems` is a hint a provider may or may not enforce, and a model can
        name a tool argument it was never shown. If this ever passes by the
        schema alone the cap is decoration.
        """
        tool = a_tool()
        assert tool_schema(tool)["input_schema"]["properties"]["paths"]["maxItems"] == CAP
        with pytest.raises(ToolError):
            build_argv(tool, {"paths": ["spec/a_spec.rb"] * (CAP + 1)})

    def test_an_uncapped_argument_is_unaffected(self):
        tool = a_tool(argument={"max_values": None})
        argv = build_argv(tool, {"paths": [f"spec/s{i}_spec.rb" for i in range(40)]})
        assert len(argv) == 41
        assert "maxItems" not in tool_schema(tool)["input_schema"]["properties"]["paths"]


class TestTheCapIsDisclosed:
    """A ceiling the model cannot see is one it can only discover by spending."""

    def test_the_description_says_the_number(self):
        described = tool_schema(a_tool())["input_schema"]["properties"]["paths"]["description"]
        assert "At most 5" in described
        assert "Spec files, or single examples." in described

    def test_the_number_is_generated_rather_than_written(self):
        # A literal in the operator's prose is wrong the first time anyone
        # changes the cap, and nothing would fail.
        described = tool_schema(a_tool(argument={"max_values": 2}))[
            "input_schema"]["properties"]["paths"]["description"]
        assert "At most 2" in described
        assert "At most 5" not in described


class TestALocatorSurvivesUntouched:
    """The capability itself: what the runner printed is what gets run.

    Argv is what makes this safe unquoted. RSpec single-quotes the bracketed
    form in its own output because brackets are shell globs; there is no shell
    here, so the model may paste it either way and neither is interpreted.
    """

    @pytest.mark.parametrize(
        "locator",
        [
            "./spec/a_spec.rb:500",
            "spec/a_spec.rb:500",
            "./spec/a_spec.rb[1:6:1:3]",
            "'./spec/a_spec.rb[1:6:1:3]'",
        ],
    )
    def test_it_reaches_argv_verbatim(self, locator):
        assert build_argv(a_tool(), {"paths": [locator]}) == ["bin/rspec", locator]


class TestTheDeclarationIsChecked:
    def test_a_cap_on_a_single_value_is_refused(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(project_tools=[
                scoped_runner(argument={"repeated": False, "max_values": 3})
            ]))
        assert "not repeated" in str(e.value)

    def test_a_cap_no_call_could_satisfy_is_refused(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(project_tools=[scoped_runner(argument={"max_values": 0})]))
        assert "max_values 0" in str(e.value)

    def test_a_capped_repeated_argument_parses(self):
        cfg = parse_config(minimal(project_tools=[scoped_runner()]))
        assert cfg.project_tools[0].arguments[0].max_values == CAP


class TestARefusedCallReachesTheLedger:
    """A withheld call that leaves no trace renders as two calls and silence.

    `tools.log`, `tool_counts` and `refusal_counts` are all built from the
    reader's and editor's ledgers, and only `dispatch` writes to them. So a cap
    that answers without dispatching owes an entry — otherwise the one thing
    that would tell an operator the ceiling is being hit is the absence of
    something, which is what nobody notices.

    It is also what makes the cap measurable later. A limit is only knowable to
    be right once something records being refused by it.
    """

    def _dispatch(self, paths, editor):
        from code_gantry.executortools import dispatch

        cfg = parse_config(minimal(project_tools=[scoped_runner()]))
        return dispatch(
            "run_tests",
            {"paths": paths},
            reader=None,
            editor=editor,
            semantic=None,
            project_tools=cfg.project_tools,
            runner=Recorder(),
        )

    def test_the_over_cap_call_is_recorded_as_a_refusal(self, tmp_path):
        from code_gantry.edittools import FileEditor

        editor = FileEditor(repo=tmp_path, edit_files=["**"])
        out = self._dispatch([f"spec/s{i}_spec.rb" for i in range(CAP + 1)], editor)

        assert out.startswith("cannot do that:")
        assert "at most 5" in out
        assert len(editor.calls) == 1, "the withheld call owes the ledger an entry"

    def test_a_call_within_the_cap_is_not_refused(self, tmp_path):
        from code_gantry.edittools import FileEditor

        editor = FileEditor(repo=tmp_path, edit_files=["**"])
        out = self._dispatch(["spec/a_spec.rb"], editor)
        assert not out.startswith("cannot do that:")

    def test_the_refusal_counts_toward_the_stall_guard(self, tmp_path):
        """`cannot do that:` is the prefix the fruitless-call guard reads.

        A model that answers a cap by trying again with a different over-long
        list is going nowhere, and that is the shape the guard exists for. The
        coupling is through a rendered string, so it is pinned rather than
        assumed.
        """
        from code_gantry.edittools import FileEditor
        from code_gantry.executorclient import _is_fruitless

        editor = FileEditor(repo=tmp_path, edit_files=["**"])
        out = self._dispatch([f"spec/s{i}_spec.rb" for i in range(CAP + 1)], editor)
        assert _is_fruitless("run_tests", out)
