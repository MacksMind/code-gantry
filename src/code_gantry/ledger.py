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

import copy

import contextlib
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from code_gantry.ledgerstore import (  # noqa: F401 - Event is this module's public type
    Draft, DynamoStore, Event, SqliteStore, Store, StoreError, boto3_table,
)

LEDGER_FILENAME = "ledger.db"
LEDGER_TABLE_ENV = "CODE_GANTRY_LEDGER_TABLE"

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
# The fold point: the plan text is rendered from the nodes as they stood
# here, and everything since sits in the projection until the next one.
PLAN_FOLDED = "plan.folded"
FINDING_CLAIMED = "finding.claimed"
FINDING_RELEASED = "finding.released"
# The thread on a thing waiting for a person: the card an investigation
# attached (`about` a finding id or a key), and a person's question back.
THREAD_RECOMMENDED = "thread.recommended"
THREAD_ASKED = "thread.asked"
# A finding or an item moved to another project's ledger: closed here,
# naming where it went and what it became there.
MOVED = "moved"
# A stage the planner drew, from derivation until a run lands or drops it.
STAGE_DERIVED = "stage.derived"
STAGE_TAKEN = "stage.taken"
STAGE_RELEASED = "stage.released"
STAGE_DROPPED = "stage.dropped"
# A run's own life, recorded so a claim can tell a run that crashed from one
# that means to come back. `disposition` on the end is how it left:
# "finished", "paused", "escalated" or "failed".
# A stage squashed to one commit and pushed as its own branch, waiting for
# whichever bay holds the landing semaphore to compose it onto the project
# branch. `candidate.dropped` is one taken back out of the pool, by the
# compose that found it guilty or by the bay reworking it.
CANDIDATE_PUSHED = "candidate.pushed"
# Composed onto the project branch and pushed, or taken back out of the pool
# by the compose that found it is what turned the composition red.
CANDIDATE_LANDED = "candidate.landed"
CANDIDATE_REJECTED = "candidate.rejected"
# A bay has taken a rejected candidate to put right. Held the way a drawn
# stage is held, so two bays never rework one branch.
REWORK_TAKEN = "rework.taken"
# Given back by a bay that could not do it — a rebase that conflicts — so
# it waits for a person rather than for whoever asks next.
REWORK_RELEASED = "rework.released"
RUN_BEGAN = "run.began"
RUN_ENDED = "run.ended"
STAGE_DONE = "stage.done"
# A suite command was green on a tree, on the origin that ran it.
SUITE_GREEN = "suite.green"

KEY_STATE_KINDS = frozenset({CLAIMED, RELEASED, LANDED, STRUCK, BLOCKED, ANSWER})
NODE_KINDS = frozenset({"document", "section", "item"})
OWNERS = frozenset({"pipeline", "human"})
NEEDS = frozenset({"pipeline", "human"})
# `amend` writes a sentence on the item at the next fold; it was written as
# `fold` before the word was reserved for the rendering step, and events
# carrying the old word are read as `amend`.
DISPOSITIONS = frozenset({"amend", "discard", "debt", "raise"})
AMEND = frozenset({"amend", "fold"})
# What a card may recommend: a finding's dispositions, a move to another
# project, and for an item that a landing or a strike closes it, or that
# the fleet can have it after all.
RECOMMENDATIONS = DISPOSITIONS | frozenset({"move", "landed", "struck", "pipeline"})

def bare_key(text: str | None) -> str:
    """A key as the ledger names it, from however a model wrote it: the
    plan renders `p.002` as the marker `{#p.002}`, and a model asked to
    copy the marker copies the braces."""
    return (text or "").strip().strip("{}").lstrip("#").strip()


class LedgerError(RuntimeError):
    """A write the ledger refuses: a stale edit, an unknown key, a bad kind."""


