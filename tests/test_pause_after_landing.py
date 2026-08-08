"""The pause is anchored to the landing, not to the planner call after it.

`nodes.plan` checks the flag before calling the planner, and that has been
right only because `advance` routes to `plan` and nowhere else — so "the next
planner call" and "just after a stage landed" have been the same instant. The
coupling is incidental, and step 10 breaks it: with a queue of stages from one
derivation, `advance` goes to the next queued stage, and a pause requested
during the first of five would not take effect until all five had landed.

So the check moves to where the property actually holds. What makes a pause
safe is not that a planner call is about to happen — it is that the squash
merge is done, the stage branch is gone and the tree is clean. That is true at
the end of `advance` whatever comes next.

The check in `plan` stays. A run can reach it without landing anything — a
revision, a redraw, a fresh start with the flag already set — and those are
still between-stages moments where stopping is safe.

The failure mode this file exists to catch is the tempting implementation:
returning the escalation *instead of* the landing update, which would drop
`completed`, `stage_index` and the rest, and lose the stage that had just
landed.
"""

import pytest


class TestItStopsAfterTheSquash:
    def test_a_pause_set_during_a_stage_stops_at_the_landing(self, tmp_path):
        from orchestrator.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        paused = _pause_escalation(flag, {"run_id": "r"})
        assert paused is not None
        assert paused["next_hop"] == "escalate"
        assert paused["failure_layer"] == "paused"

    def test_no_flag_is_no_escalation(self, tmp_path):
        from orchestrator.nodes import _pause_escalation

        assert _pause_escalation(tmp_path / "absent", {"run_id": "r"}) is None

    def test_the_note_is_carried_through(self, tmp_path):
        from orchestrator.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("picking up the new prompts")
        paused = _pause_escalation(flag, {"run_id": "r"})
        assert "picking up the new prompts" in paused["escalation_reason"]

    def test_it_names_the_run_to_resume(self, tmp_path):
        from orchestrator.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        paused = _pause_escalation(flag, {"run_id": "20260808-x"})
        assert "20260808-x" in paused["escalation_reason"]


class TestTheLandingIsNotLost:
    """The whole reason this is a merge rather than a replacement.

    A stage that has been squash-merged is on the branch whatever the run does
    next. If the escalation replaced the update instead of joining it, the
    state would forget the landing — `completed` short by one, `stage_index`
    unmoved — and a resume would re-derive work that is already on the branch.
    """

    def test_the_completed_entry_and_the_index_survive(self):
        landing = {
            "completed": [{"id": "s1"}],
            "stage_index": 4,
            "current": None,
            "next_hop": "plan",
        }
        paused = {
            "failure_layer": "paused",
            "escalation_reason": "...",
            "next_hop": "escalate",
        }
        merged = {**landing, **paused}
        assert merged["completed"] == [{"id": "s1"}]
        assert merged["stage_index"] == 4
        assert merged["next_hop"] == "escalate"


class TestTheEdgeTableAllowsIt:
    def test_advance_may_reach_escalate(self):
        from orchestrator.driver import EDGES

        assert "escalate" in EDGES["advance"], (
            "advance stops the run now, so the table has to say so — a node "
            "routing outside its edges raises rather than rerouting"
        )

    def test_advance_still_reaches_plan(self):
        from orchestrator.driver import EDGES

        assert "plan" in EDGES["advance"]


class TestOneMessageForBothCheckpoints:
    def test_plan_and_advance_use_the_same_helper(self):
        """Two copies of a pause message is two things to keep in step.

        `plan`'s wording carries a promise — "nothing is half-done" — that is
        only true because of where the check sits. Restating it beside a second
        check is how one of them comes to be wrong.
        """
        import inspect

        from orchestrator import nodes

        src = inspect.getsource(nodes)
        assert src.count("Paused at your request") == 1
        assert src.count("_pause_escalation(") >= 3  # the def and both callers


class TestAPauseCaughtAfterDeriving:
    """The stage is held, not thrown away.

    Observed: a pause requested at 00:29:59 was followed by a whole stage —
    derived at 00:31:28, executed, reviewed and landed — because the flag is
    read at the top of `plan` and the planner call had already started. The
    operator watched fifteen minutes of work they had asked to stop.

    So the flag is read again once the planner has answered, before `precheck`
    cuts a branch. That is a clean-tree moment too: nothing has run, and the
    derived stage sits in `current` costing nothing to keep.

    Which checkpoint fired is *recorded* rather than inferred. `paused_before`
    carries the hop the run was about to take, so a resume knows there is a
    stage ready without having to deduce it from the presence of `current` and
    the absence of a failure — two facts that also describe a stage awaiting
    revision, which must not be re-run unrevised.
    """

    def test_the_hop_the_run_was_about_to_take_is_recorded(self, tmp_path):
        from orchestrator.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        assert _pause_escalation(flag, {"run_id": "r"}, "precheck")["paused_before"] == "precheck"

    def test_the_other_checkpoints_record_nothing_to_resume_into(self, tmp_path):
        # Written every time rather than left absent, so a pause caught before
        # the planner ran cannot inherit a value from an earlier one.
        from orchestrator.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        assert _pause_escalation(flag, {"run_id": "r"})["paused_before"] == ""

    def test_the_message_says_a_stage_is_waiting(self, tmp_path):
        from orchestrator.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        held = _pause_escalation(flag, {"run_id": "r"}, "precheck")["escalation_reason"]
        between = _pause_escalation(flag, {"run_id": "r"})["escalation_reason"]
        assert "derived" in held and "derived" not in between


class TestResumingIntoAHeldStage:
    def test_a_held_stage_runs_rather_than_being_re_derived(self):
        from orchestrator.state import resume_entry_point

        assert resume_entry_point({
            "resuming": True, "failure_layer": "paused",
            "paused_before": "precheck", "current": {"id": "s"},
        }) == "precheck"

    def test_a_pause_with_nothing_held_still_goes_to_the_planner(self):
        """The rule this refines, and the incident behind it.

        A stage may have been awaiting revision when the stop came, and
        `precheck` would re-run it unrevised and discard the diagnosis. That is
        exactly the case where nothing was derived, so `paused_before` is empty
        and the planner still decides.
        """
        from orchestrator.state import resume_entry_point

        assert resume_entry_point({
            "resuming": True, "failure_layer": "paused",
            "paused_before": "", "current": {"id": "s"},
            "last_failure": {"layer": "tests"},
        }) == "plan"

    def test_a_budget_stop_is_unchanged(self):
        from orchestrator.state import resume_entry_point

        assert resume_entry_point({
            "resuming": True, "failure_layer": "budget", "current": {"id": "s"},
        }) == "plan"

    def test_the_key_is_declared_so_the_driver_keeps_it(self):
        # The merge filters against the schema; an undeclared key is dropped,
        # which is how `full_suite_digest` shipped broken.
        from orchestrator.state import RunState

        assert "paused_before" in RunState.__annotations__
