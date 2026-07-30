"""Node logic.

Each function takes (state, runtime) and returns a partial state update — the
contract LangGraph expects, but with the runtime passed explicitly so every node
is callable from a test with a stubbed planner, executor, and reviewer.

The shape of the loop is three escalation tiers. A failure goes back to the
executor if the executor can plausibly fix it, back to the planner if the
*stage* was drawn wrongly, and to a human only for a broken environment, a
containment breach, or something the planner could not fix. The design goal is
that a run stops for a good reason or not at all.

`next_hop` is set here and read by the graph's conditional edges. Routing lives
in state rather than control flow so a checkpoint records not just where a run
stopped but which way it was about to go.
"""

from __future__ import annotations

import json
import time

from orchestrator.config import Stage, validate_stage
from orchestrator.globs import matches_any
from orchestrator.planner import append_status
from orchestrator.prompts import (
    build_executor_prompt,
    build_planner_messages,
    build_review_messages,
)
from orchestrator.reviewer import issues_as_feedback
from orchestrator.runtime import Runtime
from orchestrator.state import (
    RunState,
    accumulate_usage,
    fresh_revision_fields,
    fresh_stage_fields,
    merge_deferrals,
    outstanding_deferrals,
)
from orchestrator.verify import Layer, Route, run_verify

STATUS_TAIL_CHARS = 4_000


def current_stage(state: RunState, rt: Runtime) -> Stage | None:
    fields = state.get("current")
    return Stage(**fields) if fields else None


def _attempt(state: RunState) -> int:
    return state.get("verify_attempt", 0) + state.get("rework_attempt", 0)


# --- plan ----------------------------------------------------------------


