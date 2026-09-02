"""Operator-declared tools the executor may call.

CodeGantry ships five read tools and three edit tools. A migration needs
more than that — `bundle install` after a manifest edit, `bundle update rails`
at the framework bump, an asset precompile — and every one of those is project
knowledge. Encoding a `dependencies:` block in Python would put "dependency
management" into CodeGantry as a domain it knows about, and the next
command would need another block. So config declares a menu and the model
reaches for it, indistinguishable from the built-ins.

Two properties carry the safety story and both are tested here rather than
asserted in prose.

**Argv, never a shell.** The command is a list and is spawned with
`shell=False`, so a model-supplied argument containing `;` or `&&` is one inert
element that makes the underlying tool error. This is what makes it safe for the
executor to supply arguments at all — there is no metacharacter to escape
because nothing interprets them. A declared command whose argv[0] is a shell
would hand that property back, so it is refused.

**The scope gate is the authorization.** Nothing here gates which stage may call
which tool. A tool that writes `Gemfile.lock` on a stage that did not declare it
fails the scope gate, which reads the tree rather than a declaration — and a
second gate in front of it would be a permission used to predict an outcome the
existing one already measures.
"""

import pytest

from code_gantry.config import ConfigError, parse_config

from test_config import minimal


def a_tool(**overrides):
    tool = {
        "name": "sync_dependencies",
        "description": "Resolve the manifest and install what it names.",
        "command": ["bundle", "install"],
    }
    tool.update(overrides)
    return tool


def cfg_with(*tools, **overrides):
    return parse_config(minimal(project_tools=[*tools], **overrides))


class TestDeclaration:
    def test_a_tool_with_no_arguments_parses(self):
        cfg = cfg_with(a_tool())
        assert [t.name for t in cfg.project_tools if t.name != 'scoped_suite'] == ["sync_dependencies"]
        assert cfg.project_tools[0].command == ["bundle", "install"]

    def test_no_tools_declared_is_the_default(self):
        """Asserted on the schema, because the fixture can no longer show it.

        `scoped_test_tool` is required and names an entry here, so every
        parsed config now carries at least that one. The property being
        pinned — an operator who declares nothing gets nothing — survives as
        the field's own default.
        """
        from code_gantry.config import ProjectConfig

        field = ProjectConfig.model_fields["project_tools"]
        assert (field.default_factory() if field.default_factory else field.default) == []

    def test_an_argument_slot_parses(self):
        cfg = cfg_with(
            a_tool(
                name="update_dependencies",
                command=["bundle", "update", "{names}"],
                arguments=[
                    {
                        "name": "names",
                        "description": "Dependencies to re-resolve.",
                        "repeated": True,
                    }
                ],
            )
        )
        assert cfg.project_tools[0].arguments[0].repeated is True

    def test_name_must_be_a_plain_identifier(self):
        # It becomes a tool name on the wire and a key in a schema.
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(name="bundle install"))
        assert "bundle install" in str(e.value)

    def test_name_must_not_shadow_a_built_in(self):
        # Two tools with one name is not a merge, it is whichever the provider
        # happens to pick — and the model cannot tell it got the wrong one.
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(name="read_file"))
        assert "read_file" in str(e.value)

    def test_two_declared_tools_may_not_share_a_name(self):
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(), a_tool(command=["bundle", "check"]))
        assert "sync_dependencies" in str(e.value)

    def test_command_may_not_be_empty(self):
        with pytest.raises(ConfigError):
            cfg_with(a_tool(command=[]))

    def test_a_description_is_required(self):
        # It is the whole of what the model knows about the tool. A tool with
        # no description is a button with no label.
        with pytest.raises(ConfigError):
            cfg_with({"name": "x", "command": ["true"]})


