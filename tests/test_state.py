"""Run state: counters, append-only history, and resume routing.

Resume routing is the part with teeth. Getting it wrong produces a loop that
looks like progress and never makes any — which is exactly the bug the first
implementation pass hit with gated stages.
"""

import pytest

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

    def test_stage_reset_clears_what_the_executor_measured(self):
        """Both figures, and the context one is a fix rather than an addition.

        It was never cleared. `execute` writes it only when the provider reported a
        token line, so a stage whose attempts never printed one kept the
        previous stage's number — and `advance` copies whatever is in state
        onto the landed `StageResult` and into `stage-costs.md`, keyed by a
        merge sha the figure has nothing to do with. That file is fed to the
        planner to size the next batch, so the failure is silent and lands
        exactly where it does damage.

        Cost is added here with it rather than after it, because a per-stage
        total that is never reset is a per-run total wearing the wrong label.
        """
        fields = fresh_stage_fields()
        assert fields["executor_context_tokens"] == 0
        assert fields["executor_cost_usd"] == 0.0

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

    def test_cache_writes_are_carried_for_both_roles(self):
        # Both clients have computed this per call for as long as they have
        # existed and it stopped at the artifact: the run totals had no key for
        # it, so pricing the run charged writes at the base input rate instead
        # of the 1.25x they cost. The fourth value to be computed correctly and
        # lost in transit.
        out = accumulate_usage(
            None, cache_write_tokens=7, planner_cache_write_tokens=11
        )
        assert out["cache_write_tokens"] == 7
        assert out["planner_cache_write_tokens"] == 11

    def test_a_fresh_usage_record_declares_every_key(self):
        # A key absent from the zero record is a key `accumulate_usage` will
        # create on first use and the report will read as missing until then.
        from orchestrator.state import zero_usage

        assert set(zero_usage()) == {
            "prompt_tokens", "cached_tokens", "cache_write_tokens",
            "completion_tokens", "planner_prompt_tokens",
            "planner_cached_tokens", "planner_cache_write_tokens",
            "planner_completion_tokens",
            # The executor had none of these while it was a subprocess whose
            # usage was scraped from a console line. In-process it reports
            # real counts, and without a home here the cache hit rate is
            # computable per attempt and nowhere for the run.
            "executor_prompt_tokens", "executor_cached_tokens",
            "executor_cache_write_tokens", "executor_completion_tokens",
            # The one key here that is not a total; see `accumulate_usage`.
            "planner_peak_prompt_tokens",
        }


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


class TestPauseRouting:
    def test_resuming_after_a_pause_replans(self):
        # A pause is not a failure. It stops at a stage boundary with no stage
        # in flight, so the planner picks up where it left off.
        state = a_state(resuming=True, failure_layer="paused", current=None)
        assert resume_entry_point(state) == "plan"

    def test_a_pause_is_neither_repo_state_nor_planning(self):
        assert "paused" not in REPO_STATE_FAILURES
        assert "paused" not in PLANNING_FAILURES


class TestResumingAnInterruptedStage:
    """An interrupted run may still have landed work.

    `resume_entry_point` sent a resume with no recorded failure to precheck, on
    the reasoning that an interruption leaves "nothing to verify". That holds
    when the kill landed before the executor applied anything. It does not hold
    when the stage branch already carries commits — and then precheck runs the
    executor again over work that is already done.

    Observed live: a stage whose edits were complete and committed was killed
    mid-attempt, resumed, and sent straight back to the executor — which spent
    ten minutes looping because there was nothing left for it to do and the
    prompt gives it no way to say so.
    """

    def test_an_interrupted_stage_with_work_goes_to_verify(self):
        from orchestrator.state import resume_entry_point

        assert resume_entry_point(
            {"resuming": True, "current": {"id": "s"}, "stage_has_work": True}
        ) == "verify"

    def test_an_interrupted_stage_without_work_still_goes_to_precheck(self):
        from orchestrator.state import resume_entry_point

        assert resume_entry_point(
            {"resuming": True, "current": {"id": "s"}, "stage_has_work": False}
        ) == "precheck"

    def test_a_recorded_failure_still_wins(self):
        # An escalation says where to re-enter; that is more specific than
        # "there are commits on the branch".
        from orchestrator.state import resume_entry_point

        assert resume_entry_point(
            {
                "resuming": True,
                "current": {"id": "s"},
                "stage_has_work": True,
                "failure_layer": "planner",
            }
        ) == "plan"

    def test_no_stage_in_flight_goes_to_the_planner(self):
        from orchestrator.state import resume_entry_point

        assert resume_entry_point({"resuming": True, "stage_has_work": False}) == "plan"


