"""Which roles may call an operator-declared tool.

`project_tools` shipped as an executor feature, and the executor was the only
role that could reach one: `planner.py` stored the declared list and used it in
exactly two places, both of them prompt text — the capability paragraph telling
the planner what the *executor* can do. So a project could declare a tool the
planner needed and the planner still could not call it.

That gap was found the expensive way. A migration's gem source lives in a
Docker volume rather than the work tree, so `search` and `read_file` cannot see
it at all, and the planner said so: it could not read what the framework
actually does. Declaring a container search as a project tool would not have
helped, because the role that wanted it was not offered it.

The fix is not "give every tool to everybody". The declared menu already
contains commands that write — `bundle install` rewrites the lockfile,
`rails app:update` overwrites templated config — and the executor is the only
role that runs inside the quarantine a stage branch provides. A planner that
dirtied the tree mid-derivation would fail the *next* stage's precheck, on a
stage with nothing wrong with it.

So a tool declares its audience. `roles` defaults to `["executor"]`, which is
what every declaration written before this meant, and an operator opts a
read-only tool into the roles that need it.

The scoping is enforced where the call is *run*, not only where the schema is
built. A model that names a tool it was not offered must be refused rather than
obeyed — a filter over what is advertised is not a boundary, which this
codebase has now learned twice.
"""

import pytest

from orchestrator.config import ConfigError, ProjectTool, ToolArgument, parse_config

from test_config import minimal


def a_tool(**overrides):
    tool = {
        "name": "gem_search",
        "description": "Search one gem's installed source, in the app container.",
        "command": ["docker", "compose", "exec", "-T", "app", "grep", "{pattern}"],
        "arguments": [{"name": "pattern", "description": "A regular expression."}],
    }
    tool.update(overrides)
    return tool


def cfg_with(*tools, **overrides):
    return parse_config(minimal(project_tools=[*tools], **overrides))


def declared(**overrides):
    """The same tool as a model object, for the callers that take one."""
    fields = dict(
        name="gem_search",
        description="Search one gem's installed source, in the app container.",
        command=["docker", "compose", "exec", "-T", "app", "grep", "{pattern}"],
        arguments=[ToolArgument(name="pattern", description="A regular expression.")],
    )
    fields.update(overrides)
    return ProjectTool(**fields)


class FakeRunner:
    """Enough of `CommandRunner` to prove a call reached one."""

    def __init__(self, stdout="lib/a.rb:1:hit"):
        self.calls = []
        self._stdout = stdout

    def run_argv(self, argv, timeout=None):
        self.calls.append(list(argv))

        class Result:
            command = " ".join(argv)
            exit_code = 0
            stdout = self._stdout
            stderr = ""
            timed_out = False

        return Result()


class TestDeclaration:
    def test_the_default_audience_is_the_executor(self):
        # Every declaration written before this field existed meant exactly
        # this, so adding it must not move a single existing tool.
        assert cfg_with(a_tool()).project_tools[0].roles == ["executor"]

    def test_a_tool_can_be_offered_to_the_planner(self):
        cfg = cfg_with(a_tool(roles=["planner"]))
        assert cfg.project_tools[0].roles == ["planner"]

    def test_a_tool_can_be_offered_to_several_roles(self):
        cfg = cfg_with(a_tool(roles=["planner", "executor", "reviewer"]))
        assert cfg.project_tools[0].roles == ["planner", "executor", "reviewer"]

    def test_an_unknown_role_is_refused(self):
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(roles=["operator"]))
        # Naming the three that exist is what an operator can act on; echoing
        # back what they typed is not.
        message = str(e.value)
        assert "planner" in message and "executor" in message
        assert "reviewer" in message

    def test_an_empty_audience_is_refused(self):
        # A tool nobody may call is a declaration with no effect, and it reads
        # in config exactly like one that works.
        with pytest.raises(ConfigError) as e:
            cfg_with(a_tool(roles=[]))
        assert "gem_search" in str(e.value)

    def test_a_repeated_role_is_refused(self):
        with pytest.raises(ConfigError):
            cfg_with(a_tool(roles=["planner", "planner"]))


