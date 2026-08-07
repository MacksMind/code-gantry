"""The hand-rolled graph driver that replaces LangGraph.

Routing was always ours — `nodes` set `next_hop` and `_router` was a table
lookup — so what the framework actually supplied was four things: the loop, a
state merge, a checkpointer, and a step ceiling. Each is reproduced here
deliberately, because two of them were load-bearing in ways that are easy to
lose.

**Schema filtering is a feature, not an accident.** LangGraph silently drops
keys `RunState` does not declare, and `state.py` says in as many words that it
relies on this: "without this line verify writes it, the schema discards it,
and the gate silently never skips — which is exactly how it shipped the first
time." A plain `dict.update` stops dropping them, which would turn a
misspelled key from a caught bug into a live one.

**Resume becomes explicit.** LangGraph resumes from a pending task, so
`resume_entry_point` was not always consulted; here it always is. That is a
behaviour change and is pinned rather than assumed.

**And the ceiling escalates.** `recursion_limit`'s own docstring says
exhausting it "surfaces as an opaque framework error rather than an escalation
— the one failure mode this tool must not have." Owning the loop is what lets
that be fixed rather than described.
"""

import json
import sqlite3

import pytest


@pytest.fixture
def rt():
    return object()


def _nodes(**fns):
    return dict(fns)


class TestTheLoop:
    def test_it_walks_until_a_node_ends_the_run(self, rt):
        from orchestrator.driver import drive

        seen = []

        def a(state, _rt):
            seen.append("a")
            return {"next_hop": "b"}

        def b(state, _rt):
            seen.append("b")
            return {"next_hop": "end", "status": "complete"}

        final = drive(
            rt, {"run_id": "r"}, nodes=_nodes(a=a, b=b),
            edges={"a": ["b"], "b": ["end"]}, entry=lambda _s: "a",
        )
        assert seen == ["a", "b"]
        assert final["status"] == "complete"

    def test_a_node_may_reach_itself_when_the_table_allows_it(self, rt):
        from orchestrator.driver import drive

        calls = {"n": 0}

        def a(state, _rt):
            calls["n"] += 1
            return {"next_hop": "a" if calls["n"] < 3 else "end"}

        drive(rt, {}, nodes=_nodes(a=a), edges={"a": ["a", "end"]}, entry=lambda _s: "a")
        assert calls["n"] == 3

    def test_routing_outside_the_table_is_a_bug_not_a_reroute(self, rt):
        # A node asking for an edge the spec does not have is a bug in the
        # node; silently rerouting would hide it.
        from orchestrator.driver import drive

        def a(state, _rt):
            return {"next_hop": "nowhere"}

        with pytest.raises(RuntimeError, match="nowhere"):
            drive(rt, {}, nodes=_nodes(a=a), edges={"a": ["end"]}, entry=lambda _s: "a")

    def test_a_node_that_says_nothing_escalates(self, rt):
        # `_router` read a missing `next_hop` as "escalate", and that stays:
        # the run must not stop silently on a node that forgot to route.
        from orchestrator.driver import drive

        def a(state, _rt):
            return {}

        def escalate(state, _rt):
            return {"next_hop": "end", "status": "escalated"}

        final = drive(
            rt, {}, nodes=_nodes(a=a, escalate=escalate),
            edges={"a": ["escalate"], "escalate": ["end"]}, entry=lambda _s: "a",
        )
        assert final["status"] == "escalated"


class TestStateMerging:
    def test_an_update_is_merged_not_replaced(self, rt):
        from orchestrator.driver import drive

        def a(state, _rt):
            return {"stage_index": 4, "next_hop": "end"}

        final = drive(
            rt, {"run_id": "r", "stage_index": 0}, nodes=_nodes(a=a),
            edges={"a": ["end"]}, entry=lambda _s: "a",
        )
        assert final["run_id"] == "r" and final["stage_index"] == 4

    def test_a_key_the_schema_does_not_declare_is_dropped(self, rt):
        """The behaviour `state.py` documents relying on.

        Keeping it means a typo in a node stays a caught bug. Losing it means
        the run carries a key nothing reads, which is how `full_suite_digest`
        shipped broken the first time — written, discarded, and silently never
        read.
        """
        from orchestrator.driver import drive

        def a(state, _rt):
            return {"stage_index": 1, "stage_indx": 99, "next_hop": "end"}

        final = drive(
            rt, {}, nodes=_nodes(a=a), edges={"a": ["end"]}, entry=lambda _s: "a"
        )
        assert final["stage_index"] == 1
        assert "stage_indx" not in final

    def test_every_declared_key_survives(self, rt):
        # The filter must be the schema, not a hand-written list that drifts.
        from orchestrator.driver import drive
        from orchestrator.state import RunState

        keys = list(RunState.__annotations__)
        assert "full_suite_digest" in keys, "the key the docstring is about"

        def a(state, _rt):
            return {k: "x" for k in keys} | {"next_hop": "end"}

        final = drive(rt, {}, nodes=_nodes(a=a), edges={"a": ["end"]}, entry=lambda _s: "a")
        assert set(keys) <= set(final)