class StaleEdit(LedgerError):
    """A human edit built on a version of the node that is no longer current."""


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
    # The claim's holder, so a claim can be found to have outlived its run.
    origin: str | None = None
    pid: int | None = None


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
    # open | answered | folded | discarded | debt | resolved | superseded.
    # `answered` is a `fold` waiting for the fold seam; `raise` leaves the
    # finding open and hands it to a person.
    status: str = "open"
    disposition: str | None = None
    answer_text: str | None = None
    target_key: str | None = None
    # The item a `debt` answer wrote under its target.
    entry_key: str | None = None
    resolved_sha: str | None = None
    # A stage drawn against this finding holds it, as a claim holds a key.
    claimed_run: str | None = None
    claimed_stage: str | None = None
    claimed_origin: str | None = None
    claimed_pid: int | None = None
    superseded_by: str | None = None
    # Where a `moved` finding went: the other ledger's label.
    moved_to: str | None = None


@dataclass
class Waiting:
    """One thing a person has to act on: a finding that needs a human, or
    an open item a person owns, with the card an investigation attached
    and the thread since. `id` is the finding's id or the item's key."""

    id: str
    kind: str  # finding | item
    title: str
    text: str
    keys: list[str]
    since: str | None
    subject: str | None = None
    total: str | None = None
    recommendation: dict | None = None
    thread: list[dict] = field(default_factory=list)


@dataclass
class DerivedStage:
    """A stage the planner drew, waiting in the ledger for a run to take it."""

    id: str
    stage_id: str
    fields: dict
    keys: list[str]
    findings: list[str]
    base_sha: str | None
    batch: str | None  # the head's id for a stage drawn behind it
    rank: int
    by_run: str | None
    origin: str
    at: str
    status: str = "derived"  # derived | taken | done | dropped
    taken_run: str | None = None
    taken_origin: str | None = None
    taken_pid: int | None = None
    reason: str | None = None


@dataclass
class Candidate:
    """A stage squashed to one commit on the base it was cut from, pushed as
    its own branch and waiting to be composed onto the project branch."""

    branch: str
    sha: str
    base: str
    stage_id: str
    # What the landing will record: keys, confirmed findings, the stage's
    # drawn record and the reviewer's observations. Carried because the bay
    # that composes it is not the bay that did the work.
    landing: dict
    # The stage itself, so a bay reworking this candidate needs nothing but
    # this record and the branch.
    fields: dict
    run_id: str | None
    origin: str
    at: str


@dataclass
class Rejection:
    """A candidate a composition could not take: it turned the composed tree
    red, or it would not replay onto the tip at all. Its branch still holds
    the work, on a base that has since moved."""

    candidate: "Candidate"
    against: str
    reason: str
    at: str
    taken_run: str | None = None
    taken_origin: str | None = None
    taken_pid: int | None = None