class TestSelection:
    def test_it_selects_by_role(self):
        from orchestrator.projecttools import for_role

        planner_only = declared(name="gem_search", roles=["planner"])
        executor_only = declared(name="bundle_install", roles=["executor"])
        both = declared(name="gem_read", roles=["planner", "executor"])
        tools = [planner_only, executor_only, both]

        assert [t.name for t in for_role("planner", tools)] == [
            "gem_search",
            "gem_read",
        ]
        assert [t.name for t in for_role("executor", tools)] == [
            "bundle_install",
            "gem_read",
        ]
        assert for_role("reviewer", tools) == []

    def test_no_tools_is_not_an_error(self):
        from orchestrator.projecttools import for_role

        assert for_role("planner", None) == []
        assert for_role("planner", []) == []


class TestThePlannerIsOfferedItsOwn:
    def test_a_planner_tool_appears_in_the_schemas(self):
        from orchestrator.plannertools import tool_schemas

        names = [
            t["name"]
            for t in tool_schemas(None, [declared(roles=["planner"])])
        ]
        assert "gem_search" in names

    def test_an_executor_only_tool_does_not(self):
        from orchestrator.plannertools import tool_schemas

        names = [
            t["name"]
            for t in tool_schemas(None, [declared(roles=["executor"])])
        ]
        assert "gem_search" not in names

    def test_the_built_ins_are_still_there(self):
        from orchestrator.plannertools import tool_schemas

        names = [t["name"] for t in tool_schemas(None, [declared(roles=["planner"])])]
        assert "read_file" in names and "search" in names

    def test_the_reviewers_rendering_carries_it_too(self):
        # Two renderings of one definition. The reviewer is on the other
        # provider, and a tool it is offered must survive `strict` mode.
        from orchestrator.plannertools import openai_tool_schemas

        tools = openai_tool_schemas(None, [declared(roles=["reviewer"])])
        mine = [t for t in tools if t["name"] == "gem_search"]
        assert mine, "the declared tool did not survive the strict rendering"
        assert mine[0]["strict"] is True
        assert mine[0]["parameters"]["additionalProperties"] is False
        assert mine[0]["parameters"]["required"] == ["pattern"]


class TestRunningOne:
    def test_a_planner_tool_runs_through_the_runner(self):
        from orchestrator.plannertools import dispatch

        runner = FakeRunner()
        out = dispatch(
            "gem_search",
            {"pattern": "unpermitted"},
            reader=None,
            semantic=None,
            project_tools=[declared(roles=["planner"])],
            runner=runner,
            role="planner",
        )
        assert runner.calls == [
            ["docker", "compose", "exec", "-T", "app", "grep", "unpermitted"]
        ]
        assert "lib/a.rb:1:hit" in out

    def test_a_tool_the_role_was_not_offered_is_refused(self):
        # The boundary is here, not in the schema. A model can name anything;
        # advertising is not authorization, and a filter over what is offered
        # would leave the command reachable to whoever asked for it by name.
        from orchestrator.plannertools import dispatch

        runner = FakeRunner()
        out = dispatch(
            "gem_search",
            {"pattern": "unpermitted"},
            reader=None,
            semantic=None,
            project_tools=[declared(roles=["executor"])],
            runner=runner,
            role="planner",
        )
        assert runner.calls == [], "an unoffered tool was run"
        assert "gem_search" in out

    def test_without_a_runner_it_says_so_rather_than_raising(self):
        # Every other failure in this dispatch becomes readable text, because
        # the planner has to be able to answer with what it has.
        from orchestrator.plannertools import dispatch

        out = dispatch(
            "gem_search",
            {"pattern": "x"},
            reader=None,
            semantic=None,
            project_tools=[declared(roles=["planner"])],
            runner=None,
            role="planner",
        )
        assert "cannot" in out.lower() or "no " in out.lower()

    def test_a_refusal_from_the_tool_is_returned_not_raised(self):
        from orchestrator.plannertools import dispatch

        runner = FakeRunner()
        out = dispatch(
            "gem_search",
            {},  # the required argument is missing
            reader=None,
            semantic=None,
            project_tools=[declared(roles=["planner"])],
            runner=runner,
            role="planner",
        )
        assert runner.calls == [], "argv was built and run without its argument"
        assert "pattern" in out


