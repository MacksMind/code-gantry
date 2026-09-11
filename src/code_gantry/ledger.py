"""The ledger: the plan tree, each key's state, and findings.

One append-only table of events; the plan tree, key states and findings are
views rebuilt from it on read, never stored, so no status column can disagree
with the history that produced it.

Every event is written by one origin and numbered within it. `origin` is the
host (later, the bay) whose file this is, and nothing writes under another
origin's name, so exchanging ledgers between hosts later is a fetch of "origin
X after seq N", not a merge.

Readers never create the file. The row is the `Event` dataclass's `asdict`, so
a field is left out of a record only on purpose.
"""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

LEDGER_FILENAME = "ledger.db"

# Kinds. Named with a dot where the subject is a thing with a lifecycle of its
# own (a node, a finding) and bare where the subject is a key's state.
NODE_UPSERTED = "node.upserted"
NODE_RETIRED = "node.retired"
NODE_MARKED = "node.marked"
CLAIMED = "claimed"
RELEASED = "released"
LANDED = "landed"
STRUCK = "struck"
BLOCKED = "blocked"
ANSWER = "answer"
FINDING_OPENED = "finding.opened"
FINDING_ANSWERED = "finding.answered"
FINDING_RESOLVED = "finding.resolved"
FINDING_SUPERSEDED = "finding.superseded"
FINDING_FOLDED = "finding.folded"