class TestNoShell:
    """The property that makes model-supplied arguments safe."""

    @pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "/bin/sh", "/bin/bash"])
    def test_a_shell_as_argv0_is_refused(self, shell):
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(command=[shell, "-c", "bundle install"]))
        assert "shell" in str(e.value).lower()

    def test_the_reason_names_what_it_protects(self):
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(command=["bash", "-lc", "x"]))
        # An operator reading this must learn why, or they will route around it.
        assert "argument" in str(e.value).lower()


class TestPlaceholders:
    def test_a_placeholder_needs_a_matching_argument(self):
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(command=["bundle", "update", "{names}"]))
        assert "names" in str(e.value)

    def test_an_argument_needs_a_matching_placeholder(self):
        # Otherwise the model is asked for a value that reaches nothing, which
        # reads to it as the tool ignoring what it said.
        with pytest.raises(ConfigError) as e:
            cfg_with(
                a_tool(
                    arguments=[{"name": "names", "description": "d", "repeated": True}]
                )
            )
        assert "names" in str(e.value)

    def test_a_placeholder_must_be_a_whole_argv_element(self):
        # `--gems={names}` would need us to decide how a repeated value joins,
        # and every answer is a quoting rule. One element, one value.
        with pytest.raises(ConfigError) as e:
            cfg_with(
                a_tool(
                    command=["bundle", "update", "--gems={names}"],
                    arguments=[{"name": "names", "description": "d"}],
                )
            )
        assert "{names}" in str(e.value)

    def test_shell_braces_are_not_placeholders(self):
        # The check was "does this element contain a `{` and a `}`", which was
        # exact while no declared command could contain shell syntax. It can
        # now: `sh` is refused as argv[0] and allowed as a later element, which
        # is how an operator writes a tool that resolves a path and then reads
        # under it — and the model's values arrive as positional parameters, so
        # nothing is interpolated into the script. Every such script has braces:
        # `|| { echo "$p" >&2; exit 1; }`, `awk "{ print }"`. Refusing them
        # rejected a working tool for containing a character.
        script = 'p=$(resolve "$1") || { echo "$p" >&2; exit 1; }\nawk "{ print }"'
        cfg = cfg_with(
            a_tool(
                command=["docker", "exec", "c", "sh", "-c", script, "_", "{gem}"],
                arguments=[{"name": "gem", "description": "d"}],
            )
        )
        assert cfg.project_tools[0].command[5] == script

    def test_a_shell_variable_in_braces_is_not_a_placeholder(self):
        # `${gem}` is the shell's own expansion of a positional or named
        # variable and names nothing of ours. Catching it would refuse a
        # correct script for spelling a variable the long way.
        cfg = cfg_with(
            a_tool(
                command=["docker", "exec", "c", "sh", "-c", 'echo "${gem}"', "{gem}"],
                arguments=[{"name": "gem", "description": "d"}],
            )
        )
        assert cfg.project_tools[0].command[5] == 'echo "${gem}"'

    def test_an_embedded_placeholder_is_still_caught_beside_shell_braces(self):
        # The guard has to survive the loosening: a real embedded placeholder
        # in a script that also has legitimate braces is exactly the case a
        # laxer check would wave through.
        with pytest.raises(ConfigError) as e:
            cfg_with(
                a_tool(
                    command=[
                        "docker", "exec", "c", "sh", "-c",
                        'x() { echo hi; }; run --gem={gem}',
                        "{gem}",
                    ],
                    arguments=[{"name": "gem", "description": "d"}],
                )
            )
        assert "{gem}" in str(e.value)


class TestDenylist:
    def test_a_declared_tool_is_covered_by_the_denylist(self):
        # `all_commands` is deliberately exhaustive rather than reflective, so
        # a new executable field silently drops out of denylist coverage. This
        # is that field.
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(command=["git", "push", "origin", "main"]))
        assert "denylist" in str(e.value)

    def test_all_commands_reports_where_it_came_from(self):
        cfg = cfg_with(a_tool())
        where = dict((w, c) for w, c in cfg.all_commands())
        assert any("sync_dependencies" in w for w in where)


