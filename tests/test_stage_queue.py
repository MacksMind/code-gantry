"""The queue: what one derivation produced that has not run yet.

`plan` keeps the first stage and holds the rest; `advance` takes the next one
instead of calling the planner. That is the whole saving — the planner is the
expensive participant, and a derivation is 5 to 7 minutes of a ~13 minute
stage.

The queue is state, not a local: it has to survive a pause, and the pause is
checked immediately after the squash precisely so a run stops *between* queued
stages rather than mid-batch.
"""

import pytest

from test_config import as_test_tools


def _cfg_for_pause():
    """Only the resume hint reads this; the queue behaviour does not care."""
    from code_gantry.config import parse_config

    return parse_config(as_test_tools({
        "target_repo": "/tmp", "base_ref": "main", "project_branch": "p",
        "plan_root": "PLAN.md", "full_test_command": "true",
        "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.6-sol"},
    }))


def _spec(sid, edit, read=(), excerpts=()):
    return {
        "id": sid, "instruction": "do it", "edit_files": list(edit),
        "read_files": list(read),
        "read_excerpts": [{"path": p, "start": 1, "end": 2} for p in excerpts],
    }


class TestTheQueueIsHeldInState:
    def test_it_is_declared_so_the_driver_keeps_it(self):
        # The merge filters against the schema. Undeclared, the queue would be
        # written by `plan` and dropped before `advance` ever saw it — the
        # silent loss `full_suite_digest` shipped with.
        from code_gantry.state import RunState

        assert "stage_queue" in RunState.__annotations__

    def test_a_fresh_run_starts_with_none(self):
        import time

        from code_gantry.state import new_state

        s = new_state(
            run_id="r", project_slug="p", config_hash="h", target_repo="/tmp",
            base_ref="main", base_sha="a", plan_sha="b", project_branch="pb",
            started_at=time.time(),
        )
        assert s["stage_queue"] == []


class TestAdvanceTakesTheNextQueuedStage:
    def _advanced(self, queue, index=3):
        """What `advance` returns, given a queue, without running a stage."""
        from code_gantry.nodes import _next_from_queue

        return _next_from_queue(
            {"stage_queue": queue, "stage_index": index}, landed_index=index
        )

    def test_an_empty_queue_routes_to_the_planner(self):
        assert self._advanced([])["next_hop"] == "plan"

    def test_a_queued_stage_becomes_current_and_skips_the_planner(self):
        update = self._advanced([_spec("two", ["app/b.rb"]), _spec("three", ["app/c.rb"])])
        assert update["next_hop"] == "precheck"
        assert update["current"]["id"] == "two"

    def test_the_rest_stays_queued(self):
        update = self._advanced([_spec("two", ["app/b.rb"]), _spec("three", ["app/c.rb"])])
        assert [s["id"] for s in update["stage_queue"]] == ["three"]

    def test_the_last_queued_stage_empties_it(self):
        update = self._advanced([_spec("two", ["app/b.rb"])])
        assert update["stage_queue"] == []
        assert update["current"]["id"] == "two"

    def test_the_index_advances_for_the_queued_stage(self):
        # Each stage is its own branch and its own log directory, so a queued
        # one cannot reuse the index of the stage that just landed.
        update = self._advanced([_spec("two", ["app/b.rb"])], index=3)
        assert update["stage_index"] == 4

    def test_a_queued_stage_starts_at_revision_zero(self):
        update = self._advanced([_spec("two", ["app/b.rb"])])
        assert update["revision"] == 0


class TestTheEdgeTableAllowsTheShortcut:
    def test_advance_may_reach_precheck(self):
        from code_gantry.driver import EDGES

        assert "precheck" in EDGES["advance"], (
            "a node routing outside its edges raises rather than rerouting"
        )

    def test_advance_still_reaches_plan_and_escalate(self):
        from code_gantry.driver import EDGES

        assert {"plan", "escalate"} <= set(EDGES["advance"])


class TestThePauseStillWins:
    """A pause beats the queue, and the queue survives it.

    The check sits after the squash for exactly this: with a queue, `advance`
    no longer routes to `plan`, so a pause that was only read before a planner
    call would wait for the whole batch. Stopping here leaves the queue in
    state, so `resume` continues the batch rather than re-deriving it.
    """

    def test_a_pause_stops_the_run_and_keeps_the_queue(self, tmp_path):
        from code_gantry.nodes import _next_from_queue, _pause_escalation

        flag = tmp_path / "paused"
        flag.write_text("")
        queued = _next_from_queue(
            {"stage_queue": [_spec("two", ["app/b.rb"])], "stage_index": 1},
            landed_index=1,
        )
        paused = _pause_escalation(flag, {"run_id": "r"}, _cfg_for_pause())
        merged = {**queued, **paused}
        assert merged["next_hop"] == "escalate"
        assert merged["current"]["id"] == "two", "the popped stage is not lost"
