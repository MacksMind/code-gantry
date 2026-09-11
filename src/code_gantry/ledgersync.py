"""The ledger across hosts, through the git remote.

Each origin's events are one JSON-lines file on a ref of its own,
`refs/code_gantry/ledger/<origin>`, pushed only by that origin and only
appended to. A sync writes our log to our ref and pushes it, fetches every
origin's ref, and ingests what is new. Hosts never address each other, and
nothing here touches a work tree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from typing import Callable

from code_gantry.gates import clip
from code_gantry.gitops import Git, GitError
from code_gantry.ledger import Ledger

REF_PREFIX = "refs/code_gantry/ledger/"
FILE = "events.jsonl"


def ref_for(origin: str) -> str:
    return f"{REF_PREFIX}{origin}"


def to_jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def from_jsonl(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@dataclass
class SyncReport:
    pushed: int | None = None  # the highest sequence pushed, None when nothing changed
    fetched: list[str] = field(default_factory=list)  # origins fetched
    ingested: dict[str, int] = field(default_factory=dict)  # new events per origin
    skipped: str = ""  # why nothing happened, when nothing did

    def summary(self) -> str:
        if self.skipped:
            return self.skipped
        parts = [f"pushed through seq {self.pushed}" if self.pushed is not None else "nothing new to push"]
        new = {k: v for k, v in self.ingested.items() if v}
        parts.append(
            "ingested " + ", ".join(f"{n} from {o}" for o, n in sorted(new.items()))
            if new else f"nothing new from {len(self.fetched)} other origin(s)"
        )
        return "; ".join(parts)


def sync_at(ledger: Ledger | None, git: Git, log: Callable[[str], object], where: str) -> None:
    """The exchange at a seam of a run. A failure is logged and the run goes
    on: this host's ledger is the record for this host, and the remote is a
    replica of it."""
    if ledger is None:
        return
    try:
        report = sync(ledger, git)
    except (GitError, OSError, ValueError) as e:
        log(f"[{where}] ledger sync failed: {clip(str(e))}")
        return
    if report.pushed is not None or any(report.ingested.values()):
        log(f"[{where}] ledger sync: {report.summary()}")


def sync(ledger: Ledger, git: Git, *, remote: str = "origin") -> SyncReport:
    """Push our log, fetch every log, ingest what is new. Raises on a git
    failure; the caller decides whether a run may go on without it."""
    report = SyncReport()
    if ledger.origin is None:
        report.skipped = "the ledger was opened for reading only"
        return report
    if not git.remote_exists(remote):
        report.skipped = f"no remote {remote!r}"
        return report

    own = ledger.export()
    text = to_jsonl(own)
    ref = ref_for(ledger.origin)
    if own and git.ref_file(ref, FILE) != text:
        last = own[-1]["seq"]
        git.write_ref_file(ref, FILE, text, f"{ledger.origin} events through seq {last}")
        git.push_ref(ref, remote)
        report.pushed = last

    git.fetch_refs(REF_PREFIX, remote)
    for other in git.list_refs(REF_PREFIX):
        origin = other[len(REF_PREFIX):]
        if origin == ledger.origin:
            continue
        report.fetched.append(origin)
        body = git.ref_file(other, FILE)
        if body is None:
            continue
        report.ingested[origin] = ledger.ingest(from_jsonl(body))
    return report
