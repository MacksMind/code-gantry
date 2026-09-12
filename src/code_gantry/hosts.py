"""Every host's state and every daemon's events, in the table.

Two more ledgers beside the projects', reached through the same store:
`_hosts`, where each daemon appends its state and the latest row per
origin is what that host is doing now; `_events`, one line per thing a
daemon did, from every host, in one sequence. A daemon writes them through
`code-gantry hosts put` and `code-gantry events put`, since it carries no
client of its own; anyone reads them with `code-gantry hosts` and
`code-gantry events --follow`, from any host, which is how one command
shows every host, every repository on it and the project each bay works.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import click

from code_gantry.ledgerstore import Draft, DynamoStore, boto3_table

HOSTS = "_hosts"
EVENTS = "_events"
# A host row is superseded by the next; a week of them is history enough.
HOST_ROW_TTL = 7 * 24 * 3600


@dataclass
class Bay:
    name: str
    repo: str
    project: str
    state: str
    run_id: str | None
    since: str | None


@dataclass
class HostState:
    origin: str
    node: str
    code: str
    at: str
    bays: list[Bay] = field(default_factory=list)


@dataclass
class DaemonEvent:
    seq: int
    origin: str
    at: str
    text: str


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- hosts --------------------------------------------------------------------

def put(store, *, origin: str, node: str, code: str, bays: list[Bay], at: str | None = None):
    draft = Draft(
        origin=origin, at=at or _utcnow(), kind="host.state", key=None, stage_id=None, run_id=None, sha=code,
        body={"node": node, "bays": [asdict(b) for b in bays]},
    )
    return store.append([draft])[0]


def latest(store) -> list[HostState]:
    """The newest row per origin, in origin order."""
    by: dict[str, HostState] = {}
    for e in store.events_after(0):
        if e.kind != "host.state":
            continue
        by[e.origin] = HostState(
            origin=e.origin, node=e.body.get("node", ""), code=e.sha or "", at=e.at,
            bays=[Bay(**b) for b in e.body.get("bays", [])],
        )
    return [by[o] for o in sorted(by)]


def _age(at: str, now: str) -> tuple[str, bool]:
    seconds = int((datetime.fromisoformat(now) - datetime.fromisoformat(at)).total_seconds())
    if seconds < 90:
        text = f"{seconds}s ago"
    elif seconds < 3600:
        text = f"{seconds // 60}m ago"
    elif seconds < 86400:
        text = f"{seconds // 3600}h {seconds % 3600 // 60}m ago"
    else:
        text = f"{seconds // 86400}d {seconds % 86400 // 3600}h ago"
    return text, seconds > 600


def render(states: list[HostState], *, now: str | None = None) -> str:
    now = now or _utcnow()
    out = []
    for h in states:
        age, stale = _age(h.at, now)
        out.append(f"{h.origin}  {h.node}  code {h.code}  written {age}{' (stale)' if stale else ''}")
        for b in h.bays:
            out.append(f"  {b.repo}/{b.project}  {b.name}  {b.state}  {b.run_id or '-'}  since {b.since or '-'}")
    return "\n".join(out)


# -- events -------------------------------------------------------------------

def event(store, *, origin: str, text: str, at: str | None = None):
    draft = Draft(origin=origin, at=at or _utcnow(), kind="daemon.event", key=None, stage_id=None, run_id=None, sha=None, body={"text": text})
    return store.append([draft])[0]


def events_after(store, seq: int) -> list[DaemonEvent]:
    return [DaemonEvent(seq=e.seq, origin=e.origin, at=e.at, text=e.body.get("text", "")) for e in store.events_after(seq) if e.kind == "daemon.event"]


def render_event(e: DaemonEvent) -> str:
    return f"{e.at} [{e.origin}] {e.text}"


# -- the table, from a config's environment --------------------------------

def config_for(path):
    from code_gantry.cli import _config_argument, _project_for

    cfg, _ = _project_for(_config_argument(path))
    return cfg


def table_for(cfg):
    from code_gantry.ledger import LEDGER_TABLE_ENV, LedgerError

    table = os.environ.get(LEDGER_TABLE_ENV)
    if not table:
        raise LedgerError(f"{LEDGER_TABLE_ENV} is not in the environment; the repository's credentials file names the table")
    return boto3_table(table)


def _store(config_path, name, *, expire_after=None):
    cfg = config_for(config_path)
    return DynamoStore(table_for(cfg), name, expire_after=expire_after)


config_option = click.option("--config", "config_path", type=click.Path(path_type=Path), default=None, help="A project config; its credentials file names the table.")


@click.group("hosts", invoke_without_command=True)
@config_option
@click.pass_context
def hosts_group(ctx, config_path):
    """Every host, every repository on it, and the project each bay works."""
    if ctx.invoked_subcommand is None:
        click.echo(render(latest(_store(config_path, HOSTS))))


@hosts_group.command("put")
@config_option
@click.option("--origin", required=True)
@click.option("--node", required=True)
@click.option("--code", required=True, help="The commit the daemon's checkout is at.")
@click.option("--bay", "bays", multiple=True, metavar="NAME|REPO|PROJECT|STATE|RUN_ID|SINCE")
def hosts_put(config_path, origin, node, code, bays):
    """A daemon's state, appended; the latest row per origin is the state."""
    parsed = []
    for raw in bays:
        name, repo, project, state, run_id, since = (raw.split("|") + [None] * 6)[:6]
        parsed.append(Bay(name, repo, project, state, run_id or None, since or None))
    put(_store(config_path, HOSTS, expire_after=HOST_ROW_TTL), origin=origin, node=node, code=code, bays=parsed)


@click.group("events", invoke_without_command=True)
@config_option
@click.option("--after", type=int, default=None, help="Sequence to start after; default the last 50.")
@click.option("--follow", is_flag=True, help="Keep printing as events arrive.")
@click.pass_context
def events_group(ctx, config_path, after, follow):
    """What every daemon did, one line each, in one sequence."""
    if ctx.invoked_subcommand is not None:
        return
    store = _store(config_path, EVENTS)
    if after is None:
        lines = events_after(store, 0)
        lines = lines[-50:]
        for e in lines:
            click.echo(render_event(e))
        last = lines[-1].seq if lines else 0
    else:
        last = after
        for e in events_after(store, after):
            click.echo(render_event(e))
            last = e.seq
    while follow:
        time.sleep(5)
        for e in events_after(store, last):
            click.echo(render_event(e))
            last = e.seq


@events_group.command("put")
@config_option
@click.option("--origin", required=True)
@click.option("--text", required=True)
def events_put(config_path, origin, text):
    """One line from a daemon."""
    event(_store(config_path, EVENTS), origin=origin, text=text)
