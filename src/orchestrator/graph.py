"""LangGraph wiring.

Deliberately thin. All the logic is in `nodes`; this module only binds the
runtime into each node, declares the edges, and owns the checkpointer. That
split means the loop's behaviour is testable without LangGraph, and a change
in its API surface touches one file.

The edges match PLAN.md's edge list exactly. Routing reads `next_hop` from
state, which each node sets.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from orchestrator import nodes
from orchestrator.runtime import Runtime
from orchestrator.state import RunState

NODES: dict[str, Callable] = {
    "precheck": nodes.precheck,
    "execute": nodes.execute,
    "gate": nodes.gate,
    "verify": nodes.verify,
    "review": nodes.review,
    "advance": nodes.advance,
    "finalize": nodes.finalize,
    "escalate": nodes.escalate,
}

# Where each node is allowed to send the run. Keeping this declarative makes
# it checkable against the spec's edge list rather than buried in lambdas.
EDGES: dict[str, list[str]] = {
    "precheck": ["execute", "gate", "escalate"],
    "execute": ["verify", "execute", "escalate"],
    "gate": ["end"],
    "verify": ["review", "advance", "execute", "escalate"],
    "review": ["advance", "execute", "escalate"],
    "advance": ["precheck", "finalize"],
    "finalize": ["end", "escalate"],
    "escalate": ["end"],
}


def entry_router(rt: Runtime) -> Callable[[RunState], str]:
    """Where a fresh invocation begins.

    A manual stage being resumed goes to `verify`: the human has been told
    what to do, and the orchestrator's only remaining job is to confirm the
    work landed green. Routing it back through `precheck` would re-enter
    `gate` and pause again without ever checking — and it would do that
    forever.

    That has to hold whether the run paused cleanly (`awaiting_human`) or
    escalated because the work was not there yet. Both are resumed the same
    way by a human who has since done something.
    """

    def route(state: RunState) -> str:
        if state.get("status") == "awaiting_human":
            return "verify"

        if state.get("resuming"):
            index = state.get("stage_index", 0)
            if index < len(rt.cfg.stages) and rt.cfg.stages[index].kind == "manual":
                return "verify"

        return "precheck"

    return route


def _router(allowed: list[str]) -> Callable[[RunState], str]:
    def route(state: RunState) -> str:
        hop = state.get("next_hop") or "escalate"
        if hop == "end":
            return END
        if hop not in allowed:
            # A node asking for an edge the spec does not have is a bug in the
            # node, and silently rerouting would hide it.
            raise RuntimeError(
                f"node routed to {hop!r}, which is not one of {allowed}"
            )
        return hop

    return route


def build_graph(rt: Runtime, checkpointer=None):
    builder = StateGraph(RunState)

    for name, fn in NODES.items():
        builder.add_node(name, _bind(fn, rt))

    builder.add_conditional_edges(START, entry_router(rt), ["precheck", "verify"])
    for name, allowed in EDGES.items():
        targets = [t for t in allowed if t != "end"]
        builder.add_conditional_edges(
            name, _router(allowed), targets + [END] if "end" in allowed else targets
        )

    return builder.compile(checkpointer=checkpointer)


def _bind(fn: Callable, rt: Runtime) -> Callable:
    def node(state: RunState) -> dict:
        return fn(state, rt)

    node.__name__ = fn.__name__
    return node


def open_checkpointer(db_path: Path | str) -> tuple[SqliteSaver, sqlite3.Connection]:
    """A SQLite checkpointer under this project's runs directory.

    The connection is returned so the caller owns its lifetime — a resume
    happens in a fresh process and must reopen the same file. `check_same_thread`
    is off because LangGraph may touch it from a worker thread.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn), conn


def recursion_limit(stage_count: int, max_test_retries: int, max_rework_retries: int) -> int:
    """LangGraph's default of 25 super-steps is far too low here.

    A single stage can legitimately cost (retries + reworks) trips around
    execute → verify → review, and a run has many stages. Exhausting the limit
    would surface as an opaque framework error rather than an escalation.
    """
    per_stage_nodes = 4  # precheck, execute, verify, review/advance
    loops = max_test_retries + max_rework_retries + 1
    return 50 + stage_count * per_stage_nodes * loops
