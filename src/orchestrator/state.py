"""The run state carried through the graph and persisted by the checkpointer.

Everything here stays JSON-serialisable: it is written to SQLite at every
super-step, and a resume reads it back in a fresh process.

Two properties are load-bearing.

**`completed` is append-only and holds completions only.** It is the prefix of
the reviewer's prompt, so it must never be rewritten — a planner inserting a
stage appends, it does not renumber. And an escalation records which stage
stopped the run in a separate field: appending escalations would make a stage
that escalated, got fixed, and completed appear twice with contradictory
outcomes, and would break the prefix at the same time.

**Two retry counters.** `max_test_retries` and `max_rework_retries` are
separate budgets and one counter cannot enforce both.
"""

from __future__ import annotations

from typing import Literal, TypedDict

Status = Literal["running", "complete", "escalated"]

FailureLayer = Literal[
    "precondition",
    "setup",
    "branch",
    "scope",
    "patterns",
    "tests",
    "checks",
    "new_tests",
    "progress",
    "paused",
    "review",
    "full_suite",
    "planner",
]

# Which failures mean "the repo changed and needs re-checking" versus "the plan
# needs revisiting". `resume` routes on this: re-entering at precheck after an
# escalation would re-run the stage and discard the human's fix.
REPO_STATE_FAILURES = frozenset(
    {"setup", "branch", "scope", "patterns", "tests", "checks", "new_tests",
     "progress", "review", "full_suite"}
)
PLANNING_FAILURES = frozenset({"precondition", "planner"})


class StageResult(TypedDict, total=False):
    id: str
    kind: str
    index: int
    revisions: int
    verify_retries: int
    rework_attempts: int
    flake_reruns_iteration: int
    flake_reruns_review_gate: int
    instruction: str
    base_sha: str
    merge_sha: str
    wall_seconds: float
    test_seconds: float
    review_verdict: str | None
    review_summary: str | None
    review_issues: list[dict]
    verify_failures: list[str]
    planner_notes: list[str]
    config_hash: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    planner_prompt_tokens: int
    planner_cached_tokens: int
    planner_completion_tokens: int


class FailureDetail(TypedDict, total=False):
    """What the planner needs at an intervention.

    An exit code is not enough: which tests failed and where they live is what
    distinguishes "widen this stage by two files" from "we skipped a
    prerequisite, insert a stage before this one".
    """

    layer: str
    summary: str
    detail: str
    out_of_scope_paths: list[str]
    failing_paths: list[str]


class RunState(TypedDict, total=False):
    run_id: str
    project_slug: str
    config_hash: str
    target_repo: str

    base_ref: str
    base_sha: str
    project_branch: str
    stage_branch: str | None

    completed: list[StageResult]
    current: dict | None          # the pending Stage, as a plain dict
    stage_index: int
    revision: int
    verify_attempt: int
    rework_attempt: int

    stage_start_sha: str
    stage_started_at: float
    started_at: float
    # `wall_clock_hours` bounds one unattended session, not a project's total
    # elapsed time. A run escalated at midnight and resumed after breakfast has
    # not spent the night working, and measuring from `started_at` would refuse
    # to resume it. `resume` therefore starts a fresh session clock.
    session_started_at: float
    # Frozen when the run stops, so `status` on an old run reports the hours it
    # took rather than the hours since.
    session_seconds: float

    last_failure: FailureDetail | None
    failure_layer: str | None
    failed_stage_id: str | None

    flake_reruns: int
    flake_reruns_review_gate: int
    # Files excused as suite flakes, accumulated across the run. A count alone
    # tells the operator there is a problem and nothing about where; these names
    # are the path back to a suite that does not need the excusing.
    flaky_files: list[str]
    test_seconds: float
    # Fingerprint of the last attempt's diff. An attempt that reproduces it
    # exactly has made no progress, and retrying costs a review for nothing.
    last_diff_digest: str

    planner_interventions: int
    # Plan steps the planner took out of order. Union-merged and never dropped
    # by omission: silent loss is the failure this exists to prevent.
    deferred: list[dict]
    planner_notes: list[str]
    review_feedback: list[str]
    review_verdict: str | None
    review_summary: str | None

    stage_usage: dict[str, int]
    run_usage: dict[str, int]

    status: Status
    escalation_reason: str | None
    resuming: bool
    # Set by `resume` from the stage branch: does the interrupted stage already
    # have commits? Decides whether an interrupted attempt is re-run or checked.
    stage_has_work: bool
    next_hop: str