class TestHowADeclaredCallIsNamedInTheLedger:
    """A declared call is named by its own arguments, in declaration order.

    `plannertools.call_detail` picks the one field worth naming a call by, from
    a fixed list — `path`, `pattern`, `glob`, `question`, `ref` — which is right
    for the five built-in read tools and is a guess about anything else. Applied
    to declared tools it went wrong in both directions on the first two written:
    a search taking `(gem, pattern, glob)` was logged under its *pattern*, so
    the ledger could not say which dependency was searched; and a read taking
    `(gem, file, first_line, last_line)` matched nothing in the list at all and
    logged with no detail whatsoever.

    The fix takes the order from the config rather than a list in code — the
    operator declares the identifying argument first because that is how a
    signature reads, and the renderer has no opinion about what any of them
    mean. Nothing here can name a gem, a path or a line number.
    """

    def _detail(self, tool, args):
        from orchestrator.projecttools import call_detail

        return call_detail(tool, args)

    def test_every_argument_is_named_in_declaration_order(self):
        tool = declared(
            name="gem_search",
            command=["x", "{gem}", "{pattern}", "{glob}"],
            arguments=[
                ToolArgument(name="gem", description="d"),
                ToolArgument(name="pattern", description="d"),
                ToolArgument(name="glob", description="d"),
            ],
        )
        detail = self._detail(
            tool, {"gem": "paperclip", "pattern": "validate_attachment", "glob": "*.rb"}
        )
        assert detail.index("paperclip") < detail.index("validate_attachment")
        assert "*.rb" in detail

    def test_a_tool_whose_arguments_match_nothing_known_still_says_something(self):
        tool = declared(
            name="gem_read",
            command=["x", "{gem}", "{file}"],
            arguments=[
                ToolArgument(name="gem", description="d"),
                ToolArgument(name="file", description="d"),
            ],
        )
        assert "actionpack" in self._detail(tool, {"gem": "actionpack", "file": "a.rb"})

    def test_a_missing_argument_does_not_lose_the_others(self):
        # The detail is what a refusal is recorded under, and a refusal is
        # exactly the case where an argument is absent.
        tool = declared(
            name="gem_read",
            command=["x", "{gem}", "{file}"],
            arguments=[
                ToolArgument(name="gem", description="d"),
                ToolArgument(name="file", description="d"),
            ],
        )
        assert "actionpack" in self._detail(tool, {"gem": "actionpack"})

    def test_a_long_value_cannot_crowd_out_the_rest(self):
        tool = declared(
            name="gem_search",
            command=["x", "{gem}", "{pattern}"],
            arguments=[
                ToolArgument(name="gem", description="d"),
                ToolArgument(name="pattern", description="d"),
            ],
        )
        detail = self._detail(tool, {"gem": "rails", "pattern": "z" * 500})
        assert "rails" in detail
        assert len(detail) < 200

    def test_it_reaches_the_ledger(self):
        # Held is not recorded. The whole point is the line an operator reads.
        from orchestrator.plannertools import dispatch

        class Reader:
            def __init__(self):
                self.recorded = []

            def record_answer(self, tool, detail, text):
                self.recorded.append((tool, detail))
                return text

        tool = declared(
            name="gem_search",
            roles=["planner"],
            command=["docker", "{gem}", "{pattern}"],
            arguments=[
                ToolArgument(name="gem", description="d"),
                ToolArgument(name="pattern", description="d"),
            ],
        )
        reader = Reader()
        dispatch(
            "gem_search",
            {"gem": "paperclip", "pattern": "attachment"},
            reader=reader,
            semantic=None,
            project_tools=[tool],
            runner=FakeRunner(),
            role="planner",
        )
        assert reader.recorded == [("gem_search", "paperclip, attachment")]


class TestTheExecutorRecordsOneToo:
    """A declared call that worked has to be in the ledger, not only one that didn't.

    The executor recorded a refused declared call on the editor's ledger and an
    answered one nowhere — so a tool that ran and returned appeared in no tool
    log, no per-cycle count, and no budget, while the same tool failing showed
    up. The ledger therefore listed only the failures, which is the shape this
    codebase has been bitten by before: a step that succeeded and a step that
    never happened rendering identically.
    """

    def test_an_answered_call_is_recorded(self):
        from orchestrator.executortools import dispatch

        class Reader:
            def __init__(self):
                self.recorded = []

            def record_answer(self, tool, detail, text):
                self.recorded.append((tool, detail))
                return text

        reader = Reader()
        dispatch(
            "gem_search",
            {"pattern": "x"},
            reader=reader,
            editor=None,
            semantic=None,
            project_tools=[declared(roles=["executor"])],
            runner=FakeRunner(),
        )
        assert reader.recorded == [("gem_search", "x")]