def plan(state: RunState, rt: Runtime) -> dict:
    """Derive the next stage, or revise the one that just failed."""
    stage = current_stage(state, rt)
    limits = rt.cfg.limits

    if stage is not None and state.get("planner_interventions", 0) >= limits.max_planner_interventions:
        return _escalate(
            "planner",
            f"The planner's global budget is exhausted "
            f"({limits.max_planner_interventions} interventions). The last "
            f"failure was: {(state.get('last_failure') or {}).get('summary')}",
        )

    if len(state.get("completed") or []) >= limits.max_stages:
        return _escalate(
            "planner",
            f"The run reached max_stages ({limits.max_stages}) without the "
            "planner declaring the project complete. Either the plan is larger "
            "than the cap or the planner is not converging.",
        )

    overrun = _wall_clock_overrun(state, limits)
    if overrun is not None:
        return _escalate("budget", overrun)

    # Checked here, with the budgets, and for the same reason: this is the
    # point where the run is between stages with nothing in flight. Stopping
    # anywhere else means a half-finished executor and a dirty tree.
    if rt.paths.pause_flag.exists():
        note = rt.paths.pause_flag.read_text().strip()
        return _escalate(
            "paused",
            "Paused at your request, between stages. Nothing is wrong and "
            "nothing is half-done: everything that landed is on the project "
            "branch and no stage was in flight.\n\n"
            + (f"Your note: {note}\n\n" if note else "")
            + f"`orchestrator resume {state.get('run_id')}` picks up from the "
            "next stage.",
        )

    messages = build_planner_messages(
        cfg=rt.cfg,
        plan=rt.plan,
        completed=state.get("completed") or [],
        current_stage=stage,
        failure=state.get("last_failure"),
        revision=state.get("revision", 0),
        interventions_used=state.get("planner_interventions", 0),
        interventions_max=limits.max_planner_interventions,
        status_tail=_status_tail(rt),
        layout=rt.layout(state.get("base_sha") or ""),
        deferred=state.get("deferred") or [],
    )

    rt.log(f"[plan] {'revising ' + stage.id if stage else 'deriving next stage'}")
    outcome = rt.planner.plan(messages)

    usage = accumulate_usage(
        state.get("run_usage"),
        planner_prompt_tokens=outcome.usage.prompt_tokens,
        planner_cached_tokens=outcome.usage.cached_tokens,
        planner_completion_tokens=outcome.usage.completion_tokens,
    )

    append_status(
        rt.project.project_dir,
        stage_index=state.get("stage_index", 0),
        stage_id=stage.id if stage else None,
        revision=state.get("revision", 0),
        verdict=outcome.verdict,
        entry=outcome.status_entry,
        reasoning=outcome.reasoning,
    )
    rt.write_artifact(
        state.get("stage_index", 0),
        stage.id if stage else "plan",
        state.get("revision", 0),
        _attempt(state),
        "planner.json",
        json.dumps(
            {
                "verdict": outcome.verdict,
                "reasoning": outcome.reasoning,
                "revision_mode": outcome.revision_mode,
                "stage": outcome.stage_fields,
                "usage": {
                    "prompt_tokens": outcome.usage.prompt_tokens,
                    "cached_tokens": outcome.usage.cached_tokens,
                    "completion_tokens": outcome.usage.completion_tokens,
                },
                "client_failure": outcome.failed,
            },
            indent=2,
        ),
    )

    notes = list(state.get("planner_notes") or [])
    notes.append(f"{outcome.verdict}: {outcome.reasoning}")
    deferred = merge_deferrals(state.get("deferred") or [], outcome.deferred)
    base = {"run_usage": usage, "planner_notes": notes, "deferred": deferred}

    if outcome.verdict == "project_complete":
        still_open = outstanding_deferrals(deferred)
        if still_open:
            rt.log(
                f"[plan] project complete, with {len(still_open)} deferred step(s) "
                "outstanding"
            )
        else:
            rt.log("[plan] project complete")
        return {**base, "next_hop": "finalize"}

    if outcome.verdict == "blocked":
        return {
            **base,
            **_escalate("planner", f"The planner blocked the run: {outcome.reasoning}"),
        }

    new_stage = rt.cfg.stage_from_planner(outcome.stage_fields or {})
    problems = validate_stage(new_stage, rt.cfg)
    if problems:
        # A malformed spec is the planner's error to fix, but it does not get
        # to burn the budget on it indefinitely — the intervention still counts.
        return {
            **base,
            **_escalate(
                "planner",
                "The planner produced a stage that failed validation:\n"
                + "\n".join(f"- {p}" for p in problems),
            ),
        }

    if outcome.verdict == "revise":
        interventions = state.get("planner_interventions", 0) + 1
        keep_branch = outcome.revision_mode == "extend"
        rt.log(
            f"[plan] revising {new_stage.id} (revision "
            f"{state.get('revision', 0) + 1}, {outcome.revision_mode})"
        )

        update = {
            **base,
            **fresh_revision_fields(),
            "current": new_stage.model_dump(),
            "revision": state.get("revision", 0) + 1,
            "planner_interventions": interventions,
            "next_hop": "precheck",
        }

        if not keep_branch:
            # The approach was wrong: discard the branch and re-cut from the
            # project tip on the way through precheck.
            update["stage_branch"] = None
            update["stage_start_sha"] = ""
        else:
            # Scope was merely too narrow. Anything the planner declined to
            # adopt is reverted; the rest of the stage's work survives.
            _revert_unadopted(state, rt, new_stage)

        return update

    # next_stage
    rt.log(f"[plan] next stage: {new_stage.id}")
    index = state.get("stage_index", 0)
    if stage is not None:
        # A predecessor inserted in front of a failing stage takes its slot; the
        # failing stage's work is abandoned rather than half-merged.
        interventions = state.get("planner_interventions", 0) + 1
    else:
        interventions = state.get("planner_interventions", 0)

    return {
        **base,
        **fresh_stage_fields(),
        "current": new_stage.model_dump(),
        "revision": 0,
        "stage_index": index,
        "planner_interventions": interventions,
        "next_hop": "precheck",
    }


