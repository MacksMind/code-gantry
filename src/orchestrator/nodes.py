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
import re
import textwrap
import time
from datetime import datetime

from orchestrator.addendum import (
    append_notes,
    append_observations,
    append_outcome,
    decode_escapes,
)
from orchestrator.commands import clip_for_model
from orchestrator.config import Stage, orthogonal_stages, validate_stage
from orchestrator.executor import (
    TRANSCRIPT_FILENAME,
    ExcerptError,
    resolve_excerpts,
)
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
    return clip_for_model(text, FEEDBACK_OUTPUT_CHARS)


def current_stage(state: RunState, rt: Runtime) -> Stage | None:
    fields = state.get("current")
    return Stage(**fields) if fields else None


def _attempt(state: RunState) -> int:
    return state.get("verify_attempt", 0) + state.get("rework_attempt", 0)


def _conventions(state: RunState, rt: Runtime) -> str:
    """The repository's agent-facing documents, at the run's fixed commit.

    One resolution for all three participants. The planner had this from the
    start; the executor and the reviewer did not, which meant the rule, the
    hand that could break it, and the gate that should catch it were reading
    three different things.
    """
    return rt.agent_context(_doc_sha(state))


def _doc_sha(state: RunState) -> str:
    return state.get("plan_sha") or state.get("base_sha") or ""


def _planner_context(state: RunState, rt: Runtime) -> str:
    """Conventions plus operations — the planner is the only one that gets both.

    The executor writes code and must obey the conventions; it runs nothing, so
    the operational half is noise it can act on wrongly. The reviewer judges
    code against conventions for the same reason. The planner is the participant
    that decides what is *possible*, which is what the operational half answers.
    """
    parts = [rt.agent_context(_doc_sha(state)), rt.operations_context(_doc_sha(state))]
    return "\n\n".join(p for p in parts if p.strip())


def _stage_diff(state: RunState, rt: Runtime) -> str:
    """The stage's cumulative diff, as the reviewer is shown it.

    Empty rather than raising: this feeds a section of a prompt that is
    optional by design, and a revision drawn without it is the behaviour that
    shipped for the whole run before it existed. Failing the planner call over
    a diff it did not previously have would be a worse outcome than the one it
    fixes.
    """
    if not state.get("stage_start_sha"):
        return ""
    try:
        return rt.git.diff(state["stage_start_sha"], ignore_line_endings=True)
    except GitError:  # pragma: no cover - defensive
        return ""


# --- plan ----------------------------------------------------------------