@dataclass
class Views:
    nodes: dict[str, Node] = field(default_factory=dict)
    key_states: dict[str, KeyState] = field(default_factory=dict)
    findings: dict[str, Finding] = field(default_factory=dict)
    derived: dict[str, DerivedStage] = field(default_factory=dict)
    # (sha, command) -> [(origin, at)]: which trees which host has proven green.
    greens: dict[tuple[str, str], list[tuple[str, str]]] = field(default_factory=dict)
    # run_id -> how it last left, or None while it is running. A run that
    # paused or escalated keeps what it holds; see `release_dead_holders`.
    runs: dict[str, str | None] = field(default_factory=dict)
    # branch -> the candidate waiting on it, until it lands or is rejected.
    candidates: dict[str, "Candidate"] = field(default_factory=dict)
    # branch -> a candidate a composition found guilty, waiting for a bay to
    # rebase it onto what has landed since and put it right.
    rejected: dict[str, "Rejection"] = field(default_factory=dict)
    # finding id or key -> every card and question on it, oldest first.
    threads: dict[str, list[dict]] = field(default_factory=dict)
    # finding id or key -> the latest card, the one a person is answering.
    recommendations: dict[str, dict] = field(default_factory=dict)
    # The nodes as they stood at the last fold, which is what the plan text
    # renders: the cacheable block changes only there. None before the
    # first fold, when the plan is rendered live.
    folded: dict[str, "Node"] | None = None

    def folded_views(self) -> "Views":
        """A view over the nodes at the last fold, for rendering the plan text."""
        return Views(nodes=self.folded) if self.folded is not None else self

    def unfolded_nodes(self) -> tuple[list["Node"], list[tuple["Node", "Node"]], list["Node"]]:
        """What has happened to the tree since the last fold: nodes added,
        `(now, then)` pairs changed, and nodes retired. All empty before
        the first fold, when there is nothing the plan text does not show."""
        if self.folded is None:
            return [], [], []
        added, changed, retired = [], [], []
        for node in self.walk():
            then = self.folded.get(node.key)
            if then is None or then.retired:
                added.append(node)
            elif (node.title, node.body, node.owner, node.blocking, node.parent, node.kind) != (
                then.title, then.body, then.owner, then.blocking, then.parent, then.kind
            ):
                changed.append((node, then))
        for key, then in self.folded.items():
            now = self.nodes.get(key)
            if not then.retired and now is not None and now.retired:
                retired.append(now)
        return added, changed, retired

    def waiting(self) -> list[Waiting]:
        """Everything waiting on a person: open human-owned items in tree
        order, then findings that need a human, oldest first."""
        out: list[Waiting] = []
        for node in self.walk():
            if node.kind == "item" and node.owner == "human" and self.is_open(node.key):
                out.append(Waiting(
                    id=node.key, kind="item", title=node.title, text=node.body, keys=[node.key],
                    since=None, recommendation=self.recommendations.get(node.key),
                    thread=list(self.threads.get(node.key, [])),
                ))
        for f in sorted(self.findings.values(), key=lambda f: f.opened_at):
            if f.status == "open" and f.needs == "human":
                out.append(Waiting(
                    id=f.id, kind="finding", title=f.subject or f.claim.splitlines()[0][:80], text=f.claim,
                    keys=list(f.keys), since=f.opened_at, subject=f.subject, total=f.total,
                    recommendation=self.recommendations.get(f.id), thread=list(self.threads.get(f.id, [])),
                ))
        return out

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

    def proven_green(self, sha: str, command: str) -> tuple[str, str] | None:
        """Who last ran `command` green on `sha`, and when — any origin, since
        a green suite is a fact about the tree (decided 2026-09-12); or None."""
        runs = self.greens.get((sha, command), [])
        if not runs:
            return None
        origin, at = max(runs, key=lambda pair: pair[1])
        return origin, at

    def rework_waiting(self) -> list["Rejection"]:
        """Rejected candidates no bay has taken, oldest first. Work that is
        closer to done than anything the planner would draw, and holding
        plan keys while it waits."""
        return sorted(
            (r for r in self.rejected.values() if not r.taken_run),
            key=lambda r: (r.at, r.candidate.branch),
        )

    def pending_candidates(self) -> list["Candidate"]:
        """Everything squashed and pushed and not yet composed, oldest
        first. The order is the order they will be replayed in, so it is
        the order they were finished in — a stage cannot be composed ahead
        of one it was drawn behind."""
        return sorted(self.candidates.values(), key=lambda c: (c.at, c.branch))

    def derived_waiting(self) -> list[DerivedStage]:
        """Stages drawn and not yet taken, a batch at a time in the order drawn."""
        return sorted(
            (d for d in self.derived.values() if d.status == "derived"),
            key=lambda d: (d.at, d.batch or d.id, d.rank, d.id),
        )

    def references_available(
        self, keys, findings, *,
        run_id: str | None = None, stage_id: str | None = None,
    ) -> list[str]:
        """What stops a stage drawn against `keys` and `findings` from
        starting: each reference that is not open, or is held by another
        stage. Empty means it may start. A reference this very stage holds
        does not count against it."""
        taken: list[str] = []
        for key in keys:
            state = self.state(key)
            mine = state.state == "claimed" and state.run_id == run_id and state.stage_id == stage_id
            if state.state != "open" and not mine:
                taken.append(f"{key} ({state.state})")
        for fid in findings:
            finding = self.findings.get(fid)
            if finding is None or finding.status not in ("open", "answered"):
                taken.append(f"{fid} ({finding.status if finding else 'unknown'})")
                continue
            held = finding.claimed_run is not None
            mine = held and finding.claimed_run == run_id and finding.claimed_stage == stage_id
            if held and not mine:
                taken.append(f"{fid} (held by {finding.claimed_run})")
        return taken


