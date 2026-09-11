"""The ledger: an append-only event log, and views derived from it."""

import ast
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from code_gantry import ledger as ledger_module
from code_gantry.ledger import (
    ANSWER,
    BLOCKED,
    CLAIMED,
    FINDING_CLAIMED,
    FINDING_FOLDED,
    FINDING_RELEASED,
    FINDING_RESOLVED,
    LANDED,
    NODE_MARKED,
    NODE_RETIRED,
    RELEASED,
    STAGE_DERIVED,
    STAGE_DONE,
    STAGE_DROPPED,
    STAGE_RELEASED,
    STAGE_TAKEN,
    STRUCK,
    Event,
    LedgerError,
    StaleEdit,
    apply_fold,
    build_views,
    fold_marks,
    open_ledger,
    read_ledger,
    should_fold,
)


def ticking():
    """A clock whose readings sort in call order."""
    n = [0]

    def clock():
        n[0] += 1
        return f"2026-09-11T00:00:{n[0]:02d}+00:00"

    return clock


@pytest.fixture
def led(tmp_path):
    return open_ledger(tmp_path / "ledger.db", origin="host-a", actor="test", clock=ticking())


def plant(led, *keys, parent=None, kind="item"):
    for i, key in enumerate(keys):
        led.upsert_node(key, parent=parent, position=i, kind=kind, title=key)


class TestTheTableIsAppendOnly:
    def test_the_module_never_updates_or_deletes_events(self):
        source = Path(ledger_module.__file__).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.upper()
                assert "UPDATE " not in text and "DELETE " not in text, node.value

    def test_events_are_numbered_per_origin_from_one(self, led):
        a = led.append("x")
        b = led.append("y")
        assert (a.origin, a.seq) == ("host-a", 1)
        assert (b.origin, b.seq) == ("host-a", 2)

    def test_the_sequence_is_one_per_ledger_across_origins(self, tmp_path):
        path = tmp_path / "ledger.db"
        a = open_ledger(path, origin="host-a")
        a.append("x")
        b = open_ledger(path, origin="host-b")
        event = b.append("y")
        # One sequence per ledger, whoever writes: ids built from it stay
        # unique without the origin having to carry them.
        assert (event.origin, event.seq) == ("host-b", 2)
        assert [(e.origin, e.seq) for e in a.events()] == [("host-a", 1), ("host-b", 2)]

    def test_the_row_is_the_dataclass(self, led):
        led.append("x", key="k", stage_id="s", run_id="r", sha="abc", note="n")
        conn = sqlite3.connect(str(led.path))
        columns = {c[1] for c in conn.execute("PRAGMA table_info(events)")}
        assert columns == {f.name for f in Event.__dataclass_fields__.values()}
        (body,) = conn.execute("SELECT body FROM events").fetchone()
        assert '"note": "n"' in body and '"actor": "test"' in body


class TestReadersNeverCreate:
    def test_an_absent_file_reads_as_empty_and_stays_absent(self, tmp_path):
        path = tmp_path / "nothing" / "ledger.db"
        led = read_ledger(path)
        assert led.events() == []
        assert led.views().nodes == {}
        assert not path.exists() and not path.parent.exists()

    def test_a_reader_cannot_write(self, tmp_path):
        open_ledger(tmp_path / "ledger.db", origin="a").append("x")
        with pytest.raises(LedgerError):
            read_ledger(tmp_path / "ledger.db").append("y")

    def test_a_reader_sees_what_a_writer_wrote(self, tmp_path):
        writer = open_ledger(tmp_path / "ledger.db", origin="a")
        writer.append("x", key="k")
        assert [e.key for e in read_ledger(tmp_path / "ledger.db").events()] == ["k"]


_CHILD = """
import sys
from code_gantry.ledger import open_ledger
led = open_ledger(sys.argv[1], origin=sys.argv[2])
for _ in range(int(sys.argv[3])):
    led.append("tick")
led.close()
"""


class TestTwoWritersShareTheFile:
    def test_processes_writing_under_different_origins_lose_nothing(self, tmp_path):
        path = tmp_path / "ledger.db"
        procs = [
            subprocess.Popen([sys.executable, "-c", _CHILD, str(path), f"p{i}", "40"])
            for i in range(3)
        ]
        for p in procs:
            assert p.wait(120) == 0
        events = read_ledger(path).events()
        assert len(events) == 120
        assert sorted({e.origin for e in events}) == ["p0", "p1", "p2"]
        # One sequence for the ledger, with no gap and no repeat, however the
        # three writers interleaved; each writer's forty all arrived.
        assert sorted(e.seq for e in events) == list(range(1, 121))
        for origin in ("p0", "p1", "p2"):
            assert sum(1 for e in events if e.origin == origin) == 40