class TestSchemas:
    def test_a_declared_tool_reaches_the_executor_menu(self):
        from code_gantry.executortools import tool_schemas

        cfg = cfg_with(a_tool())
        names = [t["name"] for t in tool_schemas(None, cfg.project_tools)]
        assert "sync_dependencies" in names

    def test_the_operator_description_is_what_the_model_reads(self):
        from code_gantry.executortools import tool_schemas

        cfg = cfg_with(a_tool(description="Runs the thing. Slow."))
        tool = next(
            t
            for t in tool_schemas(None, cfg.project_tools)
            if t["name"] == "sync_dependencies"
        )
        assert tool["description"] == "Runs the thing. Slow."

    def test_a_zero_argument_tool_takes_no_properties(self):
        from code_gantry.executortools import tool_schemas

        cfg = cfg_with(a_tool())
        tool = next(
            t
            for t in tool_schemas(None, cfg.project_tools)
            if t["name"] == "sync_dependencies"
        )
        assert tool["input_schema"]["properties"] == {}

    def test_a_repeated_argument_is_an_array_of_strings(self):
        from code_gantry.executortools import tool_schemas

        cfg = cfg_with(
            a_tool(
                command=["bundle", "update", "{names}"],
                arguments=[{"name": "names", "description": "d", "repeated": True}],
            )
        )
        tool = next(
            t
            for t in tool_schemas(None, cfg.project_tools)
            if t["name"] == "sync_dependencies"
        )
        spec = tool["input_schema"]["properties"]["names"]
        assert spec["type"] == "array"
        assert spec["items"]["type"] == "string"

    def test_strict_mode_survives_a_declared_tool(self):
        # The SDK refuses to auto-parse otherwise, and a declared tool is the
        # first schema built from operator input rather than a literal here.
        from code_gantry.executortools import openai_tool_schemas

        cfg = cfg_with(
            a_tool(
                command=["bundle", "update", "{names}"],
                arguments=[{"name": "names", "description": "d", "repeated": True}],
            )
        )
        tool = next(
            t
            for t in openai_tool_schemas(None, cfg.project_tools)
            if t["name"] == "sync_dependencies"
        )
        assert tool["strict"] is True
        assert tool["parameters"]["additionalProperties"] is False
        assert tool["parameters"]["required"] == ["names"]


class TestTheFrameworkNamesNothing:
    """Tool *names* are the project's; the strings around them are not.

    `bundle_install` belongs in a config, and calling it `sync_dependencies`
    would only make it harder for a model to know what it does. But everything
    CodeGantry itself authors on this path ships to every project, and
    the two places it authors anything are the refusal messages and the result
    header. Pinned by the names, because the leak that has happened here twice
    is prose illustrated with whatever repository was in front of the author.
    """

    def _authored_strings(self):
        from code_gantry.projecttools import build_argv, render
        from code_gantry.repotools import ToolError

        tool = cfg_with(
            a_tool(
                command=["prog", "{names}", "{one}"],
                arguments=[
                    {"name": "names", "description": "d", "repeated": True},
                    {"name": "one", "description": "d"},
                ],
            )
        ).project_tools[0]

        said = []
        for args in ({}, {"names": []}, {"names": ["x"]}, {"names": ["x"], "one": 3}):
            try:
                build_argv(tool, args, capped=True)
            except ToolError as e:
                said.append(str(e))

        said.append(render(Recorder(exit_code=1, stdout="", stderr="").run_argv(["p"])))
        return " ".join(said).lower()

    def test_no_ecosystem_appears_in_what_we_author(self):
        text = self._authored_strings()
        for word in (
            "rails", "ruby", "gemfile", "rspec", "bundler", "bundle",
            "gem", "npm", "yarn", "cargo", "pip", "poetry", "go.mod",
        ):
            assert word not in text, f"{word!r} is project knowledge in a shipped string"

    def test_the_refusals_still_name_the_argument(self):
        # The guard above must not be satisfiable by saying nothing useful.
        assert "names" in self._authored_strings()


