"""The store under the ledger: where events live and how they are appended.

Two stores answer one contract. `SqliteStore` is a file on one host, the
shape the ledger has always had. `DynamoStore` is one table every host
writes, which is what lets three or four machines that come and go — a
laptop, a desk, an RV on cellular — work one plan with nothing to
reconcile: a host that can reach the models can reach the table, and a
host that cannot is idle.

The contract is small. Every ledger has one sequence, assigned at append,
so a replay is "everything after N" in order. `exclusive()` holds one
writer at a time across a read and the writes it decides — SQLite's write
lock on the file, a lock item in the table — which is what keeps a claim
or a fold from being written twice. Ids that used to be unique per origin
stay unique, since the sequence is now unique on its own.

The Dynamo store speaks to its table through five operations; the boto3
adapter and the in-memory table both answer them, so the store is tested
without the network and the adapter is tested against the real table.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol


@dataclass
class Event:
    """One row, exactly as written."""

    origin: str
    seq: int
    at: str
    kind: str
    key: str | None = None
    stage_id: str | None = None
    run_id: str | None = None
    sha: str | None = None
    body: dict = field(default_factory=dict)

    @property
    def finding_id(self) -> str:
        """The id a `finding.opened` event confers."""
        return f"f-{self.origin}-{self.seq}"

    @property
    def derived_id(self) -> str:
        """The id a `stage.derived` event confers, by the same rule."""
        return f"d-{self.origin}-{self.seq}"


@dataclass
class Draft:
    """An event before the store has given it a sequence."""

    origin: str
    at: str
    kind: str
    key: str | None
    stage_id: str | None
    run_id: str | None
    sha: str | None
    body: dict


class LockHeld(RuntimeError):
    """Another writer held the ledger for longer than this one would wait."""


class StoreError(RuntimeError):
    """A store that cannot be used as asked: an old file, a missing table."""


class Store(Protocol):
    writable: bool

    def events_after(self, seq: int) -> list[Event]: ...
    def append(self, drafts: list[Draft]) -> list[Event]: ...
    def exclusive(self): ...
    def close(self) -> None: ...


def _columns(d: Draft | Event) -> dict:
    return {
        "origin": d.origin, "at": d.at, "kind": d.kind, "key": d.key,
        "stage_id": d.stage_id, "run_id": d.run_id, "sha": d.sha,
        "body": json.dumps(d.body, ensure_ascii=False, sort_keys=True),
    }


def _event(seq: int, row: dict) -> Event:
    body = row["body"]
    return Event(
        origin=row["origin"], seq=int(seq), at=row["at"], kind=row["kind"],
        key=row.get("key"), stage_id=row.get("stage_id"), run_id=row.get("run_id"),
        sha=row.get("sha"), body=json.loads(body) if isinstance(body, str) else dict(body or {}),
    )


# --------------------------------------------------------------------------
# SQLite: a file on one host
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq      INTEGER PRIMARY KEY,
    origin   TEXT    NOT NULL,
    at       TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    key      TEXT,
    stage_id TEXT,
    run_id   TEXT,
    sha      TEXT,
    body     TEXT    NOT NULL
)
"""