class TestKeyState:
    @pytest.mark.parametrize(
        "events, expected",
        [
            ([], "open"),
            ([(CLAIMED, {})], "claimed"),
            ([(CLAIMED, {}), (RELEASED, {})], "open"),
            ([(CLAIMED, {}), (LANDED, {"sha": "abc"})], "landed"),
            ([(STRUCK, {"reason": "zero population"})], "struck"),
            ([(BLOCKED, {"question": "which?"})], "blocked"),
            ([(BLOCKED, {"question": "which?"}), (ANSWER, {"text": "this one"})], "open"),
        ],
    )
    def test_the_last_state_event_decides(self, led, events, expected):
        plant(led, "k.001")
        for kind, body in events:
            sha = body.pop("sha", None)
            led.append(kind, key="k.001", sha=sha, **body)
        assert led.views().state("k.001").state == expected

    def test_an_answer_keeps_the_question_and_carries_the_text(self, led):
        plant(led, "k.001")
        led.append(BLOCKED, key="k.001", question="which?")
        led.append(ANSWER, key="k.001", text="this one")
        state = led.views().state("k.001")
        assert (state.question, state.answer) == ("which?", "this one")

    def test_a_landing_records_who_and_where(self, led):
        plant(led, "k.001")
        led.append(LANDED, key="k.001", sha="abc", stage_id="s", run_id="r", evidence="done")
        state = led.views().state("k.001")
        assert (state.sha, state.stage_id, state.run_id, state.evidence) == ("abc", "s", "r", "done")


class TestNodes:
    def test_upserts_build_a_tree_in_position_order(self, led):
        led.upsert_node("d.001", parent=None, position=0, kind="document", title="Doc")
        led.upsert_node("d.003", parent="d.001", position=1, kind="section", title="B")
        led.upsert_node("d.002", parent="d.001", position=0, kind="section", title="A")
        led.upsert_node("d.004", parent="d.002", position=0, kind="item", title="i")
        views = led.views()
        assert [n.key for n in views.walk()] == ["d.001", "d.002", "d.004", "d.003"]
        assert views.is_leaf("d.004") and not views.is_leaf("d.002")
        assert [a.key for a in views.ancestors("d.004")] == ["d.002", "d.001"]

    def test_a_retired_node_leaves_the_tree_but_keeps_its_key(self, led):
        plant(led, "k.001", "k.002")
        led.append(NODE_RETIRED, key="k.001")
        views = led.views()
        assert [n.key for n in views.walk()] == ["k.002"]
        assert views.next_key("k") == "k.003"

    def test_a_stale_edit_is_refused(self, led):
        plant(led, "k.001")
        led.upsert_node("k.001", parent=None, position=0, kind="item", title="v2", base_version=1)
        with pytest.raises(StaleEdit):
            led.upsert_node("k.001", parent=None, position=0, kind="item", title="v3", base_version=1)
        assert led.views().nodes["k.001"].version == 2

    def test_unknown_kind_and_owner_are_refused(self, led):
        with pytest.raises(LedgerError):
            led.upsert_node("k.001", parent=None, position=0, kind="chapter", title="x")
        with pytest.raises(LedgerError):
            led.upsert_node("k.001", parent=None, position=0, kind="item", title="x", owner="robot")

    def test_marks_accumulate_without_repeating(self, led):
        plant(led, "k.001")
        led.append(NODE_MARKED, key="k.001", mark="landed `abc`")
        led.append(NODE_MARKED, key="k.001", mark="landed `abc`")
        led.append(NODE_MARKED, key="k.001", mark="see also")
        assert led.views().nodes["k.001"].marks == ["landed `abc`", "see also"]


