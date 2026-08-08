"""The loop that walks the graph, and the checkpoint it writes as it goes.

What LangGraph supplied was never the routing — `nodes` set `next_hop` and the
router was a table lookup — but four smaller things: the loop, a state merge, a
checkpointer, and a step ceiling. Each is here, and two of them were
load-bearing in ways worth stating.

**The schema filter is a feature.** LangGraph silently dropped keys `RunState`
does not declare, and `state.py` says it relies on that: "without this line
verify writes it, the schema discards it, and the gate silently never skips —
which is exactly how it shipped the first time." A plain `dict.update` would
turn a misspelled key from a caught bug into a live one, so the filter is
explicit and comes from the schema rather than a hand-written list.

**Resume is explicit now, and that is a behaviour change.** LangGraph resumed
from a pending task, so `resume_entry_point` was not always consulted. Here a
node that dies leaves no checkpoint of its own, the last completed node's state
is what reloads, and the entry point always decides where to go.

**And the ceiling escalates.** `recursion_limit`'s docstring named the problem
it could not fix from outside: exhausting the limit "surfaces as an opaque
framework error rather than an escalation — the one failure mode this tool must
not have." Owning the loop is what lets that be an escalation.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable

from orchestrator import nodes
from orchestrator.state import RunState, resume_entry_point

NODES: dict[str, Callable] = {
    "plan": nodes.plan,
    "precheck": nodes.precheck,
    "execute": nodes.execute,
    "verify": nodes.verify,
    "review": nodes.review,
    "advance": nodes.advance,
    "finalize": nodes.finalize,
    "escalate": nodes.escalate,
}

# Where each node may send the run. Declarative so it is checkable against the
# spec rather than buried in lambdas.
EDGES: dict[str, list[str]] = {
    # Reaches itself, and it is the only node that does. A stage spec that
    # fails validation is redrawn rather than escalated — a malformed spec is
    # the definition of a stage drawn wrongly, which is the planner's tier.
    # Bounded by `max_interventions_without_landing` like every other way the
    # planner can fail to make progress.
    "plan": ["precheck", "verify", "finalize", "escalate", "plan"],
    "precheck": ["execute", "plan", "escalate"],
    # `execute` reaches itself for one case only: the executor failed to run
    # at all — a transport error, a rejected request, a missing credential —
    # so there is nothing for a gate to look at. That path existed in
    # `nodes.execute` from the start and was illegal here, which nothing
    # noticed while the subprocess editor made it almost unreachable; the
    # in-process one returns `ok=False` for exactly this and crashed the run
    # on its first stage.
    #
    # Routing it through `verify` was the alternative and would have been a
    # lie: the gate would report "the attempt produced no changes", which is
    # observably true and diagnostically wrong. Bounded by `max_test_retries`
    # like any other executor retry.
    "execute": ["verify", "plan", "execute"],
    "verify": ["review", "advance", "execute", "plan", "escalate"],
    "review": ["advance", "execute", "plan"],
    "advance": ["plan"],
    "finalize": ["end", "escalate"],
    "escalate": ["end"],
}

ENTRY_POINTS = ["plan", "precheck", "verify"]


# What the framework this replaced wrote. Recognised so a run started before
# the cutover gets told why it cannot resume rather than being reported
# missing.
_OLD_TABLES = {"checkpoints", "writes"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS steps (
    run_id TEXT NOT NULL,
    step   INTEGER NOT NULL,
    node   TEXT NOT NULL,
    state  TEXT NOT NULL,
    PRIMARY KEY (run_id, step)
)
"""


class UnreadableCheckpoint(RuntimeError):
    """A `state.db` this driver did not write.

    Its own type because the caller has to tell it from "no such run": one is
    a typo in a run id, the other is a run that predates the driver and can
    only be started again.
    """