class SqliteStore:
    def __init__(self, conn: sqlite3.Connection | None, *, writable: bool):
        self._conn = conn
        self.writable = writable

    @classmethod
    def open(cls, path: Path | str) -> "SqliteStore":
        """The writer. Creates the file, and is the only function that may."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        # Switching the journal mode and creating the table take an exclusive
        # lock the busy handler does not always wait for, so several writers
        # opening a fresh file at once can each see it locked for an instant.
        for attempt in range(50):
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(_SCHEMA)
                conn.commit()
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() or attempt == 49:
                    raise
                time.sleep(0.05 * (attempt + 1))
        _refuse_old_schema(conn, path)
        return cls(conn, writable=True)

    @classmethod
    def read(cls, path: Path | str) -> "SqliteStore":
        """A reader. An absent file is an empty ledger, and stays absent."""
        path = Path(path)
        if not path.is_file():
            return cls(None, writable=False)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")
        tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "events" not in tables:
            conn.close()
            return cls(None, writable=False)
        _refuse_old_schema(conn, path)
        return cls(conn, writable=False)

    def events_after(self, seq: int) -> list[Event]:
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT seq, origin, at, kind, key, stage_id, run_id, sha, body"
            "  FROM events WHERE seq > ? ORDER BY seq",
            (seq,),
        ).fetchall()
        names = ("origin", "at", "kind", "key", "stage_id", "run_id", "sha", "body")
        return [_event(r[0], dict(zip(names, r[1:]))) for r in rows]

    def append(self, drafts: list[Draft]) -> list[Event]:
        if self._conn is None or not self.writable:
            raise StoreError("this ledger was opened for reading only")
        # The next sequence and the insert are one transaction, taken with the
        # write lock up front, so two writers can never both read the same
        # number. Inside `exclusive()` the lock is already held.
        own = not self._conn.in_transaction
        if own:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            out = []
            for d in drafts:
                (last,) = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
                cols = _columns(d)
                self._conn.execute(
                    "INSERT INTO events (seq, origin, at, kind, key, stage_id, run_id, sha, body)"
                    " VALUES (:seq, :origin, :at, :kind, :key, :stage_id, :run_id, :sha, :body)",
                    {"seq": int(last) + 1, **cols},
                )
                out.append(_event(int(last) + 1, cols))
            if own:
                self._conn.commit()
            return out
        except BaseException:
            if own:
                self._conn.rollback()
            raise

    @contextlib.contextmanager
    def exclusive(self):
        if self._conn is None or not self.writable:
            raise StoreError("this ledger was opened for reading only")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _refuse_old_schema(conn: sqlite3.Connection, path: Path) -> None:
    """A file written before the sequence became one per ledger has a
    two-column primary key. It is read by `ledger import`, never opened."""
    pk = [row[1] for row in conn.execute("PRAGMA table_info(events)") if row[5]]
    if pk != ["seq"]:
        conn.close()
        raise StoreError(
            f"{path} is a ledger from before one sequence per ledger (key {pk}); "
            "import it into the configured ledger with `code-gantry ledger import`"
        )


# --------------------------------------------------------------------------
# DynamoDB: one table every host writes
# --------------------------------------------------------------------------

COUNTER_SEQ = 0   # the item holding the next sequence
LOCK_SEQ = -1     # the item holding the writer's lock


class Table(Protocol):
    """The five operations a ledger needs from its table."""

    def next_seq(self, pk: str, count: int) -> int: ...
    def put_event(self, pk: str, seq: int, item: dict) -> None: ...
    def query_after(self, pk: str, seq: int) -> list[tuple[int, dict]]: ...
    def try_lock(self, pk: str, holder: str, now: float, expires: float) -> bool: ...
    def unlock(self, pk: str, holder: str) -> None: ...


class MemoryTable:
    """The table in a dict, for the suite. Same operations, same answers."""

    def __init__(self):
        self._ledgers: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _ledger(self, pk: str) -> dict:
        return self._ledgers.setdefault(pk, {"events": {}, "next": 0, "lock": None})

    def next_seq(self, pk, count):
        with self._lock:
            led = self._ledger(pk)
            first = led["next"] + 1
            led["next"] += count
            return first

    def put_event(self, pk, seq, item):
        with self._lock:
            events = self._ledger(pk)["events"]
            if seq in events:
                raise StoreError(f"sequence {seq} already written in {pk}")
            events[seq] = dict(item)

    def query_after(self, pk, seq):
        with self._lock:
            events = self._ledger(pk)["events"]
            return [(s, dict(events[s])) for s in sorted(events) if s > seq]

    def try_lock(self, pk, holder, now, expires):
        with self._lock:
            led = self._ledger(pk)
            held = led["lock"]
            if held is not None and held[1] >= now:
                return False
            led["lock"] = (holder, expires)
            return True

    def unlock(self, pk, holder):
        with self._lock:
            led = self._ledger(pk)
            if led["lock"] is not None and led["lock"][0] == holder:
                led["lock"] = None

    def lock_holder(self, pk):
        with self._lock:
            held = self._ledger(pk)["lock"]
            return held[0] if held else None


class Boto3Table:
    """The same five operations against a DynamoDB table: `pk` names the
    ledger, `seq` orders it, the counter sits at seq 0 and the lock at -1."""

    def __init__(self, resource_table):
        self._t = resource_table

    @staticmethod
    def _failed(exc) -> bool:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        return code == "ConditionalCheckFailedException"

    def next_seq(self, pk, count):
        out = self._t.update_item(
            Key={"pk": pk, "seq": COUNTER_SEQ},
            UpdateExpression="ADD #n :c",
            ExpressionAttributeNames={"#n": "next"},
            ExpressionAttributeValues={":c": count},
            ReturnValues="UPDATED_NEW",
        )
        return int(out["Attributes"]["next"]) - count + 1

    def put_event(self, pk, seq, item):
        clean = {k: v for k, v in item.items() if v is not None}
        self._t.put_item(
            Item={"pk": pk, "seq": seq, **clean},
            ConditionExpression="attribute_not_exists(pk)",
        )

    def query_after(self, pk, seq):
        from boto3.dynamodb.conditions import Key

        out: list[tuple[int, dict]] = []
        kwargs = {"KeyConditionExpression": Key("pk").eq(pk) & Key("seq").gt(seq)}
        while True:
            page = self._t.query(**kwargs)
            for item in page.get("Items", []):
                out.append((int(item["seq"]), {k: _plain(v) for k, v in item.items() if k not in ("pk", "seq")}))
            last = page.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last

    def try_lock(self, pk, holder, now, expires):
        from botocore.exceptions import ClientError

        try:
            self._t.put_item(
                Item={"pk": pk, "seq": LOCK_SEQ, "holder": holder, "expires": Decimal(str(expires))},
                ConditionExpression="attribute_not_exists(pk) OR expires < :now",
                ExpressionAttributeValues={":now": Decimal(str(now))},
            )
            return True
        except ClientError as e:
            if self._failed(e):
                return False
            raise

    def unlock(self, pk, holder):
        # Released by expiring it, not deleting it: the ledger's credential
        # cannot delete anything, which is what makes the log append-only.
        from botocore.exceptions import ClientError

        try:
            self._t.update_item(
                Key={"pk": pk, "seq": LOCK_SEQ},
                UpdateExpression="SET expires = :zero",
                ConditionExpression="holder = :h",
                ExpressionAttributeValues={":h": holder, ":zero": Decimal(0)},
            )
        except ClientError as e:
            if not self._failed(e):
                raise

    def lock_holder(self, pk):
        item = self._t.get_item(Key={"pk": pk, "seq": LOCK_SEQ}).get("Item")
        if not item or float(item.get("expires", 0)) <= time.time():
            return None
        return item.get("holder")


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def boto3_table(name: str, *, region: str | None = None) -> Boto3Table:
    """The deployed table by name; credentials and region from the
    environment, which the repository's credentials file supplies."""
    import boto3

    kwargs = {"region_name": region} if region else {}
    return Boto3Table(boto3.resource("dynamodb", **kwargs).Table(name))