class TestFindings:
    def test_a_finding_opens_with_an_id_unique_across_origins(self, led):
        event = led.open_finding(keys=["k.001"], by="planner", claim="x")
        assert event.finding_id == "f-host-a-1"
        assert led.views().findings["f-host-a-1"].status == "open"

    def test_a_landing_on_a_leaf_resolves_its_findings(self, led):
        plant(led, "k.001")
        led.open_finding(keys=["k.001"], by="planner", claim="x")
        led.append(LANDED, key="k.001", sha="abc")
        finding = led.views().findings["f-host-a-2"]
        assert (finding.status, finding.resolved_sha) == ("resolved", "abc")

    def test_a_landing_on_a_section_leaves_its_findings_open(self, led):
        led.upsert_node("s.001", parent=None, position=0, kind="section", title="S")
        led.upsert_node("s.002", parent="s.001", position=0, kind="item", title="i")
        led.open_finding(keys=["s.001"], by="planner", claim="about the section")
        led.append(LANDED, key="s.002", sha="abc")
        assert led.views().findings["f-host-a-3"].status == "open"

    def test_a_reviewer_confirmed_resolution_closes_a_finding_anywhere(self, led):
        led.upsert_node("s.001", parent=None, position=0, kind="section", title="S")
        f = led.open_finding(keys=["s.001"], by="planner", claim="x")
        led.append(FINDING_RESOLVED, sha="abc", finding_id=f.finding_id)
        assert led.views().findings[f.finding_id].status == "resolved"

    def test_a_later_reading_of_the_same_subject_supersedes(self, led):
        plant(led, "k.001")
        first = led.open_finding(keys=["k.001"], by="planner", claim="3 left", subject="count", total="3")
        second = led.open_finding(keys=["k.001"], by="planner", claim="1 left", subject="count", total="1")
        views = led.views()
        assert views.findings[first.finding_id].status == "superseded"
        assert views.findings[first.finding_id].superseded_by == second.finding_id
        assert [f.id for f in views.open_findings()] == [second.finding_id]

    def test_a_different_subject_on_the_same_key_stays(self, led):
        plant(led, "k.001")
        led.open_finding(keys=["k.001"], by="planner", claim="a", subject="count")
        led.open_finding(keys=["k.001"], by="planner", claim="b", subject="callers")
        assert len(led.views().open_findings()) == 2

    def test_an_answer_then_a_fold_closes_the_row(self, led):
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="x", needs="human")
        led.answer_finding(f.finding_id, disposition="fold", text="write this", target_key="k.001")
        assert led.views().findings[f.finding_id].status == "answered"
        led.append(FINDING_FOLDED, key="k.001", finding_id=f.finding_id)
        assert led.views().findings[f.finding_id].status == "folded"

    def test_a_bad_disposition_or_need_is_refused(self, led):
        with pytest.raises(LedgerError):
            led.open_finding(keys=[], by="planner", claim="x", needs="someone")
        f = led.open_finding(keys=[], by="planner", claim="x")
        with pytest.raises(LedgerError):
            led.answer_finding(f.finding_id, disposition="ignore")
        with pytest.raises(LedgerError):
            led.answer_finding("f-nobody-9", disposition="discard")


class TestViewsAreAFunctionOfTheEvents:
    def test_the_same_events_in_any_input_order_give_the_same_views(self, led):
        plant(led, "k.001")
        led.append(CLAIMED, key="k.001")
        led.append(LANDED, key="k.001", sha="abc")
        events = led.events()
        forward = build_views(events)
        backward = build_views(list(reversed(events)))
        assert forward == backward
        assert forward.state("k.001").state == "landed"


class TestFold:
    def test_should_fold_compares_the_two_halves(self):
        assert should_fold("x" * 100, "y" * 30, ratio=0.25)
        assert not should_fold("x" * 100, "y" * 20, ratio=0.25)
        assert not should_fold("", "y", ratio=0.25)

    def test_fold_writes_marks_for_landed_and_struck_keys_once(self, led):
        plant(led, "k.001", "k.002", "k.003")
        led.append(LANDED, key="k.001", sha="abc", evidence="done")
        led.append(STRUCK, key="k.002", reason="zero population")
        assert apply_fold(led) == 2
        marks = {k: n.marks for k, n in led.views().nodes.items()}
        assert marks["k.001"] == ["landed `abc`. done"]
        assert marks["k.002"] == ["STRUCK: zero population"]
        assert marks["k.003"] == []
        assert fold_marks(led.views()) == []

    def test_fold_writes_an_answered_finding_under_its_target_and_closes_it(self, led):
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="x", needs="human")
        led.answer_finding(f.finding_id, disposition="fold", text="the constraint", target_key="k.001")
        apply_fold(led)
        views = led.views()
        assert views.nodes["k.001"].marks == ["the constraint"]
        assert views.findings[f.finding_id].status == "folded"

    def test_a_discarded_finding_is_not_folded(self, led):
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="x")
        led.answer_finding(f.finding_id, disposition="discard")
        assert apply_fold(led) == 0
        assert led.views().nodes["k.001"].marks == []