def _revert_unadopted(state: RunState, rt: Runtime, revised: Stage) -> None:
    """Revert out-of-scope paths the planner chose not to adopt.

    The child branch is the quarantine, so containment never required
    destroying work. If the revised stage widened `edit_files` to cover a path,
    the existing work on it stands. If not, that path alone goes back to the
    stage baseline — and the rest of the stage's work is untouched.
    """
    failure = state.get("last_failure") or {}
    offending = failure.get("out_of_scope_paths") or []
    if not offending:
        return

    unadopted = [p for p in offending if not matches_any(p, revised.edit_files)]
    if not unadopted:
        rt.log("[plan] planner adopted every out-of-scope path; work stands")
        return

    rt.log(f"[plan] reverting {len(unadopted)} unadopted path(s)")
    rt.git.revert_paths(state["stage_start_sha"], unadopted)


def _status_tail(rt: Runtime) -> str | None:
    path = rt.project.status
    if not path.is_file():
        return None
    text = path.read_text()
    return text[-STATUS_TAIL_CHARS:] if len(text) > STATUS_TAIL_CHARS else text


# --- precheck ------------------------------------------------------------


def precheck(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    if stage is None:  # pragma: no cover - graph never routes here without one
        return {"next_hop": "plan"}

    update: dict = {}
    rt.log(f"[precheck] stage {stage.id} revision {state.get('revision', 0)}")

    for command in stage.preconditions:
        result = rt.runner.run(command)
        if not result.ok:
            # An unmet precondition is an ordering problem the planner owns —
            # but note it cannot rewrite the precondition itself, since those
            # are operator-only. The escalation must say so plainly.
            return {
                **update,
                **_planner_failure(
                    state,
                    "precondition",
                    f"precondition never passed: {command}",
                    f"{result.summary()}\n{result.output}",
                ),
            }

    setup = stage.effective_setup_command(rt.cfg)
    if setup:
        # Before the executor, not only before verify: the executor runs the
        # test command itself and cannot be handed a stale environment.
        result = rt.runner.run(setup)
        if not result.ok:
            return {
                **update,
                **_escalate(
                    "setup",
                    f"Environment setup failed before stage {stage.id!r}. A "
                    "broken environment is not a planning defect.\n"
                    f"{result.summary()}\n{result.output}",
                ),
            }

    # Cut or resume the child branch. Anything on it is quarantined: nothing
    # reaches the project branch without passing the review gate.
    if not state.get("stage_branch"):
        branch = rt.cfg.stage_branch(state.get("stage_index", 0), stage.id)
        start = rt.git.cut_stage_branch(branch, rt.cfg.project_branch)
        update["stage_branch"] = branch
        update["stage_start_sha"] = start
        update["stage_started_at"] = time.time()
        rt.log(f"[precheck] cut {branch} at {start[:12]}")

    update["next_hop"] = "execute"
    return update


# --- execute -------------------------------------------------------------


def execute(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    attempt = _attempt(state)
    feedback = list(state.get("review_feedback") or [])

    if stage.kind == "script":
        rt.log(f"[execute] {stage.id}: script")
        result = rt.executor.run_script_stage(stage)
    else:
        context, context_results = rt.executor.gather_context(stage)
        failed = [r for r in context_results if not r.ok]
        if failed:
            return _planner_failure(
                state,
                "precondition",
                "a context command failed, so the executor prompt would have "
                "been built from missing information",
                f"{failed[0].summary()}\n{failed[0].output}",
            )

        prompt = build_executor_prompt(
            stage, rt.cfg, context=context, feedback=feedback
        )
        rt.write_artifact(
            state["stage_index"], stage.id, state.get("revision", 0), attempt,
            "prompt.md", prompt,
        )
        rt.log(f"[execute] {stage.id}: attempt {attempt}")
        # Aider's scratch files go beside this attempt's other artifacts rather
        # than into the repository under test, where they would fail the scope
        # gate. As a side effect the model's actual conversation is preserved
        # per attempt, which is the first thing worth reading when a local
        # model does something inexplicable.
        history_dir = rt.paths.attempt_dir(
            state["stage_index"], stage.id, state.get("revision", 0), attempt
        )
        history_dir.mkdir(parents=True, exist_ok=True)
        result = rt.executor.run_agent_stage(stage, prompt, history_dir=history_dir)

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "executor.log", result.log,
    )

    if result.ok:
        return {"next_hop": "verify"}

    if result.timed_out:
        what = "timed out"
        advice = ""
    elif result.unapplied_edit:
        # Precision matters here: the model wrote plenty, in a shape the editor
        # could not apply. Telling it "you produced no changes" invites it to
        # write the same thing again, louder.
        what = "could not apply the model's reply"
        advice = (
            "\n\nThe model's response was not in a form the editor could turn "
            "into a file edit — it produced text, not an applicable change. "
            "Restate the edit in the exact format the editor expects, naming "
            "the file before each block."
        )
    else:
        what = "exited non-zero"
        advice = ""

    return _retry_or_plan(
        state,
        rt,
        layer="tests",
        summary=f"the executor {what}",
        feedback=f"The previous attempt's executor {what}:\n{result.log}{advice}",
        detail=result.log,
    )


# --- verify --------------------------------------------------------------


def verify(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    attempt = _attempt(state)

    outcome = run_verify(
        stage=stage,
        cfg=rt.cfg,
        git=rt.git,
        runner=rt.runner,
        stage_start_sha=state["stage_start_sha"],
        stage_branch=state.get("stage_branch"),
        project_branch=state.get("project_branch"),
        base_ref=state.get("base_ref"),
        base_sha=state.get("base_sha"),
        previous_diff_digest=state.get("last_diff_digest") or None,
    )

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "verify.log",
        _verify_log(outcome),
    )

    accumulated = {
        "flake_reruns": state.get("flake_reruns", 0) + outcome.flake_reruns,
        "test_seconds": state.get("test_seconds", 0.0) + outcome.test_seconds,
        "last_diff_digest": outcome.diff_digest,
    }

    if outcome.passed:
        rt.log(f"[verify] {stage.id}: all gates passed")
        return {
            **accumulated,
            "failure_layer": None,
            "next_hop": "review" if stage.review else "advance",
        }

    layer = outcome.failed_layer.value if outcome.failed_layer else "tests"
    rt.log(f"[verify] {stage.id}: failed at {layer} ({outcome.route})")

    if outcome.route is Route.HUMAN:
        return {
            **accumulated,
            **_escalate(layer, f"{outcome.summary}\n\n{outcome.feedback}"),
        }

    if outcome.route is Route.PLANNER:
        return {
            **accumulated,
            **_planner_failure(
                state,
                layer,
                outcome.summary,
                outcome.feedback,
                out_of_scope_paths=outcome.out_of_scope_paths,
                failing_paths=outcome.failing_paths,
            ),
        }

    return {
        **accumulated,
        **_retry_or_plan(
            state,
            rt,
            layer=layer,
            summary=outcome.summary,
            feedback=outcome.feedback,
            detail=outcome.feedback,
            failing_paths=outcome.failing_paths,
        ),
    }


# --- review --------------------------------------------------------------


def review(state: RunState, rt: Runtime) -> dict:
    """The composite merge gate: reviewer approval *and* a green full suite.

    Cheapest first, short-circuiting. The reviewer call is seconds and pennies;
    a full suite is minutes. A stage the reviewer would reject never pays for a
    suite run, and because the suite runs only after approval it costs one
    execution per stage that lands — linear in stages, not in attempts.
    """
    stage = current_stage(state, rt)
    attempt = _attempt(state)

    diff = rt.git.diff(state["stage_start_sha"])
    messages = build_review_messages(
        stage=stage,
        cfg=rt.cfg,
        diff=diff,
        plan=rt.plan,
        completed=state.get("completed") or [],
    )

    rt.log(f"[review] {stage.id}: calling reviewer")
    # Keyed by project rather than by run: successive runs and resumes share
    # the same plan snapshot prefix, so they should share the same cache.
    outcome = rt.reviewer.review(
        messages, cache_key=f"orchestrator:{state.get('project_slug') or 'project'}"
    )

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "review.json", json.dumps(outcome.as_dict(), indent=2),
    )

    usage = accumulate_usage(
        state.get("run_usage"),
        prompt_tokens=outcome.usage.prompt_tokens,
        cached_tokens=outcome.usage.cached_tokens,
        completion_tokens=outcome.usage.completion_tokens,
    )
    stage_usage = accumulate_usage(
        state.get("stage_usage"),
        prompt_tokens=outcome.usage.prompt_tokens,
        cached_tokens=outcome.usage.cached_tokens,
        completion_tokens=outcome.usage.completion_tokens,
    )
    rt.log(
        f"[review] {stage.id}: {outcome.verdict} — {outcome.summary} "
        f"({outcome.usage.prompt_tokens} prompt, {outcome.usage.cached_tokens} cached)"
    )

    base = {
        "run_usage": usage,
        "stage_usage": stage_usage,
        "review_verdict": outcome.verdict,
        "review_summary": outcome.summary,
    }

    if outcome.verdict == "blocked":
        # Not a human's problem: with a planner in the loop, "the instruction is
        # wrong" is a planning problem with a planning fix.
        return {
            **base,
            **_planner_failure(
                state,
                "review",
                f"the reviewer blocked the stage: {outcome.summary}",
                "\n".join(
                    f"- [{i.severity}] {i.file}: {i.description}"
                    for i in outcome.issues
                ),
            ),
        }

    if outcome.verdict == "rework":
        feedback = list(state.get("review_feedback") or [])
        feedback.append(issues_as_feedback(outcome.summary, outcome.issues))
        return {
            **base,
            **_rework_or_plan(state, rt, feedback, outcome.summary),
        }

    # Approved. Now the expensive half.
    if not stage.full_suite_required(rt.cfg) or not rt.cfg.full_test_command:
        return {**base, "next_hop": "advance"}

    rt.log(f"[review] {stage.id}: approved; running the full suite")
    result = rt.runner.run(rt.cfg.full_test_command)
    seconds = result.duration_seconds

    if not result.ok:
        # Same re-run-once rule as iteration. It matters more here: the full
        # suite has far more surface for ordering and timing flakes, and a flake
        # at this gate wastes a planner intervention rather than an executor
        # attempt.
        rerun = rt.runner.run(rt.cfg.full_test_command)
        seconds += rerun.duration_seconds
        if rerun.ok:
            rt.log(f"[review] {stage.id}: full suite flaked, passed on re-run")
            return {
                **base,
                "test_seconds": state.get("test_seconds", 0.0) + seconds,
                "flake_reruns_review_gate": state.get("flake_reruns_review_gate", 0) + 1,
                "next_hop": "advance",
            }

        feedback = list(state.get("review_feedback") or [])
        feedback.append(
            "The reviewer approved this stage but the full suite failed, so it "
            f"cannot land:\n{rerun.summary()}\n{rerun.output}"
        )
        return {
            **base,
            "test_seconds": state.get("test_seconds", 0.0) + seconds,
            **_rework_or_plan(
                state, rt, feedback, "approved but the full suite was red",
                layer="full_suite",
            ),
        }

    return {
        **base,
        "test_seconds": state.get("test_seconds", 0.0) + seconds,
        "next_hop": "advance",
    }


