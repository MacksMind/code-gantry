"""What the planner is told when a stage stops being reworked.

The routing itself is right: a stage out of rework budget goes to the planner,
and the planner's judgement is what decides between `extend` and `restart`. But
a judgement is only as good as the account it is given, and this handoff gave a
misleading one.

Measured on `order-edit-item-personalization-explicit-scope`, which took three
redraws and 884 seconds of planning to reach `restart` on work the reviewer had
just approved. What it was handed:

    Summary: approved but the full suite was red — rejected 3 times
             (max_rework_retries=2)

The stage was never rejected three times. It was reworked twice — a missing
explanatory comment, and a route id that should have been optional — and both
were fixed and approved. Then one spec failed. Calling that "rejected 3 times"
describes a stage going badly when the record is a stage that converged and hit
a single real failure, and `restart` is the reasonable answer to the first
description.

The detail was worse: `feedback[-2:]` put the already-fixed reviewer findings
first and the red spec second, so the thing that actually ended the stage read
as a footnote to complaints that no longer applied.
"""

import pytest


def _rt(max_rework_retries=2, rework_reset=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        cfg=SimpleNamespace(
            limits=SimpleNamespace(max_rework_retries=max_rework_retries),
            rework_reset=rework_reset,
        ),
        log=lambda *_: None,
        git=None,
    )


class TestTheSummaryDoesNotOverstateHowBadlyItWent:
    def test_reworks_are_not_counted_as_rejections(self):
        from code_gantry.nodes import _rework_or_plan

        out = _rework_or_plan(
            {"rework_attempt": 2},
            _rt(),
            ["earlier reviewer finding", "the full suite failed: one red spec"],
            "approved but the full suite was red",
            layer="full_suite",
        )
        summary = out["last_failure"]["summary"]
        assert "rejected 3 times" not in summary
        assert "2 rework" in summary or "reworked 2" in summary
        assert "max_rework_retries=2" in summary

    def test_the_summary_still_says_the_budget_is_gone(self):
        # The planner has to know rework is not an option, or it may revise in
        # a way that expects another executor pass.
        from code_gantry.nodes import _rework_or_plan

        out = _rework_or_plan(
            {"rework_attempt": 2}, _rt(), ["a", "b"], "summary", layer="review"
        )
        assert "max_rework_retries=2" in out["last_failure"]["summary"]


class TestTheDetailLeadsWithWhatEndedTheStage:
    def test_the_latest_feedback_comes_first(self):
        from code_gantry.nodes import _rework_or_plan

        out = _rework_or_plan(
            {"rework_attempt": 2},
            _rt(),
            ["STALE: a finding already fixed", "FRESH: the full suite failed"],
            "approved but the full suite was red",
            layer="full_suite",
        )
        detail = out["last_failure"]["detail"]
        assert detail.index("FRESH") < detail.index("STALE")

    def test_earlier_feedback_is_still_carried(self):
        # It is context, not noise — a stage reworked twice for the same thing
        # is a different situation from one reworked for two different things.
        from code_gantry.nodes import _rework_or_plan

        out = _rework_or_plan(
            {"rework_attempt": 2},
            _rt(),
            ["STALE", "FRESH"],
            "s",
            layer="full_suite",
        )
        assert "STALE" in out["last_failure"]["detail"]

    def test_a_single_item_needs_no_ordering(self):
        from code_gantry.nodes import _rework_or_plan

        out = _rework_or_plan(
            {"rework_attempt": 2}, _rt(), ["only one"], "s", layer="review"
        )
        assert "only one" in out["last_failure"]["detail"]