def build_views(events: list[Event]) -> Views:
    """The three views, from the events in `seq` order.

    One sequence per ledger, assigned by the store at append, so this is the
    order the writes happened in — across origins as well as within one, and
    computed identically by every host from the same rows.

    It used to be `(at, origin, seq)`, on the argument that no two origins
    write the same node or finding so a skewed clock could not change the
    result. Two origins do write the same *key*: one host releases a claim
    another host's dead run left, and two hosts can claim one key in the
    same second. Under the clock order the release sorted before the claim
    it was releasing — `host-a` before `host-b` — and the key stayed
    held by a run that was gone; a race between two claims was settled by
    which machine was named first in the alphabet. `at` is what a person
    reads; `seq` is what happened.
    """
    views = Views()
    for event in sorted(events, key=lambda e: e.seq):
        _apply(views, event)
    return views


def _apply(views: Views, event: Event) -> None:
    body = event.body
    kind = event.kind
    if kind == CANDIDATE_PUSHED:
        # A branch pushed again is a rework that has come back: it is a
        # candidate once more and no longer something waiting to be put right.
        views.rejected.pop(body.get("branch") or "", None)
        views.candidates[body.get("branch") or ""] = Candidate(
            branch=body.get("branch") or "",
            sha=event.sha or "",
            base=body.get("base") or "",
            stage_id=event.stage_id or "",
            landing=dict(body.get("landing") or {}),
            fields=dict(body.get("fields") or {}),
            run_id=event.run_id,
            origin=event.origin,
            at=event.at,
        )
        return
    if kind == REWORK_RELEASED:
        rejection = views.rejected.get(body.get("branch") or "")
        if rejection is not None:
            rejection.taken_run = rejection.taken_run or "needs a person"
            rejection.reason = f"{rejection.reason}\n{body.get('reason') or ''}".strip()
        return
    if kind == REWORK_TAKEN:
        rejection = views.rejected.get(body.get("branch") or "")
        if rejection is not None:
            rejection.taken_run = event.run_id
            rejection.taken_origin = event.origin
            rejection.taken_pid = body.get("pid")
        return
    if kind in (CANDIDATE_LANDED, CANDIDATE_REJECTED):
        candidate = views.candidates.pop(body.get("branch") or "", None)
        if kind == CANDIDATE_REJECTED and candidate is not None:
            views.rejected[candidate.branch] = Rejection(
                candidate=candidate, against=event.sha or "",
                reason=body.get("reason") or "", at=event.at,
            )
        return
    if kind == RUN_BEGAN:
        # Beginning withdraws whatever intent a previous end recorded: a run
        # that came back and then died is dead, and one pause must not spare
        # its claims for the life of the ledger.
        views.runs[event.run_id or ""] = None
        return
    if kind == RUN_ENDED:
        views.runs[event.run_id or ""] = body.get("disposition") or "finished"
        return
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
    elif kind == PLAN_FOLDED:
        views.folded = {key: copy.deepcopy(node) for key, node in views.nodes.items()}
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
        # supersedes the earlier one. A keyed reading also supersedes an
        # older keyless one of the same subject: a finding filed without a
        # key cannot be closed by a landing or matched by anything, so the
        # subject is the only identity it has, and the keyed one is the
        # same thing said properly.
        if finding.subject:
            for other in views.findings.values():
                if (
                    other.status == "open"
                    and other.subject == finding.subject
                    and (set(other.keys) & set(finding.keys) or (not other.keys and finding.keys))
                ):
                    other.status = "superseded"
                    other.superseded_by = finding.id
        views.findings[finding.id] = finding
    elif kind == FINDING_ANSWERED:
        # Each disposition means something here, so every writer of the
        # event — a CLI, a daemon, a reply from a phone — gets one result.
        finding = views.findings.get(body.get("finding_id", ""))
        if finding and finding.status == "open":
            disposition = body.get("disposition")
            finding.disposition = disposition
            finding.answer_text = body.get("text")
            finding.target_key = body.get("target_key")
            if disposition == "discard":
                finding.status = "discarded"
            elif disposition == "debt":
                finding.status = "debt"
                finding.entry_key = body.get("entry_key")
            elif disposition == "raise":
                # Still open, and now a person's: the next answer is theirs.
                finding.needs = "human"
            else:
                finding.status = "answered"
    elif kind == THREAD_RECOMMENDED:
        about = body.get("about", "")
        entry = {"kind": "recommended", "by": body.get("actor"), "at": event.at, "card": body.get("card") or {}}
        views.threads.setdefault(about, []).append(entry)
        views.recommendations[about] = entry["card"]
    elif kind == THREAD_ASKED:
        about = body.get("about", "")
        views.threads.setdefault(about, []).append(
            {"kind": "asked", "by": body.get("actor"), "at": event.at, "text": body.get("text", "")}
        )
    elif kind == MOVED:
        about = body.get("about", "")
        to = body.get("to")
        opened_as = body.get("opened_as")
        finding = views.findings.get(about)
        if finding is not None:
            if finding.status == "open":
                finding.status = "moved"
                finding.moved_to = to
        elif about in views.nodes:
            views.key_states[about] = KeyState(
                key=about, state="struck", actor=body.get("actor"), since=event.at,
                reason=f"moved to {to} as {opened_as}", evidence=opened_as,
            )
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
    elif kind == FINDING_CLAIMED:
        finding = views.findings.get(body.get("finding_id", ""))
        if finding:
            finding.claimed_run = event.run_id
            finding.claimed_stage = event.stage_id
            finding.claimed_origin = event.origin
            finding.claimed_pid = body.get("pid")
    elif kind == FINDING_RELEASED:
        finding = views.findings.get(body.get("finding_id", ""))
        if finding:
            finding.claimed_run = finding.claimed_stage = None
            finding.claimed_origin = finding.claimed_pid = None
    elif kind == STAGE_DERIVED:
        views.derived[event.derived_id] = DerivedStage(
            id=event.derived_id, stage_id=event.stage_id or "",
            fields=dict(body.get("fields") or {}), keys=list(body.get("keys") or []),
            findings=list(body.get("findings") or []), base_sha=event.sha,
            batch=body.get("batch"), rank=int(body.get("rank", 0)),
            by_run=event.run_id, origin=event.origin, at=event.at,
        )
    elif kind == STAGE_TAKEN:
        d = views.derived.get(body.get("derived_id", ""))
        if d and d.status == "derived":
            d.status = "taken"
            d.taken_run, d.taken_origin, d.taken_pid = event.run_id, event.origin, body.get("pid")
    elif kind == STAGE_RELEASED:
        d = views.derived.get(body.get("derived_id", ""))
        if d and d.status == "taken":
            d.status = "derived"
            d.taken_run = d.taken_origin = d.taken_pid = None
    elif kind == STAGE_DROPPED:
        d = views.derived.get(body.get("derived_id", ""))
        if d and d.status in ("derived", "taken"):
            d.status, d.reason = "dropped", body.get("reason")
    elif kind == STAGE_DONE:
        d = views.derived.get(body.get("derived_id", ""))
        if d and d.status == "taken":
            d.status = "done"
    elif kind == SUITE_GREEN:
        if event.sha and body.get("command"):
            views.greens.setdefault((event.sha, body["command"]), []).append((event.origin, event.at))