class TestOneFileSeveralBays:
    """Two bays open one file. Each must see the other's writes without being
    told, and a fold must not be written twice."""

    def test_a_view_sees_another_connections_commit(self, tmp_path):
        path = tmp_path / "shared.db"
        a = open_ledger(path, origin="host", actor="bay-a")
        b = open_ledger(path, origin="host", actor="bay-b")
        plant(a, "p.001")
        assert a.views().state("p.001").state == "open"
        assert b.views().state("p.001").state == "open"
        b.append(CLAIMED, key="p.001", stage_id="s", run_id="run-b")
        assert a.views().state("p.001").state == "claimed", "a cached view missed b's claim"

    def test_two_folds_write_one_mark(self, tmp_path):
        path = tmp_path / "shared.db"
        a = open_ledger(path, origin="host", actor="bay-a")
        b = open_ledger(path, origin="host", actor="bay-b")
        plant(a, "p.001")
        a.append(LANDED, key="p.001", sha="abc1234", stage_id="s", run_id="r")
        assert apply_fold(a) == 1
        assert apply_fold(b) == 0
        assert sum(1 for e in b.events() if e.kind == NODE_MARKED) == 1

    def test_a_transaction_commits_as_one_and_rolls_back_as_one(self, tmp_path):
        path = tmp_path / "shared.db"
        a = open_ledger(path, origin="host", actor="bay-a")
        b = open_ledger(path, origin="host", actor="bay-b")
        plant(a, "p.001")
        with a.transaction():
            a.append(CLAIMED, key="p.001", stage_id="s", run_id="r")
        assert b.views().state("p.001").state == "claimed"
        with pytest.raises(RuntimeError):
            with a.transaction():
                a.append(RELEASED, key="p.001", stage_id="s", run_id="r")
                raise RuntimeError("abandon")
        assert b.views().state("p.001").state == "claimed"

    def test_a_reader_cannot_open_a_transaction(self, tmp_path):
        path = tmp_path / "shared.db"
        open_ledger(path, origin="host")
        with pytest.raises(LedgerError):
            with read_ledger(path).transaction():
                pass


class TestScope:
    def test_a_name_means_itself_and_everything_under_it(self, led):
        from code_gantry.ledger import resolve_scope

        plant(led, "p.001", kind="document")
        plant(led, "p.002", parent="p.001", kind="section")
        plant(led, "p.003", parent="p.002")
        plant(led, "p.004", parent="p.001")
        plant(led, "p.005", kind="document")
        assert resolve_scope(led.views(), ["p.002"]) == {"p.002", "p.003"}
        assert resolve_scope(led.views(), ["p.001"]) == {"p.001", "p.002", "p.003", "p.004"}
        assert resolve_scope(led.views(), ["p.003", "p.005"]) == {"p.003", "p.005"}

    def test_an_unknown_or_retired_name_is_refused_by_name(self, led):
        from code_gantry.ledger import resolve_scope

        plant(led, "p.001")
        led.append(NODE_RETIRED, key="p.001")
        with pytest.raises(LedgerError, match="'p.001' is not a key"):
            resolve_scope(led.views(), ["p.001"])
        with pytest.raises(LedgerError, match="'p.999' is not a key"):
            resolve_scope(led.views(), ["p.999"])


class TestDrawnStages:
    def _drawn(self, led, stage_id="s1", keys=("p.001",), findings=(), batch=None, rank=0, run_id="r1"):
        return led.append(
            STAGE_DERIVED, stage_id=stage_id, run_id=run_id, fields={"id": stage_id},
            keys=list(keys), findings=list(findings), batch=batch, rank=rank,
        ).derived_id

    def test_a_record_moves_from_derived_through_taken_to_done(self, led):
        plant(led, "p.001")
        did = self._drawn(led)
        assert [d.id for d in led.views().derived_waiting()] == [did]
        led.append(STAGE_TAKEN, run_id="r1", derived_id=did, pid=42)
        record = led.views().derived[did]
        assert record.status == "taken" and record.taken_run == "r1" and record.taken_pid == 42
        assert led.views().derived_waiting() == []
        led.append(STAGE_DONE, run_id="r1", derived_id=did)
        assert led.views().derived[did].status == "done"

    def test_released_returns_it_and_dropped_ends_it(self, led):
        plant(led, "p.001")
        did = self._drawn(led)
        led.append(STAGE_TAKEN, run_id="r1", derived_id=did, pid=42)
        led.append(STAGE_RELEASED, derived_id=did)
        assert led.views().derived[did].status == "derived"
        led.append(STAGE_DROPPED, derived_id=did, reason="stale")
        assert led.views().derived[did].status == "dropped"
        assert led.views().derived[did].reason == "stale"
        led.append(STAGE_TAKEN, run_id="r2", derived_id=did, pid=43)
        assert led.views().derived[did].status == "dropped", "a dropped record cannot be taken"

    def test_waiting_is_ordered_by_batch_and_rank(self, led):
        plant(led, "p.001", "p.002", "p.003")
        head = self._drawn(led, "head", ("p.001",))
        second = self._drawn(led, "second", ("p.002",), batch=head, rank=1)
        third = self._drawn(led, "third", ("p.003",), batch=head, rank=2)
        assert [d.stage_id for d in led.views().derived_waiting()] == ["head", "second", "third"]


