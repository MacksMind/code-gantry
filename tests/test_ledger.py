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
    RUN_BEGAN,
    RUN_ENDED,
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
    release_dead_holders,
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
        led.answer_finding(f.finding_id, disposition="amend", text="write this", target_key="k.001")
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


class TestSupersession:
    def test_a_keyed_reading_supersedes_an_older_keyless_one_of_the_same_subject(self, led):
        plant(led, "k.001")
        keyless = led.open_finding(keys=[], by="planner", claim="needs a browser", needs="human", subject="browser reproduction")
        keyed = led.open_finding(keys=["k.001"], by="planner", claim="needs a browser", needs="human", subject="browser reproduction")
        views = led.views()
        assert views.findings[keyless.finding_id].status == "superseded"
        assert views.findings[keyless.finding_id].superseded_by == keyed.finding_id
        assert views.findings[keyed.finding_id].status == "open"

    def test_a_keyless_reading_supersedes_nothing(self, led):
        plant(led, "k.001")
        keyed = led.open_finding(keys=["k.001"], by="planner", claim="x", needs="human", subject="browser reproduction")
        first_keyless = led.open_finding(keys=[], by="planner", claim="x", needs="human", subject="browser reproduction")
        led.open_finding(keys=[], by="planner", claim="x", needs="human", subject="browser reproduction")
        views = led.views()
        assert views.findings[keyed.finding_id].status == "open"
        assert views.findings[first_keyless.finding_id].status == "open"

    def test_an_answer_written_as_fold_reads_as_amend(self, led):
        # The word before `amend`; history carrying it means the same.
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="x")
        led.append("finding.answered", finding_id=f.finding_id, disposition="fold", text="the sentence", target_key="k.001")
        assert apply_fold(led) >= 1
        assert "the sentence" in led.views().nodes["k.001"].marks


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
        led.answer_finding(f.finding_id, disposition="amend", text="the constraint", target_key="k.001")
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


class TestDispositions:
    """Every disposition means something in the views, so whoever writes the
    answer — the CLI, a daemon, a reply from a phone — gets the same result.
    Findings are answered one at a time in any order; nothing here is a
    cursor."""

    def test_discard_closes_the_finding_and_it_leaves_the_projection(self, led):
        from code_gantry.render import render_projection

        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="not worth it")
        led.answer_finding(f.finding_id, disposition="discard", text="duplicate of k.002")
        finding = led.views().findings[f.finding_id]
        assert finding.status == "discarded"
        assert f.finding_id not in render_projection(led.views(), note_chars=600)
        assert apply_fold(led) == 0

    def test_raise_hands_the_finding_to_a_person_and_keeps_it_open(self, led):
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="reviewer", claim="which?")
        led.answer_finding(f.finding_id, disposition="raise", text="two readings; a person decides")
        finding = led.views().findings[f.finding_id]
        assert (finding.status, finding.needs) == ("open", "human")
        assert finding.answer_text == "two readings; a person decides"
        assert finding in led.views().open_findings()
        # Answered again, by the person this time, it closes like any other.
        led.answer_finding(f.finding_id, disposition="discard")
        assert led.views().findings[f.finding_id].status == "discarded"

    def test_debt_writes_an_entry_under_the_target_and_closes_the_finding(self, led):
        plant(led, "k.001")
        led.upsert_node("k.900", parent=None, position=9, kind="section", title="Technical debt")
        f = led.open_finding(keys=["k.001"], by="planner", claim="the old helper lingers")
        led.answer_finding(f.finding_id, disposition="debt", text="Remove the old helper once nothing calls it", target_key="k.900")
        views = led.views()
        finding = views.findings[f.finding_id]
        assert finding.status == "debt"
        entry = views.nodes[finding.entry_key]
        assert (entry.parent, entry.kind, entry.title, entry.owner) == ("k.900", "item", "Remove the old helper once nothing calls it", "human")
        assert entry.key == "k.901"
        assert views.state(entry.key).state == "open", "a debt entry is drawable like any item"
        assert f.finding_id not in [x.id for x in views.open_findings()]
        assert apply_fold(led) == 0

    def test_debt_needs_a_target_section_and_an_entry(self, led):
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="x")
        with pytest.raises(LedgerError, match="target"):
            led.answer_finding(f.finding_id, disposition="debt", text="an entry")
        with pytest.raises(LedgerError, match="text"):
            led.answer_finding(f.finding_id, disposition="debt", target_key="k.001")
        with pytest.raises(LedgerError, match="section"):
            led.answer_finding(f.finding_id, disposition="debt", text="an entry", target_key="k.001")

    def test_two_debt_answers_take_two_keys(self, led):
        plant(led, "k.001", "k.002")
        led.upsert_node("k.900", parent=None, position=9, kind="section", title="Technical debt")
        a = led.open_finding(keys=["k.001"], by="planner", claim="a")
        b = led.open_finding(keys=["k.002"], by="planner", claim="b")
        led.answer_finding(a.finding_id, disposition="debt", text="first", target_key="k.900")
        led.answer_finding(b.finding_id, disposition="debt", text="second", target_key="k.900")
        entries = [n for n in led.views().children("k.900")]
        assert [(n.key, n.title, n.position) for n in entries] == [("k.901", "first", 0), ("k.902", "second", 1)]


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