class TestTheConventionsFramingStopsContradictingTheMenu:
    """A fixed sentence about what a role cannot do, found by sweeping.

    `_conventions_block` tells the executor that a procedure in an agent-facing
    document "is never something for you to carry out" because "you cannot run
    commands". That was true of every project until `project_tools` shipped and
    has been false since for any project that declares one — the same defect,
    in the same words, as the planner paragraph that withheld a stream of work.
    It survived because nothing checks a prompt string against the code.

    The instruction it is making is still right: reading a runbook is not being
    told to execute it. What is wrong is the reason given for it, and a reason a
    model can see is false is worse than no reason — the tools are right there
    in its own schema.
    """

    def test_with_no_declared_tools_it_reads_as_it_always_did(self):
        from orchestrator.prompts import _conventions_block

        text = _conventions_block("Some conventions.", role="executor")
        assert "cannot run" in text.lower()

    def test_with_a_declared_tool_it_stops_claiming_it_cannot_run_anything(self):
        from orchestrator.prompts import _conventions_block

        text = _conventions_block(
            "Some conventions.",
            role="executor",
            project_tools=[declared(name="bundle_install", roles=["executor"])],
        )
        assert "cannot run commands" not in text.lower()

    def test_the_instruction_itself_survives(self):
        # The point of the sentence is unchanged: a document describing a
        # procedure is not a licence to go and perform it.
        from orchestrator.prompts import _conventions_block

        text = _conventions_block(
            "Some conventions.",
            role="executor",
            project_tools=[declared(name="bundle_install", roles=["executor"])],
        )
        assert "procedure" in text.lower()

    def test_a_planner_only_tool_does_not_soften_it(self):
        from orchestrator.prompts import _conventions_block

        text = _conventions_block(
            "Some conventions.",
            role="executor",
            project_tools=[declared(roles=["planner"])],
        )
        assert "cannot run" in text.lower()


class TestTheExecutorIsScopedToo:
    """The role that had the menu to itself is not exempt from the partition.

    It is the easy one to miss: `project_tools` was built for the executor, so
    the executor's schema builder and dispatcher take the whole list and always
    did. Adding `roles` without touching them would scope two roles out of
    three and leave the third reading every declaration — and the tools most
    likely to be planner-only are the read-only ones, so nothing would fail
    loudly. It would simply mean the partition held everywhere except where the
    feature started.
    """

    def test_a_planner_only_tool_is_not_offered_to_the_executor(self):
        from orchestrator.executortools import tool_schemas

        names = [t["name"] for t in tool_schemas(None, [declared(roles=["planner"])])]
        assert "gem_search" not in names

    def test_an_executor_tool_still_is(self):
        from orchestrator.executortools import tool_schemas

        names = [t["name"] for t in tool_schemas(None, [declared(roles=["executor"])])]
        assert "gem_search" in names
        assert "edit" in names and "read_file" in names

    def test_a_planner_only_tool_is_not_run_for_the_executor(self):
        from orchestrator.executortools import dispatch

        runner = FakeRunner()
        out = dispatch(
            "gem_search",
            {"pattern": "x"},
            reader=None,
            editor=None,
            semantic=None,
            project_tools=[declared(roles=["planner"])],
            runner=runner,
        )
        assert runner.calls == [], "an unoffered tool was run"
        assert "gem_search" in out


class TestTheTwoMenusAreDistinguishable:
    """The planner must be able to tell its own tools from the executor's.

    Both directions are load-bearing and neither is symmetric with the other.

    It has to keep being told what the *executor* can run, whether or not it
    can run the same thing. Measured on a live run: the planner ruled the whole
    framework bump undrawable, correctly, and part of its reasoning was what
    `rails_app_update` overwrites — `config/routes.rb` and forty initializers,
    three of them load-bearing patches. That is a verdict about a tool it
    cannot call, reached from the description alone, and it was right.

    And it must not read its own menu as the executor's. A stage instruction
    saying "search the gem source" is unsatisfiable if the executor was never
    offered that tool — and an unsatisfiable instruction costs a rework cycle
    at best. This is the phantom-capability direction, which is the expensive
    one: a false *constraint* withholds work and leaves no artifact to catch,
    while a false *permission* produces a stage whose premise the executor
    cannot meet.
    """

    def test_a_tool_only_the_planner_has_is_marked_as_not_the_executors(self):
        from orchestrator.planner import _system_blocks

        text = _system_blocks(
            project_tools=[
                declared(name="gem_search", roles=["planner"]),
                declared(name="bundle_install", roles=["executor"]),
            ]
        )[0]["text"]
        assert "gem_search" in text, "the planner is not told what it holds"
        assert "bundle_install" in text, "the planner is not told what the executor holds"
        # The distinction has to be stated, not left to be inferred from two
        # lists that look alike.
        assert "cannot" in text.lower() or "not available" in text.lower()

    def test_a_shared_tool_is_not_described_as_withheld(self):
        from orchestrator.planner import _system_blocks

        text = _system_blocks(
            project_tools=[declared(name="gem_read", roles=["planner", "executor"])]
        )[0]["text"]
        assert "gem_read" in text
        assert "gem_read" not in _withheld_names(text)

    def test_with_no_planner_tools_nothing_new_is_said(self):
        # A project that declares only executor tools must read exactly as it
        # did before this field existed.
        from orchestrator.planner import _system_blocks

        text = _system_blocks(project_tools=[declared(roles=["executor"])])[0]["text"]
        assert "gem_search" in text
        assert not _withheld_names(text)

    def test_the_placeholder_never_survives(self):
        from orchestrator.planner import _system_blocks

        for tools in ([], [declared(roles=["planner"])], [declared(roles=["executor"])]):
            assert "%%" not in _system_blocks(project_tools=tools)[0]["text"]