def plan(state: RunState, rt: Runtime) -> dict:
    """Derive the next stage, or revise the one that just failed."""
    stage = current_stage(state, rt)
    limits = rt.cfg.limits

    stuck = state.get("interventions_since_landing", 0)
    # Not conditioned on a stage being in flight any more. It was, and that made
    # it unreachable for the one way the planner can fail without leaving a
    # stage behind: a spec rejected by validation, which now redraws rather than
    # escalating. `advance` zeroes this on every landing, so on an ordinary
    # derivation it is 0 and the check is inert either way.
    if (
        limits.max_interventions_without_landing
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
    paused = _pause_escalation(rt.paths.pause_flag, state)
    if paused is not None:
        return paused

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
        agent_context=_planner_context(state, rt),
        deferred=state.get("deferred") or [],
        stage_costs=recent_stage_costs(rt.project.project_dir),
        # Only when a stage is under revision: deriving a new one has no branch
        # and nothing to reconcile. Read from the same sha the reviewer's diff
        # is taken from, because the point of showing it is that the two agree.
        stage_diff=_stage_diff(state, rt) if stage else None,
        # What is already drawn and waiting, so it is not derived twice — a
        # second copy of a queued stage collides with the first and one is
        # discarded, which is a whole stage of planning for nothing.
        stage_queue=state.get("stage_queue") or [],
        batch_notes=state.get("batch_notes") or [],
    )

    rt.log(f"[plan] {'revising ' + stage.id if stage else 'deriving next stage'}")
    started = time.time()
    outcome = rt.planner.plan(messages)
    planned_for = max(time.time() - started, 0.0)

    if outcome.tool_calls:
        # What it looked at, before what it decided. A stage drawn from six
        # reads and a search is a different artefact from one drawn from
        # nothing, and only this line distinguishes them afterwards.
        #
        # `read` counts what came back, and refusals are reported beside it
        # rather than folded into it. Counting this line is how the 25-call
        # ceiling was found to be binding on 24 of 65 steps, and a count that
        # quietly included denied calls would have answered that question
        # wrongly while looking exactly as authoritative.
        # A count, not the list. Each call is now logged as it returns, so
        # repeating them here would be the same bytes twice — and the reason
        # they were held to the end no longer applies, since holding them was
        # what made a 19-minute derivation opaque. The full list stays in
        # `planner.json`, which is what the measurements read.
        refused = len(outcome.tool_calls) - outcome.reads_answered
        rt.log(
            f"[plan] read {outcome.reads_answered} thing(s)"
            + (f", {refused} refused" if refused else "")
            + f" over {planned_for:.0f}s"
        )

    usage = accumulate_usage(
        state.get("run_usage"),
        planner_prompt_tokens=outcome.usage.prompt_tokens,
        planner_cached_tokens=outcome.usage.cached_tokens,
        planner_cache_write_tokens=outcome.usage.cache_write_tokens,
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
                "reads_answered": outcome.reads_answered,
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
        # Accumulated rather than assigned: a revision is more planning for the
        # same stage, and every path out of this node carries the total.
        "plan_seconds": state.get("plan_seconds", 0.0) + planned_for,
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
        # A malformed spec is the planner's error to fix, and this used to
        # escalate to a human on the first occurrence — which the comment here
        # already argued against and the code did anyway. A stage that fails
        # validation is the definition of a stage drawn wrongly, which is the
        # planner's tier of the three.
        #
        # It became worth fixing when `validate_stage` started rejecting a
        # fenced code block in the instruction. That rule asks a model to break
        # a strong habit, and one slip stopping an unattended overnight run is
        # a bad trade for a redraw that costs one planner call.
        #
        # `current` is deliberately not set: the stage does not exist, nothing
        # was cut for it, and anything keying off a stage in flight must not
        # see one. The counter is what bounds this — the redraw is a planner
        # pass that landed nothing, which is exactly what
        # `max_interventions_without_landing` counts.
        rt.log(
            f"[plan] rejected its own stage spec ({len(problems)} problem(s)); "
            "redrawing"
        )
        return {
            **base,
            "planner_interventions": state.get("planner_interventions", 0) + 1,
            "interventions_since_landing": stuck + 1,
            "last_failure": _failure_detail(
                "validation",
                "the stage spec it produced did not pass validation",
                "\n".join(f"- {p}" for p in problems),
            ),
            "next_hop": "plan",
        }

    if outcome.verdict == "revise":
        interventions = state.get("planner_interventions", 0) + 1
        keep_branch = outcome.revision_mode == "extend"
        rt.log(
            f"[plan] revising {new_stage.id} (revision "
            f"{state.get('revision', 0) + 1}, {outcome.revision_mode})"
        )

        # The queue is unaffected work and is kept, but a revision can widen
        # scope into it. Re-checked rather than discarded: the invariant needs
        # re-checking, not forgetting.
        requeued, dropped_by_revision = _requeue_after_revision(
            rt.cfg, rt.git, new_stage, state.get("stage_queue") or []
        )
        for note in dropped_by_revision:
            rt.log(f"[plan] {note}")

        update = {
            **base,
            **fresh_revision_fields(),
            "current": new_stage.model_dump(),
            "revision": state.get("revision", 0) + 1,
            "planner_interventions": interventions,
            "interventions_since_landing": stuck + 1,
            "stage_queue": requeued,
            "batch_notes": dropped_by_revision,
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

    # The rest of a batch, if the planner offered one. Checked here rather
    # than in the planner because the check resolves globs against the files
    # that exist, and only this side of the boundary can list them.
    queue, dropped_from_batch = _queue_from_batch(
        rt.cfg, rt.git, new_stage, outcome.additional_stage_fields
    )
    if queue:
        rt.log(
            f"[plan] {len(queue)} further stage(s) queued from this derivation: "
            + ", ".join(s["id"] for s in queue)
        )
    for note in dropped_from_batch:
        # Logged rather than swallowed. A batch quietly shrinking is how a
        # feature that is not working looks exactly like one that is.
        rt.log(f"[plan] {note}")

    derived = {
        **base,
        **fresh_stage_fields(),
        "current": new_stage.model_dump(),
        "revision": 0,
        "stage_index": index,
        "planner_interventions": interventions,
        "stage_queue": queue,
        # Replaced, not appended: these describe the batch just derived, and
        # the call that reads them has now happened. Carrying them forward
        # would report one overlap on every derivation for the rest of the run.
        "batch_notes": dropped_from_batch,
        "next_hop": "precheck",
    }

    # The third checkpoint, and the one an operator actually feels. The flag is
    # read at the top of this node too, but a pause requested *during* a
    # derivation arrives after that read — so the stage this call just produced
    # would be cut, run, reviewed and landed before the next read. Measured
    # once: a pause at 00:29:59 was followed by a stage derived at 00:31:28 and
    # landed fifteen minutes later.
    #
    # Nothing has run here, so the tree is as clean as it is between stages,
    # and the derived stage is held rather than discarded — re-deriving it
    # would cost another planner call for an answer already in hand.
    paused = _pause_escalation(rt.paths.pause_flag, state, "precheck")
    return {**derived, **paused} if paused else derived


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

    # The planner's notes about the plan, published here rather than held to
    # the landing gate. Their truth does not depend on the stage: they say
    # things like "the two documents contradict each other" and "not drawable",
    # found by reading the plan against the code while deriving the stage, and
    # a stage that escalates used to take them with it. That is the expensive
    # direction — a false blocker makes plan items read as blocked, and an item
    # that reads as blocked is never attempted.
    #
    # The code already conceded this for revisions, accumulating notes across a
    # redraw "because a redrawn stage is the same piece of work and its
    # observations about the plan are still true". Abandonment is the same
    # argument one step further.
    #
    # Before the cut and on the project branch, so the note precedes
    # `stage_start_sha` and the scope guard never sees a plan document in the
    # stage's diff. And it removes an obligation rather than adding one: this
    # used to be written inside `advance`, which then had to unwind it by hand
    # if any later step raised.
    #
    # The reviewer's observations are deliberately left on the landing gate.
    # Those are findings about a diff, and an abandoned diff does not exist.
    if state.get("pending_plan_notes") and rt.cfg.plan_addendum_path:
        update.update(_publish_plan_notes(state, rt, stage))

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

        # Read at the stage's start, not from the tree. The planner chose these
        # ranges against that commit, and on a rework the tree has already
        # moved under them. A range that will not resolve goes to the planner:
        # with no code in the instruction the excerpt is the code, so this is a
        # stage that cannot be attempted, and the participant that chose the
        # range is the one that can fix it.
        try:
            excerpts = resolve_excerpts(
                stage, rt.cfg, git=rt.git, sha=state.get("stage_start_sha") or ""
            )
        except ExcerptError as exc:
            return _planner_failure(
                state,
                "precondition",
                "a declared excerpt could not be read, so the executor prompt "
                "would have been built without code the instruction refers to",
                str(exc),
            )

        # Feedback is carried beside the prompt, not inside it. The difference
        # is not cosmetic: `build_executor_prompt` puts its retry opening at
        # the head of the string, so an attempt with feedback would differ from
        # one without at character zero — which is exactly wrong where a cached
        # prefix is the point. It arrives as its own conversation turn instead.
        prompt = build_executor_prompt(
            stage,
            rt.cfg,
            context=context,
            feedback=None,
            failure_layer=state.get("failure_layer"),
            cumulative_diff=cumulative_diff,
            excerpts=excerpts,
        )
        rt.write_artifact(
            state["stage_index"], stage.id, state.get("revision", 0), attempt,
            "prompt.md", prompt,
        )
        # The attempt's artifacts — the conversation, the prompt as sent, the
        # loop's own record. Kept per attempt because the first thing worth
        # reading when a stage does something inexplicable is what it was
        # actually given, and a rework overwrites nothing.
        history_dir = rt.paths.attempt_dir(
            state["stage_index"], stage.id, state.get("revision", 0), attempt
        )
        history_dir.mkdir(parents=True, exist_ok=True)
        # Named, because the one-line-per-call view in `tools.log` is not the
        # whole record: this file carries the results too, and it is the thing
        # to open when an attempt did something inexplicable. Until this line
        # an operator had to derive the directory to find it.
        rt.log(
            f"[execute] {stage.id}: attempt {attempt} — "
            f"{history_dir / TRANSCRIPT_FILENAME}"
        )
        result = rt.executor.run_agent_stage(
            stage,
            prompt,
            history_dir=history_dir,
            since_sha=state["stage_start_sha"],
            agent_context=_conventions(state, rt),
            feedback=feedback,
            failure_layer=state.get("failure_layer"),
        )

    if result.tool_counts:
        # The third agentic loop to report what it looked at. Counts rather
        # than the rendered calls the planner and reviewer log, because this
        # one makes sixty a cycle and the calls themselves are in the
        # conversation artifact beside this line's own log.
        asked = ", ".join(
            f"{n} {name}" for name, n in sorted(
                result.tool_counts.items(), key=lambda kv: -kv[1]
            )
        )
        refused = ", ".join(
            f"{n} {kind}" for kind, n in sorted(
                result.refusal_counts.items(), key=lambda kv: -kv[1]
            )
        )
        # Not the reviewer's `(N prompt, M cached)`. That line reports one
        # call, where the pair is the whole story; this loop resends its
        # conversation every turn, so a summed prompt re-counts the same prefix
        # 45 times and describes billing rather than work. Measured over 49
        # attempts, the sum spans 23k to 3.4M — impressive, and almost entirely
        # `turns × context` restated, which the tool-call count above already
        # implies. Cost is where the billing question belongs, and it goes to
        # `stage-costs.md`.
        #
        # These three answer different questions and none is derivable from
        # another. `peak` is the constraint that decides whether a batch fits —
        # the same figure recorded in `stage-costs.md`, so the log and the file
        # agree. `out` is what the model actually produced, the only number
        # here that is not a re-count of context it was handed, and the widest
        # spread of the set at 374 to 34,295. The cache rate is a percentage
        # because it is scale-free and because the informative reading is a low
        # one: the floor over that window was 50.3%, which is a prefix that
        # broke, and the raw pair buries it.
        #
        # Omitted entirely when the provider reported nothing, since "0% cached"
        # reads as a measurement rather than as its absence — the failure that
        # made Aider's cache accounting useless.
        paid = ""
        if result.usage is not None:
            prompt = getattr(result.usage, "prompt_tokens", 0)
            cached = getattr(result.usage, "cached_tokens", 0)
            rate = f", {round(cached / prompt * 100)}% cached" if prompt else ""
            paid = (
                f" ({result.context_tokens} peak, "
                f"{getattr(result.usage, 'completion_tokens', 0)} out{rate})"
            )
        rt.log(
            f"[execute] {stage.id}: {sum(result.tool_counts.values())} tool "
            f"call(s) over {result.cycles} cycle(s): {asked}"
            + (f"; refused {refused}" if refused else "")
            + paid
        )

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
    # What the loop proved green, carried to the gate so it does not ask
    # the same question of the same tree. Always written, including empty,
    # so a later attempt cannot inherit an earlier one's answers.
    measured["gate_records"] = dict(result.gate_records)
    if result.usage is not None:
        measured["run_usage"] = accumulate_usage(
            state.get("run_usage"),
            executor_prompt_tokens=getattr(result.usage, "prompt_tokens", 0),
            executor_cached_tokens=getattr(result.usage, "cached_tokens", 0),
            executor_cache_write_tokens=getattr(
                result.usage, "cache_write_tokens", 0
            ),
            executor_completion_tokens=getattr(
                result.usage, "completion_tokens", 0
            ),
        )
    # Accumulated, not replaced. Each attempt is its own Aider session with its
    # own running total, so a stage that took four attempts paid for four and
    # the figure worth recording is the stage's, not the last attempt's.
    if result.cost_usd:
        measured["executor_cost_usd"] = (
            state.get("executor_cost_usd", 0.0) + result.cost_usd
        )
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
    # A timeout with work on the tree goes to verify rather than back to the
    # executor. The loop is cooperative and commits before every gate, so
    # "there are commits" is a fact rather than the `is_clean()` inference this
    # used to draw from a subprocess that could be killed mid-write.
    if result.timed_out and rt.git.diff_names(state["stage_start_sha"]):
        rt.log(
            f"[execute] {stage.id}: the executor ran out of time but had "
            "committed its work — verifying what is there"
        )
        return {"next_hop": "verify", **measured}

    what = "timed out" if result.timed_out else "stopped without finishing"
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
        # What the executor's loop already proved on this tree. Empty on the
        # subprocess path, on script stages and on a resume that re-enters
        # here — all three being cases where nothing has tested anything.
        green_records=state.get("gate_records") or {},
    )

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "verify.log",
        _verify_log(outcome),
    )

    if outcome.flaky_files:
        _record_flakes(rt, stage.id, outcome.flaky_files, outcome.flaky_seeds)

    # A check may write. `checks` is arbitrary operator-declared shell, and an
    # autocorrecting linter is the obvious case — it is run precisely so the
    # executor does not spend an attempt on a line break. The executor commits
    # its own work before verify starts, so nothing else in the loop commits
    # what a check changed.
    #
    # Left uncommitted, it survives the stage. If the stage lands, `advance`
    # sweeps it up and no one notices; if the stage is blocked, reworked away,
    # or the run stops, it is orphaned in the tree and the *next* stage's
    # precheck refuses to cut a branch over changes it cannot attribute. That
    # escalation stopped a run, and it would have recurred on every blocked
    # stage.
    #
    # Committing here puts it on the child branch, where it is squashed on
    # landing and discarded with the branch otherwise — which is what the
    # branch-as-quarantine design already promises for everything else.
    if (
        rt.cfg.checks_commit_changes
        and state.get("stage_branch")
        and rt.git.uncommitted()
    ):
        touched = rt.git.uncommitted()
        rt.git.commit_all(f"[{stage.id}] verification checks")
        rt.log(
            f"[verify] {stage.id}: checks changed {len(touched)} file(s); "
            "committed to the stage branch"
        )

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
        agent_context=_conventions(state, rt),
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
        cache_write_tokens=outcome.usage.cache_write_tokens,
        completion_tokens=outcome.usage.completion_tokens,
    )
    stage_usage = accumulate_usage(
        state.get("stage_usage"),
        prompt_tokens=outcome.usage.prompt_tokens,
        cached_tokens=outcome.usage.cached_tokens,
        cache_write_tokens=outcome.usage.cache_write_tokens,
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
        "review_record": outcome.record,
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

    # What landed, first, and from the reviewer — the only participant that saw
    # the diff. Everything after this in the file was written before the work.
    landed = append_outcome(
        rt.cfg.target_repo,
        rt.cfg.plan_addendum_path,
        stage_id=stage.id,
        # The dedicated record, falling back to the verdict rationale for a
        # reviewer that has not been asked for one. Better a gate-shaped entry
        # than none.
        summary=state.get("review_record") or state.get("review_summary") or "",
    )
    if landed is not None:
        rt.log(f"[advance] recorded what {stage.id} landed, in the reviewer's words")

    # The planner's notes are not written here any more — `precheck` publishes
    # them when it cuts the branch, because their truth does not depend on this
    # stage landing. What remains is the reviewer's findings.
    #
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

    # The reviewer's entries — what landed, and its out-of-scope findings — are
    # written to the worktree and committed a few lines below, so anything that
    # raises in between leaves them modified and uncommitted. The next resume
    # re-enters at verify, whose scope guard sees a plan document changed by a
    # stage and routes it to the planner as the executor wandering into the
    # record of its own work — a diagnosis that is wrong, and that the planner
    # cannot act on because it did not happen.
    #
    # The planner's notes used to be in here too and are now committed by
    # `precheck` before the branch is cut, so they are outside this transaction
    # on purpose and need no unwinding. What is left is everything written by
    # the participant that saw the diff, which is exactly what this landing is
    # allowed to lose if the landing fails.
    #
    # `squash_merge` already restores the project branch if its own commit
    # fails. This covers the other half: either the stage lands or the tree is
    # as advance found it.
    try:
        rt.git.commit_all(f"[{stage.id}] wip")
        merge_sha = rt.git.squash_merge(
            branch,
            rt.cfg.project_branch,
            # Same text the progress log gets, and for the same reason: it is
            # the only account written by a participant that saw the diff.
            _commit_message(
                stage,
                state.get("review_record") or state.get("review_summary") or "",
            ),
        )
    except Exception:
        if (landed is not None or seen is not None) and rt.cfg.plan_addendum_path:
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
        "executor_cost_usd": state.get("executor_cost_usd", 0.0),
        "withheld_reads": list(state.get("withheld_reads") or []),
        "base_sha": start_sha,
        "merge_sha": merge_sha or rt.git.head_sha(),
        "wall_seconds": max(time.time() - (state.get("stage_started_at") or 0), 0.0),
        # From precheck to here. `plan_seconds` is the derivation that
        # preceded it, which no per-stage figure counted before.
        "plan_seconds": state.get("plan_seconds", 0.0),
        "test_seconds": state.get("test_seconds", 0.0),
        "review_verdict": state.get("review_verdict"),
        "review_summary": state.get("review_summary"),
        "review_record": state.get("review_record"),
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
    if result.get("executor_context_tokens") or result.get("executor_cost_usd"):
        append_stage_cost(
            rt.project.project_dir,
            stage_id=stage.id,
            merge_sha=result["merge_sha"],
            files=len(stage.edit_files),
            context_tokens=result.get("executor_context_tokens", 0),
            cost_usd=result.get("executor_cost_usd", 0.0),
            roles=_roles_for_record(rt.cfg),
        )
    rt.log(f"[advance] {stage.id} landed as {result['merge_sha'][:12]}")

    landed = {
        **fresh_stage_fields(),
        # Recorded on the stage above; cleared here so the next one is not
        # billed for this one's derivation. Not in `fresh_stage_fields`, which
        # `plan` also spreads — see its docstring.
        "plan_seconds": 0.0,
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
        # Overwritten by `_next_from_queue` when a stage is waiting. Stated
        # here so the landing has a complete answer of its own and the queue is
        # an override rather than the only thing that routes.
        "next_hop": "plan",
    }

    # A stage from the same derivation, if one is waiting. Merged after the
    # landing so `stage_index` is the landed one's when it is read, and before
    # the pause so an operator's stop still wins.
    landed = {**landed, **_next_from_queue(state, landed["stage_index"] - 1)}

    # Merged onto the landing, never in place of it. The stage is squash-merged
    # and on the branch whatever the run does next; replacing this update with
    # the escalation would leave `completed` short by one and `stage_index`
    # unmoved, and a resume would re-derive work that is already landed.
    paused = _pause_escalation(rt.paths.pause_flag, state)
    return {**landed, **paused} if paused else landed


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


def _queue_from_batch(cfg, git, first, extra_fields: list[dict]) -> tuple[list[dict], list[str]]:
    """The stages to hold behind the one being started, and what was dropped.

    The orthogonality check runs here rather than in the planner because it
    needs the repository: globs are resolved against the files that exist, not
    compared as strings, and only this side of the boundary can list them.

    The first stage is never dropped. It is the one about to run, and the check
    exists to protect what is queued behind it.

    A failure to list the repository drops the batch rather than the run: a
    stage the planner has already produced is worth more than the saving, and
    the queue is an optimisation over asking again.
    """
    if not extra_fields:
        return [], []
    # The cap counts the stage being started, so `max_batch_stages: 2` means
    # one now and one queued. Reported rather than trimmed in silence — a
    # planner spending output on stages that are discarded is exactly the
    # overthinking this feature has to be watched for, and an operator cannot
    # see it happening if the trim says nothing.
    allowed = max(cfg.planner.max_batch_stages - 1, 0)
    notes: list[str] = []
    if len(extra_fields) > allowed:
        notes.append(
            f"the planner offered {len(extra_fields) + 1} stages and the cap "
            f"(planner.max_batch_stages) is {cfg.planner.max_batch_stages}; "
            f"{len(extra_fields) - allowed} were discarded"
        )
        extra_fields = extra_fields[:allowed]
    if not extra_fields:
        return [], notes
    try:
        tracked = git.tracked_paths_now()
    except GitError:  # pragma: no cover - defensive
        return [], notes
    candidates = [first] + [cfg.stage_from_planner(f) for f in extra_fields]
    kept, dropped = orthogonal_stages(candidates, tracked)
    return [s.model_dump() for s in kept[1:]], notes + dropped


def _requeue_after_revision(cfg, git, revised, queue: list[dict]) -> tuple[list[dict], list[str]]:
    """The queue that survives a revision of the stage in front of it.

    Rework is never batched — a failed stage owns a branch and a branch belongs
    to one stage — but the stages queued behind it are unaffected work and are
    kept. What can change is the revised stage: widening `edit_files` to fix a
    scope violation may make it name a file a queued stage was drawn against.

    So the check is re-run rather than the queue discarded. Putting the revised
    stage at the head asks exactly the right question: it is first so it is
    never dropped, the queue was already pairwise orthogonal, and the only
    drops that can appear are the ones the revision caused.
    """
    if not queue:
        return [], []
    try:
        tracked = git.tracked_paths_now()
    except GitError:  # pragma: no cover - defensive
        return [], []
    stages = [revised] + [cfg.stage_from_planner(f) for f in queue]
    kept, dropped = orthogonal_stages(stages, tracked)
    return [s.model_dump() for s in kept[1:]], dropped


def _next_from_queue(state: RunState, landed_index: int) -> dict:
    """Where the run goes after a stage lands: the next queued one, or the planner.

    The whole saving of a batch. One derivation answered for several stages, so
    taking the next from the queue skips a planner call worth 5 to 7 minutes
    against a stage of about thirteen.

    The queued stage gets its own index — each stage is its own branch and its
    own log directory, and reusing the index of the stage that just landed
    would put two stages in one place.

    Returned as an update to merge rather than applied here, so `advance` can
    join it to the landing bookkeeping and a pause can be merged over the top
    of both without any of the three losing what the others wrote.
    """
    queue = list(state.get("stage_queue") or [])
    if not queue:
        return {"next_hop": "plan"}
    head, rest = queue[0], queue[1:]
    return {
        "current": head,
        "stage_queue": rest,
        "stage_index": landed_index + 1,
        "revision": 0,
        "next_hop": "precheck",
    }


def _pause_escalation(flag, state: RunState, ready_hop: str = "") -> dict | None:
    """The pause stop, if the operator has asked for one.

    Consulted at both points where the run is genuinely between stages: before
    a planner call, and immediately after a stage has been squash-merged. Those
    were the same instant while `advance` routed only to `plan`, and step 10
    separates them — a queue of stages from one derivation means `advance` goes
    to the next queued stage, and a pause requested during the first of five
    would otherwise wait for all five.

    What makes stopping safe is not that a planner call is next. It is that the
    merge is done, the stage branch is gone and the tree is clean, which is
    true at the end of `advance` whatever follows.

    One helper because the message makes a promise — "nothing is half-done" —
    that is only true where the check sits. A second copy beside a second check
    is how one of them comes to be wrong.
    """
    if not flag.exists():
        return None
    note = flag.read_text().strip()
    where = (
        "with the next stage derived and waiting to start"
        if ready_hop
        else "between stages"
    )
    return {
        **_escalate(
            "paused",
            f"Paused at your request, {where}. Nothing is wrong and nothing is "
            "half-done: everything that landed is on the project branch and no "
            "stage was in flight.\n\n"
            + (f"Your note: {note}\n\n" if note else "")
            + f"`orchestrator resume {state.get('run_id')}` picks up "
            + ("that stage." if ready_hop else "from the next stage."),
        ),
        # The hop the run was about to take, recorded rather than inferred. A
        # resume that had to deduce "there is a stage ready" from `current`
        # being set and no failure recorded would confuse it with a stage
        # awaiting revision, which must not be re-run unrevised. Written on
        # every pause, including as empty, so one stop cannot inherit the value
        # of an earlier one.
        "paused_before": ready_hop,
    }


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


def _publish_plan_notes(state: RunState, rt: Runtime, stage: Stage) -> dict:
    """Write the planner's notes to the log and commit them, on this branch.

    Committed rather than left in the worktree because `precheck` is about to
    cut a branch, and an uncommitted plan document would be swept into the
    stage's diff and reported by the scope guard as the executor editing the
    record of its own work — a diagnosis that is wrong and that the planner
    cannot act on.

    Cleared on the way out. The notes accumulate across a redraw, and a
    revision re-enters `precheck`, so without clearing them the second cut
    republishes everything the first one already wrote.
    """
    plan_sha = state.get("plan_sha") or state.get("base_sha") or ""

    def read_plan(path: str) -> str | None:
        try:
            return rt.git.show_file(plan_sha, path)
        except GitError:
            return None

    notes = state.get("pending_plan_notes") or []
    written = append_notes(
        rt.cfg.target_repo,
        rt.cfg.plan_addendum_path,
        notes,
        stage_id=stage.id,
        read_plan=read_plan,
        plan_sha=plan_sha,
    )
    if written is None:
        return {"pending_plan_notes": []}

    # Onto the project branch explicitly. On a revision `precheck` re-enters
    # with HEAD still on the previous attempt's stage branch, which
    # `cut_stage_branch(fresh=True)` is about to delete — committing the note
    # there would lose it in precisely the case this move exists to fix.
    # `cut_stage_branch` checks out the same branch a few lines later, so this
    # assumes nothing new about the state of the tree.
    if rt.git.current_branch() != rt.cfg.project_branch:
        rt.git.checkout(rt.cfg.project_branch)
    rt.git.commit_all(f"[{stage.id}] plan observations from deriving this stage")
    rt.log(
        f"[precheck] recorded {len(notes)} plan observation(s) in "
        f"{written.relative_to(rt.cfg.target_repo)}"
    )
    return {"pending_plan_notes": []}


def _first_line(stage: Stage) -> str:
    text = stage.instruction or stage.command or stage.id
    return text.strip().splitlines()[0][:70]


# git's own convention, and the width every tool that renders a log assumes.
_BODY_WIDTH = 72


def _roles_for_record(cfg) -> tuple[tuple[str, str, str], ...]:
    """Which models and efforts produced a stage, for `stage-costs.md`.

    Read off the config at landing rather than stored per role, because the
    config is the thing being tuned and a stage is the grain it is tuned at:
    the executor, planner and reviewer efforts were each changed mid-project,
    and every cost line written across those changes is otherwise identical.

    The executor's effort is `reasoning_effort` and may be absent — not every
    endpoint takes one — which is why an empty effort renders as the model
    alone rather than a trailing slash.
    """
    return (
        ("exec", cfg.executor.model, cfg.executor.reasoning_effort or ""),
        ("plan", cfg.planner.model, cfg.planner.effort or ""),
        ("review", cfg.reviewer.model, cfg.reviewer.effort or ""),
    )


def _commit_message(stage: Stage, record: str) -> str:
    """The landing commit: subject, blank line, wrapped body.

    It used to be `[{stage.id}] {instruction[:70]}` and nothing else — a
    subject cut mid-word, no body, describing the stage's *intent*, since the
    instruction is written before the work.

    The subject is now the id alone. It is not a slug of some title the tooling
    threw away: the planner authors it in that form directly, so the truncated
    remainder was repeating in prose what the identifier already said, at the
    cost of pushing the subject past 72 columns.

    The reviewer's account of what the stage actually did is the only
    description written by a participant that has seen the diff, and it was
    already in hand here, going to the progress log and nowhere else. Putting
    it in the commit is what makes `git log` on the project branch answer what
    happened rather than what was asked for — which is the same argument the
    addendum's docstring makes for preferring the fact to the claim.

    `decode_escapes` for the reason the addendum uses it — a double-escaped
    `\\u2014` otherwise lands in the commit looking like a bug in this tool —
    and so that the commit body and the progress-log entry are the same bytes
    rather than two renderings of one string that could drift.
    """
    subject = f"[{stage.id}]"
    body = _wrap_body(record)
    return f"{subject}\n\n{body}" if body else subject


def _wrap_body(record: str) -> str:
    """The reviewer's text as commit prose, paragraphs preserved.

    Long words are never broken: these bodies are dense with paths, and a path
    split across a line is a path nobody can grep for. An over-long line is the
    cheaper failure.
    """
    text = decode_escapes((record or "").strip())
    if not text:
        return ""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        collapsed = " ".join(para.split())
        if not collapsed:
            continue
        out.append(
            textwrap.fill(
                collapsed,
                width=_BODY_WIDTH,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n\n".join(out)