class TestAClaimOutlivesARunThatMeansToComeBack:
    """A claim is a lease from a live run, and the sweep that gives one back
    reads process liveness. A run that pauses or escalates has no process and
    every intention of returning — with work on a stage branch, in the
    escalated case — so liveness alone hands its stage to another bay."""

    def _held(self, tmp_path, disposition=None):
        led = open_ledger(tmp_path / "l.sqlite", origin="host-a", actor="run:r1")
        led.append(RUN_BEGAN, run_id="r1", pid=999999, bay="host-a/target")
        led.append(STAGE_DERIVED, stage_id="s1", run_id="r1", fields={"id": "s1"}, keys=["p.001"], findings=[], rank=0)
        record = next(iter(led.views().derived.values()))
        led.append(STAGE_TAKEN, stage_id="s1", run_id="r1", derived_id=record.id, pid=999999, bay="host-a/target")
        led.append(CLAIMED, key="p.001", stage_id="s1", run_id="r1", pid=999999, bay="host-a/target")
        if disposition:
            led.append(RUN_ENDED, run_id="r1", pid=999999, bay="host-a/target", disposition=disposition)
        return led

    def _sweep(self, led):
        # Another bay's run starting on the same host, while r1's process is
        # gone. Never r1 itself: a run does not release its own.
        return release_dead_holders(led, alive=lambda pid: False, keep_run="r2")

    def test_a_paused_run_keeps_its_stage(self, tmp_path):
        led = self._held(tmp_path, "paused")
        assert self._sweep(led) == 0, "another bay starting took a paused run's stage"
        assert led.views().state("p.001").state == "claimed"
        assert next(iter(led.views().derived.values())).status == "taken"

    def test_an_escalated_run_keeps_its_stage(self, tmp_path):
        # The sharper case: an escalation happens mid-stage, so a branch
        # holds work. Handing the stage on means the next bay cuts a fresh
        # branch over it.
        led = self._held(tmp_path, "escalated")
        assert self._sweep(led) == 0
        assert led.views().state("p.001").state == "claimed"

    def test_a_resumed_run_is_judged_by_the_pid_it_has_now(self, tmp_path):
        # The claims a run took before a pause carry the pid it had then.
        # Resumed, it is alive under a new pid, and a neighbour's sweep on
        # the same host read the old one as dead and took its stage.
        led = self._held(tmp_path, "paused")
        led.append(RUN_BEGAN, run_id="r1", pid=424242, bay="host-a/target")
        assert release_dead_holders(led, alive=lambda pid: pid == 424242, keep_run="r2") == 0
        assert led.views().state("p.001").state == "claimed"
        # And dead under its new pid, it is dead.
        assert release_dead_holders(led, alive=lambda pid: False, keep_run="r2") == 2

    def test_a_run_that_simply_died_still_gives_its_stage_back(self, tmp_path):
        # No end recorded at all: killed, crashed, or the machine went. This
        # is what the sweep is for and it must keep working.
        led = self._held(tmp_path)
        assert self._sweep(led) == 2
        assert led.views().state("p.001").state == "open"
        assert next(iter(led.views().derived.values())).status == "derived"

    def test_a_finished_run_gives_its_stage_back(self, tmp_path):
        led = self._held(tmp_path, "finished")
        assert self._sweep(led) == 2

    def test_a_resumed_run_is_swept_again_once_it_dies_for_real(self, tmp_path):
        # The intent is withdrawn by coming back. Without this, one pause
        # spares a run's claims for the life of the ledger.
        led = self._held(tmp_path, "paused")
        assert self._sweep(led) == 0
        led.append(RUN_BEGAN, run_id="r1", pid=999998, bay="host-a/target")
        assert self._sweep(led) == 2, "a run that came back and then died is still dead"


