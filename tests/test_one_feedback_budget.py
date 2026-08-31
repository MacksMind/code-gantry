"""One budget for how much command output a model is given.

`FEEDBACK_OUTPUT_CHARS = 4_000` was defined in `gates.py` and again in
`nodes.py`, each with its own one-line `clip` helper over the shared
`clip_for_model`. That is a regrowth of a duplication this codebase already
paid to remove: `_clip` was written twice, in `nodes.py` and `verify.py`, and
`clip_for_model` exists because writing it twice is "how the decision got made
twice in the first place". The function was centralised and the *budget* it is
called with grew a second copy.

Nothing fails when the two disagree, which is the whole problem. Tune the
executor's feedback ceiling in `gates.py` and the planner's handoff keeps
clipping at the old figure; the two simply start giving models different
amounts of the same failure to read, and the only symptom is a model reasoning
from a truncation the other never saw.

Pinned by *definition count* rather than by value. Asserting `gates.X ==
nodes.X` passes for two constants that happen to agree, which is exactly the
state this file was written to end — the sweep that found it only found it by
accident.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "code_gantry"


def _assignments(name: str) -> list[str]:
    """Modules that *define* `name` at the top level, not those importing it."""
    out = []
    for path in sorted(SRC.glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            targets = (
                node.targets if isinstance(node, ast.Assign)
                else [node.target] if isinstance(node, ast.AnnAssign)
                else []
            )
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                out.append(path.stem)
    return out


class TestOneDefinition:
    def test_the_budget_is_defined_once(self):
        where = _assignments("FEEDBACK_OUTPUT_CHARS")
        assert where == ["gates"], (
            f"defined in {where}. Two budgets for one decision disagree "
            "silently: nothing fails, the two roles just clip differently."
        )

    def test_the_feedback_budget_is_applied_in_one_place(self):
        """Named wrappers are fine; a second application of the budget is not.

        The first cut of this test forbade any function called `clip` or
        `_clip` outside `gates`, and immediately failed on `verify._clip` —
        which is a one-line delegation to `gates.clip` carrying the reason
        collapse-before-truncate matters. That is not the failure mode. What
        breaks is a second *application*: another site pairing text with a
        budget of its own, which then drifts from this one silently.

        So the invariant is on the calls, not the names. Other budgets exist
        and are allowed — a lint diff and a one-line reviewer note are not the
        same decision as a failure handed to a model — but `FEEDBACK_OUTPUT_CHARS`
        must be spent exactly once.

        Found with a parser rather than a substring, after the substring
        version failed on a correct change. It looked for `clip_for_model(` on
        a line with the budget, so renaming the clipper to
        `clip_report_for_model` — a real decision about the content's shape —
        made it match zero lines and report the budget applied nowhere. A test
        that pins a spelling is not a test that pins a decision, which is the
        distinction this file exists to draw.
        """
        applied = []
        for path in sorted(SRC.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                names = {
                    a.id for a in ast.walk(node) if isinstance(a, ast.Name)
                }
                if "FEEDBACK_OUTPUT_CHARS" in names:
                    applied.append(f"{path.stem}:{node.lineno}")
        assert len(applied) == 1, f"the feedback budget is applied at {applied}"
        assert applied[0].startswith("gates:"), applied


class TestTheValueStillReachesBothCallers:
    def test_nodes_clips_through_the_shared_budget(self):
        from code_gantry import gates, nodes

        assert nodes._clip is gates.clip or (
            nodes._clip("x" * 10_000) == gates.clip("x" * 10_000)
        )

    def test_it_actually_clips(self):
        from code_gantry import gates

        assert len(gates.clip("x" * 50_000)) <= gates.FEEDBACK_OUTPUT_CHARS

    def test_short_output_is_untouched(self):
        from code_gantry import gates

        assert gates.clip("a failure summary") == "a failure summary"
