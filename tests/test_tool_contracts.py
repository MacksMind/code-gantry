"""What the tool descriptions promise, and where the numbers in them come from.

Two defects, both found by reading a transcript rather than by any gate.

**The read ceilings were undisclosed.** A model plans its reads against limits
it cannot see: it learns a range was too wide only by spending the call, and
the clip notice arrives after the budget is gone. The same shape as batching,
which nothing suppressed — the model had simply not been told, and telling it
was one paragraph.

**`search` and `edit` read as one contract.** One takes a regular expression
and the other takes literal bytes, and neither description said so or mentioned
the other. A model spent most of a two-hour attempt escaping and re-escaping
text between them, and a human reading the transcript afterwards drew the same
wrong conclusion — which is the tell that the surface, not the reader, is the
problem.

The ceilings are *generated from the budget object that enforces them*. Three
roles run under three different budgets, so a literal here would be wrong for
at least one of them the day it was typed, and would be project knowledge in
code besides.
"""

from code_gantry.executortools import tool_schemas as executor_schemas
from code_gantry.plannertools import (
    READ_TOOLS,
    read_limits,
    read_tools,
    tool_schemas as planner_schemas,
)
from code_gantry.repotools import ReadBudget


def described(specs, name):
    return next(s["description"] for s in specs if s["name"] == name)


SMALL = ReadBudget(
    max_lines_per_call=111,
    max_total_lines=2222,
    max_calls=33,
    max_chars_per_call=4444,
    max_total_chars=55555,
)
LARGE = ReadBudget(
    max_lines_per_call=999,
    max_total_lines=88888,
    max_calls=77,
    max_chars_per_call=66666,
    max_total_chars=555555,
)


class TestTheCeilingsAreDisclosed:
    def test_every_number_comes_from_the_budget_that_enforces_it(self):
        note = read_limits(SMALL)["read_file"]
        for value in (111, 2222, 33, 4444, 55555):
            assert f"{value:,}" in note

    def test_two_roles_with_two_budgets_are_told_two_different_things(self):
        # The reason this cannot be a constant. One project here gives its
        # planner 20,000 lines and its reviewer 10,000; a literal would be
        # wrong for one of them and nothing would fail.
        small = described(read_tools(SMALL), "read_file")
        large = described(read_tools(LARGE), "read_file")
        assert small != large
        assert "2,222" in small and "2,222" not in large
        assert "88,888" in large and "88,888" not in small

    def test_the_per_call_cap_is_on_both_tools_that_it_binds(self):
        # `search` spends the same budget and is clipped by the same ceiling,
        # and an unannounced cap there is worse than on `read_file`: hits you
        # cannot see are indistinguishable from hits that do not exist.
        assert "111" in described(read_tools(SMALL), "search")

    def test_a_clipped_read_is_named_as_not_being_the_file(self):
        note = read_limits(SMALL)["read_file"]
        assert "clipped" in note
        assert "not the" in note

    def test_no_budget_leaves_the_descriptions_exactly_as_they_were(self):
        # A caller with no reader — a test stub, a role built without one —
        # gets today's strings rather than a paragraph full of zeroes.
        assert read_tools(None) == list(READ_TOOLS)
        for spec in read_tools(None):
            assert "**Bounds.**" not in spec["description"]

    def test_the_shared_half_does_not_fork_between_roles(self):
        # The property `READ_TOOLS` exists for. The ceilings are appended, so
        # the prose that keeps a lookup from becoming a belief stays one string
        # — a per-role rewrite would have cost that quietly.
        planner = described(planner_schemas(None, (), "planner", SMALL), "read_file")
        executor = described(executor_schemas(None, None, SMALL), "read_file")
        assert planner == executor
        base = described(READ_TOOLS, "read_file")
        assert planner.startswith(base)


class TestTheTwoTextArgumentsAreToldApart:
    def test_search_says_its_pattern_is_a_regular_expression(self):
        note = described(READ_TOOLS, "search")
        assert "regular expression" in note
        assert "escaped" in note

    def test_search_says_its_results_come_back_unescaped(self):
        # The actual confusion: a model took `\s` out of a search result and
        # re-escaped it into an `old_string`, over and over. The result is the
        # file's bytes; the pattern is a language. They do not round-trip.
        note = described(READ_TOOLS, "search")
        assert "unescaped" in note
        assert "never re-escaped" in note

    def test_edit_says_its_old_string_is_literal_and_names_the_contrast(self):
        note = described(executor_schemas(None), "edit")
        assert "literal text, never a pattern" in note
        assert "`search`" in note

    def test_each_write_tool_points_at_the_other(self):
        # Neither is a replacement for the other and a model has to choose. It
        # can only choose on a stated difference.
        edit = described(executor_schemas(None), "edit")
        patch = described(executor_schemas(None), "apply_patch")
        assert "apply_patch" in edit
        assert "`edit`" in patch

    def test_apply_patch_states_the_property_that_makes_it_worth_choosing(self):
        patch = described(executor_schemas(None), "apply_patch")
        assert "byte for byte" in patch
        assert "every removed line is named" in patch.lower()