class TestAClaimHeldByAnotherHost:
    """pid liveness cannot be read across machines, so a run that died on one
    host held its stage against every other until a run started there again.
    The mesh answers instead — but only about hosts that answered."""

    def _held(self, tmp_path, origin="host-b"):
        led = open_ledger(tmp_path / "l.sqlite", origin="host-a", actor="run:r9")
        other = open_ledger(tmp_path / "l.sqlite", origin=origin, actor="run:r1")
        other.append(RUN_BEGAN, run_id="r1", pid=4242, bay=f"{origin}/target")
        other.append(STAGE_DERIVED, stage_id="s1", run_id="r1", fields={"id": "s1"}, keys=["p.001"], findings=[], rank=0)
        record = next(iter(other.views().derived.values()))
        other.append(STAGE_TAKEN, stage_id="s1", run_id="r1", derived_id=record.id, pid=4242, bay=f"{origin}/target")
        other.append(CLAIMED, key="p.001", stage_id="s1", run_id="r1", pid=4242, bay=f"{origin}/target")
        other.close()
        return led

    def _sweep(self, led, answered=None, live=None):
        return release_dead_holders(
            led, alive=lambda pid: False, keep_run="r9",
            answered=answered, live=live,
        )

    def test_a_dead_run_on_a_host_that_answered_gives_its_stage_back(self, tmp_path):
        led = self._held(tmp_path)
        assert self._sweep(led, answered={"host-b"}, live=set()) == 2
        assert led.views().state("p.001").state == "open"

    def test_a_live_run_on_another_host_keeps_it(self, tmp_path):
        led = self._held(tmp_path)
        assert self._sweep(led, answered={"host-b"}, live={("host-b", "r1")}) == 0
        assert led.views().state("p.001").state == "claimed"

    def test_a_host_that_did_not_answer_keeps_everything(self, tmp_path):
        # Unreachable is not dead. That host can reach the table it wrote
        # this claim into, and may be working happily behind a link that
        # is down only from here.
        led = self._held(tmp_path)
        assert self._sweep(led, answered=set(), live=set()) == 0
        assert led.views().state("p.001").state == "claimed"

    def test_with_no_mesh_at_all_another_host_is_left_alone(self, tmp_path):
        # Today's behaviour, and what a run with no daemon still does.
        led = self._held(tmp_path)
        assert self._sweep(led) == 0

    def test_a_paused_run_on_another_host_keeps_its_stage(self, tmp_path):
        # It has no process to be alive, and every intention of returning.
        led = self._held(tmp_path)
        other = open_ledger(tmp_path / "l.sqlite", origin="host-b", actor="run:r1")
        other.append(RUN_ENDED, run_id="r1", pid=4242, bay="host-b/target", disposition="paused")
        other.close()
        assert self._sweep(led, answered={"host-b"}, live=set()) == 0