class TestCheckpointing:
    def test_each_step_is_recorded_and_the_last_one_reloads(self, tmp_path, rt):
        from orchestrator.driver import drive, load_state, open_checkpointer

        db = tmp_path / "state.db"
        write, conn = open_checkpointer(db)

        def a(state, _rt):
            return {"stage_index": 1, "next_hop": "b"}

        def b(state, _rt):
            return {"stage_index": 2, "next_hop": "end"}

        drive(
            rt, {"run_id": "r"}, nodes=_nodes(a=a, b=b), edges={"a": ["b"], "b": ["end"]},
            entry=lambda _s: "a", checkpoint=write,
        )
        conn.close()

        assert load_state(db, "r")["stage_index"] == 2

    def test_a_crash_mid_node_resumes_from_the_last_completed_one(self, tmp_path, rt):
        """The behaviour change worth pinning.

        LangGraph resumed from a pending task, so `resume_entry_point` was not
        always consulted. Here a node that dies leaves no checkpoint of its
        own, the last completed node's state is what reloads, and the entry
        point always decides where to go. More predictable, and different.
        """
        from orchestrator.driver import drive, load_state, open_checkpointer

        db = tmp_path / "state.db"
        write, conn = open_checkpointer(db)

        def a(state, _rt):
            return {"stage_index": 1, "next_hop": "b"}

        def b(state, _rt):
            raise RuntimeError("died mid-node")

        with pytest.raises(RuntimeError, match="died mid-node"):
            drive(
                rt, {"run_id": "r"}, nodes=_nodes(a=a, b=b),
                edges={"a": ["b"], "b": ["end"]}, entry=lambda _s: "a",
                checkpoint=write,
            )
        conn.close()

        reloaded = load_state(db, "r")
        assert reloaded["stage_index"] == 1, "b's work must not be half-recorded"

    def test_an_unknown_run_reloads_as_nothing(self, tmp_path):
        from orchestrator.driver import load_state, open_checkpointer

        db = tmp_path / "state.db"
        _write, conn = open_checkpointer(db)
        conn.close()
        assert load_state(db, "never-ran") is None

    def test_two_runs_in_one_file_do_not_see_each_other(self, tmp_path, rt):
        from orchestrator.driver import drive, load_state, open_checkpointer

        db = tmp_path / "state.db"
        write, conn = open_checkpointer(db)
        for run_id, index in (("r1", 7), ("r2", 9)):
            drive(
                rt, {"run_id": run_id},
                nodes=_nodes(a=lambda s, _r, i=index: {"stage_index": i, "next_hop": "end"}),
                edges={"a": ["end"]}, entry=lambda _s: "a", checkpoint=write,
            )
        conn.close()
        assert load_state(db, "r1")["stage_index"] == 7
        assert load_state(db, "r2")["stage_index"] == 9


class TestTheStepCeiling:
    def test_it_escalates_rather_than_raising(self, rt):
        """`recursion_limit`'s docstring names this as the failure to avoid.

        Under LangGraph, exhausting the limit "surfaces as an opaque framework
        error rather than an escalation — the one failure mode this tool must
        not have." Owning the loop is what lets that be fixed instead of
        described.
        """
        from orchestrator.driver import drive

        def a(state, _rt):
            return {"next_hop": "a"}

        def escalate(state, _rt):
            return {"next_hop": "end", "status": "escalated"}

        final = drive(
            rt, {}, nodes=_nodes(a=a, escalate=escalate),
            edges={"a": ["a", "escalate"], "escalate": ["end"]},
            entry=lambda _s: "a", max_steps=5,
        )
        assert final["status"] == "escalated"
        assert "step" in (final.get("escalation_reason") or "").lower()

    def test_a_run_inside_the_ceiling_is_untouched(self, rt):
        from orchestrator.driver import drive

        def a(state, _rt):
            return {"next_hop": "end", "status": "complete"}

        final = drive(
            rt, {}, nodes=_nodes(a=a), edges={"a": ["end"]},
            entry=lambda _s: "a", max_steps=5,
        )
        assert final["status"] == "complete"