def _apply_key_state(views: Views, event: Event) -> None:
    key = event.key or ""
    state = views.key_states.get(key) or KeyState(key=key)
    body = event.body
    actor = body.get("actor")
    if event.kind == CLAIMED:
        state = KeyState(
            key=key, state="claimed", actor=actor, run_id=event.run_id,
            stage_id=event.stage_id, since=event.at,
            origin=event.origin, pid=body.get("pid"),
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
    """One ledger, readable, and writable when it has an origin.

    Sits on a `Store` — a file on this host or the table every host writes —
    and keeps the events it has read, asking the store only for what follows
    the last one, so a second bay's write is seen on the next read.
    """

    def __init__(
        self,
        store: Store,
        origin: str | None,
        *,
        actor: str | None = None,
        clock: Callable[[], str] = _utcnow,
        where: str = "",
    ):
        self.store = store
        self.origin = origin
        self.actor = actor
        self.where = where
        self._clock = clock
        self._events: list[Event] = []
        self._last = 0
        self._views: Views | None = None

    @property
    def path(self) -> str:
        """Where this ledger is, for a message: a file, or a name in the table."""
        return self.where

    # -- reading ----------------------------------------------------------

    def _refresh(self) -> None:
        new = self.store.events_after(self._last)
        if new:
            self._events.extend(new)
            self._last = new[-1].seq
            self._views = None

    def events(self) -> list[Event]:
        self._refresh()
        return list(self._events)

    def views(self) -> Views:
        """Rebuilt whenever the store holds something this ledger has not
        read: after this ledger's own write, and after any other writer's."""
        self._refresh()
        if self._views is None:
            self._views = build_views(self._events)
        return self._views

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
        if self.origin is None or not self.store.writable:
            raise LedgerError("this ledger was opened for reading only")
        if actor is None:
            actor = self.actor
        if actor is not None:
            body = {"actor": actor, **body}
        draft = Draft(
            origin=self.origin, at=self._clock(), kind=kind, key=key,
            stage_id=stage_id, run_id=run_id, sha=sha, body=body,
        )
        (event,) = self.store.append([draft])
        if event.seq == self._last + 1:
            self._events.append(event)
            self._last = event.seq
        else:
            self._refresh()
        self._views = None
        return event

    def record_green(self, sha: str, command: str, *, run_id: str | None = None, stage_id: str | None = None) -> Event:
        """This origin ran `command` green on `sha`."""
        return self.append(SUITE_GREEN, sha=sha, run_id=run_id, stage_id=stage_id, command=command)

    @contextlib.contextmanager
    def transaction(self):
        """One writer across a read and the writes it decides, so what was
        read is still true when it is written against."""
        if self.origin is None or not self.store.writable:
            raise LedgerError("this ledger was opened for reading only")
        with self.store.exclusive():
            self._refresh()
            self._views = None
            try:
                yield
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
        if disposition != "debt":
            return self.append(
                FINDING_ANSWERED, actor=actor, finding_id=finding_id,
                disposition=disposition, text=text, target_key=target_key,
            )
        # `debt` is two events: the entry the finding becomes, written as an
        # item under the target section with the next key of that section's
        # prefix, and the answer naming it. Under one lock, so two answers
        # cannot take one key.
        if not target_key:
            raise LedgerError("`debt` needs a target: the section the entry goes under")
        if not text:
            raise LedgerError("`debt` needs text: the entry itself")
        with self.transaction():
            views = self.views()
            target = views.nodes.get(target_key)
            if target is None or target.retired:
                raise LedgerError(f"no such target {target_key}")
            if target.kind not in ("section", "document"):
                raise LedgerError(f"{target_key} is an item; a debt entry goes under a section")
            prefix = target_key.rpartition(".")[0] or target_key
            entry_key = views.next_key(prefix)
            self.upsert_node(
                entry_key, parent=target_key, position=len(views.children(target_key)),
                kind="item", title=text, owner="human", actor=actor,
            )
            return self.append(
                FINDING_ANSWERED, actor=actor, finding_id=finding_id,
                disposition=disposition, text=text, target_key=target_key, entry_key=entry_key,
            )

    # -- the thread on a thing waiting for a person ------------------------

    def _about(self, about: str) -> tuple[str, "Finding | Node"]:
        views = self.views()
        if about in views.findings:
            return "finding", views.findings[about]
        node = views.nodes.get(about)
        if node is not None and not node.retired:
            return "item", node
        raise LedgerError(f"nothing waiting is called {about}")

    def recommend(self, about: str, *, card: dict, actor: str | None = None) -> Event:
        """Attach a card to a finding or an item: what it says, what it
        anchors to, what was checked, what to do and what that would
        write. The latest card is the recommendation; the thread keeps
        them all."""
        self._about(about)
        recommend = card.get("recommend") if isinstance(card, dict) else None
        disposition = recommend.get("disposition") if isinstance(recommend, dict) else None
        if disposition not in RECOMMENDATIONS:
            raise LedgerError(
                f"a card recommends one of {', '.join(sorted(RECOMMENDATIONS))} under `recommend.disposition`, "
                f"not {disposition!r}"
            )
        return self.append(THREAD_RECOMMENDED, actor=actor, about=about, card=card)

    def ask(self, about: str, *, text: str, actor: str | None = None) -> Event:
        """A person's question on the thread, for the next investigation to read."""
        self._about(about)
        if not text or not text.strip():
            raise LedgerError("a question needs text")
        return self.append(THREAD_ASKED, actor=actor, about=about, text=text.strip())

    def move(
        self,
        about: str,
        *,
        to: "Ledger",
        to_label: str,
        from_label: str,
        under: str | None = None,
        actor: str | None = None,
    ) -> str:
        """Move a finding or an item to another project's ledger. Opened
        there first, with a pointer back, then closed here naming where it
        went — a crash between the two leaves a duplicate somebody can see
        rather than a loss. Returns what it became there: a finding id, or
        the item's new key under `under`, which an item needs."""
        kind, thing = self._about(about)
        if kind == "finding":
            finding = thing
            if finding.status != "open":
                raise LedgerError(f"{about} is {finding.status}, not open")
            claim = f"{finding.claim}\n\nMoved from {from_label}, where it was {about}" + (
                f" on {', '.join(finding.keys)}." if finding.keys else "."
            )
            opened = to.open_finding(
                keys=[], by=f"moved from {from_label}", claim=claim, needs="human",
                subject=finding.subject, total=finding.total, actor=actor,
            ).finding_id
        else:
            node = thing
            if not under:
                raise LedgerError("an item needs `under`: the section in the other ledger it goes under")
            with to.transaction():
                views = to.views()
                target = views.nodes.get(under)
                if target is None or target.retired or target.kind not in ("section", "document"):
                    raise LedgerError(f"{under} is not a section in the other ledger")
                prefix = under.rpartition(".")[0] or under
                opened = views.next_key(prefix)
                body = (node.body + "\n\n" if node.body else "") + f"Moved from {from_label}, where it was {about}."
                to.upsert_node(
                    opened, parent=under, position=len(views.children(under)), kind="item",
                    title=node.title, body=body, owner="human", actor=actor,
                )
        self.append(MOVED, actor=actor, about=about, to=to_label, opened_as=opened)
        return opened

    def close(self) -> None:
        self.store.close()


def open_ledger(
    path: Path | str,
    *,
    origin: str | None = None,
    actor: str | None = None,
    clock: Callable[[], str] = _utcnow,
) -> Ledger:
    """The writer on a file. Creates it, and is the only function that may."""
    return Ledger(SqliteStore.open(path), origin or default_origin(), actor=actor, clock=clock, where=str(path))


def read_ledger(path: Path | str) -> Ledger:
    """A reader on a file. An absent file is an empty ledger, and stays absent."""
    return Ledger(SqliteStore.read(path), None, where=str(path))


def ledger_for(cfg, paths, *, write: bool, origin: str | None = None, actor: str | None = None) -> Ledger:
    """The ledger a config names. `ledger.name` is a ledger in the table the
    environment names — every host's; otherwise the file `paths.ledger`, this
    host's. Credentials and the table come from the environment, which the
    repository's credentials file supplies, so nothing tracked names them."""
    if cfg.ledger is None:
        raise LedgerError("no `ledger:` section in the config")
    if cfg.ledger.name:
        table = os.environ.get(LEDGER_TABLE_ENV)
        if not table:
            raise LedgerError(
                f"ledger.name is set but {LEDGER_TABLE_ENV} is not in the environment; "
                "the repository's credentials file names the table"
            )
        store = DynamoStore(boto3_table(table), cfg.ledger.name)
        return Ledger(
            store, (origin or default_origin()) if write else None,
            actor=actor, where=f"{cfg.ledger.name} in {table}",
        )
    if write:
        return open_ledger(paths.ledger, origin=origin, actor=actor)
    return read_ledger(paths.ledger)


def import_old_file(ledger: Ledger, path: Path | str) -> int:
    """Copy a ledger file from before one sequence per ledger — sequence per
    origin, replicated by refs — into `ledger`, which must hold nothing.

    Rows are taken in the order the old file replayed them, `(at, origin,
    seq)`, and given the new ledger's sequence. The ids a `finding.opened`
    or `stage.derived` row conferred were `<origin>-<seq>`, so every body
    that names one is rewritten to the sequence the row now has; the id
    rule itself is unchanged. Returns how many rows were written.
    """
    import sqlite3

    if ledger.events():
        raise LedgerError(f"{ledger.where} already holds events; an import writes only into an empty ledger")
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT origin, seq, at, kind, key, stage_id, run_id, sha, body"
            "  FROM events ORDER BY at, origin, seq"
        ).fetchall()
    finally:
        conn.close()
    import json as _json

    old_ids = {(r[0], int(r[1])): n for n, r in enumerate(rows, start=1)}

    def renamed(value):
        if isinstance(value, str):
            for prefix in ("f-", "d-"):
                if value.startswith(prefix):
                    origin, _, seq = value[len(prefix):].rpartition("-")
                    if seq.isdigit() and (origin, int(seq)) in old_ids:
                        return f"{prefix}{origin}-{old_ids[(origin, int(seq))]}"
        return value

    drafts = []
    for origin, _seq, at, kind, key, stage_id, run_id, sha, body in rows:
        body = {k: renamed(v) for k, v in _json.loads(body).items()}
        drafts.append(Draft(origin=origin, at=at, kind=kind, key=key, stage_id=stage_id, run_id=run_id, sha=sha, body=body))
    with ledger.transaction():
        if ledger.events():
            raise LedgerError(f"{ledger.where} already holds events; an import writes only into an empty ledger")
        written = ledger.store.append(drafts)
    ledger._refresh()
    return len(written)


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
        if finding.status != "answered" or finding.disposition not in AMEND:
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
        # The fold point, when anything moved into the plan text: the
        # marks just written, or nodes added, changed or retired since
        # the last one. Nothing moved, nothing written, so folding twice
        # is folding once.
        views = ledger.views()
        if proposed or any(views.unfolded_nodes()) or views.folded is None and views.nodes:
            ledger.append(PLAN_FOLDED, actor=actor)
    return len(proposed)