class TestTheOrderEventsAreApplied:
    """One sequence per ledger, assigned at append by the store, is the order
    the writes actually happened in. Ordering by wall clock ahead of it lets
    two hosts writing the same key in one second be decided by which hostname
    sorts first."""

    def test_a_release_after_a_claim_is_applied_after_it(self, tmp_path):
        # Both in the same second, by two hosts, and 'host-a' sorts
        # before 'host-b'. Ordered by the clock the release lands
        # first and the claim re-applies over it, so the key stays held by
        # a run that is gone.
        one = open_ledger(tmp_path / "l.sqlite", origin="host-b", actor="run:r1")
        one.append(CLAIMED, key="p.001", stage_id="s1", run_id="r1", pid=42)
        two = open_ledger(tmp_path / "l.sqlite", origin="host-a", actor="run:r2")
        two.append(RELEASED, key="p.001", run_id="r1", stage_id="s1", reason="holder exited")
        assert [e.at for e in two.events()].count(two.events()[0].at) == 2, (
            "the two writes must share a timestamp, or this proves nothing"
        )
        assert two.views().state("p.001").state == "open"

    def test_the_later_claim_of_two_racing_hosts_wins(self, tmp_path):
        # Whoever wrote second holds it, whatever the two machines are
        # called. Under the clock order the answer was alphabetical.
        one = open_ledger(tmp_path / "l.sqlite", origin="host-b", actor="run:r1")
        one.append(CLAIMED, key="p.001", stage_id="s1", run_id="r1", pid=42)
        two = open_ledger(tmp_path / "l.sqlite", origin="host-a", actor="run:r2")
        two.append(CLAIMED, key="p.001", stage_id="s2", run_id="r2", pid=43)
        assert two.views().state("p.001").run_id == "r2"


class TestWaitingOnAPerson:
    """One queue for everything a person has to act on: findings that need
    a human and open human-owned items, each with the card an investigation
    attached to it and the thread of questions and cards since."""

    def test_waiting_is_human_findings_and_open_human_items(self, led):
        plant(led, "k.001")
        led.upsert_node("k.002", parent=None, position=1, kind="item", title="Check production data", owner="human")
        led.upsert_node("k.003", parent=None, position=2, kind="item", title="Already done", owner="human")
        led.append(LANDED, key="k.003", sha="abc")
        for_person = led.open_finding(keys=["k.001"], by="planner", claim="needs a person", needs="human")
        led.open_finding(keys=["k.001"], by="planner", claim="the pipeline's", needs="pipeline")
        waiting = led.views().waiting()
        assert [(w.kind, w.id) for w in waiting] == [("item", "k.002"), ("finding", for_person.finding_id)]
        item, finding = waiting
        assert item.title == "Check production data" and item.recommendation is None and item.thread == []
        assert finding.text == "needs a person" and finding.keys == ["k.001"]

    def test_the_pipelines_own_finding_that_the_work_is_already_done_waits_too(self, led):
        # The planner says an item is done in the tree; nothing in the
        # pipeline can close it, since no stage will be drawn for it, so it
        # waits on a person like the rest. An ordinary pipeline finding does not.
        plant(led, "k.001", "k.002")
        done = led.open_finding(keys=["k.001"], by="planner", claim="nothing left", needs="pipeline", total="none remain")
        led.open_finding(keys=["k.002"], by="planner", claim="7 of 24 remain", needs="pipeline", total="7 sites")
        assert [w.id for w in led.views().waiting()] == [done.finding_id]
        assert led.views().waiting()[0].total == "none remain"

    def test_a_card_and_a_question_travel_with_the_thing(self, led):
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="two readings", needs="human")
        card = {
            "says": "two readings", "anchors": ["app/models/x.rb:12"], "checked": "both callers are admin-only",
            "recommend": {"disposition": "discard", "text": "duplicate of k.002"}, "would_write": None,
        }
        led.recommend(f.finding_id, card=card, actor="claude -p")
        led.ask(f.finding_id, text="which caller is the admin one?", actor="mack")
        (w,) = led.views().waiting()
        assert w.recommendation["recommend"]["disposition"] == "discard"
        assert [(e["kind"], e["by"]) for e in w.thread] == [("recommended", "claude -p"), ("asked", "mack")]
        assert w.thread[1]["text"] == "which caller is the admin one?"
        # A later card is the recommendation; the thread keeps every one.
        led.recommend(f.finding_id, card={**card, "recommend": {"disposition": "raise", "text": "a person decides"}})
        (w,) = led.views().waiting()
        assert w.recommendation["recommend"]["disposition"] == "raise" and len(w.thread) == 3

    def test_a_card_needs_a_thing_that_exists_and_a_disposition_that_means_something(self, led):
        plant(led, "k.001")
        with pytest.raises(LedgerError):
            led.recommend("f-nobody-9", card={"recommend": {"disposition": "discard"}})
        with pytest.raises(LedgerError):
            led.recommend("k.001", card={"recommend": {"disposition": "burn"}})
        with pytest.raises(LedgerError):
            led.recommend("k.001", card={"says": "no recommendation at all"})
        led.recommend("k.001", card={"recommend": {"disposition": "landed", "sha": "abc"}})
        assert led.views().recommendations["k.001"]["recommend"]["sha"] == "abc"

    def test_an_answer_or_a_landing_takes_it_off_the_queue(self, led):
        led.upsert_node("k.002", parent=None, position=1, kind="item", title="Decide", owner="human")
        f = led.open_finding(keys=[], by="planner", claim="needs a person", needs="human")
        assert len(led.views().waiting()) == 2
        led.answer_finding(f.finding_id, disposition="discard")
        led.append(STRUCK, key="k.002", reason="not doing it")
        assert led.views().waiting() == []