class TestResumingClearsTheStopItIsUndoing:
    """`status` described the previous stop for the whole of the next session.

    Found by reading a live checkpoint: the run was at revision 2, landing work,
    and `status` still said `escalated` with the reason from an escalation an
    hour earlier. `orchestrator status` is the operator's primary question and
    it was answering with the stop that had already been fixed.

    The merge was inline in `cli.py`, which is why nothing caught it — there was
    no seam to test. That is the reason it lives here now.
    """

    def _fields(self, **over):
        from orchestrator.state import resume_fields

        args = {"stage_has_work": False, "reset_progress_budget": False}
        args.update(over)
        return resume_fields(**args)

    def test_the_run_is_running_again(self):
        assert self._fields()["status"] == "running"

    def test_the_old_escalation_reason_is_cleared(self):
        # Left in place it is reported as the current reason, and a later
        # escalation that forgets to set one would inherit it.
        assert self._fields()["escalation_reason"] is None

    def test_it_re_enters_rather_than_continuing(self):
        got = self._fields()
        assert got["resuming"] is True
        assert got["next_hop"] == ""

    def test_the_session_clock_restarts(self):
        # wall_clock_hours bounds one unattended stretch; the hours a run spent
        # waiting for a human were not spent working.
        import time

        assert self._fields()["session_started_at"] == pytest.approx(
            time.time(), abs=5
        )

    def test_work_already_on_the_branch_is_carried_in(self):
        assert self._fields(stage_has_work=True)["stage_has_work"] is True

    def test_the_progress_budget_is_untouched_by_default(self):
        # Merging an empty dict leaves the counter where it was, so an ordinary
        # resume cannot clear it by accident.
        assert "interventions_since_landing" not in self._fields()

    def test_the_progress_budget_clears_only_when_asked(self):
        got = self._fields(reset_progress_budget=True)
        assert got["interventions_since_landing"] == 0


class TestAResumeStartsFromTheCheckpoint:
    """The merge `resume_fields` is named for, which did not exist.

    Every test above passes on the delta alone, and the delta was all that
    `cli.resume` handed to `_drive` — so a resumed run began with a four-key
    state. `CLAUDE.md` records the shape: a test that pins where a value lives
    passes while the value is lost. Here the seven tests pinned the contents of
    the update and nothing asserted what it was applied *to*.

    Measured on a 14-hour run: `stage_index` restarts at zero on each resume
    (`000…030`, then `000…003`, then `000…003` in the artifact tree),
    `completed` restarts with it though it is the cacheable prefix of both paid
    prompts, `report.md` renders `(unknown)` and `None` throughout, and `drive`
    writes no checkpoint at all because it guards on `state.get("run_id")` —
    the database froze 31 stages before the run stopped.
    """

    def _saved(self, **over):
        base = {
            "run_id": "r1", "project_slug": "p", "target_repo": "/repo",
            "project_branch": "b", "base_ref": "main", "base_sha": "abc",
            "stage_index": 30, "completed": [{"id": "one"}, {"id": "two"}],
            "status": "escalated", "escalation_reason": "paused",
            "flaky_files": ["spec/a_spec.rb"],
        }
        base.update(over)
        return base

    def _input(self, **over):
        from orchestrator.state import resume_input

        return resume_input(
            self._saved(**over), stage_has_work=False, reset_progress_budget=False
        )

    def test_the_run_keeps_its_identity(self):
        # Without this the checkpoint writer never fires: `drive` guards on
        # `state.get("run_id")`, so a resumed run persists nothing.
        got = self._input()
        assert got["run_id"] == "r1"
        assert got["target_repo"] == "/repo"
        assert got["project_branch"] == "b"

    def test_it_keeps_counting_from_where_it_stopped(self):
        got = self._input()
        assert got["stage_index"] == 30
        assert [s["id"] for s in got["completed"]] == ["one", "two"]

    def test_the_accumulated_record_survives(self):
        assert self._input()["flaky_files"] == ["spec/a_spec.rb"]

    def test_the_delta_still_wins_over_the_saved_stop(self):
        # The whole point of the delta: the previous stop must not be reported
        # as the current one for the rest of the session.
        got = self._input()
        assert got["status"] == "running"
        assert got["escalation_reason"] is None
        assert got["resuming"] is True


class TestAPeakIsNotATotal:
    """`accumulate_usage` sums, and a high-water mark must not be summed.

    Two calls do not make a larger call than either of them. Summed, a peak
    grows monotonically, looks exactly like a context figure, and would be
    compared against a window it never approached — which is the reading the
    summed figure already invites and the peak exists to correct.

    Decided in the helper rather than at each call site because there are four
    of those in two modules, and a field whose arithmetic depends on the
    caller remembering is the shape of thing this codebase has lost twice.
    """

    def test_totals_still_add(self):
        from orchestrator.state import accumulate_usage

        out = accumulate_usage(None, planner_prompt_tokens=100)
        out = accumulate_usage(out, planner_prompt_tokens=250)
        assert out["planner_prompt_tokens"] == 350

    def test_a_peak_takes_the_maximum(self):
        from orchestrator.state import accumulate_usage

        out = accumulate_usage(None, planner_peak_prompt_tokens=480_000)
        out = accumulate_usage(out, planner_peak_prompt_tokens=190_000)
        assert out["planner_peak_prompt_tokens"] == 480_000

    def test_a_later_larger_call_raises_it(self):
        from orchestrator.state import accumulate_usage

        out = accumulate_usage(None, planner_peak_prompt_tokens=190_000)
        out = accumulate_usage(out, planner_peak_prompt_tokens=480_000)
        assert out["planner_peak_prompt_tokens"] == 480_000

    def test_the_key_is_declared_so_the_schema_does_not_drop_it(self):
        # Four defects here have been values computed correctly and lost
        # crossing a schema that had no key for them.
        from orchestrator.state import zero_usage

        assert "planner_peak_prompt_tokens" in zero_usage()
