"""One ledger summariser for all three agentic roles.

Every role reports what it looked at, and each had grown its own arithmetic:
the executor merged a reader and an editor and bucketed refusals, the reviewer
counted a single reader, the planner reported a bare total with no breakdown at
all. Three shapes of one operation — which is exactly how `number_lines` became
three copies of one format string while every test stayed green, and how
`_clip` came to be written twice.

The schema half had already drifted for real, which is why this file exists
rather than a note: `executortools._strictify` recursed into nested objects and
arrays and `plannertools`' inline copy did not. Harmless only because no read
tool's schema nests, and invisible to every test.

These tests pin the sharing rather than the output. Asserting the rendered
string in three places is what lets three implementations agree today and
diverge later.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "orchestrator"


def _module(name):
    return ast.parse((SRC / f"{name}.py").read_text())


def _function_names(name):
    return {
        n.name
        for n in ast.walk(_module(name))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class TestThereIsOneOfEach:
    def test_only_repotools_defines_the_counters(self):
        for module in ("planner", "reviewer", "executor", "nodes", "executortools"):
            names = _function_names(module)
            assert "count_calls" not in names, module
            assert "render_counts" not in names, module

    def test_only_plannertools_defines_strictify(self):
        # The one that had actually forked.
        for module in ("executortools", "reviewer", "planner"):
            assert "_strictify" not in _function_names(module), module
            assert "strictify" not in _function_names(module), module

    def test_the_shared_ones_exist_where_they_are_expected(self):
        assert {"count_calls", "count_refusals", "render_counts"} <= _function_names(
            "repotools"
        )
        assert {"strictify", "as_strict_tool"} <= _function_names("plannertools")


class TestAllThreeRolesUseThem:
    def test_every_role_counts_through_the_shared_helper(self):
        for module in ("planner", "reviewer", "executor"):
            text = (SRC / f"{module}.py").read_text()
            assert "count_calls" in text, f"{module} does not use the shared counter"

    def test_no_role_hand_rolls_a_counts_string(self):
        # The giveaway shape: sorting a counts dict by value at a log site.
        for module in ("planner", "reviewer", "executor", "nodes"):
            text = (SRC / f"{module}.py").read_text()
            assert "key=lambda kv: -kv[1]" not in text, (
                f"{module} is rendering counts itself; use render_counts"
            )

    def test_both_schema_builders_go_through_one_renderer(self):
        for module in ("plannertools", "executortools"):
            text = (SRC / f"{module}.py").read_text()
            assert "as_strict_tool" in text, module
            assert '"strict": True' not in text or module == "plannertools", (
                f"{module} builds the strict envelope itself"
            )


class TestTheBehaviourTheyShare:
    def test_counts_are_busiest_first(self):
        from orchestrator.repotools import render_counts

        out = render_counts({"search": 2, "read_file": 9, "git_show": 5})
        assert out == "9 read_file, 5 git_show, 2 search"

    def test_nothing_renders_as_empty_not_as_a_stray_separator(self):
        from orchestrator.repotools import render_counts

        assert render_counts({}) == ""

    def test_two_ledgers_merge(self):
        # The executor's case, and the reason the helper is variadic.
        from orchestrator.repotools import count_calls
        from orchestrator.repotools import ToolCall

        reader = [ToolCall(tool="read_file", detail="a", lines=1)]
        editor = [
            ToolCall(tool="edit", detail="a", lines=1),
            ToolCall(tool="edit", detail="b", lines=1),
        ]
        assert count_calls(reader, editor) == {"read_file": 1, "edit": 2}

    def test_strictify_recurses_which_is_the_half_that_had_forked(self):
        from orchestrator.plannertools import strictify

        out = strictify(
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"a": {"type": "string"}},
                            "required": [],
                        },
                    }
                },
                "required": ["items"],
            }
        )
        nested = out["properties"]["items"]["items"]
        assert nested["additionalProperties"] is False
        assert nested["required"] == ["a"]