class TestMove:
    """General debt is a project like any other, so moving a thing there is
    a move between ledgers: opened there with a pointer back, closed here
    naming where it went. The destination is written first, so a crash
    between the two leaves a duplicate somebody can see rather than a loss."""

    @pytest.fixture
    def other(self, tmp_path):
        other = open_ledger(tmp_path / "other.db", origin="host-a", actor="test", clock=ticking())
        other.upsert_node("q.001", parent=None, position=0, kind="document", title="General debt")
        other.upsert_node("q.002", parent="q.001", position=0, kind="section", title="Inherited")
        return other

    def test_a_finding_moved_opens_there_and_closes_here(self, led, other):
        plant(led, "k.001")
        f = led.open_finding(keys=["k.001"], by="planner", claim="belongs to general debt", needs="human", subject="s", total="landed")
        opened = led.move(f.finding_id, to=other, to_label="repo/debt", from_label="repo/rails-5")
        here = led.views().findings[f.finding_id]
        assert (here.status, here.moved_to) == ("moved", "repo/debt")
        there = other.views().findings[opened]
        assert (there.status, there.needs, there.keys, there.subject, there.total) == ("open", "human", [], "s", "landed")
        assert there.by == "moved from repo/rails-5"
        assert "belongs to general debt" in there.claim and "k.001" in there.claim and "repo/rails-5" in there.claim
        assert f.finding_id not in [w.id for w in led.views().waiting()]
        assert opened in [w.id for w in other.views().waiting()]

    def test_a_moved_finding_refuses_an_answer_and_says_where_it_went(self, led, other):
        f = led.open_finding(keys=[], by="planner", claim="x", needs="human")
        led.move(f.finding_id, to=other, to_label="repo/debt", from_label="repo/rails-5")
        with pytest.raises(LedgerError, match="is moved, not open; it went to repo/debt"):
            led.answer_finding(f.finding_id, disposition="raise", text="y")

    def test_an_item_moved_becomes_an_item_there_and_is_struck_here(self, led, other):
        led.upsert_node("k.002", parent=None, position=1, kind="item", title="Delete the columns", body="They are unread.", owner="human")
        opened = led.move("k.002", to=other, to_label="repo/debt", from_label="repo/rails-5", under="q.002")
        node = other.views().nodes[opened]
        assert (node.parent, node.kind, node.title, node.owner) == ("q.002", "item", "Delete the columns", "human")
        assert "They are unread." in node.body and "repo/rails-5" in node.body and "k.002" in node.body
        state = led.views().state("k.002")
        assert state.state == "struck" and state.reason == "moved to repo/debt as q.003"
        assert "k.002" not in [w.id for w in led.views().waiting()]

    def test_an_item_takes_its_open_findings_with_it_re_keyed(self, led, other):
        # A finding is about its item; left behind it points at a key that
        # is struck as moved, and sits on the wrong project's queue.
        led.upsert_node("k.002", parent=None, position=1, kind="item", title="Delete the columns", owner="human")
        f = led.open_finding(keys=["k.002"], by="planner", claim="needs a decision", needs="human", subject="decision")
        closed = led.open_finding(keys=["k.002"], by="planner", claim="old", needs="pipeline")
        led.answer_finding(closed.finding_id, disposition="discard")
        opened = led.move("k.002", to=other, to_label="repo/debt", from_label="repo/rails-5", under="q.002")
        here = led.views().findings[f.finding_id]
        assert (here.status, here.moved_to) == ("moved", "repo/debt")
        there = [x for x in other.views().findings.values() if x.status == "open"]
        assert [x.keys for x in there] == [[opened]] and there[0].subject == "decision" and there[0].needs == "human"
        assert [w.id for w in led.views().waiting()] == []

    def test_a_finding_moved_after_its_item_lands_on_the_new_key(self, led, other):
        led.upsert_node("k.002", parent=None, position=1, kind="item", title="Delete the columns", owner="pipeline")
        opened = led.move("k.002", to=other, to_label="repo/debt", from_label="repo/rails-5", under="q.002")
        f = led.open_finding(keys=["k.002"], by="planner", claim="late reading", needs="human", subject="late")
        moved = led.move(f.finding_id, to=other, to_label="repo/debt", from_label="repo/rails-5")
        assert other.views().findings[moved].keys == [opened]

    def test_a_moved_item_keeps_its_owner(self, led, other):
        # A pipeline item moved to the project whose branch the fix lands on
        # is still the fleet's; a person's item is still the person's.
        led.upsert_node("k.005", parent=None, position=4, kind="item", title="Fix the trigger", owner="pipeline")
        opened = led.move("k.005", to=other, to_label="repo/debt", from_label="repo/rails-5", under="q.002")
        assert other.views().nodes[opened].owner == "pipeline"

    def test_an_item_needs_a_section_to_go_under(self, led, other):
        led.upsert_node("k.002", parent=None, position=1, kind="item", title="Delete the columns", owner="human")
        with pytest.raises(LedgerError):
            led.move("k.002", to=other, to_label="repo/debt", from_label="repo/rails-5")
        with pytest.raises(LedgerError):
            led.move("k.002", to=other, to_label="repo/debt", from_label="repo/rails-5", under="q.999")
        assert led.views().state("k.002").state == "open"


