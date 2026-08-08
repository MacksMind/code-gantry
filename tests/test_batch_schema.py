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
    }
    base.update(over)
    return base


class TestTheFieldItself:
    def test_it_defaults_to_empty(self):
        from orchestrator.planner import PlannerResponse

        r = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=_fields()
        )
        assert r.additional_stages == []

    def test_it_accepts_a_batch(self):
        from orchestrator.planner import PlannerResponse

        r = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e",
            stage=_fields("one"),
            additional_stages=[_fields("two"), _fields("three")],
        )
        assert [s.id for s in r.additional_stages] == ["two", "three"]

    def test_the_description_does_not_read_as_a_quota(self):
        """A number in the prose is an invitation to fill it.

        The measured risk is not a bad stage but a slower derivation: asked for
        up to five, the planner may survey as though it needs five, and one
        call costing five calls' worth of thinking has saved nothing.
        """
        from orchestrator.planner import PlannerResponse

        d = PlannerResponse.model_fields["additional_stages"].description.lower()
        assert "leave it empty" in d or "normally empty" in d
        assert "stand alone" in d or "stands alone" in d


class TestEveryBatchedStageIsChecked:
    """A batched stage is not a lesser stage."""

    def _planner(self, validate):
        from orchestrator.config import PlannerConfig
        from orchestrator.planner import AnthropicPlanner

        p = AnthropicPlanner.__new__(AnthropicPlanner)
        p.cfg = PlannerConfig(model="claude-opus-5")
        p.validate_stage_fields = validate
        return p

    def test_a_problem_in_a_batched_stage_is_reported(self):
        from orchestrator.planner import AnthropicPlanner, PlannerResponse

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
        from orchestrator.planner import AnthropicPlanner, PlannerResponse

        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e",
            stage=_fields("one"), additional_stages=[_fields("two")],
        )
        assert AnthropicPlanner._unusable(self._planner(lambda f: []), parsed) is None

    def test_the_first_stage_is_still_checked(self):
        from orchestrator.planner import AnthropicPlanner, PlannerResponse

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
        from orchestrator.config import parse_config

        cfg = parse_config({
            "target_repo": ".", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        smuggled = _fields("two", command="rm -rf /")
        built = cfg.stage_from_planner(smuggled)
        assert built.command in (None, ""), "an executable field crossed the boundary"
