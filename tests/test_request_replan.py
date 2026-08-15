"""The executor can hand a stage back instead of failing its way out.

Two shapes, measured on one run rather than imagined. A stage whose criteria
contradicted the project's own spec could not be completed by anyone, and the
executor said so plainly — "the reported issues are already resolved, no
additional edits were made" — which reached the planner as "the attempt
reproduced the previous diff", a symptom of the thing rather than the thing.
And a version bump scoped to one directory broke fifty examples in ten files it
was forbidden to touch, which reached the executor again as "tests failed".

Both routes were correct readings of what the tree showed. Neither carried why,
because the tree cannot show it. `request_replan` is that channel, and it
claims nothing about the work: no gate is skipped, nothing lands, and the
redrawn stage faces every check and a review exactly as before.
"""

from dataclasses import dataclass

import pytest

from code_gantry import nodes
from code_gantry.executortools import REPLAN_TOOL, dispatch, tool_schemas

from test_nodes import StubExecutor, StubPlanner, make, planned_stage, with_stage


@dataclass
class ReplanningExecutor(StubExecutor):
    """A stub that hands the stage back, as `request_replan` makes the real one."""

    kind: str = "unsatisfiable"
    reason: str = "config/routes.rb is outside my scope"

    def run_agent_stage(self, *a, **k):
        had = list(self.edits)
        out = super().run_agent_stage(*a, **k)
        # The real loop commits its own work before the attempt ends, and
        # `committed_work` is measured from the sha either side. A stub that
        # wrote without committing would report every exploratory replan as
        # having found nothing, which is the answer the test is checking for.
        if had and self.repo is not None:
            import subprocess

            for args in (["add", "-A"], ["commit", "-qm", "executor cycle"]):
                subprocess.run(
                    ["git", "-C", str(self.repo), *args],
                    check=True,
                    capture_output=True,
                )
        out.replan_kind = self.kind
        out.replan_reason = self.reason
        return out


class TestTheToolIsOffered:
    def test_it_is_in_the_executor_schema(self):
        names = [t["name"] for t in tool_schemas(None)]
        assert "request_replan" in names

    def test_the_two_kinds_are_an_enum_rather_than_free_text(self):
        """The kinds decide what the planner does, so they cannot be prose.

        Recovering an intent by matching words in a sentence is how three
        distinct refusal causes became one bucket once they rendered the same
        way — the routing has to travel as a value.
        """
        kind = REPLAN_TOOL["input_schema"]["properties"]["kind"]
        assert kind["enum"] == ["unsatisfiable", "incomplete"]
        assert set(REPLAN_TOOL["input_schema"]["required"]) == {"kind", "reason"}

    def test_dispatch_answers_it_so_the_conversation_stays_well_formed(self):
        """A tool call the provider sees no result for is a malformed exchange.

        The client reads the arguments off the call and ends the attempt, so
        this reply is not how the signal travels — but its absence would break
        the next request for a reason unrelated to replans.
        """
        answer = dispatch(
            "request_replan",
            {"kind": "incomplete", "reason": "the bump broke ten other files"},
            reader=None,
            editor=None,
            semantic=None,
        )
        assert "planner" in answer.lower()


class TestItChangesWhereTheGraphGoes:
    """The point of the field, and the thing a unit test of it would miss.

    `ExecutorTurn.stopped` was set in three places and read in none, under a
    comment saying the loop must not treat it as finished — a control field
    nothing routes on reviews as correct and does nothing. So these assert the
    destination, not the value.
    """

    @pytest.mark.parametrize("kind", ["unsatisfiable", "incomplete"])
    def test_a_request_routes_to_the_planner_rather_than_the_gates(
        self, repo, tmp_path, kind
    ):
        ex = ReplanningExecutor(repo=repo, kind=kind)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        with_stage(state, rt)
        out = nodes.execute(state, rt)

        assert out["failure_layer"] == "replan"
        assert out["next_hop"] == "plan"

    def test_the_reason_reaches_the_planner_verbatim(self, repo, tmp_path):
        """It is the whole of what the planner gets, so it cannot be summarised.

        A closing paragraph naming six directories was already being written to
        `executor.log` and read by nobody; the planner re-derived the same
        answer twenty minutes and a full suite later.
        """
        ex = ReplanningExecutor(
            repo=repo,
            kind="incomplete",
            reason="fifty examples in ten files I may not touch",
        )
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        with_stage(state, rt)
        out = nodes.execute(state, rt)

        assert (
            "fifty examples in ten files I may not touch"
            in out["last_failure"]["detail"]
        )

    @pytest.mark.parametrize(
        "kind,expected",
        [
            ("unsatisfiable", "Rewrite what it requires"),
            ("incomplete", "Widen this stage"),
        ],
    )
    def test_the_two_kinds_tell_the_planner_different_things(
        self, repo, tmp_path, kind, expected
    ):
        """Same destination, different instruction.

        Conflating them gets a stage revised forever instead of widened, which
        is the shape that cost four revisions and thirteen attempts.
        """
        ex = ReplanningExecutor(repo=repo, kind=kind)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        with_stage(state, rt)
        out = nodes.execute(state, rt)

        assert expected in out["last_failure"]["detail"]


