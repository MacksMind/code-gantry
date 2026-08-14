"""What the executor may ask for, and what it must not be able to ask for.

Two kinds of assertion here. The schema ones are about the provider contract —
strict mode is not a preference, the SDK refuses to auto-parse without it. The
rest are invariants: that the read tools say the same thing to both roles, that
no tool runs a command, and that no description ships one project's vocabulary
to every other project's executor.
"""

import pytest

from code_gantry.edittools import FileEditor
from code_gantry.executortools import (
    EDIT_TOOLS,
    dispatch,
    openai_tool_schemas,
    tool_schemas,
)
from code_gantry.gitops import Git
from code_gantry.plannertools import READ_TOOLS
from code_gantry.repotools import ReadBudget, RepoReader


@pytest.fixture
def pair(tmp_path):
    import subprocess

    repo = tmp_path / "target"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "order.rb").write_text("class Order\nend\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "first"],
    ):
        subprocess.run(args, cwd=repo, check=True)

    reader = RepoReader(Git(repo), repo, ReadBudget())
    editor = FileEditor(repo=repo, edit_files=["app/**"])
    return reader, editor, repo


class TestTheProviderContract:
    def test_every_schema_is_valid_for_strict_mode(self):
        # Strict requires every property in `required` and
        # `additionalProperties: false`, recursively. Without it the SDK will
        # not auto-parse: "Only `strict` function tools can be auto-parsed".
        def check(schema, where):
            assert schema.get("additionalProperties") is False, where
            props = schema.get("properties") or {}
            assert set(schema.get("required") or []) == set(props), where
            for name, spec in props.items():
                if spec.get("type") == "object":
                    check(spec, f"{where}.{name}")
                items = spec.get("items")
                if isinstance(items, dict) and items.get("type") == "object":
                    check(items, f"{where}.{name}[]")

        for tool in openai_tool_schemas(None):
            assert tool["strict"] is True, tool["name"]
            check(tool["parameters"], tool["name"])

    def test_the_nested_edit_list_is_strictified_too(self):
        # The planner's version never had to do this — none of its schemas
        # nest. `edit` carries a list of objects with an optional flag.
        edit = next(t for t in openai_tool_schemas(None) if t["name"] == "edit")
        items = edit["parameters"]["properties"]["edits"]["items"]
        assert items["additionalProperties"] is False
        assert set(items["required"]) == {"old_string", "new_string", "replace_all"}

    def test_an_optional_property_becomes_nullable_rather_than_absent(self):
        # The shape strict mode provides for "may be omitted". `dispatch` reads
        # with `.get`, so a null arrives as a missing argument.
        read = next(t for t in openai_tool_schemas(None) if t["name"] == "read_file")
        assert read["parameters"]["properties"]["start"]["type"] == ["integer", "null"]

    def test_the_tool_is_flat_not_nested_under_a_function_object(self):
        # The nested shape is chat/completions. The Responses API takes name,
        # description and parameters at the top level.
        for tool in openai_tool_schemas(None):
            assert "function" not in tool
            assert tool["name"]


class TestInvariants:
    def test_no_tool_runs_a_command(self):
        # CodeGantry never executes model-authored shell, and a tool that
        # scheduled the tests would hand over the scheduling even though the
        # command itself stayed operator config. Lint, commit and tests are
        # loop steps precisely so the executor cannot finish without being
        # shown what its edits did.
        names = {t["name"] for t in tool_schemas(None)}
        assert names == {
            "read_file",
            "list_files",
            "search",
            "git_show",
            "git_diff",
            "edit",
            "create_file",
            "delete_file",
        }
        blob = " ".join(t["description"].lower() for t in tool_schemas(None))
        for word in ("run the tests", "shell", "execute a command", "bash"):
            assert word not in blob

    def test_the_read_tools_say_the_same_thing_to_both_roles(self):
        # Imported by reference, not copied. A planner and an executor told
        # different things would be reasoning from different contracts about
        # the same repository.
        executor_reads = {
            t["name"]: t["description"]
            for t in tool_schemas(None)
            if t["name"] in {r["name"] for r in READ_TOOLS}
        }
        for tool in READ_TOOLS:
            assert executor_reads[tool["name"]] is tool["description"]

    def test_no_description_ships_one_projects_vocabulary(self):
        # Project knowledge belongs in config, never in a model-facing string.
        # A tool description naming a framework is a hint shipped to every
        # other project's executor.
        #
        # Widened from EDIT_TOOLS to every model-facing tool table, and to the
        # nested parameter descriptions, after `path_glob` was found reading
        # "e.g. app/controllers" — a framework hint that had sat one level
        # below where this test was looking for its whole life. A rule pinned
        # over half its surface is pinned over the half that was easy to reach.
        from code_gantry.plannertools import READ_TOOLS

        strings = []
        for table in (EDIT_TOOLS, READ_TOOLS):
            for tool in table:
                strings.append(tool.get("description", ""))
                schema = tool.get("input_schema", {})
                for prop in schema.get("properties", {}).values():
                    strings.append(prop.get("description", ""))
                    items = prop.get("items", {})
                    for nested in items.get("properties", {}).values():
                        strings.append(nested.get("description", ""))
        blob = " ".join(strings).lower()
        for word in (
            "rails", "django", "rspec", "pytest", "ruby", "python",
            ".rb", ".py", "app/", "spec/", "src/", "controller", "migration",
        ):
            assert word not in blob, word


