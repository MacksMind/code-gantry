"""The planner must be told what the executor can actually do.

`PLANNER_SYSTEM_PROMPT` states, flatly, that the executor "cannot run commands"
and that "what it has no tool for is running anything". That was true of every
project until `project_tools` shipped, and it is now false for any project that
declares one — while the *plan* documents, written by people who knew about the
new tools, say the opposite.

The planner did exactly the right thing with the contradiction and it cost us
anyway. Verbatim, anchored to the plan:

    under this pipeline's contract the executor has read tools only and cannot
    run bundler, so a gem-resolution stage cannot read Bundler's answer …
    the two documents and the pipeline contract disagree, so check which holds
    before drawing one of these rather than trusting either

That is the expensive failure direction named in `PlanNote.kind`: work that
reads as blocked is never attempted, and nothing downstream catches it, because
a stage that is never drawn leaves no artifact to be wrong. A whole stream of
gem work was withheld on the strength of a sentence in our own prompt.

So the capability paragraph is *generated from the config* rather than asserted.
A tool the operator declares is a tool the planner is told about, by
construction — the same reason the reviewer is told about the editor's newline
normalisation rather than being left to rediscover it.
"""

from code_gantry.config import ProjectTool, ToolArgument


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
    def test_it_still_says_the_executor_cannot_run_anything(self):
        text = _text([]).lower()
        assert "no tool for is running anything" in text or "cannot run" in text

    def test_it_still_warns_against_asking_for_a_shell(self):
        # The failure that paragraph was written for: one such instruction cost
        # ten minutes of a model looping over hallucinated command output.
        assert "invent" in _text([]).lower()


class TestWithDeclaredTools:
    def test_the_tool_is_named(self):
        assert "bundle_install" in _text([a_tool()])

    def test_the_operator_description_is_carried(self):
        assert "Install what the manifest names" in _text([a_tool()])

    def test_the_blanket_denial_is_gone(self):
        # The sentence that withheld the work. It must not survive alongside a
        # declared tool, or the planner is handed the same contradiction from
        # one paragraph instead of two documents.
        text = _text([a_tool()]).lower()
        assert "no tool for is running anything" not in text
        assert "cannot run commands" not in text

    def test_it_still_refuses_an_undeclared_command(self):
        # The partition is unchanged: declared tools are a menu, not a shell.
        text = _text([a_tool()]).lower()
        assert "invent" in text or "not declared" in text

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

    def test_a_project_with_no_tools_reads_as_it_always_did(self):
        # The constant is a template now, so it is no longer the final text.
        from code_gantry.planner import _system_blocks

        text = _system_blocks()[0]["text"]
        assert "no tool for is running anything" in text

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