class TestOrigin:
    """Every event names the origin that wrote it, and the daemon's runs
    name the host file's `origin:`. A session's CLI must name the same one,
    or its findings get ids under a second name for the same host and the
    claim-release logic compares two names for one machine."""

    def test_the_variable_wins(self, monkeypatch):
        from code_gantry.ledger import default_origin
        monkeypatch.setenv("CODE_GANTRY_ORIGIN", "named")
        assert default_origin() == "named"

    def test_then_the_host_file(self, tmp_path, monkeypatch):
        from code_gantry.ledger import default_origin
        monkeypatch.delenv("CODE_GANTRY_ORIGIN", raising=False)
        host = tmp_path / "host.exs"
        host.write_text('[\n  origin: "host-b",\n  code_gantry: "/x",\n]\n')
        monkeypatch.setenv("CODE_GANTRY_HOST_FILE", str(host))
        assert default_origin() == "host-b"

    def test_then_the_short_hostname(self, tmp_path, monkeypatch):
        import socket
        from code_gantry.ledger import default_origin
        monkeypatch.delenv("CODE_GANTRY_ORIGIN", raising=False)
        monkeypatch.setenv("CODE_GANTRY_HOST_FILE", str(tmp_path / "absent.exs"))
        assert default_origin() == socket.gethostname().split(".")[0]


class TestDismissingACandidate:
    def test_a_dismissed_rejection_waits_for_nobody(self, led):
        from code_gantry.ledger import CANDIDATE_DISMISSED, CANDIDATE_PUSHED, CANDIDATE_REJECTED
        led.append(CANDIDATE_PUSHED, sha="a" * 40, stage_id="s", branch="stage/001-x", base="b" * 40, landing={}, fields={})
        led.append(CANDIDATE_REJECTED, sha="c" * 40, branch="stage/001-x", reason="would not replay")
        assert [r.candidate.branch for r in led.views().rework_waiting()] == ["stage/001-x"]
        led.append(CANDIDATE_DISMISSED, branch="stage/001-x", reason="the work is already on the branch")
        assert led.views().rework_waiting() == [] and led.views().pending_candidates() == []

    def test_a_dismissed_pending_candidate_is_not_composed(self, led):
        from code_gantry.ledger import CANDIDATE_DISMISSED, CANDIDATE_PUSHED
        led.append(CANDIDATE_PUSHED, sha="a" * 40, stage_id="s", branch="stage/002-y", base="b" * 40, landing={}, fields={})
        led.append(CANDIDATE_DISMISSED, branch="stage/002-y", reason="duplicate")
        assert led.views().pending_candidates() == []