def new_state(
    run_id: str,
    project_slug: str,
    config_hash: str,
    target_repo: str,
    base_ref: str,
    base_sha: str,
    project_branch: str,
    started_at: float,
) -> RunState:
    return RunState(
        run_id=run_id,
        project_slug=project_slug,
        config_hash=config_hash,
        target_repo=target_repo,
        base_ref=base_ref,
        base_sha=base_sha,
        project_branch=project_branch,
        stage_branch=None,
        completed=[],
        current=None,
        stage_index=0,
        revision=0,
        verify_attempt=0,
        rework_attempt=0,
        stage_start_sha="",
        stage_started_at=0.0,
        started_at=started_at,
        session_started_at=started_at,
        last_diff_digest="",
        last_failure=None,
        failure_layer=None,
        failed_stage_id=None,
        flake_reruns=0,
        flake_reruns_review_gate=0,
        flaky_files=[],
        test_seconds=0.0,
        planner_interventions=0,
        deferred=[],
        planner_notes=[],
        review_feedback=[],
        review_verdict=None,
        review_summary=None,
        stage_usage=_zero_usage(),
        run_usage=_zero_usage(),
        status="running",
        escalation_reason=None,
        resuming=False,
        next_hop="",
    )


def _zero_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "cached_tokens": 0,
        "completion_tokens": 0,
        "planner_prompt_tokens": 0,
        "planner_cached_tokens": 0,
        "planner_completion_tokens": 0,
    }


def fresh_stage_fields() -> dict:
    """Counters and per-stage scratch that reset when a stage completes.

    Not `revision`: that belongs to the stage being replaced, and `plan` sets it
    when it derives or revises.
    """
    return {
        "stage_branch": None,
        "stage_start_sha": "",
        "stage_started_at": 0.0,
        "verify_attempt": 0,
        "rework_attempt": 0,
        "flake_reruns": 0,
        "flake_reruns_review_gate": 0,
        "test_seconds": 0.0,
        "last_diff_digest": "",
        "last_failure": None,
        "failure_layer": None,
        "failed_stage_id": None,
        "review_feedback": [],
        "review_verdict": None,
        "review_summary": None,
        "planner_notes": [],
        "stage_usage": _zero_usage(),
    }


def fresh_revision_fields() -> dict:
    """Counters that reset when the planner revises a stage.

    The stage keeps its identity and its accumulated review feedback history is
    cleared, because the instruction it was rejected against no longer applies.
    """
    return {
        "verify_attempt": 0,
        "rework_attempt": 0,
        # A redrawn stage is a different instruction, so reproducing the old
        # diff under it is not evidence of being stuck.
        "last_diff_digest": "",
        "last_failure": None,
        "failure_layer": None,
        "review_feedback": [],
        "review_verdict": None,
        "review_summary": None,
    }


def merge_deferrals(existing: list[dict], reported: list[dict]) -> list[dict]:
    """Union by plan_step, keeping insertion order.

    Never a replacement. The planner sends what it currently believes is
    outstanding, and a call that omits one must not delete it — a model
    forgetting is exactly the failure this structure exists to prevent.
    Resolution is explicit instead, via the `resolved` flag.

    Order is preserved because this is rendered into a cached prompt prefix,
    and reshuffling it would invalidate the cache for no reason.
    """
    merged = [dict(entry) for entry in existing]
    by_step = {entry.get("plan_step"): entry for entry in merged}

    for entry in reported:
        step = entry.get("plan_step")
        if not step:
            continue
        if step in by_step:
            by_step[step].update(entry)
        else:
            new = dict(entry)
            merged.append(new)
            by_step[step] = new
    return merged


def outstanding_deferrals(entries: list[dict] | None) -> list[dict]:
    """Those not yet marked resolved."""
    return [e for e in (entries or []) if not e.get("resolved")]


def accumulate_usage(current: dict[str, int] | None, **deltas: int) -> dict[str, int]:
    out = dict(current or _zero_usage())
    for key, value in deltas.items():
        out[key] = out.get(key, 0) + value
    return out


def resume_entry_point(state: RunState) -> str:
    """Where a resumed run re-enters the graph.

    A human has changed something since the escalation. Which node to enter
    depends on what they were asked to fix — check the repo, or re-plan against
    an edited plan document. Anything else re-runs the stage from the top and
    throws the fix away.
    """
    if not state.get("resuming"):
        return "precheck" if state.get("current") else "plan"

    layer = state.get("failure_layer")
    if layer in PLANNING_FAILURES:
        return "plan"
    if layer in REPO_STATE_FAILURES:
        return "verify"
    if layer in ("budget", "paused"):
        # Neither is a defect in the repository or the plan, so neither is in
        # either set. But a stage may have been awaiting revision when the stop
        # came, and precheck would re-run it unrevised and discard the
        # diagnosis. Hand it back to the planner.
        return "plan"
    # Interrupted mid-run with no recorded failure. Whether there is anything
    # to verify depends on whether the executor got far enough to commit: the
    # caller sets `stage_has_work` from the stage branch. With work on the
    # branch, verify it — running the executor again over a finished stage is
    # how a completed stage spent ten minutes looping, with nothing left to do
    # and no way for the model to say so. Without work, pick up where the
    # stage was.
    if state.get("current"):
        return "verify" if state.get("stage_has_work") else "precheck"
    return "plan"