# --- advance -------------------------------------------------------------


def advance(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    start_sha = state["stage_start_sha"]
    branch = state["stage_branch"]

    # Commit anything the executor left uncommitted, then squash the whole
    # child branch onto the project branch as one commit. Aider's intermediate
    # commits — some of them red, since it commits before testing — are
    # discarded by the squash. That is why "every commit on the project branch
    # is green" and "Aider commits before testing" are both true.
    rt.git.commit_all(f"[{stage.id}] wip")
    merge_sha = rt.git.squash_merge(
        branch, rt.cfg.project_branch, f"[{stage.id}] {_first_line(stage)}"
    )
    rt.git.delete_branch(branch)

    usage = state.get("stage_usage") or {}
    result = {
        "id": stage.id,
        "kind": stage.kind,
        "index": state["stage_index"],
        "revisions": state.get("revision", 0),
        "verify_retries": state.get("verify_attempt", 0),
        "rework_attempts": state.get("rework_attempt", 0),
        "flake_reruns_iteration": state.get("flake_reruns", 0),
        "flake_reruns_review_gate": state.get("flake_reruns_review_gate", 0),
        "instruction": stage.instruction or "",
        "base_sha": start_sha,
        "merge_sha": merge_sha or rt.git.head_sha(),
        "wall_seconds": max(time.time() - (state.get("stage_started_at") or 0), 0.0),
        "test_seconds": state.get("test_seconds", 0.0),
        "review_verdict": state.get("review_verdict"),
        "review_summary": state.get("review_summary"),
        "verify_failures": [],
        "planner_notes": list(state.get("planner_notes") or []),
        "config_hash": state.get("config_hash", ""),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }

    completed = list(state.get("completed") or [])
    completed.append(result)
    rt.log(f"[advance] {stage.id} landed as {result['merge_sha'][:12]}")

    return {
        **fresh_stage_fields(),
        "completed": completed,
        "current": None,
        "stage_index": state["stage_index"] + 1,
        "revision": 0,
        "next_hop": "plan",
    }


# --- finalize ------------------------------------------------------------


def finalize(state: RunState, rt: Runtime) -> dict:
    command = rt.cfg.full_test_command
    if not command:
        return {"status": "complete", "next_hop": "end", **_session_elapsed(state)}

    rt.log("[finalize] running the full suite on the project branch tip")
    result = rt.runner.run(command)
    if result.ok:
        return {"status": "complete", "next_hop": "end", **_session_elapsed(state)}

    return {
        **_escalate(
            "full_suite",
            "Every stage passed on its own, but the full suite failed on the "
            f"project branch tip:\n{result.summary()}\n{result.output}",
        )
    }


# --- escalate ------------------------------------------------------------


def escalate(state: RunState, rt: Runtime) -> dict:
    reason = state.get("escalation_reason") or "escalated without a recorded reason"
    rt.log(f"[escalate] {reason}")
    stage = current_stage(state, rt)
    return {
        "status": "escalated",
        "failed_stage_id": stage.id if stage else None,
        "next_hop": "end",
        **_session_elapsed(state),
    }


# --- helpers -------------------------------------------------------------


def _verify_log(outcome) -> str:
    """What the gates decided, and why.

    Built from the verdict first and command output second. The scope, pattern
    and branch-identity gates fail without running anything, so a log built
    only from command results is empty for exactly the failures hardest to
    diagnose afterwards — which is how the first live run left a human reading
    planner.json to find out which regex had matched.
    """
    parts: list[str] = []
    if outcome.passed:
        parts.append("all gates passed")
    else:
        layer = getattr(outcome.failed_layer, "value", outcome.failed_layer)
        route = getattr(outcome.route, "value", outcome.route)
        parts.append(f"FAILED at layer {layer!r} (routed to {route})")
        if outcome.summary:
            parts.append(outcome.summary)
        if outcome.feedback:
            parts.append(outcome.feedback)

    for result in outcome.results:
        parts.append(f"{result.summary()}\n{result.output}")

    return "\n\n".join(p for p in parts if p)


def _session_elapsed(state: RunState) -> dict:
    """Freeze how long this session ran, at the point it stopped.

    Recorded rather than computed at report time so that `status` on a run from
    last week reports the hours it actually took, not the hours since.
    """
    started = state.get("session_started_at") or state.get("started_at") or 0.0
    if not started:
        return {}
    return {"session_seconds": max(time.time() - started, 0.0)}


def _wall_clock_overrun(state: RunState, limits) -> str | None:
    """The session's time budget, checked before any further paid work.

    Enforced at `plan` rather than inside a stage, which means the deadline can
    be overshot by at most one stage. That is deliberate: killing an executor
    mid-attempt would leave a child branch dangling and throw away work that may
    be minutes from landing. The rule is "no new planner call past the
    deadline", not "stop mid-sentence".

    Measured from `session_started_at`, so resuming an escalated run gets a
    fresh budget rather than inheriting the hours a human spent asleep.
    """
    budget_hours = limits.wall_clock_hours
    if not budget_hours or budget_hours <= 0:
        return None

    # Fall back to the run start for checkpoints written before the session
    # clock existed; a missing value must not read as the epoch.
    started = state.get("session_started_at") or state.get("started_at") or 0.0
    if not started:
        return None

    elapsed_hours = (time.time() - started) / 3600.0
    if elapsed_hours < budget_hours:
        return None

    reason = (
        f"The session reached its wall_clock_hours budget "
        f"({budget_hours:g}h; {elapsed_hours:.1f}h elapsed) before the planner "
        "declared the project complete. Nothing is broken — the work simply did "
        "not fit. Everything that landed is on the project branch, and "
        "`resume` starts a fresh budget."
    )
    failure = (state.get("last_failure") or {}).get("summary")
    if failure:
        # The stage being revised when time ran out still matters; escalating
        # for time must not swallow why.
        reason += f" The stage in flight was being revised because: {failure}"
    return reason


def _escalate(layer: str, reason: str) -> dict:
    return {"failure_layer": layer, "escalation_reason": reason, "next_hop": "escalate"}


def _planner_failure(
    state: RunState,
    layer: str,
    summary: str,
    detail: str,
    out_of_scope_paths: list[str] | None = None,
    failing_paths: list[str] | None = None,
) -> dict:
    """Hand the failure to the planner with what it needs to act on."""
    return {
        "failure_layer": layer,
        "last_failure": {
            "layer": layer,
            "summary": summary,
            "detail": detail,
            "out_of_scope_paths": out_of_scope_paths or [],
            "failing_paths": failing_paths or [],
        },
        "next_hop": "plan",
    }


def _retry_or_plan(
    state: RunState,
    rt: Runtime,
    layer: str,
    summary: str,
    feedback: str,
    detail: str,
    failing_paths: list[str] | None = None,
) -> dict:
    """Executor retry while the budget holds, then the planner."""
    consumed = state.get("verify_attempt", 0)
    if consumed >= rt.cfg.limits.max_test_retries:
        return _planner_failure(
            state,
            layer,
            f"{summary} after {consumed + 1} attempts "
            f"(max_test_retries={rt.cfg.limits.max_test_retries})",
            detail,
            failing_paths=failing_paths,
        )

    accumulated = list(state.get("review_feedback") or [])
    accumulated.append(feedback)
    return {
        "failure_layer": layer,
        "verify_attempt": consumed + 1,
        "review_feedback": accumulated,
        "next_hop": "execute",
    }


def _rework_or_plan(
    state: RunState,
    rt: Runtime,
    feedback: list[str],
    summary: str,
    layer: str = "review",
) -> dict:
    """Rework while the budget holds, then the planner."""
    consumed = state.get("rework_attempt", 0)
    if consumed >= rt.cfg.limits.max_rework_retries:
        return _planner_failure(
            state,
            layer,
            f"{summary} — rejected {consumed + 1} times "
            f"(max_rework_retries={rt.cfg.limits.max_rework_retries})",
            "\n\n".join(feedback[-2:]),
        )

    if rt.cfg.rework_reset:
        # One clean single-purpose diff per attempt, rather than the rejected
        # attempt plus its correction.
        rt.log(f"[review] resetting to {state['stage_start_sha'][:8]} before rework")
        rt.git.reset_hard(state["stage_start_sha"])

    return {
        "failure_layer": layer,
        "review_feedback": feedback,
        "rework_attempt": consumed + 1,
        "next_hop": "execute",
    }


def _first_line(stage: Stage) -> str:
    text = stage.instruction or stage.command or stage.id
    return text.strip().splitlines()[0][:70]
