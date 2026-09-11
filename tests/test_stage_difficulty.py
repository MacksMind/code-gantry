"""The planner rates each stage, so the rating can be checked against outcomes.

Not routed on — nothing reads it to choose a model. It is recorded beside what
the stage actually cost, because the only way to learn whether the planner can
tell a hard stage from an easy one is to have it say so and then look.

Two rules from `CLAUDE.md` shape the field. It is **required**, because an
optional field with a conditional trigger is answered with nothing —
`observations` came back empty 278 times out of 278 across two prompt revisions
written to encourage it, while a required field the planner is always expected
to fill was answered 630 times. And it asks something **true every time**:
every stage has a difficulty, so there is no honest way to decline.

The description deliberately carries **no tie-breaker**. "When unsure, say
high" is right for a field that *routes*, because the failure is asymmetric —
calling a hard stage easy is what produced 33 turns of reading with zero edits
and a burned attempt. It is wrong for a field being *measured*: it would skew
the distribution by instruction, and the result would describe compliance with
our own hint rather than the planner's discernment. If routing is ever added,
the bias goes in then.

Diff size is explicitly not the question. A one-line change that requires
knowing why the existing line is wrong is harder than a fifty-file rename, and
nothing in the stage spec can see that difference.
"""

import pytest

from code_gantry.config import PLANNER_WRITABLE_FIELDS, Stage


class TestThePartition:
    def test_the_planner_may_set_it(self):
        """Declarative in the strongest sense: an adjective about the work.

        There is nothing here a planner could turn into an instruction to run,
        which is the question `CLAUDE.md` says to answer deliberately whenever
        a field is added to `Stage`.
        """
        assert "difficulty" in PLANNER_WRITABLE_FIELDS

    def test_a_stage_without_one_still_loads(self):
        """`Stage` is extra='forbid' and `current` is a dumped Stage, so a
        checkpoint written before this field existed must still resume."""
        assert Stage(id="s", instruction="do it").difficulty == ""


class TestTheSchema:
    def test_it_is_required(self):
        from code_gantry.planner import PlannedStage

        assert PlannedStage.model_fields["difficulty"].is_required()

    def test_only_three_answers_are_accepted(self):
        from code_gantry.planner import PlannedStage

        for value in ("low", "medium", "high"):
            spec = PlannedStage(id="s", instruction="i", edit_files=["a.rb"], plan_keys=["p.002"], resolves=[], difficulty=value)
            assert spec.difficulty == value
        with pytest.raises(Exception):
            PlannedStage(id="s", instruction="i", edit_files=["a.rb"], plan_keys=["p.002"], resolves=[], difficulty="trivial")

    def test_the_description_rejects_diff_size(self):
        """A model can see file counts, so the description has to say that is
        not what is being asked."""
        from code_gantry.planner import PlannedStage

        text = (PlannedStage.model_fields["difficulty"].description or "").lower()
        assert "size" in text or "files" in text

    def test_it_offers_no_tie_breaker(self):
        """Pinned, because a default answer is the easiest thing to add back
        and it would quietly turn this measurement into a measurement of the
        hint. It belongs with routing, which does not exist yet."""
        from code_gantry.planner import PlannedStage

        text = (PlannedStage.model_fields["difficulty"].description or "").lower()
        assert "when unsure" not in text
        assert "default" not in text


class TestItIsRecordedBesideTheOutcome:
    def test_the_cost_line_carries_it(self, tmp_path):
        from code_gantry.planner import append_stage_cost

        path = append_stage_cost(
            tmp_path, "a-stage", "abc123", files=2, context_tokens=1000,
            difficulty="high",
        )
        assert "high" in path.read_text()

    def test_an_unrated_stage_does_not_break_the_line(self, tmp_path):
        from code_gantry.planner import append_stage_cost

        path = append_stage_cost(
            tmp_path, "a-stage", "abc123", files=2, context_tokens=1000,
        )
        assert "a-stage" in path.read_text()