class Recorder:
    """Stands in for `CommandRunner`, capturing argv rather than running it."""

    def __init__(self, exit_code=0, stdout="ok", stderr=""):
        self.argv = None
        self.timeout = None
        self._result = (exit_code, stdout, stderr)

    # `log` is part of the contract: a declared tool passes `log=None` so its
    # command stays out of the run log. A fake that omits it passes while the
    # real runner would reject the call.
    def run_argv(self, argv, timeout=None, log=None):
        from code_gantry.commands import CommandResult

        self.argv = list(argv)
        self.timeout = timeout
        code, out, err = self._result
        return CommandResult(
            command=" ".join(argv),
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_seconds=0.1,
            timed_out=False,
        )


class TestInvocation:
    def _tool(self, **over):
        return cfg_with(a_tool(**over)).project_tools[0]

    def test_a_zero_argument_tool_runs_its_command(self):
        from code_gantry.projecttools import invoke

        runner = Recorder()
        invoke(self._tool(), {}, runner)
        assert runner.argv == ["bundle", "install"]

    def test_a_repeated_argument_expands_in_place(self):
        from code_gantry.projecttools import invoke

        tool = self._tool(
            command=["bundle", "update", "{names}"],
            arguments=[{"name": "names", "description": "d", "repeated": True}],
        )
        runner = Recorder()
        invoke(tool, {"names": ["rails", "nokogiri"]}, runner)
        assert runner.argv == ["bundle", "update", "rails", "nokogiri"]

    def test_a_scalar_argument_becomes_one_element(self):
        from code_gantry.projecttools import invoke

        tool = self._tool(
            command=["rake", "{task}"],
            arguments=[{"name": "task", "description": "d"}],
        )
        runner = Recorder()
        invoke(tool, {"task": "db:migrate"}, runner)
        assert runner.argv == ["rake", "db:migrate"]

    def test_a_metacharacter_is_one_inert_element(self):
        # The whole reason arguments can be model-supplied.
        from code_gantry.projecttools import invoke

        tool = self._tool(
            command=["bundle", "update", "{names}"],
            arguments=[{"name": "names", "description": "d", "repeated": True}],
        )
        runner = Recorder()
        invoke(tool, {"names": ["rails; rm -rf /"]}, runner)
        assert runner.argv == ["bundle", "update", "rails; rm -rf /"]

    def test_an_empty_repeated_argument_is_refused(self):
        # Not "then update everything": an omitted scope must never widen to
        # the unscoped command by accident.
        from code_gantry.projecttools import invoke
        from code_gantry.repotools import ToolError

        tool = self._tool(
            command=["bundle", "update", "{names}"],
            arguments=[{"name": "names", "description": "d", "repeated": True}],
        )
        runner = Recorder()
        with pytest.raises(ToolError):
            invoke(tool, {"names": []}, runner)
        assert runner.argv is None, "nothing may run when an argument is missing"

    def test_a_missing_required_argument_is_refused(self):
        from code_gantry.projecttools import invoke
        from code_gantry.repotools import ToolError

        tool = self._tool(
            command=["rake", "{task}"],
            arguments=[{"name": "task", "description": "d"}],
        )
        with pytest.raises(ToolError):
            invoke(tool, {}, Recorder())

    def test_a_non_string_argument_is_refused(self):
        from code_gantry.projecttools import invoke
        from code_gantry.repotools import ToolError

        tool = self._tool(
            command=["rake", "{task}"],
            arguments=[{"name": "task", "description": "d"}],
        )
        with pytest.raises(ToolError):
            invoke(tool, {"task": {"nested": "object"}}, Recorder())


