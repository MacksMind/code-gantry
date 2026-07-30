"""Node logic.

Each function takes (state, runtime) and returns a partial state update, the
same contract LangGraph expects — but with the runtime passed explicitly, so
every node is callable from a test with a stubbed executor and reviewer.

`next_hop` is set here and read by the graph's conditional edges. Routing
lives in the state rather than in control flow so a checkpoint records not
just where a run stopped but which way it was about to go.
"""

from __future__ import annotations

import json
import time

from orchestrator.config import Stage
from orchestrator.prompts import build_executor_prompt, build_review_messages
from orchestrator.reviewer import issues_as_feedback
from orchestrator.runtime import Runtime
from orchestrator.state import RunState, fresh_stage_fields
from orchestrator.verify import run_verify


def current_stage(state: RunState, rt: Runtime) -> Stage:
    return rt.cfg.stages[state["stage_index"]]


def current_stage_or_none(state: RunState, rt: Runtime) -> Stage | None:
    """None once the stage list is exhausted.

    `finalize` escalates after `advance` has already moved past the last
    stage, so an escalation is not always attributable to a stage.
    """
    index = state.get("stage_index", 0)
    return rt.cfg.stages[index] if index < len(rt.cfg.stages) else None


# --- precheck ------------------------------------------------------------


