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
from datetime import datetime

from orchestrator.addendum import append_notes, append_observations
from orchestrator.commands import truncate_middle
from orchestrator.config import Stage, validate_stage
from orchestrator.executor import resolve_excerpts
from orchestrator.flake import adjudicate, append_flakes, predates_stage
from orchestrator.gitops import GitError
from orchestrator.globs import matches_any
from orchestrator.planner import append_stage_cost, append_status, recent_stage_costs
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
from orchestrator.verify import Layer, Route, diff_digest, run_verify

STATUS_TAIL_CHARS = 4_000

# Command output bound where it reaches a model or a log, rather than where it
# is captured. The runner keeps everything so the parsers can see it; a prompt
# cannot carry a third of a megabyte of rspec, and a planner intervention is
# expensive enough without paying for a coverage report.
FEEDBACK_OUTPUT_CHARS = 4_000


def _clip(text: str) -> str:
    return truncate_middle(text or "", FEEDBACK_OUTPUT_CHARS)


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

    stuck = state.get("interventions_since_landing", 0)
    if (
        stage is not None
        and limits.max_interventions_without_landing
        and stuck >= limits.max_interventions_without_landing
    ):
        return _escalate(
            "planner",
            f"{stuck} planner intervention(s) without landing a stage "
            f"(max_interventions_without_landing="
            f"{limits.max_interventions_without_landing}). A run that keeps "
            "landing work is not capped, so this is the signal that it has "
            "stopped making progress rather than that it has done a lot.\n\n"
            f"The last failure was: "
            f"{(state.get('last_failure') or {}).get('summary')}\n\n"
            # Said here because the natural next move is `resume`, and resume
            # alone re-enters at this same check and prints this same message,
            # having spent a preflight and an environment setup to do it. Every
            # other escalation means "fix it and resume"; this one does not,
            # and nothing else distinguishes them.
            "**Resume alone will not clear this.** The counter only resets "
            "when a stage lands, and no stage can land while this check stops "
            "the run before the planner is called. Your options:\n"
            "  - `orchestrator resume <run_id> --reset-progress-budget`, if "
            "you have changed something that makes the earlier failures no "
            "longer apply. That is you asserting it, not the run inferring "
            "it.\n"
            "  - Raise `max_interventions_without_landing` and approve the "
            "config, if the work legitimately needs more attempts.\n"
            "  - Start a fresh run. The progress log, stage costs, flake "
            "record and project branch all outlive this run, so a new one "
            "picks up where the work is rather than where the run was.",
        )

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
        # Live, not the snapshot: this one says what has been done.
        plan=rt.live_plan,
        completed=state.get("completed") or [],
        current_stage=stage,
        failure=state.get("last_failure"),
        opening_failure=state.get("opening_failure"),
        revision=state.get("revision", 0),
        interventions_used=state.get("planner_interventions", 0),
        interventions_max=limits.max_planner_interventions,
        status_tail=_status_tail(rt),
        layout=rt.layout(state.get("plan_sha") or state.get("base_sha") or ""),
        agent_context=rt.agent_context(
            state.get("plan_sha") or state.get("base_sha") or ""
        ),
        deferred=state.get("deferred") or [],
        stage_costs=recent_stage_costs(rt.project.project_dir),
    )

    rt.log(f"[plan] {'revising ' + stage.id if stage else 'deriving next stage'}")
    outcome = rt.planner.plan(messages)

    if outcome.tool_calls:
        # What it looked at, before what it decided. A stage drawn from six
        # reads and a search is a different artefact from one drawn from
        # nothing, and only this line distinguishes them afterwards.
        rt.log(
            f"[plan] read {len(outcome.tool_calls)} thing(s): "
            + "; ".join(outcome.tool_calls)
        )

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
                    # Billed above base rate. A prefix written on every call and
                    # never read back costs more than no caching at all, and the
                    # run totals average that away — per call is where it shows.
                    "cache_write_tokens": outcome.usage.cache_write_tokens,
                    "completion_tokens": outcome.usage.completion_tokens,
                },
                # Both recorded even when empty, and that is the point. An
                # absent key cannot be told apart from a feature that never
                # ran, and "the planner looked and had nothing to say" is a
                # different fact from "the planner did not look" — one is the
                # plan being accurate, the other is a bug.
                "tool_calls": list(outcome.tool_calls),
                "plan_notes": list(outcome.plan_notes),
                "client_failure": outcome.failed,
                # Present only when we rejected an answer the model did give.
                # Null for a refusal or a transport failure, where the verdict
                # above is the whole of what happened.
                "rejected_answer": outcome.raw,
            },
            indent=2,
        ),
    )

    notes = list(state.get("planner_notes") or [])
    notes.append(f"{outcome.verdict}: {outcome.reasoning}")
    deferred = merge_deferrals(state.get("deferred") or [], outcome.deferred)
    base = {
        "run_usage": usage,
        "planner_notes": notes,
        "deferred": deferred,
        # Held until the stage lands. Accumulated across revisions, because a
        # redrawn stage is the same piece of work and its observations about
        # the plan are still true.
        "pending_plan_notes": (state.get("pending_plan_notes") or [])
        + list(outcome.plan_notes),
    }

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
            "interventions_since_landing": stuck + 1,
        }

        if not keep_branch:
            # The approach was wrong: discard the branch and re-cut from the
            # project tip on the way through precheck.
            update["stage_branch"] = None
            update["stage_start_sha"] = ""
            update["next_hop"] = "precheck"
        else:
            # Scope was merely too narrow. Anything the planner declined to
            # adopt is reverted; the rest of the stage's work survives.
            _revert_unadopted(state, rt, new_stage)
            # Re-entering at verify is what makes that survival mean anything.
            # `extend` asserts the approach was right, so what is on the branch
            # is the revised stage's work already done — possibly all of it.
            # Routing onward to the executor hands a finished diff to a model
            # holding an instruction that still describes it as undone, and
            # "make this change" has no safe reading once the change is already
            # true: one stage answered it by deleting the line above its target,
            # to produce a diff. The gates read state rather than intent, so ask
            # them instead. If the revision did add work, residue or the tests
            # fail and route to the executor then, with the gap named.
            #
            # Nothing precheck does is owed here. `preconditions` are operator-
            # only, so a revision cannot have changed them and they passed
            # already; setup runs inside verify; the branch is kept by
            # construction; and the clean-tree check exempts revisions.
            update["next_hop"] = "verify"

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

    # Cleared here as well as in verify: a resume re-enters at plan, precheck
    # or verify, and plan routes onward through one of the latter two — so
    # between them, every path consumes it exactly once.
    update: dict = {"resuming": False}
    rt.log(f"[precheck] stage {stage.id} revision {state.get('revision', 0)}")

    # Between stages the tree is clean: `advance` commits everything it lands,
    # and the executor commits its own work. Anything uncommitted here was
    # written outside the pipeline — a crash between `merge --squash` and
    # `commit`, an editor left open, a human mid-edit. Cutting a stage branch
    # over it sweeps those files into the next stage's diff, where the scope
    # guard reports them as the executor editing out of scope. It did nothing
    # of the kind, and the stage pays a retry to find that out.
    #
    # Never on a resume, for the reason preflight gives for its own exemption:
    # a run is resumed because a human just fixed something and that fix is
    # normally uncommitted. Guarding it here would make every escalation
    # unrecoverable.
    #
    # And only for a newly drawn stage about to cut its first branch. A
    # revision re-enters precheck mid-stage, and `advance` — the thing that
    # leaves a clean tree — has not run: it is `advance` that calls
    # `commit_all`, so an uncommitted attempt in the tree is the ordinary state
    # of a restart, not evidence of anything. Checking there would escalate
    # every rework the planner redraws.
    fresh_stage = state.get("revision", 0) == 0 and not state.get("stage_branch")
    if fresh_stage and not state.get("resuming"):
        dirty = rt.git.uncommitted()
        if dirty:
            listed = "\n".join(dirty[:20])
            more = f"\n… and {len(dirty) - 20} more" if len(dirty) > 20 else ""
            return {
                **update,
                **_escalate(
                    "workspace",
                    f"The working tree is not clean before stage {stage.id!r}, "
                    "and nothing in the pipeline leaves it that way between "
                    "stages. Cutting a branch over these would attribute them "
                    "to the executor and fail the stage on scope.\n\n"
                    f"{listed}{more}\n\n"
                    "Commit them if they are wanted, discard them if they are "
                    "debris, then resume.",
                ),
            }

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
                    f"{result.summary()}\n{_clip(result.output)}",
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
                    f"{result.summary()}\n{_clip(result.output)}",
                ),
            }

    # Cut or resume the child branch. Anything on it is quarantined: nothing
    # reaches the project branch without passing the review gate.
    if not state.get("stage_branch"):
        # No branch in state means a fresh start: either the first attempt at
        # this stage, or a restart where the planner discarded the approach. In
        # both cases any branch left under this name is the thing being
        # discarded, so it must not be inherited.
        branch = rt.cfg.stage_branch(state.get("stage_index", 0), stage.id)
        start = rt.git.cut_stage_branch(branch, rt.cfg.project_branch, fresh=True)
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
                f"{failed[0].summary()}\n{_clip(failed[0].output)}",
            )

        # What the stage has already done, so a retry can amend it rather than
        # reconstruct it. Only when there is something there: on a first attempt
        # this is empty, and on a rework with `rework_reset` it has just been
        # thrown away, so in both cases the section is absent rather than empty.
        cumulative_diff = ""
        if state.get("stage_start_sha"):
            try:
                cumulative_diff = rt.git.diff(state["stage_start_sha"])
            except GitError:  # pragma: no cover - defensive
                cumulative_diff = ""

        prompt = build_executor_prompt(
            stage,
            rt.cfg,
            context=context,
            feedback=feedback,
            failure_layer=state.get("failure_layer"),
            cumulative_diff=cumulative_diff,
            excerpts=resolve_excerpts(stage, rt.cfg),
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

    if result.dropped_reads:
        rt.log(
            f"[execute] {stage.id}: withheld {len(result.dropped_reads)} "
            f"reference file(s) to stay inside the read budget "
            f"({', '.join(result.dropped_reads)})"
        )

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "executor.log", result.log,
    )

    # Carried even on the failing paths below: an attempt that timed out with
    # 60k of context loaded is exactly the datum that should shrink the next
    # stage, and it is the one most likely to be discarded.
    measured = {"executor_context_tokens": result.context_tokens} if result.context_tokens else {}
    # Same reasoning, and the same gap it closes. The planner chooses
    # `read_files` and the tool silently truncates the tail of that choice to
    # fit `max_read_lines`; logging it tells the operator and leaves the
    # planner picking blind. Four stages running asked for one reference too
    # many, each time a large model the instruction went on to reason about.
    if result.dropped_reads:
        measured["withheld_reads"] = list(result.dropped_reads)

    if result.ok:
        return {"next_hop": "verify", **measured}

    # An unapplied edit on a tree that has changed is not a failed attempt.
    # The editor reports every block it could not apply, including ones it
    # could not apply *because the work was already there* — verbatim, "the
    # REPLACE lines are already in Gemfile!". A model that emits one good block
    # and two redundant ones therefore lands the change and is recorded as
    # having produced nothing.
    #
    # Observed twice on the same stage. The gem removal committed the edit,
    # was retried, committed it again, and burned ten to fifteen minutes an
    # attempt re-doing finished work — the first time costing the stage its
    # whole budget and an escalation whose stated cause was wrong.
    #
    # So ask the tree. If the diff since the stage started is non-empty, hand
    # it to verify: the gates exist to judge a tree, and they are better at it
    # than a report from the editor about its own blocks. If the tree really is
    # untouched, nothing below changes.
    # A timeout counts too, but only on a *committed* tree. The first cut of
    # this excluded timeouts on the reasoning that a stage killed mid-write can
    # have half an edit — true, but "was it killed" is the wrong discriminator.
    # The editor commits after applying, so a kill mid-write leaves the tree
    # dirty; a kill while it churns on redundant blocks leaves it clean with
    # commits ahead. That is the difference worth testing.
    #
    # Observed on two consecutive stages: the edit applied, the editor
    # committed it, the model kept re-issuing blocks for work already done —
    # "the REPLACE lines are already in app/models/item_svg.rb!" — and the
    # reflection loop ran until the 900s kill. Three attempts, forty-five
    # minutes, all of it redoing finished work before the fourth happened to
    # stop early enough to be counted.
    committed = not result.timed_out or (
        rt.git.is_clean() and bool(rt.git.diff_names(state["stage_start_sha"]))
    )
    if (result.unapplied_edit or result.timed_out) and committed:
        if rt.git.diff_names(state["stage_start_sha"]):
            why = (
                "was killed but had committed its work"
                if result.timed_out
                else "could not apply part of its reply, but the tree has changed"
            )
            rt.log(f"[execute] {stage.id}: the editor {why} — verifying what is there")
            return {"next_hop": "verify", **measured}

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
        feedback=f"The previous attempt's executor {what}:\n{_clip(result.log)}{advice}",
        detail=_clip(result.log),
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
        plan_sha=state.get("plan_sha") or state.get("base_sha"),
        previous_diff_digest=state.get("last_diff_digest") or None,
        previous_failure_layer=state.get("failure_layer") or None,
        resuming=bool(state.get("resuming")),
    )

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "verify.log",
        _verify_log(outcome),
    )

    if outcome.flaky_files:
        _record_flakes(rt, stage.id, outcome.flaky_files, outcome.flaky_seeds)

    accumulated = {
        # Consumed here. `resuming` means "this is the first step after a
        # resume", and every reader treats it that way — but nothing cleared
        # it, so it meant "this run has been resumed at some point" and stayed
        # true forever. The progress layer treats it as a hard bypass, so a
        # single resume disabled "the attempt reproduced the previous diff
        # exactly" for the remainder of the run. Observed: a stage produced
        # byte-identical diffs on two attempts, both rejected for the same
        # reason, and the guard that exists to redraw such a stage never fired.
        "resuming": False,
        "flake_reruns": state.get("flake_reruns", 0) + outcome.flake_reruns,
        "flaky_files": _merge_flaky(state, outcome.flaky_files),
        "test_seconds": state.get("test_seconds", 0.0) + outcome.test_seconds,
        "last_diff_digest": outcome.diff_digest,
        "full_suite_digest": outcome.full_suite_digest,
    }

    if outcome.unscoped_tests:
        rt.log(
            f"[verify] {stage.id}: nothing identified which specs this stage "
            "affects, so the whole suite ran. Declaring test_paths on the stage "
            "would scope it."
        )

    if outcome.exempt_pattern_files:
        rt.log(
            f"[verify] {stage.id}: forbidden patterns matched in "
            f"{', '.join(outcome.exempt_pattern_files)} and were excused as "
            "tests. A test proving a construct is gone has to name it."
        )

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

    # Line-ending churn hidden from the reviewer: it is the editor's doing in
    # whichever direction the host's platform dictates, and judging it costs a
    # stage that cannot comply.
    diff = rt.git.diff(state["stage_start_sha"], ignore_line_endings=True)
    messages = build_review_messages(
        stage=stage,
        cfg=rt.cfg,
        diff=diff,
        plan=rt.plan,
        completed=state.get("completed") or [],
        progress_log=rt.live_progress_log,
    )

    rt.log(f"[review] {stage.id}: calling reviewer")
    # Keyed by project rather than by run: successive runs and resumes share
    # the same plan snapshot prefix, so they should share the same cache.
    outcome = rt.reviewer.review(
        messages, cache_key=f"orchestrator:{state.get('project_slug') or 'project'}"
    )

    # What it looked at, before the verdict that used it. An approval reached
    # after reading the file the diff depends on and one reached from the diff
    # alone read identically in the log otherwise, and those are exactly the
    # two cases worth telling apart while watching a run.
    if outcome.tool_calls:
        rt.log(
            f"[review] {stage.id}: read {len(outcome.tool_calls)} thing(s): "
            + "; ".join(outcome.tool_calls)
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

    if outcome.observations:
        rt.log(
            f"[review] {stage.id}: {len(outcome.observations)} observation(s) "
            "outside this stage: "
            + "; ".join(o.file for o in outcome.observations)
        )

    base = {
        "run_usage": usage,
        "stage_usage": stage_usage,
        "review_verdict": outcome.verdict,
        "review_summary": outcome.summary,
        # Replaced, not accumulated. Every review of a stage sees the whole
        # cumulative diff, so the newest set supersedes the last rather than
        # adding to it — otherwise a stage reworked twice reports each finding
        # three times. Written by `advance` if the stage lands, and dropped
        # with the stage if it never does.
        "pending_observations": [o.model_dump() for o in outcome.observations],
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

    # Verify may already have run this exact command on this exact tree — that
    # is what happens whenever a stage declares no `test_paths` and the tests
    # layer falls back to the whole suite. Re-running it here tests the same
    # bytes with the same command: the reviewer reads a diff, it does not edit.
    #
    # It is not merely wasted minutes. The second run re-rolls every
    # order-dependent example in the suite, so on a legacy suite it is a fresh
    # chance to trip over one — at the gate, where a flake costs a planner
    # intervention rather than an executor attempt. Two of the first night's
    # three gate flakes were on a suite that had just passed clean.
    #
    # Compared, never trusted: a digest that has moved for any reason falls
    # through to running the suite, which is the old behaviour.
    tested = state.get("full_suite_digest")
    if tested and tested == diff_digest(rt.git, state["stage_start_sha"]):
        rt.log(
            f"[review] {stage.id}: approved; the full suite already passed at "
            "verify on this tree, so the gate does not repeat it"
        )
        return {**base, "next_hop": "advance"}

    rt.log(f"[review] {stage.id}: approved; running the full suite")
    result = rt.runner.run(rt.cfg.full_test_command)
    seconds = result.duration_seconds

    if not result.ok:
        # Persisted before anything acts on it. This is the most expensive
        # failure in the loop — the stage is already approved and a rejection
        # here resets the work — and it was the one output nobody kept. Its
        # absence cost an afternoon: the flake gate silently stopped matching,
        # and the only copy of the evidence was a file the very next run
        # truncated.
        rt.write_artifact(
            state.get("stage_index", 0),
            stage.id,
            state.get("revision", 0),
            _attempt(state),
            "full-suite.log",
            f"{result.summary()}\n\n{result.output}",
        )

    if result.signal is not None:
        # The stage is already approved and its diff is already correct. Reading
        # a killed suite as a rejection would send correct work back for rework,
        # against the same dead environment.
        return {
            **base,
            "test_seconds": state.get("test_seconds", 0.0) + seconds,
            **_escalate(
                "full_suite",
                f"The full suite was killed by signal {result.signal} rather "
                "than failing. The reviewer had already approved this stage, so "
                "nothing is wrong with the work — the environment went away "
                "underneath it.\n\nPut it back and resume; the gate re-runs "
                f"from here.\n{result.summary()}",
            ),
        }

    if not result.ok:
        # Re-run what failed, not everything. It matters more here than during
        # iteration: the full suite has far more surface for ordering flakes,
        # and a flake at this gate wastes a planner intervention rather than an
        # executor attempt. A spec the stage itself touched is exempt — see
        # `flake.adjudicate`.
        verdict = adjudicate(
            output=result.output,
            command=rt.cfg.full_test_command,
            cfg=rt.cfg,
            runner=rt.runner,
        )
        seconds += verdict.seconds
        if verdict.flaked:
            rt.log(f"[review] {stage.id}: full suite flaked — {verdict.summary}")
            _record_flakes(rt, stage.id, verdict.files, verdict.seeds)
            return {
                **base,
                "test_seconds": state.get("test_seconds", 0.0) + seconds,
                "flake_reruns_review_gate": state.get("flake_reruns_review_gate", 0) + 1,
                "flaky_files": _merge_flaky(state, verdict.files),
                "next_hop": "advance",
            }

        # A real failure is not necessarily *this stage's* failure. Ask the one
        # question that separates them before spending an attempt: were these
        # files already red before the stage ran? The executor is scoped to
        # `edit_files`, so when the answer is yes it cannot fix them under any
        # instruction, and every rework attempt is spent to learn nothing.
        baseline = predates_stage(
            files=verdict.files,
            base_sha=state.get("stage_start_sha", ""),
            cfg=rt.cfg,
            runner=rt.runner,
            git=rt.git,
            # Moving the tree moves the environment with it. A stage that
            # touched the Dockerfile or the schema would otherwise have the
            # base tree's specs run against containers built for the tip.
            setup_command=stage.effective_setup_command(rt.cfg),
        )
        seconds += baseline.seconds
        if baseline.checked:
            rt.log(f"[review] {stage.id}: baseline — {baseline.summary}")

        if baseline.predates:
            return {
                **base,
                "test_seconds": state.get("test_seconds", 0.0) + seconds,
                **_planner_failure(
                    state,
                    "full_suite",
                    "the full suite is red on failures that predate this stage",
                    "The reviewer approved this stage and the full suite is "
                    f"red, but {baseline.summary}. The stage is scoped to its "
                    "own files and cannot repair these, so reworking it would "
                    "produce the same diff and the same red suite.\n\nFix the "
                    "failing specs as their own stage, then draw this one "
                    f"again.\n{_clip(baseline.output)}",
                    failing_paths=baseline.files,
                ),
            }

        feedback = list(state.get("review_feedback") or [])
        feedback.append(
            "The reviewer approved this stage but the full suite failed, so it "
            f"cannot land ({verdict.summary}):\n"
            f"{_clip(verdict.output or result.output)}"
        )
        if baseline.checked and baseline.files:
            feedback.append(f"Baseline check: {baseline.summary}")
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


def _record_flakes(
    rt: Runtime, stage_id: str, files: list[str], seeds: dict[str, str]
) -> None:
    """Write the excusal down where it outlives the run.

    Logged as well as filed, because a record nobody knows was written is not
    much better than no record.
    """
    path = append_flakes(
        rt.project.project_dir,
        stage_id,
        files,
        seeds,
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    for name in files:
        seed = seeds.get(name)
        rt.log(
            f"[flake] {name} seed {seed}" if seed
            else f"[flake] {name} — no seed reported"
        )
    rt.log(f"[flake] recorded in {path}")


def _merge_flaky(state: RunState, files: list[str]) -> list[str]:
    """Union, order-preserving. The same file flakes across stages."""
    out = list(state.get("flaky_files") or [])
    for e in files:
        if e not in out:
            out.append(e)
    return out


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
    # Written on the stage branch, before the squash picks it up, so the
    # observations land inside the commit they are about. One commit per stage
    # holds, and a reader of that commit sees both what changed and what it
    # revealed about the plan.
    #
    # After every gate has run, not before: the scope guard would see a plan
    # document modified by a stage that never touched it. That ordering means
    # the content is unexamined by the gates, which is acceptable here in a way
    # it would not be for code — this is markdown at a configured path, written
    # by the orchestrator from structured planner output, not a model editing
    # the repository. The guards exist to catch the executor wandering.
    plan_sha = state.get("plan_sha") or state.get("base_sha") or ""

    def read_plan(path: str) -> str | None:
        """A plan document as it stood at `plan_sha`.

        Read from the commit rather than the worktree because that is the
        revision the planner was shown and cited line numbers against. The
        worktree has moved: this very function runs after a stage landed, and
        the log itself is a plan document that grows on every landing.
        """
        try:
            return rt.git.show_file(plan_sha, path)
        except GitError:
            return None

    written = append_notes(
        rt.cfg.target_repo,
        rt.cfg.plan_addendum_path,
        state.get("pending_plan_notes") or [],
        stage_id=stage.id,
        read_plan=read_plan,
        plan_sha=plan_sha,
    )
    if written is not None:
        rt.log(
            f"[advance] recorded {len(state.get('pending_plan_notes') or [])} "
            f"plan observation(s) in {written.relative_to(rt.cfg.target_repo)}"
        )

    # The reviewer's findings, after the planner's and into the same file. Both
    # answer "what does the plan not yet know?"; they differ in who noticed and
    # in what about. Written only on landing, so a finding from a stage that
    # was abandoned never enters the record.
    seen = append_observations(
        rt.cfg.target_repo,
        rt.cfg.plan_addendum_path,
        state.get("pending_observations") or [],
        stage_id=stage.id,
    )
    if seen is not None:
        rt.log(
            f"[advance] recorded {len(state.get('pending_observations') or [])} "
            f"reviewer observation(s) in {seen.relative_to(rt.cfg.target_repo)}"
        )

    # Commit anything the executor left uncommitted, then squash the whole
    # child branch onto the project branch as one commit. Aider's intermediate
    # commits — some of them red, since it commits before testing — are
    # discarded by the squash. That is why "every commit on the project branch
    # is green" and "Aider commits before testing" are both true.
    # Before anything is committed, not after: a pre-commit hook rejecting
    # trailing whitespace on added lines is common, and neither the executor
    # nor its linter reliably avoids one. `git commit` raising here strands a
    # staged merge on the project branch and ends the run.
    stripped = rt.git.strip_added_trailing_whitespace(start_sha)
    if stripped:
        rt.log(
            f"[advance] removed trailing whitespace from added lines in "
            f"{', '.join(stripped)}"
        )

    # The note is written to the worktree and committed a few lines below, so
    # anything that raises in between leaves it modified and uncommitted. The
    # next resume re-enters at verify, whose scope guard sees a plan document
    # changed by a stage and routes it to the planner as the executor wandering
    # into the record of its own work — a diagnosis that is wrong, and that the
    # planner cannot act on because it did not happen.
    #
    # `squash_merge` already restores the project branch if its own commit
    # fails. This covers the other half: either the stage lands or the tree is
    # as advance found it.
    try:
        rt.git.commit_all(f"[{stage.id}] wip")
        merge_sha = rt.git.squash_merge(
            branch, rt.cfg.project_branch, f"[{stage.id}] {_first_line(stage)}"
        )
    except Exception:
        if written is not None and rt.cfg.plan_addendum_path:
            rt.log("[advance] landing failed; unwinding the plan note")
            rt.git.revert_paths(start_sha, [rt.cfg.plan_addendum_path])
        raise
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
        "executor_context_tokens": state.get("executor_context_tokens", 0),
        "withheld_reads": list(state.get("withheld_reads") or []),
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
    # Recorded after the squash, keyed by the sha that survives it. The run's
    # own state carries this too, but only until the run ends; this is the copy
    # a later run can calibrate against.
    if result.get("executor_context_tokens"):
        append_stage_cost(
            rt.project.project_dir,
            stage_id=stage.id,
            merge_sha=result["merge_sha"],
            files=len(stage.edit_files),
            context_tokens=result["executor_context_tokens"],
        )
    rt.log(f"[advance] {stage.id} landed as {result['merge_sha'][:12]}")

    return {
        **fresh_stage_fields(),
        "completed": completed,
        "current": None,
        # Cleared here, by the only node that writes them, rather than by the
        # per-stage reset — which `plan` also applies, over the notes it has
        # just accumulated.
        "pending_plan_notes": [],
        "pending_observations": [],
        "stage_index": state["stage_index"] + 1,
        "revision": 0,
        # Something landed, so the run is making progress: the stuck counter
        # starts again. A run that keeps landing work is bounded by the wall
        # clock rather than by an intervention count picked in advance.
        "interventions_since_landing": 0,
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
            f"project branch tip:\n{result.summary()}\n{_clip(result.output)}",
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
        parts.append(f"{result.summary()}\n{_clip(result.output)}")

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


def _failure_detail(
    layer: str,
    summary: str,
    detail: str,
    out_of_scope_paths: list[str] | None = None,
    failing_paths: list[str] | None = None,
) -> dict:
    return {
        "layer": layer,
        "summary": summary,
        "detail": detail,
        "out_of_scope_paths": out_of_scope_paths or [],
        "failing_paths": failing_paths or [],
    }


def _opening(state: RunState, detail: dict) -> dict:
    """Claim the sequence's first failure, or leave the claim standing.

    Write-once per stage or revision. Whichever failure got here first is the
    diagnosis; everything after it is what that failure caused, and overwriting
    is precisely the defect this exists to fix.
    """
    if state.get("opening_failure"):
        return {}
    return {"opening_failure": detail}


def _planner_failure(
    state: RunState,
    layer: str,
    summary: str,
    detail: str,
    out_of_scope_paths: list[str] | None = None,
    failing_paths: list[str] | None = None,
) -> dict:
    """Hand the failure to the planner with what it needs to act on."""
    latest = _failure_detail(
        layer, summary, detail, out_of_scope_paths, failing_paths
    )
    return {
        "failure_layer": layer,
        "last_failure": latest,
        **_opening(state, latest),
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
        # Recorded on the way to the executor, not only on the way to the
        # planner. This is the branch the diagnosis is usually lost on: the
        # real failure retries, the retries stop making progress, and only the
        # guard that noticed reaches the planner.
        **_opening(
            state, _failure_detail(layer, summary, detail, failing_paths=failing_paths)
        ),
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
        **_opening(
            state, _failure_detail(layer, summary, "\n\n".join(feedback[-2:]))
        ),
        "next_hop": "execute",
    }


def _first_line(stage: Stage) -> str:
    text = stage.instruction or stage.command or stage.id
    return text.strip().splitlines()[0][:70]
