"""LangGraph wiring.

Deliberately thin. All the logic is in `nodes`; this module binds the runtime
into each node, declares the edges, and owns the checkpointer — so the loop's
behaviour is testable without LangGraph, and a change in its API surface touches
one file.

The edges match PLAN.md's edge list exactly. Note what is largely *absent*:
paths straight to `escalate`. That is the point of the design.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from orchestrator import nodes
from orchestrator.runtime import Runtime
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
    # Not `execute` — the node routes to `verify` when it ran and to `plan`
    # when a context command failed, and never back to itself. A retry is the
    # graph re-entering `execute` from `verify` or `review`, not a self-loop.
    "execute": ["verify", "plan"],
    "verify": ["review", "advance", "execute", "plan", "escalate"],
    "review": ["advance", "execute", "plan"],
    "advance": ["plan"],
    "finalize": ["end", "escalate"],
    "escalate": ["end"],
}

ENTRY_POINTS = ["plan", "precheck", "verify"]


def _router(allowed: list[str]) -> Callable[[RunState], str]:
    def route(state: RunState) -> str:
        hop = state.get("next_hop") or "escalate"
        if hop == "end":
            return END
        if hop not in allowed:
            # A node asking for an edge the spec does not have is a bug in the
            # node; silently rerouting would hide it.
            raise RuntimeError(f"node routed to {hop!r}, not one of {allowed}")
        return hop

    return route


def build_graph(rt: Runtime, checkpointer=None):
    builder = StateGraph(RunState)

    for name, fn in NODES.items():
        builder.add_node(name, _bind(fn, rt))

    builder.add_conditional_edges(START, resume_entry_point, ENTRY_POINTS)
    for name, allowed in EDGES.items():
        targets = [t for t in allowed if t != "end"]
        builder.add_conditional_edges(
            name,
            _router(allowed),
            targets + [END] if "end" in allowed else targets,
        )

    return builder.compile(checkpointer=checkpointer)


def _bind(fn: Callable, rt: Runtime) -> Callable:
    def node(state: RunState) -> dict:
        return fn(state, rt)

    node.__name__ = fn.__name__
    return node


def open_checkpointer(db_path: Path | str) -> tuple[SqliteSaver, sqlite3.Connection]:
    """A SQLite checkpointer under the project's runs directory.

    The connection is returned so the caller owns its lifetime — a resume
    happens in a fresh process and must reopen the same file.
    `check_same_thread` is off because LangGraph may touch it from a worker.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn), conn


def recursion_limit(
    max_stages: int,
    max_test_retries: int,
    max_rework_retries: int,
    max_planner_interventions: int,
) -> int:
    """LangGraph's default of 25 super-steps is far too low here.

    A stage can legitimately cost (retries + reworks) trips around
    execute → verify → review, a project has many stages, and every planner
    intervention adds a lap. Exhausting the limit surfaces as an opaque
    framework error rather than an escalation — the one failure mode this tool
    must not have — so the ceiling is computed with slack rather than tuned.
    """
    per_stage_nodes = 5  # plan, precheck, execute, verify, review/advance
    loops = max_test_retries + max_rework_retries + 1
    stage_cost = per_stage_nodes * loops
    return 100 + max_stages * stage_cost + max_planner_interventions * stage_cost
