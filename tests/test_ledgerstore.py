"""The store under the ledger: one contract, two stores.

`SqliteStore` is a file on one host; `DynamoStore` is the table every host
writes. Both assign one sequence per ledger, hand back everything after a
sequence in order, and hold one writer at a time across a read and the
writes it decides. The Dynamo store is driven here through an in-memory
table that answers the same five operations the boto3 adapter does; the
adapter itself is proven against the real table when credentials are in the
environment, and skipped otherwise.
"""

import os
import threading
import time
import uuid

import pytest

from code_gantry.ledgerstore import (
    DynamoStore, MemoryTable, SqliteStore, Draft, LockHeld, boto3_table,
)


def draft(kind="x", origin="host-a", at="2026-01-01T00:00:00+00:00", **body):
    return Draft(origin=origin, at=at, kind=kind, key=None, stage_id=None, run_id=None, sha=None, body=body)


@pytest.fixture(params=["sqlite", "dynamo"])
def store_factory(request, tmp_path):
    """A callable giving a fresh handle on the same store, as a second bay would open."""
    if request.param == "sqlite":
        path = tmp_path / "ledger.db"
        return lambda: SqliteStore.open(path)
    table = MemoryTable()
    return lambda: DynamoStore(table, "repo/project")


class TestTheContract:
    def test_sequence_is_one_per_ledger_across_origins(self, store_factory):
        s = store_factory()
        a = s.append([draft(origin="host-a")])[0]
        b = s.append([draft(origin="host-b")])[0]
        c, d = s.append([draft(origin="host-a"), draft(origin="host-a")])
        assert [e.seq for e in (a, b, c, d)] == [1, 2, 3, 4]
        assert [e.origin for e in (a, b)] == ["host-a", "host-b"]

    def test_events_after_returns_what_follows_in_order(self, store_factory):
        s = store_factory()
        s.append([draft(kind="one"), draft(kind="two"), draft(kind="three")])
        assert [e.kind for e in s.events_after(0)] == ["one", "two", "three"]
        assert [e.kind for e in s.events_after(2)] == ["three"]
        assert s.events_after(3) == []

    def test_a_second_handle_sees_the_first_handles_writes(self, store_factory):
        first, second = store_factory(), store_factory()
        first.append([draft(kind="from-first")])
        assert [e.kind for e in second.events_after(0)] == ["from-first"]
        second.append([draft(kind="from-second")])
        assert [e.kind for e in first.events_after(1)] == ["from-second"]

    def test_body_round_trips_with_unicode(self, store_factory):
        s = store_factory()
        s.append([draft(note="café — ünïcode", n=3, nested={"a": [1, 2]})])
        (e,) = s.events_after(0)
        assert e.body == {"note": "café — ünïcode", "n": 3, "nested": {"a": [1, 2]}}

    def test_exclusive_serialises_two_writers(self, store_factory):
        first, second = store_factory(), store_factory()
        order = []
        with first.exclusive():
            t = threading.Thread(target=lambda: (second.exclusive().__enter__(), order.append("second")))
            t.start()
            time.sleep(0.3)
            order.append("first")
        t.join(timeout=5)
        assert order == ["first", "second"]


class TestTheDynamoLock:
    def test_a_lock_left_by_a_dead_holder_expires(self):
        table = MemoryTable()
        clock = [1000.0]

        def tick():
            clock[0] += 0.2
            return clock[0]

        dead = DynamoStore(table, "repo/p", clock=tick, lock_ttl=30)
        holding = dead.exclusive()
        holding.__enter__()  # never exited: the process died, and the reference is kept so nothing tidies up
        live = DynamoStore(table, "repo/p", clock=tick, lock_ttl=30, lock_wait=1.0)
        with pytest.raises(LockHeld):
            with live.exclusive():
                pass
        clock[0] += 31
        with live.exclusive():
            pass
        assert holding is not None

    def test_the_lock_names_its_holder(self):
        table = MemoryTable()
        s = DynamoStore(table, "repo/p", holder="bay1:123")
        with s.exclusive():
            assert table.lock_holder("repo/p") == "bay1:123"
        assert table.lock_holder("repo/p") is None

    def test_ledgers_do_not_share_a_sequence_or_a_lock(self):
        table = MemoryTable()
        a, b = DynamoStore(table, "repo/one"), DynamoStore(table, "repo/two")
        a.append([draft(), draft()])
        assert b.append([draft()])[0].seq == 1
        with a.exclusive():
            with b.exclusive():
                pass


@pytest.mark.skipif(
    not os.environ.get("CODE_GANTRY_LEDGER_TABLE") or not os.environ.get("AWS_ACCESS_KEY_ID"),
    reason="no ledger table in the environment",
)
class TestTheRealTable:
    """The boto3 adapter against the deployed table, under a throwaway
    ledger name whose rows the table expires an hour later — the ledger's
    credential cannot delete, by design. Skipped without credentials, so
    the suite passes with none."""

    def test_append_read_and_lock(self):
        table = boto3_table(os.environ["CODE_GANTRY_LEDGER_TABLE"])
        name = f"_test/{uuid.uuid4()}"
        s = DynamoStore(table, name, holder="test", expire_after=3600)
        with s.exclusive():
            first = s.append([draft(kind="one", note="ünïcode")])[0]
            assert table.lock_holder(name) == "test"
        assert table.lock_holder(name) is None
        second = s.append([draft(kind="two")])[0]
        assert (first.seq, second.seq) == (1, 2)
        assert [e.kind for e in s.events_after(0)] == ["one", "two"]
        assert s.events_after(0)[0].body == {"note": "ünïcode"}