class TestTheSeamToTheProvider:
    """The value crosses a schema boundary, so it is tested where it lands.

    `_tools` has its own method because this seam has broken twice, both times
    with the constructor taking an argument and nothing carrying it further.
    A budget rendered correctly and dropped on the way to the request is the
    same defect a third time, and four of this codebase's recorded bugs are
    exactly that shape.
    """

    def test_the_reader_s_own_budget_reaches_the_sent_schema(self, tmp_path):
        import subprocess

        from code_gantry.config import ExecutorConfig
        from code_gantry.executorclient import OpenAIExecutorModel
        from code_gantry.gitops import Git
        from code_gantry.repotools import RepoReader

        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "a.rb").write_text("x\n")
        for args in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "T"],
            ["git", "config", "commit.gpgsign", "false"],
            ["git", "add", "-A"],
            ["git", "commit", "-q", "-m", "first"],
        ):
            subprocess.run(args, cwd=repo, check=True)

        reader = RepoReader(Git(repo), repo, SMALL)
        model = OpenAIExecutorModel(ExecutorConfig(model="gpt-5.6-luna"), client=None)
        sent = model._tools(None, reader.budget)

        blob = str(sent)
        assert "2,222" in blob
        assert "111" in blob

    def test_no_budget_still_produces_a_usable_menu(self, tmp_path):
        from code_gantry.config import ExecutorConfig
        from code_gantry.executorclient import OpenAIExecutorModel

        model = OpenAIExecutorModel(ExecutorConfig(model="gpt-5.6-luna"), client=None)
        names = {t.get("name") for t in model._tools(None, None)}
        assert "read_file" in names and "apply_patch" in names


class TestTheNumbersAreNotWrittenDown:
    def test_no_module_states_a_read_ceiling_as_a_literal_in_a_description(self):
        # The rule this is enforcing: a prompt describing a capability is
        # generated from the thing that grants it. A number typed into a
        # description is a second statement of a config value, and the copy is
        # the one the model reads.
        import ast
        import pathlib

        for path in (
            pathlib.Path("src/code_gantry/plannertools.py"),
            pathlib.Path("src/code_gantry/executortools.py"),
        ):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(
                    node.value, str
                ):
                    continue
                for banned in ("400 lines", "3,000 lines", "6,000 lines", "25 calls"):
                    assert banned not in node.value, f"{banned} in {path}"


class TestThePromptAndTheSchemaAgree:
    """A capability can go missing between two correct changes.

    `_executor_system_prompt` enumerates the write tools in prose, and
    `EDIT_TOOLS` declares them. Neither knows about the other, and nothing
    compares two sections of one document — which is how a rewrite once
    dropped the framing on a rework and dozens of stages ran without it, with
    no error and no test.

    Adding a fourth write tool without a line in the prompt would leave a model
    holding a schema for something the instructions never mention. Which is
    survivable, and the reverse is not: prose describing a tool that no longer
    exists is where a deleted thing goes on living.
    """

    def test_every_write_tool_is_named_in_the_prompt_that_teaches_them(self):
        from code_gantry.executortools import EDIT_TOOLS
        from code_gantry.prompts import _executor_system_prompt

        prompt = _executor_system_prompt(None)
        for spec in EDIT_TOOLS:
            assert f"`{spec['name']}`" in prompt, spec["name"]

    def test_the_prompt_names_no_write_tool_that_does_not_exist(self):
        import re

        from code_gantry.executortools import EDIT_TOOLS, REPLAN_TOOL
        from code_gantry.plannertools import READ_TOOLS
        from code_gantry.prompts import _executor_system_prompt

        specs = [*EDIT_TOOLS, *READ_TOOLS, REPLAN_TOOL]
        # Arguments as well as tools, derived from the same schemas rather
        # than listed here — a hand-written exception list is the thing this
        # test is about, one level up.
        real = {s["name"] for s in specs} | {"semantic_search"}
        for spec in specs:
            real |= set(spec["input_schema"].get("properties", {}))
            for prop in spec["input_schema"].get("properties", {}).values():
                real |= set(
                    prop.get("items", {}).get("properties", {})
                    if isinstance(prop.get("items"), dict)
                    else {}
                )

        prompt = _executor_system_prompt(None)
        for token in set(re.findall(r"`([a-z][a-z0-9_]*_[a-z0-9_]+)`", prompt)):
            # Operator config, named in the prompt and declared nowhere here.
            if token.endswith("_command") or token in {"edit_files", "read_files"}:
                continue
            assert token in real, f"prompt names a tool that does not exist: {token}"