class TestWhatTheModelSeesBack:
    def _tool(self, **over):
        return cfg_with(a_tool(**over)).project_tools[0]

    def test_success_carries_the_exit_code(self):
        from code_gantry.projecttools import invoke

        out = invoke(self._tool(), {}, Recorder(exit_code=0, stdout="Bundle complete"))
        assert "exit 0" in out
        assert "Bundle complete" in out

    def test_failure_is_returned_as_text_not_raised(self):
        # The point of the tool is that the model learns immediately. An
        # exception here would end the cycle instead of informing it.
        from code_gantry.projecttools import invoke

        out = invoke(
            self._tool(),
            {},
            Recorder(exit_code=1, stdout="", stderr="Could not find gem 'x'"),
        )
        assert "exit 1" in out
        assert "Could not find gem 'x'" in out

    def test_stderr_is_included_even_when_stdout_is_empty(self):
        # Bundler puts its resolution errors on stderr, and an empty answer
        # reads to a model as "nothing happened".
        from code_gantry.projecttools import invoke

        out = invoke(self._tool(), {}, Recorder(exit_code=1, stdout="", stderr="boom"))
        assert "boom" in out


class TestDispatch:
    def test_a_declared_tool_dispatches_to_its_command(self):
        from code_gantry.executortools import dispatch

        cfg = cfg_with(a_tool())
        runner = Recorder()
        out = dispatch(
            "sync_dependencies",
            {},
            reader=None,
            editor=None,
            semantic=None,
            project_tools=cfg.project_tools,
            runner=runner,
        )
        assert runner.argv == ["bundle", "install"]
        assert "exit 0" in out

    def test_a_refusal_comes_back_as_text(self):
        from code_gantry.executortools import dispatch

        cfg = cfg_with(
            a_tool(
                command=["bundle", "update", "{names}"],
                arguments=[{"name": "names", "description": "d", "repeated": True}],
            )
        )
        out = dispatch(
            "sync_dependencies",
            {"names": []},
            reader=None,
            editor=None,
            semantic=None,
            project_tools=cfg.project_tools,
            runner=Recorder(),
        )
        assert "cannot do that" in out


class TestWiring:
    """The journey, not the endpoints.

    Four defects on this project have been values computed correctly, written
    correctly, and lost in transit — and the shape here is the one that has
    already broken twice at this exact seam: `tool_log` was added to
    `OpenAIExecutorModel`, the caller existed, and nothing joined them. A
    declared tool that never reaches the provider call is a config key that
    silently does nothing, which is the failure this codebase refuses
    everywhere else.
    """

    def test_the_model_is_handed_the_declared_tools_and_a_runner(
        self, repo, tmp_path, monkeypatch
    ):
        from code_gantry import executorclient
        from code_gantry.config import Stage

        from test_runtime import a_config

        seen = {}

        class Spy:
            def __init__(self, cfg, client=None, log=None, tool_log=None, **kwargs):
                seen.update(kwargs)

            def run(self, *a, **kw):  # pragma: no cover - never reached
                raise AssertionError

        monkeypatch.setattr(executorclient, "OpenAIExecutorModel", Spy)

        cfg = a_config(repo)
        cfg = cfg.model_copy(
            update={"project_tools": cfg_with(a_tool()).project_tools}
        )
        from code_gantry.commands import CommandRunner
        from code_gantry.executor import Executor

        runner = CommandRunner(cwd=repo, timeout=60)
        executor = Executor(cfg, runner)
        try:
            executor.run_agent_stage(
                Stage(id="s", instruction="do", edit_files=["a.py"]), "prompt"
            )
        except AssertionError:
            pass

        declared = [t.name for t in seen.get("project_tools", []) if t.name != "scoped_suite"]
        assert declared == ["sync_dependencies"]
        assert seen.get("runner") is runner

    def test_a_declared_tool_reaches_the_schemas_the_provider_is_sent(self):
        # One step further than construction: held is not sent.
        from code_gantry.executorclient import OpenAIExecutorModel

        cfg = cfg_with(a_tool())
        model = OpenAIExecutorModel(
            cfg.executor,
            client=object(),
            project_tools=cfg.project_tools,
            runner=Recorder(),
        )
        names = [t["name"] for t in model._tools(None)]
        assert "sync_dependencies" in names
