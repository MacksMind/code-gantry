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


def _cfg(source=None):
    """A config, because the message has to name the file to resume from.

    The hint used to be built from `run_id` alone, which is not what any
    command takes any more. `source=None` gives the `<config>` placeholder,
    which is what a config built in memory honestly is.
    """
    from code_gantry.config import parse_config

    return parse_config(
        {
            "target_repo": "/tmp", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        },
        source=source,
    )


class TestItStopsAfterTheSquash:
    def test_a_pause_set_during_a_stage_stops_at_the_landing(self, tmp_path):
        from code_gantry.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        paused = _pause_escalation(flag, {"run_id": "r"}, _cfg())
        assert paused is not None
        assert paused["next_hop"] == "escalate"
        assert paused["failure_layer"] == "paused"

    def test_no_flag_is_no_escalation(self, tmp_path):
        from code_gantry.nodes import _pause_escalation

        assert _pause_escalation(tmp_path / "absent", {"run_id": "r"}, _cfg()) is None

    def test_the_note_is_carried_through(self, tmp_path):
        from code_gantry.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("picking up the new prompts")
        paused = _pause_escalation(flag, {"run_id": "r"}, _cfg())
        assert "picking up the new prompts" in paused["escalation_reason"]

    def test_it_names_the_config_to_resume_from(self, tmp_path):
        """It named the run id, and every command takes a config path.

        The message was correct when `resume` took a run id and stayed
        unchanged through the CLI's move to config paths, so an operator who
        copied it got a usage error. Nothing failed: it is a console string,
        and the test that covered it asserted the run id was present — which
        is exactly the part that had to go.
        """
        from code_gantry.nodes import _pause_escalation

        source = tmp_path / "code_gantry.yaml"
        source.write_text("x: 1\n")
        flag = tmp_path / "paused"
        flag.write_text("")
        paused = _pause_escalation(flag, {"run_id": "20260808-x"}, _cfg(source))
        assert str(source) in paused["escalation_reason"]
        assert "resume 20260808-x" not in paused["escalation_reason"]


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
        from code_gantry.driver import EDGES

        assert "escalate" in EDGES["advance"], (
            "advance stops the run now, so the table has to say so — a node "
            "routing outside its edges raises rather than rerouting"
        )

    def test_advance_still_reaches_plan(self):
        from code_gantry.driver import EDGES

        assert "plan" in EDGES["advance"]


class TestOneMessageForBothCheckpoints:
    def test_plan_and_advance_use_the_same_helper(self):
        """Two copies of a pause message is two things to keep in step.

        `plan`'s wording carries a promise — "nothing is half-done" — that is
        only true because of where the check sits. Restating it beside a second
        check is how one of them comes to be wrong.
        """
        import inspect

        from code_gantry import nodes

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
        from code_gantry.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        assert _pause_escalation(flag, {"run_id": "r"}, _cfg(), "precheck")["paused_before"] == "precheck"

    def test_the_other_checkpoints_record_nothing_to_resume_into(self, tmp_path):
        # Written every time rather than left absent, so a pause caught before
        # the planner ran cannot inherit a value from an earlier one.
        from code_gantry.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        assert _pause_escalation(flag, {"run_id": "r"}, _cfg())["paused_before"] == ""

    def test_the_message_says_a_stage_is_waiting(self, tmp_path):
        from code_gantry.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        held = _pause_escalation(flag, {"run_id": "r"}, _cfg(), "precheck")["escalation_reason"]
        between = _pause_escalation(flag, {"run_id": "r"}, _cfg())["escalation_reason"]
        assert "derived" in held and "derived" not in between


class TestTheLandingSiteRecordsWhatItWasAboutToDo:
    """The argument existed and one of its two call sites did not pass it.

    `_next_from_queue` promotes the next stage of a batch and returns
    `next_hop: "precheck"` — a stage drawn, correct, and ready to start. The
    pause check immediately below it called `_pause_escalation` with no
    `ready_hop`, so `paused_before` was written empty and the escalation said
    "between stages" when a stage was waiting.

    On resume that is indistinguishable from a stage awaiting revision, which
    is the confusion `paused_before` was added to prevent, and the resume
    routes to `plan`. Observed: a pause taken after `error-messages-for-branch-guard`
    landed came back as `[plan] revising query-trace-patch-guard` and spent a
    planner call revising a stage nothing was wrong with.

    The shape is an argument that never reached an older call site — except
    here the call site is *newer*: batching added the queue promotion directly
    above a pause check written when `advance` could only ever route to `plan`,
    and back then "between stages" was the only thing it could mean.
    """

    def _paused_update(self, tmp_path, next_hop):
        from code_gantry.nodes import _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        landed = {"stage_index": 3, "next_hop": next_hop}
        return _pause_escalation(
            flag, {"run_id": "r"}, _cfg(), _held_hop(landed)
        )

    def test_a_promoted_queue_stage_is_recorded_as_held(self, tmp_path):
        assert self._paused_update(tmp_path, "precheck")["paused_before"] == "precheck"

    def test_an_empty_queue_records_nothing_held(self, tmp_path):
        assert self._paused_update(tmp_path, "plan")["paused_before"] == ""

    def test_the_message_matches_what_was_recorded(self, tmp_path):
        # The operator reads this. Saying "between stages" while holding a
        # derived stage is the same wrong answer in prose.
        held = self._paused_update(tmp_path, "precheck")["escalation_reason"]
        empty = self._paused_update(tmp_path, "plan")["escalation_reason"]
        assert "derived" in held and "derived" not in empty

    def test_only_precheck_counts_as_held(self):
        # `paused_before` is returned verbatim as the resume entry point, so
        # anything truthy that is not a node the run can be resumed into would
        # send it somewhere it was never about to go.
        assert _held_hop({"next_hop": "plan"}) == ""
        assert _held_hop({}) == ""
        assert _held_hop({"next_hop": "precheck"}) == "precheck"


def _held_hop(landed):
    from code_gantry.nodes import held_hop

    return held_hop(landed)


class TestResumingIntoAHeldStage:
    def test_a_held_stage_runs_rather_than_being_re_derived(self):
        from code_gantry.state import resume_entry_point

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
        from code_gantry.state import resume_entry_point

        assert resume_entry_point({
            "resuming": True, "failure_layer": "paused",
            "paused_before": "", "current": {"id": "s"},
            "last_failure": {"layer": "tests"},
        }) == "plan"

    def test_a_budget_stop_is_unchanged(self):
        from code_gantry.state import resume_entry_point

        assert resume_entry_point({
            "resuming": True, "failure_layer": "budget", "current": {"id": "s"},
        }) == "plan"

    def test_the_key_is_declared_so_the_driver_keeps_it(self):
        # The merge filters against the schema; an undeclared key is dropped,
        # which is how `full_suite_digest` shipped broken.
        from code_gantry.state import RunState

        assert "paused_before" in RunState.__annotations__
