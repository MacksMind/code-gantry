"""Run state: counters, append-only history, and resume routing.

Resume routing is the part with teeth. Getting it wrong produces a loop that
looks like progress and never makes any — which is exactly the bug the first
implementation pass hit with gated stages.
"""

from orchestrator.state import (
    PLANNING_FAILURES,
    REPO_STATE_FAILURES,
    accumulate_usage,
    fresh_revision_fields,
    fresh_stage_fields,
    new_state,
    resume_entry_point,
)


def a_state(**overrides):
    state = new_state(
        run_id="r1",
        project_slug="rails-upgrade",
        config_hash="abc123",
        target_repo="/tmp/app",
        base_ref="main",
        base_sha="deadbeef",
        project_branch="upgrade/rails-5",
        started_at=1000.0,
    )
    state.update(overrides)
    return state


class TestInitialState:
    def test_starts_with_no_stage(self):
        # The planner derives the first one; there is no static list.
        assert a_state()["current"] is None

    def test_starts_running(self):
        assert a_state()["status"] == "running"

    def test_history_starts_empty(self):
        assert a_state()["completed"] == []

    def test_both_retry_counters_exist(self):
        # Two budgets need two counters.
        state = a_state()
        assert state["verify_attempt"] == 0
        assert state["rework_attempt"] == 0

    def test_planner_interventions_start_at_zero(self):
        assert a_state()["planner_interventions"] == 0

    def test_the_session_clock_starts_with_the_run(self):
        # A fresh run's session is the run. They diverge only on resume.
        state = a_state()
        assert state["session_started_at"] == state["started_at"] == 1000.0

    def test_no_awaiting_human_status(self):
        # There is no planned pause; a human's involvement is an escalation.
        assert a_state()["status"] in ("running", "complete", "escalated")


class TestResetScopes:
    def test_stage_reset_clears_both_counters(self):
        fields = fresh_stage_fields()
        assert fields["verify_attempt"] == 0
        assert fields["rework_attempt"] == 0

    def test_stage_reset_clears_the_branch(self):
        assert fresh_stage_fields()["stage_branch"] is None

    def test_stage_reset_does_not_touch_revision(self):
        # revision belongs to the stage being replaced; plan sets it.
        assert "revision" not in fresh_stage_fields()

    def test_stage_reset_does_not_touch_history(self):
        assert "completed" not in fresh_stage_fields()

    def test_stage_reset_does_not_touch_planner_budget(self):
        # The intervention budget is global across the run.
        assert "planner_interventions" not in fresh_stage_fields()

    def test_revision_reset_clears_counters_and_feedback(self):
        # Feedback was against an instruction that no longer applies.
        fields = fresh_revision_fields()
        assert fields["verify_attempt"] == 0
        assert fields["rework_attempt"] == 0
        assert fields["review_feedback"] == []

    def test_revision_reset_keeps_the_branch(self):
        # A scope-widening revision builds on existing work.
        assert "stage_branch" not in fresh_revision_fields()


class TestUsageAccumulation:
    def test_adds_to_existing_totals(self):
        out = accumulate_usage({"prompt_tokens": 100}, prompt_tokens=50)
        assert out["prompt_tokens"] == 150

    def test_starts_from_zero_when_absent(self):
        assert accumulate_usage(None, prompt_tokens=10)["prompt_tokens"] == 10

    def test_tracks_planner_and_reviewer_separately(self):
        # They are different models at different prices.
        out = accumulate_usage(None, prompt_tokens=10, planner_prompt_tokens=20)
        assert out["prompt_tokens"] == 10
        assert out["planner_prompt_tokens"] == 20


class TestFailureClassification:
    def test_repo_and_planning_failures_do_not_overlap(self):
        assert not (REPO_STATE_FAILURES & PLANNING_FAILURES)

    def test_setup_is_a_repo_state_failure(self):
        assert "setup" in REPO_STATE_FAILURES

    def test_precondition_is_a_planning_failure(self):
        # Preconditions are operator-only, so a human edits the config or the
        # plan, not the repo.
        assert "precondition" in PLANNING_FAILURES

    def test_review_gate_failures_are_repo_state(self):
        assert "review" in REPO_STATE_FAILURES
        assert "full_suite" in REPO_STATE_FAILURES


class TestResumeRouting:
    def test_a_fresh_run_with_no_stage_goes_to_plan(self):
        assert resume_entry_point(a_state()) == "plan"

    def test_a_fresh_run_with_a_pending_stage_goes_to_precheck(self):
        state = a_state(current={"id": "s1"})
        assert resume_entry_point(state) == "precheck"

    def test_resuming_after_a_repo_failure_verifies(self):
        # The human changed the repo. Check it before doing anything else —
        # re-running the stage would discard the fix.
        state = a_state(resuming=True, failure_layer="tests", current={"id": "s1"})
        assert resume_entry_point(state) == "verify"

    def test_resuming_after_a_setup_failure_verifies(self):
        state = a_state(resuming=True, failure_layer="setup", current={"id": "s1"})
        assert resume_entry_point(state) == "verify"

    def test_resuming_after_a_planning_failure_replans(self):
        # The human edited the plan document or the config.
        state = a_state(resuming=True, failure_layer="planner", current={"id": "s1"})
        assert resume_entry_point(state) == "plan"

    def test_resuming_after_an_unmet_precondition_replans(self):
        state = a_state(resuming=True, failure_layer="precondition", current={"id": "s1"})
        assert resume_entry_point(state) == "plan"

    def test_resuming_an_interruption_with_no_failure_picks_up_the_stage(self):
        # Killed mid-run rather than escalated: nothing to verify.
        state = a_state(resuming=True, failure_layer=None, current={"id": "s1"})
        assert resume_entry_point(state) == "precheck"

    def test_resuming_an_interruption_with_no_stage_replans(self):
        state = a_state(resuming=True, failure_layer=None, current=None)
        assert resume_entry_point(state) == "plan"

    def test_resuming_after_a_budget_stop_replans(self):
        # Time ran out while a stage was awaiting revision. Re-entering at
        # precheck would re-run it unrevised and throw the diagnosis away.
        state = a_state(resuming=True, failure_layer="budget", current={"id": "s1"})
        assert resume_entry_point(state) == "plan"

    def test_a_budget_stop_is_neither_repo_state_nor_planning(self):
        # It is not a defect in either place — the work just did not fit.
        assert "budget" not in REPO_STATE_FAILURES
        assert "budget" not in PLANNING_FAILURES

    def test_never_routes_to_precheck_after_a_repo_failure(self):
        # This is the loop-forever bug: precheck re-runs the stage from the top.
        for layer in REPO_STATE_FAILURES:
            state = a_state(resuming=True, failure_layer=layer, current={"id": "s1"})
            assert resume_entry_point(state) != "precheck", layer
