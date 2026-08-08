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


class TestAnOldCheckpointSaysWhatItIs:
    """A run started before the cutover cannot be resumed, and must say so.

    `load_state` reads a table this driver created. A `state.db` written by the
    previous framework has different tables entirely, so the honest answer is
    "not readable", and the first version returned `None` — which `resume`
    reports as "no checkpoint for run <id>". That reads like the run never
    existed, sending an operator to look for a typo in the run id rather than
    telling them the one true thing: the work is safe on the branch, and this
    run ends here.

    Detected by what the file contains rather than by a version marker, because
    there is no marker to read on a database written by something else.
    """

    def _langgraph_shaped(self, path):
        import sqlite3

        # The real shape, read off a live run's file rather than imagined:
        # `checkpoints` and `writes`. The first version of this fixture made up
        # one table, which is how the detection came to be written as "is ours
        # missing" instead of "is theirs present".
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint BLOB)")
        conn.execute("CREATE TABLE writes (thread_id TEXT, task_id TEXT)")
        conn.execute("INSERT INTO checkpoints VALUES ('r', X'0102')")
        conn.commit()
        conn.close()

    def test_it_is_named_as_the_older_format(self, tmp_path):
        from orchestrator.driver import UnreadableCheckpoint, load_state

        db = tmp_path / "state.db"
        self._langgraph_shaped(db)
        with pytest.raises(UnreadableCheckpoint) as e:
            load_state(db, "r")
        assert "older" in str(e.value).lower()
        assert "branch" in str(e.value).lower(), "must say the work is not lost"

    def test_a_run_that_never_existed_is_still_just_missing(self, tmp_path):
        # The two cases must stay distinguishable: nothing to resume is not the
        # same as something that cannot be read.
        from orchestrator.driver import load_state, open_checkpointer

        db = tmp_path / "state.db"
        _w, conn = open_checkpointer(db)
        conn.close()
        assert load_state(db, "never-ran") is None

    def test_it_is_still_recognised_after_a_read_created_our_table(self, tmp_path):
        # What actually happened: a read against a live run's database left an
        # empty `steps` table behind, and the detection then saw both formats.
        import sqlite3

        from orchestrator.driver import UnreadableCheckpoint, load_state

        db = tmp_path / "state.db"
        self._langgraph_shaped(db)
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE steps (run_id TEXT, step INT, node TEXT, state TEXT)")
        conn.commit()
        conn.close()
        with pytest.raises(UnreadableCheckpoint):
            load_state(db, "r")

    def test_reading_does_not_write(self, tmp_path):
        # A reader that creates tables is a bug in its own right, and this one
        # destroyed the evidence the check above depends on.
        import sqlite3

        from orchestrator.driver import load_state

        db = tmp_path / "state.db"
        sqlite3.connect(str(db)).close()
        load_state(db, "r")
        conn = sqlite3.connect(str(db))
        tables = [n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        assert tables == [], f"read created {tables}"

    def test_an_absent_file_is_missing_not_unreadable(self, tmp_path):
        from orchestrator.driver import load_state

        assert load_state(tmp_path / "nope.db", "r") is None


class TestTheCheckpointSequenceSurvivesAResume:
    """`step` counted from zero inside one `drive` call, and the key is
    `(run_id, step)` — so a resumed session overwrote rows 1..n of the session
    before it while that session's tail survived at higher numbers. `load_state`
    ordered by `step`, so it returned whichever session had run *longest*, which
    on a resumed run is reliably the older one.

    Measured on a 14-hour run: rows 1-65 were the live session with `completed`
    climbing to 39, rows 66-210 were nine hours stale, and `load_state` returned
    row 210 — `completed` 31, naming a stage that had never run.
    """

    def _write(self, tmp_path, run_id, steps):
        from orchestrator.driver import open_checkpointer

        write, conn = open_checkpointer(tmp_path / "s.db")
        try:
            for i, completed in enumerate(steps, start=1):
                write(run_id, i, "plan", {"run_id": run_id, "completed": completed})
        finally:
            conn.close()

    def test_a_resume_appends_rather_than_renumbering(self, tmp_path):
        from orchestrator.driver import last_step

        self._write(tmp_path, "r", [1, 2, 3])
        assert last_step(tmp_path / "s.db", "r") == 3

    def test_an_unknown_run_starts_at_zero(self, tmp_path):
        from orchestrator.driver import last_step, open_checkpointer

        write, conn = open_checkpointer(tmp_path / "s.db")
        conn.close()
        assert last_step(tmp_path / "s.db", "r") == 0
        assert last_step(tmp_path / "missing.db", "r") == 0

    def test_the_latest_write_wins_even_when_its_step_is_lower(self, tmp_path):
        # The live shape: a long old session, then a short new one that
        # renumbered over its head. Ordering by `step` picks the stale tail.
        from orchestrator.driver import load_state, open_checkpointer

        self._write(tmp_path, "r", list(range(1, 11)))     # old session, steps 1-10
        write, conn = open_checkpointer(tmp_path / "s.db")
        try:
            write("r", 1, "plan", {"run_id": "r", "completed": 99})
        finally:
            conn.close()
        assert load_state(tmp_path / "s.db", "r")["completed"] == 99

    def test_the_runaway_guard_counts_this_session_not_the_sequence(self, tmp_path):
        # Seeded from the sequence it would trip on the first node of any
        # resumed run, which is a ceiling deciding an outcome rather than
        # catching a loop.
        from orchestrator.driver import drive

        seen = []

        def node(state, rt):
            seen.append(1)
            return {"next_hop": "end"}

        out = drive(
            None, {"run_id": "r"}, nodes={"plan": node}, edges={"plan": ["end"]},
            entry=lambda s: "plan", max_steps=5, start_step=500,
        )
        assert seen == [1], "the guard fired on a run that had taken one step"
        assert out["next_hop"] == "end"
