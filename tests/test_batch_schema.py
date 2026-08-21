"""The planner may answer with more than one stage, and every one is checked.

`additional_stages` sits beside `stage` rather than replacing it, so the shape
of a normal answer does not change: `stage` is always the next one, and the
list is empty unless a batch is obviously available. That is deliberately the
*inverse* of the `observations` lesson — there an optional field came back
empty 278 times out of 278 and the fix was to require it, because output was
wanted every time. Here declining is the good outcome, so a conditional
trigger is right and the field must not read as a quota to fill.

The safety-critical half is that a batched stage is not a lesser stage. Every
one goes through the same allowlist filter and the same validation as the
first, or the partition this whole design rests on would have a second door:
`stage` filtered, `additional_stages` trusted.
"""

import pytest


def _fields(sid="s", **over):
    base = {
        "id": sid,
        "instruction": "make it so",
        "edit_files": ["app/a.rb"],
        "read_files": [],
        "read_excerpts": [],
        "constraints": "",
        "acceptance": "",
        "forbidden_patterns": [],
        "must_not_remain": [],
        "test_paths": [],
        "require_new_tests": False,
        "difficulty": "medium",
    }
    base.update(over)
    return base


class TestTheFieldItself:
    def test_it_defaults_to_empty(self):
        from code_gantry.planner import PlannerResponse

        r = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=_fields()
        )
        assert r.additional_stages == []

    def test_it_accepts_a_batch(self):
        from code_gantry.planner import PlannerResponse

        r = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e",
            stage=_fields("one"),
            additional_stages=[_fields("two"), _fields("three")],
        )
        assert [s.id for s in r.additional_stages] == ["two", "three"]

    def test_the_field_defers_to_the_prompt_for_how_many(self):
        """This test used to assert the opposite, and the reversal is measured.

        It required the description to read "normally empty" or "leave it
        empty", on the reasoning that a number in the prose is an invitation to
        fill it — asked for up to five, the planner may survey as though it
        needs five, and one call costing five calls' worth of thinking has
        saved nothing. That risk is real and the guard against it is kept; what
        was wrong was where the guidance lived and what it left out.

        The field was the *only* thing the planner was told about batching, and
        the cap appeared nowhere at all — not in the schema, not in the prompt,
        reachable only in `config.py` and in the trim at `nodes.advance`. So
        the planner was asked to decline an option whose size it could not see,
        by a description that told it declining was correct. That is the shape
        `CLAUDE.md` records for `observations`: empty 278 times out of 278.
        Under `max_batch_stages: 5`, two consecutive derivations returned one
        stage each.

        So the number moves to the prompt, where the cap is known and can be
        stated as one, and this field points at it instead of pre-empting it.
        """
        from code_gantry.planner import PlannerResponse

        d = PlannerResponse.model_fields["additional_stages"].description.lower()
        assert "stated in the prompt" in d
        # No count here: the field cannot know the cap, and a number written
        # into it would be wrong for every project that set a different one.
        assert not any(str(n) in d for n in range(2, 10))
        # The constraint that decides whether an offered stage survives travels
        # with the field as well as the prompt, because this is what the model
        # is looking at while it writes them. It is no longer "stand alone" —
        # stages run in order and may build on each other — but the one thing
        # that cannot survive an earlier stage still has to be named here.
        assert "read_excerpts" in d

    def test_the_prompt_keeps_the_guard_against_surveying_for_the_cap(self):
        # The concern the old assertion existed for. Stating a ceiling must not
        # read as a target: the block has to say, in the same breath, that a
        # batch of one is a correct answer and that extra reading defeats the
        # purpose.
        from types import SimpleNamespace

        from code_gantry.prompts import _batch_block

        text = _batch_block(
            SimpleNamespace(planner=SimpleNamespace(max_batch_stages=5))
        ).lower()
        assert "batch of one" in text
        assert "cost more than it saved" in text


class TestEveryBatchedStageIsChecked:
    """A batched stage is not a lesser stage."""

    def _planner(self, validate):
        from code_gantry.config import PlannerConfig
        from code_gantry.planner import AnthropicPlanner

        p = AnthropicPlanner.__new__(AnthropicPlanner)
        p.cfg = PlannerConfig(model="claude-opus-5")
        p.validate_stage_fields = validate
        return p

    def test_a_problem_in_a_batched_stage_is_reported(self):
        from code_gantry.planner import AnthropicPlanner, PlannerResponse

        def validate(fields):
            return ["fenced code block"] if fields["id"] == "three" else []

        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e",
            stage=_fields("one"),
            additional_stages=[_fields("two"), _fields("three")],
        )
        problem = AnthropicPlanner._unusable(self._planner(validate), parsed)
        assert problem and "three" in problem, problem

    def test_a_clean_batch_reports_nothing(self):
        from code_gantry.planner import AnthropicPlanner, PlannerResponse

        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e",
            stage=_fields("one"), additional_stages=[_fields("two")],
        )
        assert AnthropicPlanner._unusable(self._planner(lambda f: []), parsed) is None

    def test_the_first_stage_is_still_checked(self):
        from code_gantry.planner import AnthropicPlanner, PlannerResponse

        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e",
            stage=_fields("one"), additional_stages=[_fields("two")],
        )
        problem = AnthropicPlanner._unusable(
            self._planner(lambda f: ["bad"] if f["id"] == "one" else []), parsed
        )
        assert problem and "one" in problem


class TestTheAllowlistAppliesToEveryStage:
    def test_an_executable_field_is_filtered_from_a_batched_stage(self):
        """The partition must not have a second door.

        `stage_from_planner` filters against `PLANNER_WRITABLE_FIELDS`, and a
        batched stage that skipped it would let the planner author a command
        by putting it second in the list.
        """
        from code_gantry.config import parse_config

        cfg = parse_config({
            "target_repo": ".", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        smuggled = _fields("two", checks=["rm -rf /"])
        built = cfg.stage_from_planner(smuggled)
        assert built.checks == [], "an executable field crossed the boundary"