def default_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class DynamoStore:
    writable = True

    def __init__(
        self,
        table: Table,
        name: str,
        *,
        holder: str | None = None,
        clock: Callable[[], float] = time.time,
        lock_ttl: float = 60.0,
        lock_wait: float = 120.0,
        expire_after: float | None = None,
    ):
        self._table = table
        self.name = name
        self._holder = holder or default_holder()
        self._clock = clock
        self._lock_ttl = lock_ttl
        self._lock_wait = lock_wait
        self._depth = 0
        # A throwaway ledger's rows carry a `ttl` the table expires; a real
        # ledger's never do. Nothing here can delete a row either way.
        self._expire_after = expire_after

    def events_after(self, seq: int) -> list[Event]:
        return [_event(s, row) for s, row in self._table.query_after(self.name, seq)]

    def append(self, drafts: list[Draft]) -> list[Event]:
        if not drafts:
            return []
        first = self._table.next_seq(self.name, len(drafts))
        out = []
        for offset, d in enumerate(drafts):
            cols = _columns(d)
            item = dict(cols)
            if self._expire_after is not None:
                item["ttl"] = int(self._clock() + self._expire_after)
            self._table.put_event(self.name, first + offset, item)
            out.append(_event(first + offset, cols))
        return out

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """One writer at a time for this ledger, for as long as the block
        runs, bounded by `lock_ttl` so a holder that dies frees it."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        deadline = self._clock() + self._lock_wait
        pause = 0.1
        while True:
            now = self._clock()
            if self._table.try_lock(self.name, self._holder, now, now + self._lock_ttl):
                break
            if now >= deadline:
                raise LockHeld(f"the ledger {self.name!r} stayed locked for {self._lock_wait:.0f}s")
            time.sleep(pause)
            pause = min(pause * 2, 2.0)
        self._depth = 1
        try:
            yield
        finally:
            self._depth = 0
            self._table.unlock(self.name, self._holder)

    def close(self) -> None:
        return None