def open_checkpointer(db_path: Path | str) -> tuple[Callable, sqlite3.Connection]:
    """A writer and its connection, whose lifetime the caller owns.

    Returned as a pair for the reason the LangGraph version was: a resume
    happens in a fresh process and must reopen the same file, so nothing here
    may hold the handle past the run.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.execute(_SCHEMA)
    conn.commit()

    def write(run_id: str, step: int, node: str, state: dict) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO steps (run_id, step, node, state) VALUES (?,?,?,?)",
            (run_id, step, node, json.dumps(state, default=str)),
        )
        conn.commit()

    return write, conn


def load_state(db_path: Path | str, run_id: str) -> RunState | None:
    """The state after the last node that finished, or None for an unknown run.

    Its own connection, opened and closed, because the caller that resumes has
    not built a runtime yet and `status` reads this without driving anything.
    """
    path = Path(db_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    try:
        tables = {
            name for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        # Named by what it *is*, not by the absence of ours. The first version
        # asked "is `steps` missing", which is a weaker question and was
        # defeated immediately: this function used to run `CREATE TABLE IF NOT
        # EXISTS` before reading, so a single read against a live run's
        # database left our table inside it and the check then saw both. A
        # reader that writes is a bug on its own; it also erased the evidence
        # it was about to look for.
        if _OLD_TABLES <= tables:
            raise UnreadableCheckpoint(
                f"{path} is in an older format and cannot be resumed. Nothing "
                "is lost: every landed stage is squash-merged onto the project "
                "branch, and status.md, the progress log and stage-costs.md "
                "carry across runs. Start a new run against the same project."
            )
        if "steps" not in tables:
            return None
        row = conn.execute(
            "SELECT state FROM steps WHERE run_id = ? ORDER BY step DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def _merge(state: dict, update: dict | None) -> dict:
    """State plus what the node returned, minus what the schema does not know.

    The filter is the whole reason this is a function. See the module
    docstring: a key `RunState` has never heard of is a typo, and dropping it
    is what makes it a bug the tests catch rather than a value the run carries
    and nothing reads.
    """
    if not update:
        return state
    known = RunState.__annotations__
    return {**state, **{k: v for k, v in update.items() if k in known}}


def _next(node: str, state: dict, edges: dict[str, list[str]]) -> str:
    allowed = edges.get(node, [])
    hop = state.get("next_hop") or "escalate"
    if hop == "end":
        return "end"
    if hop not in allowed:
        # A node asking for an edge the spec does not have is a bug in the
        # node; silently rerouting would hide it.
        raise RuntimeError(f"node {node!r} routed to {hop!r}, not one of {allowed}")
    return hop


def default_max_steps(
    max_stages: int,
    max_test_retries: int,
    max_rework_retries: int,
    max_planner_interventions: int,
) -> int:
    """A runaway guard, computed with slack rather than tuned.

    The same arithmetic `recursion_limit` used, kept because the reasoning was
    right: a stage legitimately costs (retries + reworks) trips around
    execute → verify → review, a project has many stages, and every planner
    intervention adds a lap. What changes is what happens at the ceiling —
    an escalation the operator can read, not a framework error.
    """
    per_stage_nodes = 5  # plan, precheck, execute, verify, review/advance
    loops = max_test_retries + max_rework_retries + 1
    stage_cost = per_stage_nodes * loops
    return 100 + max_stages * stage_cost + max_planner_interventions * stage_cost


def drive(
    rt,
    state: dict,
    nodes: dict[str, Callable] | None = None,
    edges: dict[str, list[str]] | None = None,
    entry: Callable[[dict], str] = resume_entry_point,
    checkpoint: Callable | None = None,
    max_steps: int = 100_000,
) -> dict:
    """Walk the graph from `entry` until a node routes to `end`.

    The checkpoint is written *after* each node returns, which is what makes a
    crash mid-node resume from the last completed one rather than from a
    half-applied update.
    """
    nodes = NODES if nodes is None else nodes
    edges = EDGES if edges is None else edges
    hop = entry(state)
    step = 0
    while True:
        if step >= max_steps:
            # Not an exception. The ceiling exists to catch a loop that is not
            # making progress, and an operator needs to be told that in the
            # run's own vocabulary — which means going through `escalate` like
            # every other stop, so the report and the exit code are the ones
            # they already know how to read.
            state = _merge(
                state,
                {
                    "next_hop": "escalate",
                    "escalation_reason": (
                        f"the run passed {max_steps} steps without finishing, "
                        "which means it is looping rather than progressing"
                    ),
                },
            )
            hop = "escalate"
            max_steps = step + 2  # let escalate and its exit run

        update = nodes[hop](state, rt)
        state = _merge(state, update)
        step += 1
        if checkpoint is not None and state.get("run_id"):
            checkpoint(state["run_id"], step, hop, state)

        nxt = _next(hop, state, edges)
        if nxt == "end":
            return state
        hop = nxt