KEY_STATE_KINDS = frozenset({CLAIMED, RELEASED, LANDED, STRUCK, BLOCKED, ANSWER})
NODE_KINDS = frozenset({"document", "section", "item"})
OWNERS = frozenset({"pipeline", "human"})
NEEDS = frozenset({"pipeline", "human"})
DISPOSITIONS = frozenset({"fold", "discard", "debt", "raise"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    origin   TEXT    NOT NULL,
    seq      INTEGER NOT NULL,
    at       TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    key      TEXT,
    stage_id TEXT,
    run_id   TEXT,
    sha      TEXT,
    body     TEXT    NOT NULL,
    PRIMARY KEY (origin, seq)
)
"""


class LedgerError(RuntimeError):
    """A write the ledger refuses: a stale edit, an unknown key, a bad kind."""


class StaleEdit(LedgerError):
    """A human edit built on a version of the node that is no longer current."""


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
        """The id a `finding.opened` event confers: unique across origins for free."""
        return f"f-{self.origin}-{self.seq}"


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


@dataclass
class Node:
    key: str
    parent: str | None
    position: int
    kind: str
    title: str
    body: str = ""
    owner: str = "pipeline"
    blocking: bool = False
    path: str | None = None
    version: int = 0
    retired: bool = False
    marks: list[str] = field(default_factory=list)


@dataclass
class KeyState:
    """What a key is in, derived from its last state-changing event."""

    key: str
    state: str = "open"  # open | claimed | landed | struck | blocked
    actor: str | None = None
    run_id: str | None = None
    stage_id: str | None = None
    sha: str | None = None
    since: str | None = None
    question: str | None = None
    answer: str | None = None
    reason: str | None = None  # why a key was struck
    evidence: str | None = None


@dataclass
class Finding:
    id: str
    keys: list[str]
    by: str  # planner | reviewer | human | reconcile | <stage-id>
    claim: str
    needs: str = "pipeline"
    total: str | None = None
    at_sha: str | None = None
    subject: str | None = None
    opened_at: str = ""
    stage_id: str | None = None
    run_id: str | None = None
    status: str = "open"  # open | answered | resolved | superseded | folded
    disposition: str | None = None
    answer_text: str | None = None
    target_key: str | None = None
    resolved_sha: str | None = None
    superseded_by: str | None = None


@dataclass
class Views:
    nodes: dict[str, Node] = field(default_factory=dict)
    key_states: dict[str, KeyState] = field(default_factory=dict)
    findings: dict[str, Finding] = field(default_factory=dict)

    # -- tree -------------------------------------------------------------

    def children(self, key: str | None) -> list[Node]:
        """Live children of `key` (None for the documents), in position order."""
        return sorted(
            (n for n in self.nodes.values() if n.parent == key and not n.retired),
            key=lambda n: (n.position, n.key),
        )

    def documents(self) -> list[Node]:
        return self.children(None)

    def walk(self, key: str | None = None) -> list[Node]:
        """Every live node under `key`, depth first, in document order."""
        out: list[Node] = []
        for child in self.children(key):
            out.append(child)
            out.extend(self.walk(child.key))
        return out

    def is_leaf(self, key: str) -> bool:
        node = self.nodes.get(key)
        return node is not None and not any(
            c.kind in ("section", "item") for c in self.children(key)
        )

    def ancestors(self, key: str) -> list[Node]:
        out: list[Node] = []
        node = self.nodes.get(key)
        while node is not None and node.parent is not None:
            node = self.nodes.get(node.parent)
            if node is None:
                break
            out.append(node)
        return out

    def next_key(self, prefix: str) -> str:
        """The next unassigned key under `prefix`, counting retired nodes too."""
        highest = 0
        for key in self.nodes:
            stem, _, number = key.rpartition(".")
            if stem == prefix and number.isdigit():
                highest = max(highest, int(number))
        return f"{prefix}.{highest + 1:03d}"

    # -- state ------------------------------------------------------------

    def state(self, key: str) -> KeyState:
        return self.key_states.get(key) or KeyState(key=key)

    def is_open(self, key: str) -> bool:
        return self.state(key).state == "open"

    def open_findings(self) -> list[Finding]:
        return [f for f in self.findings.values() if f.status == "open"]

    def findings_on(self, key: str) -> list[Finding]:
        return [f for f in self.findings.values() if key in f.keys]


def build_views(events: list[Event]) -> Views:
    """The three views, from the events in `(at, origin, seq)` order.

    Within one origin that is write order; across origins it is the one order
    every host can compute. No two origins write the same node or finding, so
    clock skew between them cannot change the result.
    """
    views = Views()
    for event in sorted(events, key=lambda e: (e.at, e.origin, e.seq)):
        _apply(views, event)
    return views


def _apply(views: Views, event: Event) -> None:
    body = event.body
    kind = event.kind
    if kind == NODE_UPSERTED:
        key = event.key or ""
        current = views.nodes.get(key)
        node = Node(
            key=key,
            parent=body.get("parent"),
            position=int(body.get("position", 0)),
            kind=body.get("node_kind", "item"),
            title=body.get("title", ""),
            body=body.get("body", ""),
            owner=body.get("owner", "pipeline"),
            blocking=bool(body.get("blocking", False)),
            path=body.get("path"),
            version=(current.version + 1) if current else 1,
            retired=False,
            marks=list(current.marks) if current else [],
        )
        views.nodes[key] = node
    elif kind == NODE_RETIRED:
        node = views.nodes.get(event.key or "")
        if node:
            node.retired = True
    elif kind == NODE_MARKED:
        node = views.nodes.get(event.key or "")
        if node:
            mark = body.get("mark", "")
            if mark and mark not in node.marks:
                node.marks.append(mark)
    elif kind in KEY_STATE_KINDS:
        _apply_key_state(views, event)
    elif kind == FINDING_OPENED:
        finding = Finding(
            id=event.finding_id,
            keys=list(body.get("keys") or ([event.key] if event.key else [])),
            by=body.get("by", "planner"),
            claim=body.get("claim", ""),
            needs=body.get("needs", "pipeline"),
            total=body.get("total"),
            at_sha=event.sha,
            subject=body.get("subject"),
            opened_at=event.at,
            stage_id=event.stage_id,
            run_id=event.run_id,
        )
        # Later wins: a fresh reading of the same subject on the same key
        # supersedes the earlier one.
        if finding.subject:
            for other in views.findings.values():
                if (
                    other.status == "open"
                    and other.subject == finding.subject
                    and set(other.keys) & set(finding.keys)
                ):
                    other.status = "superseded"
                    other.superseded_by = finding.id
        views.findings[finding.id] = finding
    elif kind == FINDING_ANSWERED:
        finding = views.findings.get(body.get("finding_id", ""))
        if finding and finding.status == "open":
            finding.status = "answered"
            finding.disposition = body.get("disposition")
            finding.answer_text = body.get("text")
            finding.target_key = body.get("target_key")
    elif kind == FINDING_RESOLVED:
        finding = views.findings.get(body.get("finding_id", ""))
        if finding and finding.status in ("open", "answered"):
            finding.status = "resolved"
            finding.resolved_sha = event.sha
    elif kind == FINDING_SUPERSEDED:
        finding = views.findings.get(body.get("finding_id", ""))
        if finding and finding.status == "open":
            finding.status = "superseded"
            finding.superseded_by = body.get("by")
    elif kind == FINDING_FOLDED:
        finding = views.findings.get(body.get("finding_id", ""))
        if finding and finding.status == "answered":
            finding.status = "folded"


def _apply_key_state(views: Views, event: Event) -> None:
    key = event.key or ""
    state = views.key_states.get(key) or KeyState(key=key)
    body = event.body
    actor = body.get("actor")
    if event.kind == CLAIMED:
        state = KeyState(
            key=key, state="claimed", actor=actor, run_id=event.run_id,
            stage_id=event.stage_id, since=event.at,
        )
    elif event.kind == RELEASED:
        state = KeyState(key=key, state="open", actor=actor, since=event.at)
    elif event.kind == LANDED:
        state = KeyState(
            key=key, state="landed", actor=actor, run_id=event.run_id,
            stage_id=event.stage_id, sha=event.sha, since=event.at,
            evidence=body.get("evidence"),
        )
        # A landing on a leaf closes its findings; on a section it does not,
        # since one item landing says nothing about the section.
        if views.is_leaf(key):
            for finding in views.findings.values():
                if finding.status in ("open", "answered") and key in finding.keys:
                    finding.status = "resolved"
                    finding.resolved_sha = event.sha
    elif event.kind == STRUCK:
        state = KeyState(
            key=key, state="struck", actor=actor, run_id=event.run_id,
            stage_id=event.stage_id, sha=event.sha, since=event.at,
            reason=body.get("reason"), evidence=body.get("evidence"),
        )
    elif event.kind == BLOCKED:
        state = KeyState(
            key=key, state="blocked", actor=actor, run_id=event.run_id,
            stage_id=event.stage_id, since=event.at,
            question=body.get("question"),
        )
    elif event.kind == ANSWER:
        state = KeyState(
            key=key, state="open", actor=actor, since=event.at,
            question=state.question, answer=body.get("text"),
        )
    views.key_states[key] = state


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_origin() -> str:
    return socket.gethostname()


class Ledger:
    """One ledger file, readable, and writable when it has an origin.

    A writer holds one connection and commits per event. WAL and a busy
    timeout, because a run and the operator's CLI share the file.
    """

    def __init__(
        self,
        path: Path | None,
        conn: sqlite3.Connection | None,
        origin: str | None,
        *,
        actor: str | None = None,
        clock: Callable[[], str] = _utcnow,
    ):
        self.path = path
        self._conn = conn
        self.origin = origin
        self.actor = actor
        self._clock = clock
        self._views: Views | None = None
        self._views_version: int | None = None

    # -- reading ----------------------------------------------------------

    def events(self) -> list[Event]:
        if self._conn is None:
            return []
        rows = self._conn.execute(
            "SELECT origin, seq, at, kind, key, stage_id, run_id, sha, body"
            "  FROM events ORDER BY at, origin, seq"
        ).fetchall()
        return [
            Event(
                origin=r[0], seq=r[1], at=r[2], kind=r[3], key=r[4],
                stage_id=r[5], run_id=r[6], sha=r[7], body=json.loads(r[8]),
            )
            for r in rows
        ]

    def views(self) -> Views:
        """Rebuilt from the events whenever they have changed: after this
        ledger's own write, and after any other connection's commit, which
        SQLite reports through `data_version`. Several bays share one file,
        so a cache that only knew its own writes would miss their claims."""
        version = self._data_version()
        if self._views is None or version != self._views_version:
            self._views = build_views(self.events())
            self._views_version = version
        return self._views

    def _data_version(self) -> int:
        if self._conn is None:
            return 0
        return int(self._conn.execute("PRAGMA data_version").fetchone()[0])

    def since(self, origin: str, seq: int) -> list[Event]:
        """This origin's events after `seq` — the unit a later replication fetches."""
        return [e for e in self.events() if e.origin == origin and e.seq > seq]

    # -- writing ----------------------------------------------------------

    def append(
        self,
        kind: str,
        *,
        key: str | None = None,
        stage_id: str | None = None,
        run_id: str | None = None,
        sha: str | None = None,
        actor: str | None = None,
        **body,
    ) -> Event:
        if self._conn is None or self.origin is None:
            raise LedgerError("this ledger was opened for reading only")
        if actor is None:
            actor = self.actor
        if actor is not None:
            body = {"actor": actor, **body}
        # The next sequence number and the insert are one transaction, taken
        # with the write lock up front, so two writers can never both read the
        # same number. Inside `transaction()` the lock is already held and the
        # caller commits.
        own = not self._conn.in_transaction
        if own:
            self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM events WHERE origin = ?",
                (self.origin,),
            ).fetchone()
            event = Event(
                origin=self.origin,
                seq=int(row[0]) + 1,
                at=self._clock(),
                kind=kind,
                key=key,
                stage_id=stage_id,
                run_id=run_id,
                sha=sha,
                body=body,
            )
            record = asdict(event)
            record["body"] = json.dumps(event.body, ensure_ascii=False, sort_keys=True)
            self._conn.execute(
                "INSERT INTO events (origin, seq, at, kind, key, stage_id, run_id, sha, body)"
                " VALUES (:origin, :seq, :at, :kind, :key, :stage_id, :run_id, :sha, :body)",
                record,
            )
            if own:
                self._conn.commit()
        except BaseException:
            if own:
                self._conn.rollback()
            raise
        self._views = None
        return event

    @contextlib.contextmanager
    def transaction(self):
        """One write lock across a read and the writes it decides, so what
        was read is still true when it is written against."""
        if self._conn is None or self.origin is None:
            raise LedgerError("this ledger was opened for reading only")
        self._conn.execute("BEGIN IMMEDIATE")
        self._views = None
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._views = None

    def upsert_node(
        self,
        key: str,
        *,
        parent: str | None,
        position: int,
        kind: str,
        title: str,
        body: str = "",
        owner: str = "pipeline",
        blocking: bool = False,
        path: str | None = None,
        base_version: int | None = None,
        actor: str | None = None,
    ) -> Event:
        """Write a node, refusing an edit built on a version that has moved on.

        `base_version` is the version the editor saw; a mismatch is a refusal
        with a re-render rather than a silent overwrite.
        """
        if kind not in NODE_KINDS:
            raise LedgerError(f"unknown node kind {kind!r}")
        if owner not in OWNERS:
            raise LedgerError(f"unknown owner {owner!r}")
        current = self.views().nodes.get(key)
        if base_version is not None:
            have = current.version if current else 0
            if have != base_version:
                raise StaleEdit(
                    f"{key} is at version {have}, not {base_version}; re-render "
                    "and edit again"
                )
        return self.append(
            NODE_UPSERTED, key=key, actor=actor,
            parent=parent, position=position, node_kind=kind, title=title,
            body=body, owner=owner, blocking=blocking, path=path,
        )

    def open_finding(
        self,
        *,
        keys: list[str],
        by: str,
        claim: str,
        needs: str = "pipeline",
        total: str | None = None,
        subject: str | None = None,
        at_sha: str | None = None,
        stage_id: str | None = None,
        run_id: str | None = None,
        actor: str | None = None,
    ) -> Event:
        if needs not in NEEDS:
            raise LedgerError(f"a finding needs 'pipeline' or 'human', not {needs!r}")
        return self.append(
            FINDING_OPENED, key=keys[0] if keys else None, sha=at_sha,
            stage_id=stage_id, run_id=run_id, actor=actor,
            keys=list(keys), by=by, claim=claim, needs=needs, total=total,
            subject=subject,
        )

    def answer_finding(
        self,
        finding_id: str,
        *,
        disposition: str,
        text: str | None = None,
        target_key: str | None = None,
        actor: str | None = None,
    ) -> Event:
        if disposition not in DISPOSITIONS:
            raise LedgerError(f"unknown disposition {disposition!r}")
        if finding_id not in self.views().findings:
            raise LedgerError(f"no finding {finding_id}")
        return self.append(
            FINDING_ANSWERED, actor=actor, finding_id=finding_id,
            disposition=disposition, text=text, target_key=target_key,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def open_ledger(
    path: Path | str,
    *,
    origin: str | None = None,
    actor: str | None = None,
    clock: Callable[[], str] = _utcnow,
) -> Ledger:
    """The writer. Creates the file, and is the only function that may."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    # Switching the journal mode and creating the table take an exclusive lock
    # the busy handler does not always wait for, so several writers opening a
    # fresh file at once can each see it locked for an instant. Retried, not
    # trusted to the timeout.
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
    return Ledger(path, conn, origin or default_origin(), actor=actor, clock=clock)


def read_ledger(path: Path | str) -> Ledger:
    """A reader. An absent file is an empty ledger, and stays absent."""
    path = Path(path)
    if not path.is_file():
        return Ledger(path, None, None)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    tables = {
        name for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "events" not in tables:
        conn.close()
        return Ledger(path, None, None)
    return Ledger(path, conn, None)


# --------------------------------------------------------------------------
# Fold
# --------------------------------------------------------------------------


def should_fold(plan_text: str, projection: str, ratio: float) -> bool:
    """Whether the churning half is large enough, against the stable one, to fold.

    Block 1 is re-sent every derivation and block 0 is rewritten once per
    fold; the ratio is that cost comparison.
    """
    if not plan_text:
        return False
    return len(projection) / len(plan_text) > ratio


def fold_marks(views: Views) -> list[tuple[str, str, dict]]:
    """What a fold would write, as `(kind, key, body)` triples; nothing applied.

    Landed and struck keys get their mark; answered findings with a `fold`
    disposition are written under their target key and closed. Mechanical and
    idempotent, which is what lets the run do it at the derivation seam.
    """
    out: list[tuple[str, str, dict]] = []
    for key, state in sorted(views.key_states.items()):
        node = views.nodes.get(key)
        if node is None or node.retired:
            continue
        if state.state == "landed":
            mark = "landed"
            if state.sha and state.sha not in (state.evidence or ""):
                mark += f" `{state.sha}`"
            if state.evidence:
                mark += f". {state.evidence}"
        elif state.state == "struck":
            mark = "STRUCK" + (f": {state.reason}" if state.reason else "")
            if state.evidence:
                mark += f". {state.evidence}"
        else:
            continue
        if mark not in node.marks:
            out.append((NODE_MARKED, key, {"mark": mark}))
    for finding in sorted(views.findings.values(), key=lambda f: f.id):
        if finding.status != "answered" or finding.disposition != "fold":
            continue
        target = finding.target_key or (finding.keys[0] if finding.keys else None)
        if target and finding.answer_text:
            out.append((NODE_MARKED, target, {"mark": finding.answer_text}))
        out.append((FINDING_FOLDED, target or "", {"finding_id": finding.id}))
    return out


def apply_fold(ledger: Ledger, *, actor: str | None = None) -> int:
    """Write what `fold_marks` proposes. Returns how many events were written.

    Proposed and written under one lock, so two bays folding at once cannot
    both write the same mark."""
    with ledger.transaction():
        proposed = fold_marks(ledger.views())
        for kind, key, body in proposed:
            ledger.append(kind, key=key or None, actor=actor, **body)
    return len(proposed)


def resolve_scope(views: Views, names) -> set[str]:
    """The keys a run may draw from: each name and everything under it."""
    out: set[str] = set()
    for name in names:
        node = views.nodes.get(name)
        if node is None or node.retired:
            raise LedgerError(f"{name!r} is not a key in the plan")
        out.add(name)
        out.update(n.key for n in views.walk(name))
    return out