def _revises():
    """A planner that revises the stage in place, which is the budget's path."""
    from code_gantry.planner import PlannerOutcome

    return StubPlanner([
        PlannerOutcome(
            "revise", "r", "e",
            stage_fields=planned_stage(), revision_mode="extend",
        )
    ])


class TestItLandsNothingAndSkipsNothing:
    def test_it_is_a_planning_failure_so_a_resume_re_enters_at_the_planner(self):
        """Not `verify`: the gates were never waiting on an answer.

        Re-entering there would ask them a question nobody raised, and
        re-running the executor would put it back exactly where it stopped.
        """
        from code_gantry.state import (
            PLANNING_FAILURES,
            REPO_STATE_FAILURES,
            resume_entry_point,
        )

        assert "replan" in PLANNING_FAILURES
        assert "replan" not in REPO_STATE_FAILURES
        assert resume_entry_point({"resuming": True, "failure_layer": "replan"}) == "plan"

    def test_exploring_does_not_spend_the_without_landing_budget(
        self, repo, tmp_path
    ):
        """Capping exploration at three contradicts asking for it.

        The budget detects a run that has stopped making progress. An attempt
        that made a change to find out what the change does is the opposite,
        and `max_planner_interventions` is the absolute backstop underneath —
        global across the run, so nothing here is unbounded.
        """
        ex = ReplanningExecutor(
            repo=repo, kind="incomplete", edits=[("app.py", "changed\n")]
        )
        cfg, rt, state = make(repo, tmp_path, executor=ex, planner=_revises())
        state["interventions_since_landing"] = 2
        with_stage(state, rt)

        failure = nodes.execute(state, rt)
        assert failure["last_failure"]["committed_work"] is True

        state.update(failure)
        assert nodes.plan(state, rt)["interventions_since_landing"] == 2

    def test_claiming_exploration_without_committing_still_spends_it(
        self, repo, tmp_path
    ):
        """Otherwise the label is the budget's off switch.

        An attempt that says `incomplete` and moved nothing explored nothing —
        it is a stuck attempt wearing the other word. The sha either side of it
        decides, which is a fact rather than a claim.
        """
        ex = ReplanningExecutor(repo=repo, kind="incomplete", edits=[])
        cfg, rt, state = make(repo, tmp_path, executor=ex, planner=_revises())
        state["interventions_since_landing"] = 1
        with_stage(state, rt)

        failure = nodes.execute(state, rt)
        assert failure["last_failure"]["committed_work"] is False

        state.update(failure)
        assert nodes.plan(state, rt)["interventions_since_landing"] == 2

    def test_it_does_not_clear_the_without_landing_budget(self, repo, tmp_path):
        """The only thing bounding replan then redraw then replan.

        Per-revision retries reset themselves when a stage is redrawn, which is
        right — a new stage should not inherit spent attempts. This counter is
        deliberately not among them: clearing it here would make the tool an
        unbounded loop generator, and three requests with nothing landing in
        between is exactly the signal a human should look at it.
        """
        ex = ReplanningExecutor(repo=repo)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state["interventions_since_landing"] = 2
        with_stage(state, rt)
        out = nodes.execute(state, rt)

        assert out.get("interventions_since_landing", 2) != 0