class TestDispatch:
    def test_an_edit_applies_and_reports(self, pair):
        reader, editor, repo = pair
        out = dispatch(
            "edit",
            {"path": "app/order.rb", "edits": [
                {"old_string": "class Order", "new_string": "class Purchase"}
            ]},
            reader, editor, None,
        )
        assert "applied 1 edit" in out
        assert "class Purchase" in (repo / "app" / "order.rb").read_text()

    def test_a_refusal_is_text_not_an_exception_and_is_recorded(self, pair):
        reader, editor, _ = pair
        out = dispatch(
            "edit",
            {"path": "app/order.rb", "edits": [
                {"old_string": "nope", "new_string": "x"}
            ]},
            reader, editor, None,
        )
        assert out.startswith("cannot do that:")
        assert editor.calls[0].refusal
        assert editor.calls[0].detail == "app/order.rb"

    def test_an_out_of_scope_write_is_refused_at_the_tool(self, pair):
        reader, editor, repo = pair
        (repo / "other.rb").write_text("x\n")
        out = dispatch(
            "edit",
            {"path": "other.rb", "edits": [{"old_string": "x", "new_string": "y"}]},
            reader, editor, None,
        )
        assert "not in this stage's scope" in out
        assert (repo / "other.rb").read_text() == "x\n"

    def test_an_empty_edit_list_is_refused(self, pair):
        reader, editor, _ = pair
        out = dispatch("edit", {"path": "app/order.rb", "edits": []}, reader, editor, None)
        assert "no edits given" in out

    def test_read_tools_still_reach_the_planner_dispatcher(self, pair):
        reader, editor, _ = pair
        out = dispatch("read_file", {"path": "app/order.rb"}, reader, editor, None)
        assert "class Order" in out

    def test_an_unknown_tool_is_reported_not_raised(self, pair):
        reader, editor, _ = pair
        assert "unknown tool" in dispatch("nope", {}, reader, editor, None)


class TestTheSemanticToolWarnsThatItLags:
    """The executor's own description, not the planner's.

    One paragraph separates them and it is the one that matters. Index lag
    never arose for the other two roles because both read a tree nobody is
    editing — the planner before a stage starts, the reviewer after it has
    committed. The executor is the first caller whose own uncommitted work is
    missing from what it is shown, and the misses it produces are exactly the
    refusals this session spent its time on.
    """

    def _tool(self):
        return next(
            t for t in tool_schemas(object()) if t["name"] == "semantic_search"
        )

    def test_it_says_the_index_is_behind_the_working_tree(self):
        text = self._tool()["description"]
        assert "out of date" in text
        assert "edited in this session" in text
        assert "several commits behind" in text

    def test_it_forbids_quoting_a_snippet_into_an_edit(self):
        text = self._tool()["description"]
        assert "Never quote a snippet" in text
        assert "read_file" in text

    def test_it_is_not_the_planners_description(self):
        from code_gantry.plannertools import SEMANTIC_TOOL

        assert self._tool()["description"] != SEMANTIC_TOOL["description"]
        # Same tool to the model either way, so name and schema must not fork.
        assert self._tool()["name"] == SEMANTIC_TOOL["name"]
        assert self._tool()["input_schema"] is SEMANTIC_TOOL["input_schema"]

    def test_it_is_absent_when_no_index_is_configured(self):
        assert "semantic_search" not in {t["name"] for t in tool_schemas(None)}


class TestTheExecutorGetsTheSameUnderstanding:
    """One description, one addendum — not two descriptions.

    The executor's variant led with "good for locating the place to work on"
    and spent the rest on the index lagging its own uncommitted edits. That has
    the emphasis backwards. If a stage instruction is complete the executor has
    no reason to ask the index anything; the cases where it does are a gap the
    instruction did not cover, and interpreting reviewer feedback — and both of
    those are *how does this work* questions, the same ones the planner and
    reviewer ask.

    Observed: a reviewer's rework issue named a chain across three files (the
    place that checks, the model it delegates to, the model holding the values).
    An executor handed that feedback needs to trace exactly what the reviewer
    traced. Told only that the tool locates a place to work, it has been given
    the wrong tool for the job it actually has.

    The lag is real and is the executor's alone — it is the only role whose own
    uncommitted edits are missing from what it is shown — so it stays. It is an
    addendum. `executortools`' own docstring already argues the general form:
    the read tools are imported rather than restated, because a planner and an
    executor told different things reason from different contracts about the
    same repository.
    """

    def _text(self):
        from code_gantry.executortools import SEMANTIC_TOOL_FOR_EDITING

        return SEMANTIC_TOOL_FOR_EDITING["description"]

    def test_it_carries_the_shared_substance(self):
        from code_gantry.plannertools import SEMANTIC_TOOL

        shared = SEMANTIC_TOOL["description"]
        assert shared in self._text(), (
            "the executor should be given the same understanding, not a second "
            "description that can drift from it"
        )

    def test_it_names_both_questions(self):
        text = self._text().lower()
        assert "every kind of this" in text
        assert "how does this work" in text

    def test_the_lag_survives_as_an_addendum(self):
        text = self._text().lower()
        assert "edited in this session" in text or "your own" in text

    def test_the_lag_comes_after_the_substance(self):
        # An addendum that leads is not an addendum; it is the description.
        text = self._text().lower()
        assert text.index("how does this work") < text.index("this session")

    def test_it_is_still_one_tool(self):
        from code_gantry.executortools import SEMANTIC_TOOL_FOR_EDITING
        from code_gantry.plannertools import SEMANTIC_TOOL

        assert SEMANTIC_TOOL_FOR_EDITING["name"] == SEMANTIC_TOOL["name"]
        assert (
            SEMANTIC_TOOL_FOR_EDITING["input_schema"] == SEMANTIC_TOOL["input_schema"]
        )
