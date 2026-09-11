"""The planner is told what the executor can run, generated from the config.

A tool the operator declares is a tool the planner is told about, by
construction; a project declaring none is told so through its own file. A
sentence asserting a capability outlives the fact it was written about, and a
false constraint withholds work that leaves no artifact to be caught.
"""

from code_gantry.config import ProjectTool, ToolArgument
from code_gantry.promptfiles import text


def a_tool(**over):
    fields = dict(
        name="bundle_install",
        description="Install what the manifest names, in the app container.",
        command=["docker", "compose", "exec", "-T", "app", "bundle", "install"],
    )
    fields.update(over)
    return ProjectTool(**fields)


def _text(tools):
    from code_gantry.planner import executor_capability_block

    return executor_capability_block(tools)


class TestWithNoDeclaredTools:
    def test_it_sends_the_no_tools_file(self):
        assert text("planner/capability_no_declared_tools") in _text([])
        assert text("planner/capability_declared_intro") not in _text([])


class TestWithDeclaredTools:
    def test_the_tool_is_named(self):
        assert "bundle_install" in _text([a_tool()])

    def test_the_operator_description_is_carried(self):
        assert "Install what the manifest names" in _text([a_tool()])

    def test_the_no_tools_file_is_not_sent_beside_a_declared_tool(self):
        # The denial must not survive alongside a declared tool, or the planner
        # is handed a contradiction from one paragraph instead of two documents.
        assert text("planner/capability_no_declared_tools") not in _text([a_tool()])

    def test_the_declared_menu_is_framed_by_its_files(self):
        # Declared tools are a menu, not a shell: the intro and the outro that
        # say so travel with the list.
        rendered = _text([a_tool()])
        assert text("planner/capability_declared_intro") in rendered
        assert text("planner/capability_declared_outro") in rendered
        assert rendered.index("bundle_install") < rendered.index(text("planner/capability_declared_outro"))

    def test_an_argument_is_described(self):
        tool = a_tool(
            name="bundle_update",
            command=["bundle", "update", "{names}"],
            arguments=[ToolArgument(name="names", description="Which to update.")],
        )
        assert "names" in _text([tool])


class TestItReachesTheRealPrompt:
    """Held is not sent. This seam has broken twice on other fields."""

    def test_a_declared_tool_appears_in_the_system_prompt(self):
        from code_gantry.planner import _system_blocks

        text = _system_blocks(project_tools=[a_tool()])[0]["text"]
        assert "bundle_install" in text

    def test_a_project_with_no_tools_gets_the_no_tools_file(self):
        # The constant is a template; what a model reads is the rendered block.
        from code_gantry.planner import _system_blocks

        assert text("planner/capability_no_declared_tools") in _system_blocks()[0]["text"]

    def test_the_placeholder_never_survives_into_a_prompt(self):
        # A template marker reaching a model is worse than a stale sentence:
        # it is unreadable and says nothing about what the executor can do.
        from code_gantry.planner import _system_blocks

        for tools in ([], [a_tool()]):
            assert "%%" not in _system_blocks(project_tools=tools)[0]["text"]

    def test_the_capability_text_is_inside_the_cached_block(self):
        # Fixed for a run, so it belongs in the prefix rather than beside the
        # per-call material.
        from code_gantry.planner import _system_blocks

        block = _system_blocks(project_tools=[a_tool()])[0]
        assert block.get("cache_control")
        assert "bundle_install" in block["text"]