def precheck(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    update: dict = {}

    # Entering the stage for the first time: pin the baseline every diff and
    # every gate for this stage is measured against.
    if not state.get("stage_start_sha"):
        update["stage_start_sha"] = rt.git.head_sha()
        update["stage_started_at"] = time.time()

    rt.log(f"[precheck] stage {stage.id} ({stage.kind})")

    for command in stage.preconditions:
        result = rt.runner.run(command)
        if not result.ok:
            # An unmet precondition is an ordering error in the config. No
            # amount of rework fixes a stage that should not have started.
            return {
                **update,
                "failure_layer": "precondition",
                "next_hop": "escalate",
                "escalation_reason": (
                    f"Precondition failed for stage {stage.id!r}:\n"
                    f"{result.summary()}\n{result.output}"
                ),
            }

    setup = stage.effective_setup_command(rt.cfg)
    if setup:
        # Run before the executor, not only before verify: the executor runs
        # the test command itself via --auto-test and cannot be handed a stale
        # container or unresolved dependencies.
        result = rt.runner.run(setup)
        if not result.ok:
            return {
                **update,
                "failure_layer": "setup",
                "next_hop": "escalate",
                "escalation_reason": (
                    f"Environment setup failed before stage {stage.id!r}:\n"
                    f"{result.summary()}\n{result.output}"
                ),
            }

    update["next_hop"] = "gate" if stage.kind == "manual" else "execute"
    return update


# --- execute -------------------------------------------------------------


def execute(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    attempt = state["verify_attempt"] + state["rework_attempt"]
    feedback = list(state.get("review_feedback") or [])

    if stage.kind == "script":
        rt.log(f"[execute] stage {stage.id}: script")
        result = rt.executor.run_script_stage(stage)
    else:
        # A rework is a fresh invocation. Nothing of the prior attempt's
        # conversation carries over, so the prompt restates everything.
        context, context_results = rt.executor.gather_context(stage)
        failed_context = [r for r in context_results if not r.ok]
        if failed_context:
            return {
                "next_hop": "escalate",
                "failure_layer": "precondition",
                "escalation_reason": (
                    f"A context command failed for stage {stage.id!r}, so the "
                    "executor prompt would have been built from missing "
                    f"information:\n{failed_context[0].summary()}\n"
                    f"{failed_context[0].output}"
                ),
            }

        prompt = build_executor_prompt(stage, rt.cfg, context=context, feedback=feedback)
        rt.write_attempt_artifact(
            state["stage_index"], stage.id, attempt, "prompt.md", prompt
        )
        rt.log(f"[execute] stage {stage.id}: attempt {attempt}")
        result = rt.executor.run_agent_stage(stage, prompt)

    rt.write_attempt_artifact(
        state["stage_index"], stage.id, attempt, "executor.log", result.log
    )

    if result.ok:
        return {"next_hop": "verify"}

    # The executor itself failed or timed out. Treat it as a retryable
    # attempt, with its own output as the feedback.
    what = "timed out" if result.timed_out else "exited non-zero"
    return _retry_or_escalate(
        state,
        rt,
        stage,
        layer="tests",
        feedback=f"The previous attempt's executor {what}:\n{result.log}",
        reason=f"The executor {what} for stage {stage.id!r}",
    )


# --- gate ----------------------------------------------------------------


def gate(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    rt.log(f"[gate] stage {stage.id} awaiting human")
    return {
        "status": "awaiting_human",
        "next_hop": "end",
        "escalation_reason": None,
    }


# --- verify --------------------------------------------------------------


def verify(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    attempt = state["verify_attempt"] + state["rework_attempt"]

    outcome = run_verify(
        stage=stage,
        cfg=rt.cfg,
        git=rt.git,
        runner=rt.runner,
        stage_start_sha=state["stage_start_sha"],
    )

    rt.write_attempt_artifact(
        state["stage_index"],
        stage.id,
        attempt,
        "verify.log",
        "\n\n".join(f"{r.summary()}\n{r.output}" for r in outcome.results),
    )

    accumulated_flakes = state["flake_reruns"] + outcome.flake_reruns
    accumulated_test_time = state["test_seconds"] + outcome.test_seconds

    if outcome.passed:
        rt.log(f"[verify] stage {stage.id}: all layers passed")
        return {
            "flake_reruns": accumulated_flakes,
            "test_seconds": accumulated_test_time,
            "failure_layer": None,
            "next_hop": "review" if stage.reviews_enabled else "advance",
        }

    layer = outcome.failed_layer.value if outcome.failed_layer else "tests"
    rt.log(f"[verify] stage {stage.id}: failed at {layer}")

    base = {
        "flake_reruns": accumulated_flakes,
        "test_seconds": accumulated_test_time,
        "last_test_output": outcome.feedback,
    }

    if not outcome.retryable:
        return {
            **base,
            "failure_layer": layer,
            "next_hop": "escalate",
            "escalation_reason": (
                f"Stage {stage.id!r} failed the {layer} gate, which does not "
                f"consume a retry:\n{outcome.feedback}"
            ),
        }

    return {
        **base,
        **_retry_or_escalate(
            state,
            rt,
            stage,
            layer=layer,
            feedback=outcome.feedback,
            reason=f"Stage {stage.id!r} kept failing the {layer} gate",
        ),
    }


# --- review --------------------------------------------------------------


def review(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    attempt = state["verify_attempt"] + state["rework_attempt"]

    diff = rt.git.diff(state["stage_start_sha"])
    documents = _read_reference_docs(rt)
    messages = build_review_messages(
        stage=stage, cfg=rt.cfg, diff=diff, documents=documents
    )

    rt.log(f"[review] stage {stage.id}: calling reviewer")
    outcome = rt.reviewer.review(messages)

    rt.write_attempt_artifact(
        state["stage_index"],
        stage.id,
        attempt,
        "review.json",
        json.dumps(outcome.as_dict(), indent=2),
    )

    usage = dict(state.get("stage_usage") or {})
    usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + outcome.usage.prompt_tokens
    usage["cached_tokens"] = usage.get("cached_tokens", 0) + outcome.usage.cached_tokens
    usage["completion_tokens"] = (
        usage.get("completion_tokens", 0) + outcome.usage.completion_tokens
    )

    rt.log(
        f"[review] stage {stage.id}: {outcome.verdict} — {outcome.summary} "
        f"({outcome.usage.prompt_tokens} prompt, "
        f"{outcome.usage.cached_tokens} cached, "
        f"{outcome.usage.completion_tokens} completion)"
    )

    base = {
        "stage_usage": usage,
        "review_verdict": outcome.verdict,
    }

    if outcome.verdict == "approved":
        return {**base, "next_hop": "advance", "review_summary": outcome.summary}

    if outcome.verdict == "blocked":
        # Blocked does not consume a retry: the problem is upstream of the
        # executor, and grinding through rework attempts will not fix it.
        return {
            **base,
            "next_hop": "escalate",
            "escalation_reason": (
                f"The reviewer blocked stage {stage.id!r}: {outcome.summary}\n"
                + "\n".join(
                    f"- [{i.severity}] {i.file}: {i.description}"
                    for i in outcome.issues
                )
            ),
        }

    feedback = list(state.get("review_feedback") or [])
    feedback.append(issues_as_feedback(outcome.summary, outcome.issues))

    if state["rework_attempt"] >= rt.cfg.limits.max_rework_retries:
        return {
            **base,
            "review_feedback": feedback,
            "next_hop": "escalate",
            "escalation_reason": (
                f"Stage {stage.id!r} was rejected "
                f"{state['rework_attempt'] + 1} times "
                f"(max_rework_retries={rt.cfg.limits.max_rework_retries}). "
                f"Last verdict: {outcome.summary}"
            ),
        }

    if rt.cfg.rework_reset:
        # Each attempt should produce one clean single-purpose diff, not the
        # rejected attempt plus its correction.
        rt.log(f"[review] resetting to {state['stage_start_sha'][:8]} before rework")
        rt.git.reset_hard(state["stage_start_sha"])

    return {
        **base,
        "review_feedback": feedback,
        "rework_attempt": state["rework_attempt"] + 1,
        "next_hop": "execute",
    }


# --- advance -------------------------------------------------------------


def advance(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    start_sha = state["stage_start_sha"]

    # The only place the orchestrator commits on its own behalf: squash
    # whatever the stage left uncommitted into one labelled commit.
    end_sha = rt.git.commit_all(f"[{stage.id}] {_first_line(stage)}")
    if end_sha is None:
        end_sha = rt.git.head_sha()

    commit_range = f"{start_sha[:12]}..{end_sha[:12]}" if end_sha != start_sha else None
    usage = state.get("stage_usage") or {}

    result = {
        "id": stage.id,
        "kind": stage.kind,
        "outcome": "complete",
        "commit_range": commit_range,
        "wall_seconds": max(time.time() - (state.get("stage_started_at") or 0), 0.0),
        "test_seconds": state["test_seconds"],
        "verify_retries": state["verify_attempt"],
        "rework_attempts": state["rework_attempt"],
        "flake_reruns": state["flake_reruns"],
        "failed_layer": None,
        "review_verdict": state.get("review_verdict"),
        "review_summary": state.get("review_summary"),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "cached_tokens": usage.get("cached_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }

    history = list(state.get("history") or [])
    history.append(result)

    next_index = state["stage_index"] + 1
    rt.log(f"[advance] stage {stage.id} complete ({commit_range or 'no new commits'})")

    return {
        **fresh_stage_fields(),
        "history": history,
        "stage_index": next_index,
        "next_hop": "precheck" if next_index < len(rt.cfg.stages) else "finalize",
    }


# --- finalize ------------------------------------------------------------


def finalize(state: RunState, rt: Runtime) -> dict:
    command = rt.cfg.full_test_command
    if not command:
        rt.log("[finalize] no full_test_command configured")
        return {"status": "complete", "next_hop": "end"}

    rt.log("[finalize] running the full suite")
    result = rt.runner.run(command)
    if result.ok:
        return {"status": "complete", "next_hop": "end"}

    # Every stage passed on its own; their composition did not.
    return {
        "next_hop": "escalate",
        "failure_layer": "tests",
        "escalation_reason": (
            "Every stage passed individually, but the full suite failed at the "
            f"end of the run:\n{result.summary()}\n{result.output}"
        ),
    }


# --- escalate ------------------------------------------------------------


def escalate(state: RunState, rt: Runtime) -> dict:
    reason = state.get("escalation_reason") or "escalated without a recorded reason"
    rt.log(f"[escalate] {reason}")

    stage = current_stage_or_none(state, rt)

    # Deliberately not appended to `history`, which records *completed*
    # stages. A run that escalates, gets fixed, and is resumed would otherwise
    # carry both an "escalated" and a "complete" row for the same stage. The
    # report renders the failed stage from these fields instead.
    return {
        "status": "escalated",
        "failed_stage_id": stage.id if stage else None,
        "next_hop": "end",
    }


# --- helpers -------------------------------------------------------------


def _retry_or_escalate(
    state: RunState, rt: Runtime, stage: Stage, layer: str, feedback: str, reason: str
) -> dict:
    consumed = state["verify_attempt"]
    if consumed >= rt.cfg.limits.max_test_retries:
        return {
            "failure_layer": layer,
            "next_hop": "escalate",
            "escalation_reason": (
                f"{reason} after {consumed + 1} attempts "
                f"(max_test_retries={rt.cfg.limits.max_test_retries}):\n{feedback}"
            ),
        }

    accumulated = list(state.get("review_feedback") or [])
    accumulated.append(feedback)
    return {
        "failure_layer": layer,
        "verify_attempt": consumed + 1,
        "review_feedback": accumulated,
        "next_hop": "execute",
    }


def _read_reference_docs(rt: Runtime) -> list[tuple[str, str]]:
    documents = []
    for ref, path in zip(rt.cfg.reference_docs, rt.cfg.reference_doc_paths()):
        try:
            documents.append((ref, path.read_text()))
        except OSError as e:  # pragma: no cover - validate catches this first
            rt.log(f"[review] could not read reference doc {ref}: {e}")
    return documents


def _first_line(stage: Stage) -> str:
    text = stage.instruction or stage.command or stage.human_steps or stage.id
    return text.strip().splitlines()[0][:70]