class TestReferencesAvailable:
    def test_open_keys_and_open_findings_are_available(self, led):
        plant(led, "p.001")
        fid = led.open_finding(keys=["p.001"], by="reviewer", claim="x").finding_id
        assert led.views().references_available(["p.001"], [fid]) == []

    def test_a_claimed_key_is_not_unless_this_stage_holds_it(self, led):
        plant(led, "p.001")
        led.append(CLAIMED, key="p.001", run_id="r1", stage_id="s")
        views = led.views()
        assert views.references_available(["p.001"], []) == ["p.001 (claimed)"]
        assert views.references_available(["p.001"], [], run_id="r1", stage_id="s") == []

    def test_a_held_finding_is_not_unless_this_stage_holds_it(self, led):
        plant(led, "p.001")
        fid = led.open_finding(keys=["p.001"], by="reviewer", claim="x").finding_id
        led.append(FINDING_CLAIMED, run_id="r1", stage_id="s", finding_id=fid, pid=1)
        views = led.views()
        assert views.references_available([], [fid]) == [f"{fid} (held by r1)"]
        assert views.references_available([], [fid], run_id="r1", stage_id="s") == []
        led.append(FINDING_RELEASED, finding_id=fid)
        assert led.views().references_available([], [fid]) == []

    def test_a_landed_key_is_not_but_a_finding_on_it_may_be(self, led):
        plant(led, "p.001", "p.002")
        led.append(LANDED, key="p.001", sha="abc", run_id="r1", stage_id="s")
        fid = led.open_finding(keys=["p.001"], by="reviewer", claim="after the fact").finding_id
        views = led.views()
        assert views.references_available(["p.001"], []) == ["p.001 (landed)"]
        assert views.references_available([], [fid]) == []

    def test_the_scope_bounds_both(self, led):
        plant(led, "p.001", "p.002")
        fid = led.open_finding(keys=["p.002"], by="reviewer", claim="x").finding_id
        views = led.views()
        assert views.references_available(["p.001"], [fid], scope={"p.001"}) == [f"{fid} (outside this run's scope)"]
        assert views.references_available(["p.002"], [], scope={"p.001"}) == ["p.002 (outside this run's scope)"]


class TestClaimsAreLeases:
    def _hold_everything(self, led, run_id, pid):
        plant(led, f"k-{run_id}")
        led.append(CLAIMED, key=f"k-{run_id}", run_id=run_id, stage_id="s", pid=pid)
        fid = led.open_finding(keys=[f"k-{run_id}"], by="reviewer", claim="x").finding_id
        led.append(FINDING_CLAIMED, run_id=run_id, stage_id="s", finding_id=fid, pid=pid)
        did = led.append(STAGE_DERIVED, stage_id="s", run_id=run_id, fields={"id": "s"}, keys=[f"k-{run_id}"], findings=[], batch=None, rank=0).derived_id
        led.append(STAGE_TAKEN, run_id=run_id, derived_id=did, pid=pid)
        return fid, did

    def test_a_dead_holder_on_this_host_is_released(self, led):
        from code_gantry.ledger import release_dead_holders

        fid, did = self._hold_everything(led, "dead", 111)
        fid2, did2 = self._hold_everything(led, "alive", 222)
        assert release_dead_holders(led, alive=lambda pid: pid == 222) == 3
        views = led.views()
        assert views.state("k-dead").state == "open"
        assert views.findings[fid].claimed_run is None
        assert views.derived[did].status == "derived"
        assert views.state("k-alive").state == "claimed"
        assert views.findings[fid2].claimed_run == "alive"
        assert views.derived[did2].status == "taken"

    def test_another_hosts_holder_and_the_run_itself_are_left_alone(self, tmp_path):
        from code_gantry.ledger import release_dead_holders

        path = tmp_path / "shared.db"
        theirs = open_ledger(path, origin="host-b", actor="b")
        self._hold_everything(theirs, "remote", 333)
        mine = open_ledger(path, origin="host-a", actor="a")
        self._hold_everything(mine, "me", 444)
        assert release_dead_holders(mine, alive=lambda pid: False, keep_run="me") == 0
        views = mine.views()
        assert views.state("k-remote").state == "claimed"
        assert views.state("k-me").state == "claimed"