_COMING_BACK = frozenset({"paused", "escalated"})


def release_dead_holders(
    ledger: Ledger,
    *,
    alive: Callable[[int], bool],
    keep_run: str | None = None,
    answered: set[str] | None = None,
    live: set[tuple[str, str]] | None = None,
) -> int:
    """Give back every claim, finding and taken stage held by a run that is
    gone for good. A claim is a lease from a live run; the kernel cannot
    release it as it does a file lock, so somebody else must.

    On this host that is process liveness. On another it is `answered` and
    `live` from the mesh — which origins could be asked, and which runs are
    alive on them. **A host that did not answer keeps everything it holds**:
    it can reach the table it wrote the claim into and may be working
    happily behind a link that is down only from here, so unreachable and
    dead must never arrive at the same conclusion. With no mesh to ask,
    every other host keeps everything, which is where this started.

    Returns how many were released.
    """
    released = 0
    with ledger.transaction():
        views = ledger.views()

        def dead(origin, pid, run_id) -> bool:
            if run_id and run_id == keep_run:
                return False
            # A pause or an escalation ends the process and keeps the claim.
            # Both exit meaning to come back — an escalation with work on a
            # stage branch — and liveness cannot tell either from a crash, so
            # the run says on its way out which it was. Without this, the next
            # bay to start on the host hands the stage to somebody else and
            # the branch is orphaned.
            if views.runs.get(run_id or "") in _COMING_BACK:
                return False
            if origin == ledger.origin:
                return pid is not None and not alive(int(pid))
            # Another host. Only its own daemon can say, and only while it
            # is answering; silence is not an answer.
            if not answered or origin not in answered:
                return False
            return (origin, run_id) not in (live or set())

        for key, state in views.key_states.items():
            if state.state == "claimed" and dead(state.origin, state.pid, state.run_id):
                ledger.append(RELEASED, key=key, run_id=state.run_id, stage_id=state.stage_id, reason="holder exited")
                released += 1
        for finding in views.findings.values():
            if finding.claimed_run and dead(finding.claimed_origin, finding.claimed_pid, finding.claimed_run):
                ledger.append(FINDING_RELEASED, finding_id=finding.id, run_id=finding.claimed_run, reason="holder exited")
                released += 1
        for d in views.derived.values():
            if d.status == "taken" and dead(d.taken_origin, d.taken_pid, d.taken_run):
                ledger.append(STAGE_RELEASED, derived_id=d.id, run_id=d.taken_run, reason="holder exited")
                released += 1
    return released


