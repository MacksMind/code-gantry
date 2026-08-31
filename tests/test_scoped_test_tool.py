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


class TestTheReference:
    """`scoped_test_tool` names a declared tool, and the name is the project's.

    A literal in code — `run_tests` — would be one ecosystem's vocabulary
    shipped to every other, the same fault as a tool description reading
    `e.g. app/controllers/foo.rb`. So config points at an entry, the way
    `plan_root` points at a document, and the failure modes are a reference's:
    a name matching nothing, and a tool whose shape cannot serve the role.

    Naming it after the runner is what makes the correspondence exact. RSpec
    prints `rspec ./spec/a_spec.rb:79` in its own failure output, so with a
    tool called `rspec` the printed line *is* the call — a name on the left, a
    `paths` value on the right.
    """

    def _cfg(self, **over):
        tool = scoped_runner(**over.pop("tool", {}))
        data = minimal(project_tools=[tool], scoped_test_tool=over.pop("names", tool["name"]))
        data.update(over)
        return parse_config(data)

    def test_the_tool_is_resolved_by_name_and_not_by_position(self):
        """Two tools, and the named one is not the first.

        Written this way after a falsification pass failed to fail: with a
        single declared tool, resolving by name and taking `project_tools[0]`
        are the same answer, so the test could not tell them apart.
        """
        other = {
            "name": "bundle_install",
            "description": "Resolve the manifest.",
            "command": ["bundle", "install"],
            "roles": ["executor"],
        }
        cfg = parse_config(minimal(
            project_tools=[other, scoped_runner()], scoped_test_tool="run_tests"
        ))
        assert [t.name for t in cfg.project_tools][0] == "bundle_install"
        assert cfg.scoped_test_argv(["spec/a_spec.rb:79"]) == [
            "bin/rspec", "spec/a_spec.rb:79"
        ]

    def test_a_name_matching_nothing_is_refused(self):
        with pytest.raises(ConfigError) as e:
            self._cfg(names="no_such_tool")
        assert "not a declared project tool" in str(e.value)
        # Names what *is* declared, because the usual cause is a typo and the
        # answer is sitting in the same file.
        assert "run_tests" in str(e.value)

    def test_a_tool_with_no_repeated_argument_is_refused(self):
        with pytest.raises(ConfigError) as e:
            self._cfg(tool={"arguments": [], "command": ["bin/rspec"]})
        assert "no repeated argument" in str(e.value)

    def test_a_command_with_no_placeholder_is_refused(self):
        """Otherwise every "scoped" run would silently be a full one.

        Caught by the tool's own validator rather than by the reference, and
        the message is better for it — the reference only knows the tool
        cannot serve the role, while the tool knows exactly which argument
        reaches nothing. A second check here would be unreachable.
        """
        with pytest.raises(ConfigError) as e:
            self._cfg(tool={"command": ["bin/rspec"]})
        assert "the command never uses it" in str(e.value)

    def test_the_selection_is_required(self):
        data = minimal()
        data.pop("scoped_test_tool")
        data.pop("project_tools")
        with pytest.raises(ConfigError) as e:
            parse_config(data)
        assert "scoped_test_tool is not set" in str(e.value)

    def test_the_full_suite_stays_a_command(self):
        """No role runs it, so it is nobody's tool.

        The executor has no notion of `edit_files`: faced with a red spec
        outside the stage it will edit that spec, and a full run gives it
        minutes per pass in which to do so. With no model in the path there is
        nothing model-supplied to keep out of a shell, so this stays a string
        and an operator's `a && b` keeps working.
        """
        cfg = self._cfg(full_test_command="bin/dc_start app && bin/parallel_rspec")
        assert cfg.full_test_command == "bin/dc_start app && bin/parallel_rspec"
        assert not hasattr(cfg, "full_test_tool")


class TestTheScopedRunHasNoShell:
    """The property argv buys, exercised against a real process.

    The first version of this test used a bracket locator —
    `spec/a_spec.rb[1:6:1:3]` — on the reasoning that brackets are a shell
    glob. It could not fail: POSIX `sh` leaves an *unmatched* glob unchanged,
    so that token survives a shell by luck rather than by design. RSpec quotes
    it in its own output for the case where something does match.

    So the cases pinned here are ones a shell demonstrably changes: a path
    containing a space, which word-splits into two arguments, and a glob with
    a file present for it to expand to. Both arrive as one element under argv,
    and the model may paste either with or without RSpec's quoting.
    """

    def _echoes_argv(self, tmp_path):
        from test_config import runner_script

        seen = tmp_path / "argv.txt"
        script = runner_script(tmp_path, f'printf "%s\\n" "$@" > {seen}', "echo_argv")
        tool = ProjectTool(
            name="rspec",
            description="Run the suite.",
            command=[script, "{paths}"],
            arguments=[ToolArgument(name="paths", description="…", repeated=True)],
            roles=["executor"],
        )
        return tool, seen

    def _run(self, tmp_path, tool, paths):
        from code_gantry.commands import CommandRunner

        CommandRunner(cwd=tmp_path, timeout=30).run_argv(build_argv(tool, {"paths": paths}))

    def test_a_path_with_a_space_stays_one_argument(self, tmp_path):
        tool, seen = self._echoes_argv(tmp_path)
        path = "./spec/a spec_spec.rb:79"
        self._run(tmp_path, tool, [path])
        assert seen.read_text().splitlines() == [path]

    def test_a_locator_is_not_expanded_against_the_tree(self, tmp_path):
        # A file the glob would match, so a shell would replace the token with
        # the filename and the runner would be pointed somewhere else entirely.
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "a_spec.rb1").write_text("x\n")
        tool, seen = self._echoes_argv(tmp_path)
        locator = "spec/a_spec.rb[1:6:1:3]"
        self._run(tmp_path, tool, [locator])
        assert seen.read_text().splitlines() == [locator]