def _withheld_names(text: str) -> list[str]:
    """The tools the prompt says the executor does not have.

    Reads the generated sentence rather than re-deriving the answer from the
    config: a test that recomputes what the code computes passes when both are
    wrong together.
    """
    from orchestrator.planner import PLANNER_ONLY_MARKER

    for line in text.splitlines():
        if PLANNER_ONLY_MARKER in line:
            return [w.strip(" `,.") for w in line.split(PLANNER_ONLY_MARKER)[1].split()]
    return []


class TestTheCapabilityParagraph:
    """What the planner is told the *executor* can do stays about the executor."""

    def test_a_planner_only_tool_is_not_described_as_the_executors(self):
        from orchestrator.planner import executor_capability_block

        text = executor_capability_block([declared(roles=["planner"])])
        assert "gem_search" not in text

    def test_an_executor_tool_is_still_described(self):
        from orchestrator.planner import executor_capability_block

        text = executor_capability_block([declared(roles=["executor"])])
        assert "gem_search" in text

    def test_a_planner_only_tool_leaves_the_denial_standing(self):
        # The denial is about the executor. A tool the planner alone may call
        # does not make it false, and softening it here would recreate the
        # contradiction the generated paragraph exists to prevent.
        from orchestrator.planner import executor_capability_block

        text = executor_capability_block([declared(roles=["planner"])]).lower()
        assert "no tool for is running anything" in text or "cannot run" in text


class TestItIsWiredForReal:
    """Held is not sent, and a tool with nothing to run it is held.

    `build_runtime` is where anything the clients need from the run is bound —
    the log, the tool log, the stage validator. The runner is the same shape of
    value: computed correctly in one place, consumed correctly in another, and
    invisible to a unit test on either side if the line connecting them is
    missing.
    """

    @pytest.fixture
    def assembled(self, repo, tmp_path):
        from orchestrator.planner import AnthropicPlanner
        from orchestrator.reviewer import OpenAIReviewer
        from orchestrator.runtime import ProjectPaths, RunPaths, build_runtime

        cfg = parse_config(
            minimal(
                target_repo=str(repo),
                project_tools=[
                    a_tool(name="gem_search", roles=["planner", "reviewer"]),
                    a_tool(name="bundle_install", roles=["executor"]),
                ],
            )
        )
        project = ProjectPaths(tmp_path / "projects" / "proj")
        return build_runtime(
            cfg,
            project,
            RunPaths(project, "r1"),
            planner=AnthropicPlanner(cfg.planner, client=object()),
            reviewer=OpenAIReviewer(cfg.reviewer, client=object()),
        )

    def test_the_planner_can_reach_a_runner(self, assembled):
        assert assembled.planner.runner is assembled.runner

    def test_the_reviewer_can_reach_a_runner(self, assembled):
        assert assembled.reviewer.runner is assembled.runner

    def test_the_reviewer_is_given_the_declared_tools(self, assembled):
        # It builds its own reader and had no route to the config's menu at
        # all; the planner at least held the list it could not call.
        assert "gem_search" in [t.name for t in assembled.reviewer.project_tools]

    def test_each_role_sees_only_its_own(self, assembled):
        from orchestrator.projecttools import for_role

        assert [t.name for t in for_role("planner", assembled.planner.project_tools)] == [
            "gem_search"
        ]
        assert [
            t.name for t in for_role("executor", assembled.planner.project_tools)
        ] == ["bundle_install"]
