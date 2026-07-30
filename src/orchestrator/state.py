"""The run state carried through the graph and persisted by the checkpointer.

Everything here must stay JSON-serialisable: it is written to SQLite at every
super-step, and a resume reads it back in a fresh process.

One deviation from PLAN.md's sketch, for a reason the sketch itself implies:
the spec lists a single `attempt` counter but defines two limits,
`max_test_retries` and `max_rework_retries`. One counter cannot enforce two
budgets, so there are two — `verify_attempt` and `rework_attempt`.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Status = Literal["running", "complete", "escalated", "awaiting_human"]


class StageResult(TypedDict, total=False):
    id: str
    kind: str
    outcome: str
    commit_range: str | None
    wall_seconds: float
    test_seconds: float
    verify_retries: int
    rework_attempts: int
    flake_reruns: int
    failed_layer: str | None
    review_verdict: str | None
    review_summary: str | None
    review_issues: list[dict]
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int


class RunState(TypedDict, total=False):
    run_id: str
    config_path: str
    target_repo: str

    base_ref: str
    base_sha: str
    branch: str

    stage_ids: list[str]
    stage_index: int
    stage_start_sha: str
    stage_started_at: float

    verify_attempt: int
    rework_attempt: int
    flake_reruns: int
    test_seconds: float

    last_test_output: str | None
    failure_layer: str | None
    review_feedback: list[str]
    review_verdict: str | None
    review_summary: str | None

    stage_usage: dict[str, int]

    history: list[StageResult]
    status: Status
    escalation_reason: str | None

    # Set by each node, read by the conditional edges. Explicit routing keeps
    # the decision visible in the checkpoint rather than hidden in control
    # flow, which matters a lot when reconstructing why a run stopped.
    next_hop: str


def new_state(
    run_id: str,
    config_path: str,
    target_repo: str,
    base_ref: str,
    base_sha: str,
    branch: str,
    stage_ids: list[str],
) -> RunState:
    return RunState(
        run_id=run_id,
        config_path=config_path,
        target_repo=target_repo,
        base_ref=base_ref,
        base_sha=base_sha,
        branch=branch,
        stage_ids=stage_ids,
        stage_index=0,
        stage_start_sha="",
        stage_started_at=0.0,
        verify_attempt=0,
        rework_attempt=0,
        flake_reruns=0,
        test_seconds=0.0,
        last_test_output=None,
        failure_layer=None,
        review_feedback=[],
        review_verdict=None,
        review_summary=None,
        stage_usage={"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0},
        history=[],
        status="running",
        escalation_reason=None,
        next_hop="",
    )


def fresh_stage_fields() -> dict:
    """Counters that reset when a stage begins or completes."""
    return {
        "stage_start_sha": "",
        "stage_started_at": 0.0,
        "verify_attempt": 0,
        "rework_attempt": 0,
        "flake_reruns": 0,
        "test_seconds": 0.0,
        "failure_layer": None,
        "last_test_output": None,
        "review_feedback": [],
        "review_verdict": None,
        "review_summary": None,
        "stage_usage": {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0},
    }
