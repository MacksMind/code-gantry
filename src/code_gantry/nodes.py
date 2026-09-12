"""Node logic.

Each function takes (state, runtime) and returns a partial state update. The
runtime is passed explicitly rather than reached for, which is what makes every
node callable from a test with a stubbed planner, executor, and reviewer.

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
import contextlib
import hashlib
import os
import time
from datetime import datetime
from pathlib import Path

from code_gantry import hostlock, mesh
from code_gantry.commands import clip_for_model
from code_gantry.cachekey import cache_key
from code_gantry.config import ProjectConfig, Stage, validate_stage
from code_gantry.executor import (
    TRANSCRIPT_FILENAME,
    ExcerptError,
    resolve_excerpts,
)
from code_gantry.flake import adjudicate, append_flakes, predates_stage
from code_gantry.gateway import resolve_policy
from code_gantry.gitops import GitError
from code_gantry.ledger import (
    CANDIDATE_PUSHED,
    REWORK_RELEASED,
    REWORK_TAKEN,
    CLAIMED,
    FINDING_RESOLVED,
    LANDED,
    RELEASED,
    Ledger,
    apply_fold,
    should_fold,
    FINDING_CLAIMED,
    FINDING_RELEASED,
    STAGE_DERIVED,
    STAGE_DONE,
    STAGE_DROPPED,
    STAGE_TAKEN,
)
# The gate's clip, under the name thirteen call sites here already use.
# Imported rather than redefined: the budget and the helper are one
# decision, and `gates` is where the other half of it lives. Safe at
# module level — `gates` imports nothing that reaches back here.
from code_gantry.gates import clip as _clip
from code_gantry.repotools import render_counts
from code_gantry.globs import matches_any
from code_gantry.planner import append_stage_cost, append_status, recent_stage_costs
from code_gantry.prompts import (
    build_executor_prompt,
    build_planner_messages,
    build_review_messages,
)
from code_gantry.reviewer import issues_as_feedback
from code_gantry.runtime import Runtime, ledger_references
from code_gantry.state import (
    usage_deltas,
    clear_rework_after_approval,
    RunState,
    zero_usage,
    accumulate_usage,
    evidence_surviving_a_revision,
    fresh_revision_fields,
    fresh_stage_fields,
)
from code_gantry.verify import Layer, Route, diff_digest, run_verify


# Command output bound where it reaches a model or a log, rather than where it
# is captured. The runner keeps everything so the parsers can see it; a prompt
# cannot carry a third of a megabyte of rspec, and a planner intervention is
# expensive enough without paying for a coverage report.
#
# The budget lives in `gates`, with the other half of this decision. It was
# declared here too, identically, and nothing would have failed when the two
# drifted — one role would simply have started giving a model less of a failure
# to read than the other. That is the same duplication `clip_for_model` was
# extracted to end, regrown one level up: the function was centralised and the
# number it is called with was not.

# A reviewer note in the run log is for a human scanning it, not the record —
# the whole finding, its detail and its evidence are in `review.json` and the
# progress log. Enough to know whether to go and look.
REVIEWER_NOTE_CHARS = 240




def current_stage(state: RunState, rt: Runtime) -> Stage | None:
    """The stage in flight, rebuilt from the checkpoint.

    Filtered to the fields `Stage` still declares, because `Stage` is
    `extra="forbid"` and `current` was written by whatever build derived it.
    Deleting a field would otherwise raise on the next *resume* of a run
    already hours deep — a pydantic error from inside a node, for a stage that
    is perfectly valid.

    Checked before it happened: the live checkpoint held `kind: "agent"` on a
    34-stage run when script stages were removed. A fresh run is always a
    fallback, since the landed work is on the project branch rather than in the
    checkpoint, but it discards the derived stage and the queue behind it —
    a poor trade for a field nobody reads. `driver._merge` already drops keys
    `RunState` does not declare; this is the same rule for the one structure
    that rebuilds a pydantic model out of state.
    """
    fields = state.get("current")
    if not fields:
        return None
    known = set(Stage.model_fields)
    return Stage(**{k: v for k, v in fields.items() if k in known})


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


def _render_sent_prompt(messages: list[dict]) -> str:
    """The planner prompt as sent, with a size for every block.

    Plain text rather than JSON: its reader is a person asking why a prompt is
    the size it is, and the first thing they need is the arithmetic.
    """
    lines = ["# Planner prompt as sent", ""]
    total = 0
    parts: list[str] = []
    for i, message in enumerate(messages):
        content = message.get("content")
        blocks = (
            [{"text": content}] if isinstance(content, str) else list(content or [])
        )
        for j, block in enumerate(blocks):
            text = block.get("text", "") if isinstance(block, dict) else str(block)
            total += len(text)
            cache = "" if not isinstance(block, dict) else (
                "  [cache breakpoint]" if block.get("cache_control") else ""
            )
            label = f"message {i} ({message.get('role')}) block {j}"
            lines.append(f"- {label}: {len(text):,} chars{cache}")
            parts.append(f"\n\n## {label} — {len(text):,} chars{cache}\n\n{text}")
    lines.insert(1, "")
    lines.insert(1, f"**Total: {total:,} characters across {len(parts)} block(s).**")
    return "\n".join(lines) + "".join(parts) + "\n"


def _gate_history(state: RunState, rt: Runtime) -> list[dict]:
    """This stage's gate verdicts, read back out of the checkpoint.

    Read rather than carried, so there is no field for a reset helper to
    forget. Returns nothing before a stage exists, and nothing if the read
    fails: the planner call is worth making without this, and a checkpoint
    that cannot be opened is not a reason to stop a run that is otherwise
    fine.
    """
    # Imported here because `driver` imports this module; `pin_modules` loads
    # the package up front so a live run never resolves this from disk.
    from code_gantry.driver import gate_history

    stage = state.get("current")
    if not stage or not stage.get("id"):
        return []
    try:
        return gate_history(rt.paths.state_db, rt.paths.run_id, stage["id"])
    except Exception:  # pragma: no cover - a read that fails costs one block
        return []


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
    # An exploratory replan is not a run that has stopped making progress —
    # it is one that made a change precisely to find out what the change does,
    # and the budget below exists to detect the opposite. Counting it would
    # cap exploration at three, which contradicts asking for it.
    #
    # Read off the fact rather than the declared kind: an attempt that says
    # "incomplete" and committed nothing explored nothing, and is a stuck
    # attempt wearing the other label. The sha either side of it decides.
    _failed = state.get("last_failure") or {}
    exploratory = bool(
        _failed.get("layer") == "replan" and _failed.get("committed_work")
    )
    charge = 0 if exploratory else 1
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
            f"  - `{rt.cfg.resume_command(flags='--reset-progress-budget')}`, "
            "if you have changed something that makes the earlier failures no "
            "longer apply. That is you asserting it, not the run inferring "
            "it.\n"
            "  - Raise `max_interventions_without_landing`, if the work "
            "legitimately needs more attempts. Editing the config ends this "
            "run: it is pinned to the config's blob sha, so the next command "
            "is `start` rather than `resume`, and what has landed is on the "
            "project branch either way.\n"
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
    paused = _pause_escalation(rt.paths.pause_flag, state, rt.cfg)
    if paused is not None:
        return paused

    plan_text, projection = rt.plan_text(), rt.projection()
    if should_fold(plan_text, projection, rt.cfg.ledger.fold_ratio):
        written = apply_fold(rt.ledger, actor=f"run:{rt.paths.run_id}")
        rt.log(f"[plan] folded {written} mark(s) into the plan text")
        plan_text, projection = rt.plan_text(), rt.projection()

    if stage is not None and rt.ledger is not None:
        elsewhere = _held_elsewhere(rt, stage)
        if elsewhere:
            # A resume can arrive holding a stage another run took in the
            # meantime; revising it would spend a planner call on work that
            # is already someone else's.
            rt.log(f"[plan] letting go of {stage.id}: {', '.join(elsewhere)}")
            stage = None
            state = {**state, "current": None, "revision": 0}

    # What is already drawn, before anything is paid for and before the
    # planner semaphore is reached for at all: the second bay to arrive
    # finds the first bay's stages waiting and takes one instead of drawing
    # them again.
    #
    # Outside the semaphore, because taking is short and deriving is not.
    # A stage becomes available in the middle of somebody's derivation when
    # a run dies and the next run on its host gives its claims back — which
    # is the run about to work it. Behind the semaphore that bay would sit
    # out a whole planner call before it could pick up what it had just
    # freed. `_take_derived` holds the ledger's own writer instead, which is
    # what makes the check and the claim one act.
    if stage is None:
        # Rework first: it is closer to done than anything the planner would
        # draw, and it is holding plan keys while it waits.
        taken = _take_rework(rt, state) or _take_derived(rt, state)
        if taken is not None:
            return taken

    # One derivation at a time against this ledger anywhere. Through the
    # daemon rather than this machine's own lock, because the bays that race
    # are on different hosts — one project, one planner, whichever machine
    # it runs on. A revision draws nothing from the open list, so it holds
    # no semaphore and keeps no other bay waiting through its planner call.
    #
    # **Never taken while the ledger's writer is held.** The fold and the
    # take both reach for that inside this, so a bay that blocked here
    # holding it would wait for a deriver that is waiting for it.
    holding = (
        mesh.hold(_planner_lock(rt), f"{bay_id(rt)} {rt.paths.run_id}", rt.log)
        if stage is None else contextlib.nullcontext([0.0])
    )
    with holding as waited:
        if waited[0]:
            rt.log(f"[plan] waited {waited[0]:.0f}s for the planner semaphore")
        if stage is None:
            # Asked again now it holds it: a derivation that finished while
            # this bay waited has left stages nobody has taken, and drawing
            # more would be paying for what is already there.
            taken = _take_derived(rt, state)
            if taken is not None:
                return taken
            plan_text, projection = rt.plan_text(), rt.projection()

        messages = build_planner_messages(
            cfg=rt.cfg,
            plan_text=plan_text,
            projection=projection,
            completed=state.get("completed") or [],
            current_stage=stage,
            failure=state.get("last_failure"),
            opening_failure=state.get("opening_failure"),
            gate_history=_gate_history(state, rt),
            revision=state.get("revision", 0),
            interventions_used=state.get("planner_interventions", 0),
            interventions_max=limits.max_planner_interventions,
            layout=rt.layout(state.get("plan_sha") or state.get("base_sha") or ""),
            agent_context=_planner_context(state, rt),
            stage_costs=recent_stage_costs(rt.project.project_dir),
            # The runner's own tally, read live for the same reason the plan is:
            # it describes the tree, and the stages being drawn are what change
            # it. Behind the cache mark, on the same clock as the progress log.
            test_warnings=rt.live_test_warnings,
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

        # Before the call, because everything in it is known now and because the
        # one time it is wanted is when the call does not come back. The executor
        # has had `sent-prompt.md` for exactly this reason; the planner has had
        # nothing, and a 400 rejecting a prompt as too long left no way to find out
        # what was in it. An hour of reconstruction from `config.yaml` and the plan
        # tree accounted for 575,633 characters of a prompt the provider measured
        # at 1,077,433 tokens, and reconstruction cannot be made to converge —
        # a prompt is assembled from a dozen optional inputs and the missing one is
        # by definition the one you did not think to pass.
        #
        # Sizes beside the text, because the question asked of this file is almost
        # always "which block is enormous" rather than "what does it say".
        rt.write_artifact(
            state.get("stage_index", 0),
            stage.id if stage else "plan",
            state.get("revision", 0),
            _attempt(state),
            "planner-prompt.md",
            _render_sent_prompt(messages),
        )

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
            detail = render_counts(outcome.tool_counts or {})
            rt.log(
                f"[plan] read {outcome.reads_answered} thing(s)"
                + (f", {refused} refused" if refused else "")
                + f" over {planned_for:.0f}s"
                + (f": {detail}" if detail else "")
                + _spent(rt.planner)
            )

        usage = accumulate_usage(
            state.get("run_usage"),
            **usage_deltas("planner_", outcome.usage),
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
                    # Every field of the structured response, and the completeness
                    # is the point: this artifact is what anyone reaches for to ask
                    # what the planner returned on a call. It used to omit three —
                    # `status_entry`, `additional_stages` and the deferral list —
                    # and the omission was silent, so a question answered from here
                    # got a confident wrong answer instead of a missing one.
                    "status_entry": outcome.status_entry,
                    "additional_stages": outcome.additional_stage_fields,
                    "revision_mode": outcome.revision_mode,
                    "stage": outcome.stage_fields,
                    "usage": {
                        "prompt_tokens": outcome.usage.prompt_tokens,
                        "cached_tokens": outcome.usage.cached_tokens,
                        # Billed above base rate. A prefix written on every call and
                        # never read back costs more than no caching at all, and the
                        # run totals average that away — per call is where it shows.
                        "cache_write_tokens": outcome.usage.cache_write_tokens,
                        # The part of the line above that cost 2x rather than
                        # 1.25x. Recorded beside its total because a sum of two
                        # rates cannot be re-derived from the sum afterwards, and
                        # this artifact is what a later cost question is asked of.
                        "cache_write_1h_tokens": outcome.usage.cache_write_1h_tokens,
                        "completion_tokens": outcome.usage.completion_tokens,
                        # The one figure here that is not a total: the largest
                        # single call of the loop. The others say what the
                        # derivation cost; this says how close it came to the
                        # window it has to fit inside, which is the question the
                        # read budgets exist to answer and the one nothing was
                        # recording when a call was rejected at 1,103,000 tokens.
                        "peak_prompt_tokens": outcome.usage.peak_prompt_tokens,
                    },
                    # Both recorded even when empty, and that is the point. An
                    # absent key cannot be told apart from a feature that never
                    # ran, and "the planner looked and had nothing to say" is a
                    # different fact from "the planner did not look" — one is the
                    # plan being accurate, the other is a bug.
                    "tool_calls": list(outcome.tool_calls),
                    # And what the semantic index actually said, which the line
                    # above cannot carry. Every other read here is reproducible
                    # from its path and the sha; a semantic hit depends on an
                    # index, a cutoff and an embedding model, so the same question
                    # later returns something else and the record was the only
                    # copy there was ever going to be.
                    "semantic_results": list(outcome.semantic_results),
                    "tool_counts": dict(outcome.tool_counts),
                    "reads_answered": outcome.reads_answered,
                    "plan_notes": list(outcome.plan_notes),
                    "client_failure": outcome.failed,
                    # Present only when we rejected an answer the model did give.
                    # Null for a refusal or a transport failure, where the verdict
                    # above is the whole of what happened.
                    "rejected_answer": outcome.raw,
                    # Always present, null included, and the same shape the other
                    # two roles write. A block used to say only that there was no
                    # verdict; this says what came back instead.
                    "turn_end": outcome.turn_end,
                },
                indent=2,
            ),
        )

        notes = list(state.get("planner_notes") or [])
        notes.append(f"{outcome.verdict}: {outcome.reasoning}")
        base = {
            "run_usage": usage,
            # And onto the stage, which is what makes a per-stage figure possible
            # for the participant that spends most of the money. Accumulated for
            # the same reason `plan_seconds` is: a revision is more planning for
            # the same stage.
            "stage_usage": accumulate_usage(
                state.get("stage_usage"),
                **usage_deltas("planner_", outcome.usage),
            ),
            "planner_notes": notes,
            # Accumulated rather than assigned: a revision is more planning for the
            # same stage, and every path out of this node carries the total.
            "plan_seconds": state.get("plan_seconds", 0.0) + planned_for,
        }

        # Published when found: a note is true whether or not the stage lands.
        opened = open_findings(
            rt.ledger, rt.git, outcome.plan_notes, by="planner",
            stage_id=stage.id if stage else (outcome.stage_fields or {}).get("id"),
            run_id=rt.paths.run_id,
            log=rt.log,
        )
        if opened:
            rt.log(f"[plan] opened {opened} finding(s)")

        if outcome.verdict == "project_complete":
            rt.log("[plan] project complete")
            return {**base, "next_hop": "finalize"}

        if outcome.verdict == "blocked":
            return {
                **base,
                **_escalate("planner", f"The planner blocked the run: {outcome.reasoning}"),
            }

        new_stage = rt.cfg.stage_from_planner(outcome.stage_fields or {})
        known_keys, open_ids = ledger_references(rt.ledger)
        problems = validate_stage(
            new_stage, rt.cfg, known_keys=known_keys, open_findings=open_ids,
        )
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
                "interventions_since_landing": stuck + charge,
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
                "current": {
                    **new_stage.model_dump(),
                    "derived_id": (state.get("current") or {}).get("derived_id", ""),
                    **evidence_surviving_a_revision(state.get("current"), keep_branch),
                },
                "revision": state.get("revision", 0) + 1,
                "planner_interventions": interventions,
                "interventions_since_landing": stuck + charge,
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
            rt.cfg, rt.git, new_stage, outcome.additional_stage_fields,
            ledger=rt.ledger,
        )
        # Everything this derivation produced, named, on one line and on every
        # derivation. Two lines said this before — the stage about to run, and a
        # count of the rest *if there were any* — so a derivation that returned one
        # stage said nothing about being a batch of one. That is the reading the
        # question is actually about: `additional_stages` was added to amortise a
        # seven-minute planner call over several stages, and whether it is doing
        # so is answered by the distribution, which cannot be recovered from a log
        # that only speaks up when the answer is greater than one.
        new_stage, queue = _record_derivation(rt, new_stage, queue, state)
        rt.log(f"[plan] derived: " + ", ".join([new_stage.id, *(s["id"] for s in queue)]))
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
        paused = _pause_escalation(rt.paths.pause_flag, state, rt.cfg, "precheck")
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

    # Before the branch is cut and before anything runs: a stage whose quoted
    # ranges no longer describe the file cannot be executed, and the executor
    # would be handed our excerpt block as the only code it gets.
    moved = stale_excerpts(rt.git, stage)
    if moved:
        # Said out loud, because the reason reached the planner and the
        # checkpoint and nowhere a person would look. Observed live: a stage
        # landed cleanly at 11:42:03, the next printed its precheck header at
        # 11:42:03 and `[plan] revising` at 11:42:04, and nothing in between
        # said why — so a correct rejection, one second after a green full
        # suite, read as something having gone wrong. Every other gate names
        # its failure in the log; this one routed silently.
        #
        # The queue count goes with it because discarding the tail is the
        # other half of what just happened and is otherwise invisible.
        queued = len(state.get("stage_queue") or [])
        rt.log(
            f"[precheck] {stage.id}: back to the planner — "
            f"{', '.join(moved)} changed since it was quoted"
            + (f"; {queued} queued stage(s) left in the ledger" if queued else "")
        )
        if rt.ledger is not None:
            _drop_derived(rt, stage, "stale excerpts: " + ", ".join(moved))
        return {**update, **_stale_excerpt_failure(state, stage, moved)}

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

    # Claim the keys before the branch is cut; a key another run has taken
    # or a person has closed sends the stage back to the planner.
    if rt.ledger is not None:
        taken = _references_taken(rt, stage, state)
        if taken:
            queued = len(state.get("stage_queue") or [])
            rt.log(
                f"[precheck] {stage.id}: back to the planner — "
                f"{', '.join(taken)} not available"
                + (f"; {queued} queued stage(s) left in the ledger" if queued else "")
            )
            _drop_derived(rt, stage, "references taken: " + ", ".join(taken))
            return {**update, **_taken_key_failure(state, stage, taken)}
        _claim_references(rt, stage, state)

    # Cut or resume the child branch. Anything on it is quarantined: nothing
    # reaches the project branch without passing the review gate.
    if not state.get("stage_branch"):
        # No branch in state means a fresh start: either the first attempt at
        # this stage, or a restart where the planner discarded the approach. In
        # both cases any branch left under this name is the thing being
        # discarded, so it must not be inherited.
        if rt.cfg.remote_landing:
            try:
                if _sync_project_branch(rt):
                    rt.log(f"[precheck] {stage.id}: origin moved; the stage starts from the pulled tip")
            except GitError as e:
                return {
                    **update,
                    **_escalate(
                        "remote_landing",
                        f"Pulling {rt.cfg.project_branch!r} from origin before "
                        f"cutting {stage.id!r} failed:\n{_clip(str(e))}\n\n"
                        "Resolve the branch against origin by hand and resume.",
                    ),
                }
        branch = rt.cfg.stage_branch(state.get("stage_index", 0), stage.id)
        start = rt.git.cut_stage_branch(branch, rt.cfg.project_branch, fresh=True)
        update["stage_branch"] = branch
        update["stage_start_sha"] = start
        update["stage_started_at"] = time.time()
        rt.log(f"[precheck] cut {branch} at {start[:12]}")

    # The model this stage runs against, chosen here and held for every attempt
    # and revision of it. A routing policy resolved once per *run* re-sampled
    # the frontier only when a human restarted: one run held one model for 30
    # stages and another for the 11 after it, and the switch was a resume
    # rather than the router changing its mind. Per stage follows a price move
    # mid-run, gives each stage one model to attribute its cost and its rework
    # to, and lets a model that is serving badly stop at the next stage instead
    # of lasting the run.
    #
    # Not per attempt, and not per turn: those are one conversation, and a turn
    # served by a different model reads nothing of the prefix the others built.
    # Measured on the artifacts — 2 attempts of 468 had two models in one
    # conversation, one of them a single foreign turn inside 32.
    #
    # "Only when unset" is what makes a revision keep the stage's model, and it
    # relies on `fresh_stage_fields` clearing it when a stage begins or lands.
    # Without that half it locks the first stage's answer for the whole run,
    # which is what it shipped as: 042 resolved and 043 never asked.
    if not state.get("stage_executor_model"):
        update["stage_executor_model"] = resolve_policy(
            rt.cfg.executor, log=rt.log
        ).model

    update["next_hop"] = "execute"
    return update


# --- execute -------------------------------------------------------------


def served_summary(served_models) -> str:
    """Which model answered, for the execute line.

    Under a router `cfg.model` names a *policy* — `openrouter/pareto-code`
    resolved to three different models in one day — so the configured name in
    the log answers a different question from the one an operator is asking.
    The artifact has carried this since it existed; the log is what anyone
    actually reads while a run is going, and reading it meant inferring the
    model from the tier.

    Ordered by turns because the majority model is the one that did the work,
    and a split is the case worth seeing: sticky routing is a five-minute
    window, an attempt often runs longer, and a switch mid-attempt means a cold
    prefix at full input price. Without the split that arrives as an
    unexplained bill.

    Empty for an endpoint that echoes no model, rather than ` via `: absence
    and a model named nothing are different facts.
    """
    if not served_models:
        return ""
    ranked = sorted(served_models.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(ranked) == 1:
        return f" via {ranked[0][0]}"
    return " via " + ", ".join(f"{name} x{turns}" for name, turns in ranked)


def execute(state: RunState, rt: Runtime) -> dict:
    stage = current_stage(state, rt)
    attempt = _attempt(state)
    feedback = list(state.get("review_feedback") or [])

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
    # Either side of the attempt, because "did this one commit anything" is the
    # only honest reading of whether it made progress — `stage_start_sha` is
    # the stage's, so an earlier attempt's work would answer for this one.
    before_sha = rt.git.head_sha()
    result = rt.executor.run_agent_stage(
        stage,
        prompt,
        history_dir=history_dir,
        since_sha=state["stage_start_sha"],
        agent_context=_conventions(state, rt),
        feedback=feedback,
        failure_layer=state.get("failure_layer"),
        # Chosen once at stage start and held for every attempt and revision:
        # a model that changes inside a conversation reads nothing of the
        # prefix the turns before it built.
        model=state.get("stage_executor_model", ""),
    )

    if result.tool_counts:
        # The third agentic loop to report what it looked at. Counts rather
        # than the rendered calls the planner and reviewer log, because this
        # one makes sixty a cycle and the calls themselves are in the
        # conversation artifact beside this line's own log.
        asked = render_counts(result.tool_counts)
        refused = render_counts(result.refusal_counts)
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
        # reads as a measurement rather than as its absence, which is the
        # difference between "no cache" and "nobody looked".
        paid = ""
        served = served_summary(getattr(result, "served_models", None))
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
            # Only when it happened, like `refused` above: a zero here would
            # be one more number to read past on every stage. Printed at all
            # because six of one run's 76 attempts ended this way and nothing
            # said so — the artifact recorded `ok: True` with an empty log,
            # and the only outward sign was a stage that quietly did nothing.
            + (
                f"; {result.empty_finishes} empty finish(es)"
                if result.empty_finishes
                else ""
            )
            + paid
            + served
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
    # Summed across attempts, like the cost eleven lines below and for the same
    # reason: a stage that took three passes really did load context three
    # times, and the figure worth keeping is the stage's rather than the last
    # attempt's. It was assigned rather than accumulated, so a stage whose
    # final attempt was a one-line fix recorded that attempt's high-water mark
    # as the whole stage's. Measured on one run: two stages of two and three
    # attempts, together 4.4M and 1.5M prompt tokens, recorded 12,933 and
    # 16,079 — below the opening prompt of a single turn, in the number the
    # planner sizes batches from.
    #
    # Summed peaks rather than summed cache writes, which was the alternative.
    # Writes count only newly-cached material, so a stage reusing an earlier
    # stage's prefix looks small precisely because it was efficient: one stage
    # on that run wrote 21,547 while carrying 82,015. Peaks are per
    # conversation, so they neither double-count within an attempt nor depend
    # on how well the cache held.
    measured = (
        {
            "executor_context_tokens": (
                state.get("executor_context_tokens", 0) + result.context_tokens
            )
        }
        if result.context_tokens
        else {}
    )
    # What the loop proved green, carried to the gate so it does not ask
    # the same question of the same tree. Always written, including empty,
    # so a later attempt cannot inherit an earlier one's answers.
    measured["gate_records"] = dict(result.gate_records)
    if result.usage is not None:
        deltas = usage_deltas("executor_", result.usage)
        measured["run_usage"] = accumulate_usage(state.get("run_usage"), **deltas)
        # The same deltas onto the stage. Every attempt is its own session, so
        # a stage reworked three times is billed for three.
        measured["stage_usage"] = accumulate_usage(
            state.get("stage_usage"), **deltas
        )
    # Accumulated, not replaced. Each attempt is its own executor session with
    # its own running total, so a stage that took four attempts paid for four
    # and the figure worth recording is the stage's, not the last attempt's.
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

    if result.gate_unrunnable:
        # The loop could not run a gate — the harness refused to build the
        # gate's command, before anything was spawned. Above `commit_refused`
        # because it stopped the cycle earlier: a config the pipeline cannot
        # express is nobody's defect but the operator's, and every attempt
        # would be refused identically.
        return {
            **measured,
            **_escalate(
                "gates",
                f"A gate could not be run on stage {stage.id!r}. Not a failing "
                "check and not a planning defect — the harness refused to "
                "build the gate's command:\n"
                f"{_clip(result.gate_unrunnable)}\n\n"
                "The attempt's work is committed on the stage branch. Fix "
                "what the refusal names and resume.",
            ),
        }

    if result.commit_refused:
        # The loop could not record its work. Escalated on the same grounds as
        # the setup command above — the hook will refuse the next attempt
        # identically, so a retry buys nothing and the planner cannot rewrite a
        # hook any more than it can rewrite a precondition.
        #
        # Ahead of every branch below because those all read a tree they assume
        # was committed: `git.is_clean()` is not consulted here precisely
        # because the in-process loop was said to guarantee it.
        return {
            **measured,
            **_escalate(
                "commit",
                f"The executor finished stage {stage.id!r} and the repository "
                "refused to record its work. A commit hook, not a gate, and "
                "not a planning defect:\n"
                f"{_clip(result.commit_refused)}\n\n"
                "The edits are still in the working tree. Clear what the hook "
                "objects to and resume.",
            ),
        }

    if result.replan_kind:
        # The model handed the stage back. Below `commit_refused`, which is a
        # human escalation and outranks it, and above everything else: the
        # branches that follow all diagnose an attempt from what it left in the
        # tree, and this attempt has said what it found. Whatever it committed
        # stays on the branch for the planner to build on; nothing lands, and
        # the redrawn stage still faces every gate and a review.
        kinds = {
            "unsatisfiable": (
                "The executor reports that this stage cannot be completed as "
                "written. Rewrite what it requires — do not re-issue it."
            ),
            "incomplete": (
                "The executor made the change and reports that doing so "
                "revealed work the plan did not anticipate. Widen this stage "
                "or draw the sequence that covers what it found."
            ),
        }
        return {
            **measured,
            **_planner_failure(
                state,
                "replan",
                f"the executor asked for a replan ({result.replan_kind})",
                f"{kinds.get(result.replan_kind, '')}\n\n"
                f"{_clip(result.replan_reason)}",
                committed_work=rt.git.head_sha() != before_sha,
            ),
        }

    if result.ok:
        # A rework that edited nothing has usually just said why, and until now
        # nobody read it. `result.log` is the model's closing text; on the
        # normal path it is written to `executor.log` and consumed by nothing,
        # because the only consumers are the timeout and turns-exhausted
        # branches below. So the loop breaks on `not editor.touched`, the gates
        # judge a tree the attempt did not move, and the account of why it did
        # not move goes to a file.
        #
        # Measured on `remove-non-admin-catch-all-retry` attempt 1: 73 tool
        # calls, zero edits, and a closing paragraph naming the problem exactly
        # — "the reported full-suite failures require changes to other
        # application/spec files involving URL generation, but those files are
        # outside the permitted list". Twenty minutes and a full suite later the
        # planner re-derived that unaided and widened `edit_files` to the six
        # directories the executor had named. The revision's planner prompt
        # contains the sentence zero times.
        #
        # It is held for the *next* failure rather than claimed as the opening
        # one. The first cut did claim it, and it was dead code by
        # construction: `opening_failure` is write-once and the full-suite
        # failure that caused this rework had already taken it one node
        # earlier, in `_rework_or_plan`. Correct behaviour on that helper's
        # part — the suite failure genuinely is the diagnosis — and it meant
        # the claim here could never fire in the one case it was written for.
        #
        # `review_feedback` is the wrong home too, for a different reason: both
        # handoffs compose their detail from `feedback[-2:]`, so a third kind
        # of entry pushes the failure that actually ended the stage out of the
        # window. Traced on the observed sequence, the note would have been
        # displaced by the reviewer rework that followed it.
        #
        # So it rides the next `FailureDetail` instead, which is causally right
        # — the gate is about to fail *because* the executor declined — and
        # arrives whether that goes to another attempt or to the planner.
        # Routing here is deliberately unchanged: the gates decide, and a
        # model's voluntary stop does not get to end a stage.
        #
        # `cumulative_diff` rather than the attempt counter, because the
        # question is whether there is prior work to have left alone, and the
        # tree answers that. A first attempt with no edits is the scope gate's
        # sentence and not this one.
        #
        # Except when the attempt was stopped for repeating itself, which is
        # the one stop that sentence describes wrongly. The others here are
        # decisions — a model that says the files it needs are out of scope has
        # concluded something, and on a first attempt "produced no changes" is
        # a fair summary of it. A model cut off after ten identical calls has
        # concluded nothing, and letting the gate speak for it sends the
        # planner to redraw a stage that was never the problem. Measured on one
        # run: three attempts ended this way, and one of them was a first
        # attempt that made 590 calls and applied a single edit.
        stopped_unproductive = bool(getattr(result, "unproductive_stop", ""))
        if (
            not result.edits_applied
            and result.log
            and (cumulative_diff or stopped_unproductive)
        ):
            return {
                "next_hop": "verify",
                **measured,
                "executor_note": _clip(result.log),
            }
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

    # The ceiling, said in its own words. `verify` will report "the attempt
    # produced no changes", which is observably true and points the planner at
    # a badly drawn stage — the wrong problem when the executor was still
    # working and simply ran out of turns.
    if result.turns_exhausted and not result.edits_applied:
        reason = _no_change_reason(True, result.model_turns)
        rt.log(f"[execute] {stage.id}: {reason}")
        return {
            **_retry_or_plan(
                state, rt, Layer.EXECUTOR,
                "the executor ran out of model turns before it edited anything",
                reason, reason,
            ),
            **measured,
        }

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


def _spent(role) -> str:
    """How much of this step's read budget went, as ` (120k/800k chars)`.

    Permanent rather than temporary, because the failure it makes visible was
    invisible for a whole run: the character counter was never reset between
    steps, so it climbed past the ceiling and every later planner call was
    refused on its first read — 14 of 31 calls, each drawing a stage blind, and
    the only outward sign was the planner saying it could not read. A budget
    whose consumption is not printed cannot be seen to leak.

    It also answers the tuning question the read budget has never been able to
    answer from outside: whether a ceiling is anywhere near binding in normal
    operation, or is a backstop that never fires.
    """
    reader = getattr(role, "reader", None)
    limit = getattr(getattr(reader, "budget", None), "max_total_chars", 0)
    if reader is None or not limit:
        return ""
    return f" ({reader.spend.chars // 1000}k/{limit // 1000}k chars)"


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
        # What the executor's loop already proved on this tree. Empty on a
        # resume that re-enters here, nothing having tested anything yet.
        green_records=state.get("gate_records") or {},
    )

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "verify.log",
        _verify_log(outcome),
    )

    if outcome.flaky_files:
        _record_flakes(
            rt, stage.id, outcome.flaky_files, outcome.flaky_seeds,
            outcome.flaky_examples,
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
        try:
            rt.git.commit_all(f"[{stage.id}] verification checks")
        except GitError as e:
            # A repository may refuse a commit. Hooks are the ordinary reason —
            # a whitespace or lint gate on staged content — and the refusal is
            # foreseeable, so it must not leave here as a traceback. It did: a
            # run three stages deep died at this line because the editor's
            # line-ending normalisation rewrote a CRLF file whole, turning
            # every pre-existing trailing space into an *added* line for the
            # hook to find.
            #
            # Escalated rather than routed to the executor, and what decides
            # that is *whose changes these are*. This commit carries what the
            # checks rewrote, not the model's work. Handing it back would ask
            # the executor to fight the linter, which rewrites the same bytes
            # on the next cycle; the retry budget then delivers a hook to the
            # planner, which can do nothing about one either. It is repository
            # policy, the same family as the setup command failing, and the
            # only participant who can satisfy it is a person.
            #
            # Nothing is discarded to get past it: the rewrites stay in the
            # tree, and a resume re-runs this commit once the objection is
            # cleared.
            rt.log(f"[verify] {stage.id}: the repository refused the commit")
            return {
                **accumulated,
                **_escalate(
                    "checks",
                    f"{len(touched)} file(s) were left uncommitted after the "
                    "checks ran, and the repository refused to commit them. "
                    "This is a commit hook, not one of this stage's gates:\n\n"
                    f"{_clip(str(e))}\n\n"
                    "Whose changes these are is not knowable from here and the "
                    "message does not guess. Usually they are what an "
                    "autocorrecting check rewrote — but the executor's own "
                    "commit runs earlier and the same hook refuses that too, "
                    "in which case this is the stage's work as well. The run "
                    "log says which.\n\n"
                    "Nothing has been discarded: the changes are still in the "
                    "working tree. Clear what the hook objects to and resume, "
                    "and the commit is retried from here.",
                ),
            }
        # "left in the tree after the checks", not "changed by the checks".
        # Usually they are the same thing and the old wording said so — but the
        # executor's own commit can be refused by a hook and silently leave its
        # work here, and this line then credited fifteen model edits to the
        # linter. Observed once, in the run that produced the escalation above.
        rt.log(
            f"[verify] {stage.id}: committed {len(touched)} file(s) left in "
            "the tree after the checks"
        )

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
        plan_text=rt.plan_text(),
        completed=state.get("completed") or [],
        projection=rt.projection(),
        agent_context=_conventions(state, rt),
        proposed=_proposed_resolutions(rt, stage),
    )

    rt.log(f"[review] {stage.id}: calling reviewer")
    # Keyed by project rather than by run: successive runs and resumes share
    # the same plan snapshot prefix, so they should share the same cache.
    outcome = rt.reviewer.review(
        messages,
        cache_key=cache_key(
            "code_gantry", state.get("project_slug") or "project"
        ),
    )

    # What it looked at, before the verdict that used it. An approval reached
    # after reading the file the diff depends on and one reached from the diff
    # alone read identically in the log otherwise, and those are exactly the
    # two cases worth telling apart while watching a run.
    if outcome.tool_calls:
        # Counts, not the calls. Joining them put several thousand characters
        # on one line of a timeline meant to be skimmed — nineteen rendered
        # reads including two semantic queries and a two-hundred-character
        # regex, on a single review.
        #
        # Nothing is lost, which is the part that had to be checked rather than
        # assumed: `_log_new_calls` already streams every call to `tools.log`
        # one per line as it happens, and `review.json` keeps the ordered list.
        # This was the third copy and the only one whose reader cannot afford
        # it. The executor's line reached the same shape for the same reason.
        #
        # The property the line exists for survives: a verdict reached after
        # reading and one reached from the diff alone still read differently,
        # which is the whole question this answers while a run is watched.
        detail = render_counts(outcome.tool_counts or {})
        rt.log(
            f"[review] {stage.id}: read {len(outcome.tool_calls)} thing(s)"
            + (f": {detail}" if detail else "")
            + _spent(rt.reviewer)
        )

    rt.write_artifact(
        state["stage_index"], stage.id, state.get("revision", 0), attempt,
        "review.json", json.dumps(outcome.as_dict(), indent=2),
    )

    usage = accumulate_usage(
        state.get("run_usage"),
        **usage_deltas("", outcome.usage),
    )
    stage_usage = accumulate_usage(
        state.get("stage_usage"),
        **usage_deltas("", outcome.usage),
    )
    # The peak beside the total, because they answer different questions and
    # the total was twice mistaken for the answer to the second. A tool loop
    # bills per turn: `prompt` is what the review *cost*, `peak` is how close
    # its largest single call came to the window it has to fit inside. A budget
    # whose consumption is never printed cannot be seen to leak, and neither
    # can a ceiling's headroom.
    rt.log(
        f"[review] {stage.id}: {outcome.verdict} — {outcome.summary} "
        f"({outcome.usage.prompt_tokens} prompt, {outcome.usage.cached_tokens} cached, "
        f"{outcome.usage.peak_prompt_tokens} peak)"
    )

    for note in outcome.observations:
        # One line each, and the finding on it. The first version joined the
        # file names with semicolons and said nothing else, so a human reading
        # the run log learned that something had been noticed somewhere and had
        # to open an artifact to find out what — for the rarest thing the
        # reviewer produces. The paths are long enough that two of them filled
        # the line on their own.
        rt.log(
            f"[review] {stage.id}: reviewer note on {note.file} — "
            + clip_for_model(note.finding.strip(), REVIEWER_NOTE_CHARS)
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
        "pending_resolved": list(outcome.resolved),
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

    # The refund goes here, before the suite runs, because both exits below
    # need it: a red suite routes back to the executor with a fresh budget, and
    # a green one lands and clears everything anyway. See
    # `clear_rework_after_approval` for why approval is the right trigger and
    # why it happens once.
    base = {**base, **clear_rework_after_approval(state)}

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
            _record_flakes(
                rt, stage.id, verdict.files, verdict.seeds, verdict.examples
            )
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
        # Onto the stage, so the executor's inner loop *runs* the spec rather
        # than only being told about it. The command is assembled by code from
        # the filename the suite produced — no model is asked to name it, which
        # is the difference between a fact and a claim.
        current = dict(state.get("current") or {})
        if verdict.files:
            current["suite_failing_paths"] = sorted(
                {*(current.get("suite_failing_paths") or []), *verdict.files}
            )
        return {
            **base,
            "current": current,
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
    rt: Runtime,
    stage_id: str,
    files: list[str],
    seeds: dict[str, str],
    examples: dict[str, list[str]] | None = None,
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
        examples=examples or {},
        run_id=rt.paths.run_id,
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
    # child branch onto the project branch as one commit. The executor's
    # intermediate commits — some of them red, since it commits before testing
    # — are discarded by the squash. That is why "every commit on the project
    # branch is green" and "the executor commits before it tests" are both
    # true.
    # Written on the stage branch, before the squash picks it up, so the
    # observations land inside the commit they are about. One commit per stage
    # holds, and a reader of that commit sees both what changed and what it
    # revealed about the plan.
    #
    # After every gate has run, not before: the scope guard would see a plan
    # document modified by a stage that never touched it. That ordering means
    # the content is unexamined by the gates, which is acceptable here in a way
    # it would not be for code — this is markdown at a configured path, written
    # by CodeGantry from structured planner output, not a model editing
    # the repository. The guards exist to catch the executor wandering.
    # Commit anything the executor left uncommitted, then squash the whole
    # child branch onto the project branch as one commit. The executor's
    # intermediate commits — some of them red, since it commits before testing
    # — are discarded by the squash. That is why "every commit on the project
    # branch is green" and "the executor commits before it tests" are both
    # true.
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

    rt.git.commit_all(f"[{stage.id}] wip")
    message = _commit_message(
        stage,
        state.get("review_record") or state.get("review_summary") or "",
        trailers=_landing_trailers(rt, stage, state, start_sha),
    )

    publication = None
    if rt.cfg.compose_landings:
        # Nothing lands here. The stage becomes a candidate for whichever
        # bay holds the landing semaphore, and the keys stay claimed until
        # that bay has composed it onto the project branch and pushed:
        # until then there is no tree anywhere that has this work in it.
        # The two halves of landing a stage, and both of them are `advance`:
        # make the candidate, then land what is pending if this bay can have
        # the semaphore. Attempted rather than waited for, so a bay that
        # cannot have it goes on to its next stage — and offers again when
        # it finishes that one, which is what keeps a candidate from
        # waiting long for somebody to notice it.
        merge_sha = _push_candidate(rt, stage, state, branch, start_sha, message)
        _compose_if_free(rt)
        return _advance_result(state, rt, stage, start_sha, merge_sha, publication)

    merge_sha = rt.git.squash_merge(branch, rt.cfg.project_branch, message)
    rt.git.delete_branch(branch)
    if rt.cfg.remote_landing and merge_sha:
        merge_sha, publication = _publish_landing(rt, stage)
    if rt.ledger is not None:
        _record_landing(rt, stage, state, merge_sha or rt.git.head_sha())
        # The pushed tree passed a full suite: the stage's own before it
        # reached here, or the publication's re-run on the rebased tree; a
        # publication that escalated is the one case it did not. Recorded so
        # the next preflight on any host reads it rather than proving it again.
        if publication is None and rt.cfg.full_test_command:
            rt.ledger.record_green(
                merge_sha or rt.git.head_sha(), rt.cfg.full_test_command,
                run_id=rt.paths.run_id, stage_id=stage.id,
            )

    return _advance_result(state, rt, stage, start_sha, merge_sha, publication)


def _advance_result(state: RunState, rt: Runtime, stage: Stage, start_sha: str, merge_sha, publication):
    """What `advance` answers, whichever way the stage left the bay: the
    completed record, the costs, and where the run goes next."""
    usage = state.get("stage_usage") or {}
    result = {
        "id": stage.id,
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
        # Null when nothing has landed, never the candidate's sha standing in
        # for one. A candidate is a commit on a branch of its own that no
        # composition has taken yet — and may never take, if it turns one
        # red — so calling it the commit this stage landed as is a claim
        # about the project branch that is not true, made to every reader of
        # this record and to every planner call for the rest of the run.
        "merge_sha": None if rt.cfg.compose_landings else (merge_sha or rt.git.head_sha()),
        "candidate_sha": merge_sha if rt.cfg.compose_landings else None,
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
        "plan_keys": list(stage.plan_keys),
        "resolves": list(state.get("pending_resolved") or []),
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
    spend = _stage_spend(rt.cfg, usage, result.get("executor_cost_usd") or None)
    if result.get("executor_context_tokens") or spend:
        append_stage_cost(
            rt.project.project_dir,
            stage_id=stage.id,
            merge_sha=result["merge_sha"],
            files=len(stage.edit_files),
            context_tokens=result.get("executor_context_tokens", 0),
            spend=spend,
            roles=_roles_for_record(rt.cfg),
            # Measured off the landing commit rather than taken from the
            # stage's declared scope. `edit_files` is a permission and stages
            # routinely touch less than it allows.
            changed=rt.git.shortstat(result["merge_sha"]),
            # What the planner said this would take, beside what it took.
            difficulty=stage.difficulty,
        )
    if result["merge_sha"]:
        rt.log(f"[advance] {stage.id} landed as {result['merge_sha'][:12]}")
    elif result["candidate_sha"]:
        rt.log(f"[advance] {stage.id} is a candidate at {result['candidate_sha'][:12]}, waiting to be composed")

    landed = {
        **fresh_stage_fields(),
        # Recorded on the stage above; cleared here so the next one is not
        # billed for this one's derivation. Not in `fresh_stage_fields`, which
        # `plan` also spreads — see its docstring.
        "plan_seconds": 0.0,
        # Same door, same reason: `plan` writes the derivation's tokens into
        # this and then spreads `fresh_stage_fields` over its own update, so
        # the reset has to happen where the stage ends rather than where the
        # next one begins.
        "stage_usage": zero_usage(),
        "completed": completed,
        "current": None,
        # Cleared here, by the only node that writes them, rather than by the
        # per-stage reset — which `plan` also applies, over the notes it has
        # just accumulated.
        "pending_resolved": [],
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
    landed = {
        **landed,
        **_next_from_queue(
            state, landed["stage_index"] - 1,
            views=rt.views() if rt.ledger is not None else None,
        ),
    }

    # Merged onto the landing, never in place of it. The stage is squash-merged
    # and on the branch whatever the run does next; replacing this update with
    # the escalation would leave `completed` short by one and `stage_index`
    # unmoved, and a resume would re-derive work that is already landed.
    #
    # `held_hop` reads what the landing decided rather than asking again. When
    # a batch promoted its next stage this is a *held stage* pause, not a
    # between-stages one, and saying so is what stops the resume re-planning
    # it: without the argument, `paused_before` was empty and a stage that was
    # drawn and correct came back as `[plan] revising <id>`, paying for a
    # planner call to revise something nothing was wrong with.
    paused = _pause_escalation(
        rt.paths.pause_flag, state, rt.cfg, held_hop(landed)
    )
    if publication:
        # Landed locally and recorded; what failed is publication, and the
        # next precheck pulls again once a person has resolved it.
        return {**landed, **publication}
    return {**landed, **paused} if paused else landed


def held_hop(landed: dict) -> str:
    """The hop this landing is holding a ready stage for, if any.

    Derived from the landing update rather than recomputed, because
    `_next_from_queue` has already decided — asking the queue a second time is
    how two answers to one question come to disagree.

    Only `precheck` counts. `paused_before` is returned verbatim as the resume
    entry point, so any other truthy value would send a resume to a node the
    run was never about to enter.
    """
    return "precheck" if landed.get("next_hop") == "precheck" else ""


# --- finalize ------------------------------------------------------------


def finalize(state: RunState, rt: Runtime) -> dict:
    """Check the tip is the commit the gates approved, and stop.

    This ran the full suite until it was measured. On the only path that
    reaches here the tree is the one the last landing's review gate ran that
    same command on: verified on run 20260904-120923, where the tip was
    `05e65fc5ba44` — the commit `advance` had just produced — with nothing
    between the two runs but a planner derivation, which touches no file. So a
    second run could only discover nondeterminism in the suite.

    And it scored that second sample harder than the first. A red suite at the
    review gate goes through `flake.adjudicate`: rerun the file whole and
    alone, excuse it, record it to `flakes.jsonl`. Here there was none of that,
    so 22 stages that each landed on a green-or-excused suite were ended by one
    failure in 6,139 examples on a commit already tested. **A second sample
    scored more harshly than the first is not a second check**, and the
    escalation clipped its own diagnosis: the `Failure/Error:` block was in the
    6,427 characters `_clip` dropped from the middle, and finalize writes no
    `full-suite.log`, so the run cost three minutes and reported a failure
    nobody could read.

    What is left is the part the suite could not answer anyway, and it costs
    two git commands. `pin_modules` pins *our* code for the length of a run and
    says nothing about the target repository, so a human editing it mid-run and
    anything landing on the branch from outside the pipeline are both live —
    and both are invisible to a suite, which would pass on the edited tree.
    """
    completed = state.get("completed") or []
    if not completed:
        # Nothing landed, so there is no approved commit to compare against and
        # the tip belongs to whatever ran before this session.
        return {"status": "complete", "next_hop": "end", **_session_elapsed(state)}

    approved = (completed[-1] or {}).get("merge_sha") or ""
    head = rt.git.head_sha()
    if rt.cfg.compose_landings:
        # The tip is not this run's to predict. Every bay moves the project
        # branch when it holds the landing semaphore, so a tip that differs
        # from anything this run produced is the arrangement working. What
        # is still worth asking is the other half — that nothing outside the
        # pipeline left changes in the tree.
        approved = ""
    if approved and head != approved:
        return {
            **_escalate(
                "branch_moved",
                f"The project branch tip is {head}, and the last stage this run "
                f"landed produced {approved}. Something outside the pipeline "
                "committed to the branch: every gate's verdict describes a tree "
                "that is no longer what the branch points at.",
            )
        }
    if not rt.git.is_clean():
        return {
            **_escalate(
                "tree_dirty",
                "Every stage landed, but the working tree has changes no stage "
                "made. The gates read the tree, so their verdicts are about "
                "content that is not what is committed:\n"
                + "\n".join(rt.git.uncommitted()),
            )
        }
    rt.log(
        f"[finalize] tip is {approved}, the commit the last stage landed; tree clean"
        if approved else f"[finalize] tip is {head[:12]}; tree clean"
    )
    return {"status": "complete", "next_hop": "end", **_session_elapsed(state)}


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


def _stale_excerpt_failure(state: RunState, stage, moved: list[str]) -> dict:
    """Back to the planner, and the rest of the batch goes with it.

    Discarding the queue is the other half of letting batched stages stack.
    The prompt tells the planner its stages run in order and may build on each
    other; the obligation that buys is that a stage which does not run as drawn
    invalidates everything written behind it. Whatever the planner returns for
    this one is a different stage from the one the tail assumed, so keeping the
    queue would leave stages standing on a premise nothing established — the
    exact failure this check exists to prevent, one level up.

    Cheap to be safe about: the tail costs one derivation to redraw, and the
    planner is told it was discarded rather than left to notice.
    """
    queue = list(state.get("stage_queue") or [])
    listed = ", ".join(repr(p) for p in moved)
    detail = (
        "You drew this stage as part of a batch, against the tree as it stood "
        f"at {stage.excerpt_base_sha[:12]}. A stage in front of it has landed "
        "since, and the file you quoted is not the file that is there now — so "
        "the line numbers in `read_excerpts` no longer point at what you "
        "meant.\n\n"
        "**If an earlier stage of this same batch edited it, that is the "
        "cause, and it was your own doing.** Batched stages run in order and "
        "may build on each other freely; a quoted line range is the one thing "
        "that does not survive an earlier stage moving it, because a number "
        "cannot be re-derived from the file it points into.\n\n"
        "Redraw this stage against the file as it is now. Re-read the range "
        "and quote it again, or drop the excerpt and describe what you want "
        "instead — the executor can read the file itself."
    )
    if queue:
        behind = ", ".join(f"`{s.get('id')}`" for s in queue)
        detail += (
            f"\n\nThe stages queued behind it — {behind} — have been "
            "discarded along with it. They were drawn assuming this stage ran "
            "as written, and it did not. Draw them again if they are still the "
            "work you want."
        )
    return {
        **_planner_failure(
            state,
            "excerpt",
            f"stage {stage.id!r} quotes {listed}, which has changed since you "
            "read it",
            detail,
        ),
        "stage_queue": [],
    }


def stale_excerpts(git, stage) -> list[str]:
    """Excerpted paths whose bytes have moved since the planner read them.

    The whole of what replaced the static orthogonality pass, and it is smaller
    because it measures rather than predicts.

    The old check asked whether an earlier stage in the same batch was
    *permitted* to touch a file a later stage quotes. Three faults, and the
    third ends the argument: it predicted, and a stage spec is already a
    prediction; it could only see batch-mates, so a file moved by a human, by
    `rubocop -A`, or by a resume onto an advanced branch was invisible; and
    `edit_files` is a permission rather than a record — a stage very often
    declares a file editable and never edits it, so the check fired over a
    superset of what happened and dropped usable stages for edits that never
    occurred.

    This asks the only question that matters: are the bytes under this line
    range the same bytes. Blob ids rather than commit ids, because a commit
    changes whenever anything in the tree changes and almost none of that is
    about this file.

    Empty `excerpt_base_sha` means the stage was derived against the tree as it
    stands and there is no window to check.
    """
    base = getattr(stage, "excerpt_base_sha", "")
    if not base or not stage.read_excerpts:
        return []
    moved = []
    for path in dict.fromkeys(e.path for e in stage.read_excerpts):
        try:
            if git.blob_at(base, path) != git.blob_at("HEAD", path):
                moved.append(path)
        except GitError:  # pragma: no cover - defensive
            moved.append(path)
    return moved


def _queue_from_batch(
    cfg, git, first, extra_fields: list[dict], *,
    ledger: Ledger | None = None,
) -> tuple[list[dict], list[str]]:
    """The stages to hold behind the one being started, and what was trimmed.

    The cap is the operator's policy; the conversion through
    `stage_from_planner` keeps an executable field off a batched stage; and
    every queued stage is validated against the ledger the way the head is,
    because a queued stage that cites no key would fail only when reached.
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
    # The commit these stages' excerpt line numbers were chosen against.
    # Recorded here, by the machinery, because the run knows it and a model
    # asked for it would be making a claim instead.
    try:
        base = git.rev_parse("HEAD")
    except GitError:  # pragma: no cover - defensive
        base = ""
    known_keys, open_ids = ledger_references(ledger)
    queued = []
    for fields in extra_fields:
        stage = cfg.stage_from_planner(fields)
        problems = validate_stage(
            stage, cfg, known_keys=known_keys, open_findings=open_ids,
        )
        if problems:
            notes.append(
                f"queued stage `{stage.id}` was dropped: " + "; ".join(problems)
            )
            continue
        queued.append({**stage.model_dump(), "excerpt_base_sha": base})
    return queued, notes


def _requeue_after_revision(cfg, git, revised, queue: list[dict]) -> tuple[list[dict], list[str]]:
    """The queue behind a revised stage, which is all of it.

    Rework is never batched — a failed stage owns a branch and a branch belongs
    to one stage — and the stages queued behind it are unaffected work.

    This used to re-run the orthogonality check, because a revision that
    widened `edit_files` could newly overlap a queued stage. With the check
    gone there is nothing to re-run: whether the revision actually disturbs a
    queued stage's excerpts is answered at that stage's own precheck, against
    the tree, by `stale_excerpts`. Widening a permission is not disturbing
    anything, which was the flaw in asking here.
    """
    return list(queue), []

def _no_change_reason(turns_exhausted: bool, turns: int) -> str:
    """Why an attempt produced nothing, said so the planner fixes the right thing.

    Two different problems wore the same sentence for a while. A model that
    stops having changed nothing has decided there is nothing to do — that is a
    stage drawn wrongly, and redrawing it is the answer. A model still asking
    for things when its turns run out was working: the stage is too large to
    survey inside the budget it was given.

    Live cost of not distinguishing them: `cart-explicit-routes` spent four
    attempts making 85, 103, 76 and 138 tool calls, every one a read, and hit
    the ceiling each time. The planner was told "the attempt produced no
    changes" and went looking for a badly drawn stage.
    """
    if not turns_exhausted:
        return (
            "The executor stopped without changing anything, which means it "
            "decided there was nothing to do. Either the work is already done "
            "or the instruction did not describe something it could act on."
        )
    return (
        f"The executor was still working when it hit its ceiling of {turns} "
        "model turns, so it never got as far as editing. It was not stuck and "
        "the instruction was not wrong — it could not finish surveying the "
        "code in the budget it had. Draw this smaller, or narrow what it has "
        "to read to answer the question."
    )


def _next_from_queue(state: RunState, landed_index: int, views=None) -> dict:
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
    # A queued stage another run has taken, or that was dropped, is skipped:
    # the ledger's record of it is the one that counts.
    if views is not None:
        queue = [
            s for s in queue
            if not s.get("derived_id")
            or (views.derived.get(s["derived_id"]) or _absent).status == "derived"
        ]
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


def _pause_escalation(
    flag, state: RunState, cfg: ProjectConfig, ready_hop: str = ""
) -> dict | None:
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
            + f"`{cfg.resume_command()}` picks up "
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
    committed_work: bool = False,
) -> dict:
    return {
        "layer": layer,
        "summary": summary,
        "detail": detail,
        "out_of_scope_paths": out_of_scope_paths or [],
        "failing_paths": failing_paths or [],
        "committed_work": committed_work,
    }


def _consume_executor_note(state: RunState, detail: str) -> tuple[str, dict]:
    """Fold in what the executor said when it stopped without editing.

    Written by `execute`, read by whichever handoff comes next, and cleared as
    it is read — it describes one attempt, and a second failure that carried it
    again would be attributing it to work it never saw.

    Measured on `remove-non-admin-catch-all-retry` attempt 1: 73 tool calls,
    zero edits, and a closing paragraph naming the cause exactly — "the
    reported full-suite failures require changes to other application/spec
    files involving URL generation, but those files are outside the permitted
    list". `result.log` was written to `executor.log` and read by nothing; the
    only consumers were the timeout and turns-exhausted branches. Twenty
    minutes, a reviewer call, a 246s suite and a 106s baseline later the
    planner re-derived it unaided and widened `edit_files` to the six
    directories the executor had already named. The revision's planner prompt
    contains the sentence zero times.
    """
    note = state.get("executor_note")
    if not note:
        return detail, {}
    joined = (
        f"{detail}\n\nThe previous attempt left the branch unchanged. What the "
        f"executor said when it stopped:\n{note}"
    ).strip()
    return joined, {"executor_note": None}


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
    committed_work: bool = False,
) -> dict:
    """Hand the failure to the planner with what it needs to act on."""
    detail, cleared = _consume_executor_note(state, detail)
    latest = _failure_detail(
        layer, summary, detail, out_of_scope_paths, failing_paths, committed_work
    )
    return {
        "failure_layer": layer,
        "last_failure": latest,
        **_opening(state, latest),
        **cleared,
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

    detail, cleared = _consume_executor_note(state, detail)
    accumulated = list(state.get("review_feedback") or [])
    accumulated.append(feedback)
    return {
        "failure_layer": layer,
        "verify_attempt": consumed + 1,
        "review_feedback": accumulated,
        **cleared,
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
    """Rework while the budget holds, then the planner.

    The handoff is where the planner's judgement is formed, so what it says has
    to be what happened. Two corrections, both measured on
    `order-edit-item-personalization-explicit-scope`, which spent 884 seconds of
    planning to reach `restart` on a diff the reviewer had just approved.

    **A rework is not a rejection.** The old summary read "rejected 3 times" for
    a stage reworked twice — a missing comment, and a route id that should have
    been optional — both fixed and approved, followed by one red spec. "Rejected
    three times" describes a stage going badly; the record was a stage that
    converged and then hit a single real failure, and `restart` is a sensible
    answer to the first description and an expensive one to the second.

    **The newest feedback leads.** `feedback[-2:]` joined in order put the
    already-fixed reviewer findings ahead of the failure that actually ended the
    stage, so the red spec read as a footnote to complaints that no longer
    applied. Earlier feedback is still carried — two reworks for the same thing
    is a different situation from two for different things — but it follows.
    """
    consumed = state.get("rework_attempt", 0)
    if consumed >= rt.cfg.limits.max_rework_retries:
        recent = list(reversed(feedback[-2:]))
        return _planner_failure(
            state,
            layer,
            f"{summary} — after {consumed} rework attempt(s) "
            f"(max_rework_retries={rt.cfg.limits.max_rework_retries}, so "
            "another executor pass is not available)",
            "\n\n".join(recent),
        )

    if rt.cfg.rework_reset:
        # One clean single-purpose diff per attempt, rather than the rejected
        # attempt plus its correction.
        rt.log(f"[review] resetting to {state['stage_start_sha'][:8]} before rework")
        rt.git.reset_hard(state["stage_start_sha"])

    detail, cleared = _consume_executor_note(state, "\n\n".join(feedback[-2:]))
    return {
        "failure_layer": layer,
        "review_feedback": feedback,
        "rework_attempt": consumed + 1,
        **_opening(state, _failure_detail(layer, summary, detail)),
        **cleared,
        "next_hop": "execute",
    }


def _first_line(stage: Stage) -> str:
    text = stage.instruction or stage.command or stage.id
    return text.strip().splitlines()[0][:70]


# git's own convention, and the width every tool that renders a log assumes.
_BODY_WIDTH = 72


def _stage_spend(cfg, usage: dict, executor_cost: float | None = None) -> list[dict]:
    """What each of the three roles billed for one stage.

    One shape for all of them, because they were not comparable before: the
    executor's line carried a *peak* context figure beside a dollar amount
    computed from its *summed* usage — two different quantities reading as
    one — the reviewer's tokens went only to the log, and the planner's went
    nowhere at all. The planner is 91% of the bill and was the role with no
    per-stage record.

    Tokens first and always; the dollars are optional and omitted for an
    unpriced model. `None` from `price_usage` is not `0.0` — a rate table that
    reports zero for "not priced" as readily as for "free" makes a local
    endpoint and a missing rate identical — and the two are told apart here by
    the field simply being absent.
    """
    from code_gantry import pricing

    # Loaded on first use, not up front. Every role may report what it was
    # billed, in which case the table is never needed — and this is the
    # function `advance` calls per landing, where an eager load once refetched
    # 1.76MB of somebody else's rate card on every stage.
    cached: dict = {}

    def prices() -> dict:
        if "map" not in cached:
            cached["map"] = pricing.cached_price_map(cfg)
        return cached["map"]

    out: list[dict] = []
    # The reviewer's keys are unprefixed: it was the first role to write here
    # and the shape was not role-aware yet. Named explicitly rather than
    # inferred, so adding a fourth role is a line here and not a rule to work
    # out.
    for label, prefix, model in (
        ("planner", "planner_", cfg.planner.model),
        ("executor", "executor_", cfg.executor.model),
        ("reviewer", "", cfg.reviewer.model),
    ):
        prompt = usage.get(f"{prefix}prompt_tokens", 0)
        completion = usage.get(f"{prefix}completion_tokens", 0)
        known = executor_cost if label == "executor" else None
        if not prompt and not completion and not known:
            continue
        cached_tokens = usage.get(f"{prefix}cached_tokens", 0)
        writes = usage.get(f"{prefix}cache_write_tokens", 0)
        row = {
            "role": label,
            "prompt": prompt,
            "cached": cached_tokens,
            "completion": completion,
        }
        # Where one is recorded. The summed figure is what the role was billed
        # for; the peak is how large its largest single call got, and only the
        # second is comparable to a context window. A tool loop makes them
        # differ by more than an order of magnitude — 6.6M billed against a
        # call that never approached it — and the summed one invites exactly
        # the wrong reading.
        peak = usage.get(f"{prefix}peak_prompt_tokens", 0)
        if peak:
            row["peak"] = peak
        # The loop already priced its own attempt, through this same
        # `price_usage` and the same table, and it is the only participant
        # that does. Preferring its figure keeps one arithmetic rather than
        # two agreeing ones — the second is the one that drifts, and
        # `_price`'s own docstring says so.
        # A gateway bills us and says what it billed; that beats deriving the
        # same number from a rate table, and under a router it is the only
        # answer available. Same precedence as the executor's own figure just
        # above — one arithmetic, chosen by a condition, rather than two that
        # can disagree.
        cost = known
        if cost is None:
            cost = usage.get(f"{prefix}provider_cost_usd")
        if cost is None:
            cost = pricing.price_usage(
                pricing.entry_for(prices(), model),
                prompt, cached_tokens, writes, completion,
                writes_1h=usage.get(f"{prefix}cache_write_1h_tokens", 0),
            )
        if cost:
            row["cost_usd"] = cost
        out.append(row)
    return out


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


def _commit_message(
    stage: Stage, record: str, *, trailers: list[tuple[str, str]] = ()
) -> str:
    """The landing commit: what was asked, what landed, and git trailers.

    Asked and landed are kept apart and labelled, because the instruction is
    written before the work and the reviewer's record after it. The trailers
    carry what a later reader rebuilds the ledger from: keys, resolved
    findings, which model held which role, the config, the base commit.
    """
    subject = f"[{stage.id}]"
    asked = "\n\n".join(
        part for part in (
            stage.instruction or "",
            f"Constraints: {stage.constraints}" if stage.constraints else "",
            f"Acceptance: {stage.acceptance}" if stage.acceptance else "",
        ) if part
    )
    sections = []
    if asked:
        sections.append("Asked:\n\n" + _wrap_body(clip_for_model(asked, 1500)))
    body = _wrap_body(record)
    if body:
        sections.append("Landed:\n\n" + body)
    trailer_lines = "\n".join(f"{key}: {value}" for key, value in trailers if value)
    if trailer_lines:
        sections.append(trailer_lines)
    return "\n\n".join([subject, *sections]) if sections else subject


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


_ESCAPE = re.compile(r"(?<!\\)\\u([0-9a-fA-F]{4})")


def decode_escapes(text: str) -> str:
    """`\\u2014` written literally becomes the character it meant.

    A model that double-escapes a non-ASCII character emits a backslash
    followed by `u2014`, which would land in a commit looking like a bug in
    this tool. A sequence already escaped by a preceding backslash is left
    alone.
    """
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text or "")


def open_findings(
    ledger: Ledger | None,
    git,
    notes: list[dict],
    *,
    by: str,
    stage_id: str | None,
    run_id: str | None,
    log=None,
) -> int:
    """Open one finding per planner note. Returns how many were opened."""
    if ledger is None or not notes:
        return 0
    views = ledger.views()
    try:
        at_sha = git.head_sha()
    except GitError:
        at_sha = None
    opened = 0
    for note in notes:
        key = (note.get("key") or "").strip()
        node = views.nodes.get(key)
        if node is None or node.retired:
            if log:
                log(f"[ledger] note on unknown key {key!r} filed without a key")
            key = ""
        claim = "\n\n".join(
            part for part in (
                decode_escapes(note.get("finding") or "").strip(),
                decode_escapes(note.get("observation") or "").strip(),
            ) if part
        )
        total = (note.get("total") or "").strip()
        ledger.open_finding(
            keys=[key] if key else [],
            by=by,
            claim=claim,
            needs=note.get("needs") or "pipeline",
            total=None if total.lower() in ("", "none") else total,
            subject=(note.get("subject") or "").strip() or None,
            at_sha=at_sha,
            stage_id=stage_id,
            run_id=run_id,
        )
        opened += 1
    return opened


class _Absent:
    status = "dropped"


_absent = _Absent()


def _references_taken(rt: Runtime, stage: Stage, state: RunState) -> list[str]:
    """What stops this stage from starting: keys and findings it is drawn
    against that are not available, unless this very stage holds them; and
    its own drawn record, if another run took it first."""
    views = rt.views()
    taken = views.references_available(
        stage.plan_keys, stage.resolves,
        run_id=rt.paths.run_id, stage_id=stage.id,
    )
    if stage.derived_id:
        record = views.derived.get(stage.derived_id)
        if record is not None and record.status == "taken" and record.taken_run != rt.paths.run_id:
            taken.append(f"{stage.derived_id} (taken by {record.taken_run})")
        elif record is not None and record.status in ("done", "dropped"):
            taken.append(f"{stage.derived_id} ({record.status})")
    return taken


def _held_elsewhere(rt: Runtime, stage: Stage) -> list[str]:
    """The stage's keys, findings or drawn record, where another run holds
    them or they are closed; empty when this run may go on with it."""
    return _references_taken(rt, stage, {})


def _claim_references(rt: Runtime, stage: Stage, state: RunState) -> None:
    """Hold every key and finding the stage is drawn against, and its drawn
    record, for this run and stage. Idempotent across a resume.

    **Never takes what another run holds.** A claim that overwrote one would
    be a claim that steals, and the stage would go on to `precheck` reading
    its own claim as proof it may start. What is held elsewhere is left
    alone and `precheck` sends the stage back, which is the answer whether
    the planner drew against a held reference or a bay reached it first.
    """
    views = rt.views()
    run_id, pid = rt.paths.run_id, os.getpid()
    for key in stage.plan_keys:
        current = views.state(key)
        if current.state == "claimed" and current.run_id != run_id:
            continue
        if current.state == "claimed" and current.run_id == run_id and current.stage_id == stage.id:
            continue
        rt.ledger.append(CLAIMED, key=key, stage_id=stage.id, run_id=run_id, pid=pid, bay=bay_id(rt))
    for fid in stage.resolves:
        finding = views.findings.get(fid)
        if finding is not None and finding.claimed_run and finding.claimed_run != run_id:
            continue
        if finding is not None and finding.claimed_run == run_id and finding.claimed_stage == stage.id:
            continue
        rt.ledger.append(FINDING_CLAIMED, stage_id=stage.id, run_id=run_id, finding_id=fid, pid=pid, bay=bay_id(rt))
    if stage.derived_id:
        record = views.derived.get(stage.derived_id)
        if record is not None and record.status == "derived":
            rt.ledger.append(STAGE_TAKEN, stage_id=stage.id, run_id=run_id, derived_id=stage.derived_id, pid=pid, bay=bay_id(rt))


def _drop_derived(rt: Runtime, stage: Stage, reason: str) -> None:
    """Withdraw the stage's drawn record *and* everything taking it held.

    Taking a stage is what claims its keys, so dropping it is what gives
    them back — the two are one act read backwards, and they were not.
    Without this a run that took a stage and found it unstartable kept its
    keys for the rest of its life while working something else entirely,
    and the keys are what the planner reads to decide there is nothing to
    draw. Four bays did that to each other and every one of them blocked.
    """
    if rt.ledger is None:
        return
    # Before the record, and not conditional on there being one: what the
    # stage holds is held whether or not the planner drew it from a batch.
    _unclaim_references(rt, stage)
    if not stage.derived_id:
        return
    record = rt.views().derived.get(stage.derived_id)
    mine = record is not None and (
        record.status == "derived" or (record.status == "taken" and record.taken_run == rt.paths.run_id)
    )
    if mine:
        rt.ledger.append(STAGE_DROPPED, stage_id=stage.id, run_id=rt.paths.run_id, derived_id=stage.derived_id, reason=reason)


def _unclaim_references(rt: Runtime, stage: Stage) -> None:
    """Give back what this run holds for this stage, and nothing else: a key
    another stage of this run is working is not this stage's to release."""
    views, run_id = rt.views(), rt.paths.run_id
    for key in stage.plan_keys:
        current = views.state(key)
        if current.state == "claimed" and current.run_id == run_id and current.stage_id == stage.id:
            rt.ledger.append(RELEASED, key=key, run_id=run_id, stage_id=stage.id, reason="the stage was not started")
    for fid in stage.resolves:
        finding = views.findings.get(fid)
        if finding is not None and finding.claimed_run == run_id and finding.claimed_stage == stage.id:
            rt.ledger.append(FINDING_RELEASED, finding_id=fid, run_id=run_id, stage_id=stage.id, reason="the stage was not started")


def _planner_lock(rt: Runtime) -> str:
    """One planner at a time per ledger, on every host at once: the name is
    the ledger's identity — its name in the table, or the file — never a
    bay's own work dir, which would give every bay a semaphore of its own.

    The same ledger is the same name on every machine, which is what makes
    it one queue rather than one per host. Taken through `mesh`, so the
    daemons decide it between them."""
    identity = rt.cfg.ledger.name or str(rt.project.ledger.resolve())
    digest = hashlib.sha1(identity.encode()).hexdigest()[:12]
    return f"planner-{digest}"


def _compose_if_free(rt: Runtime) -> None:
    """Compose what is pending, if this bay can have the landing semaphore.

    Answers nothing: a composition is not this run's work and its outcome
    belongs to the ledger, not to this run's state. A failure here must not
    stop a bay that was on its way to do something else, so it is logged and
    the run goes on — the candidates stay pending and the next bay to hold
    the semaphore composes them.
    """
    from code_gantry import lander

    try:
        outcome = lander.compose(rt)
    except Exception as e:  # noqa: BLE001 - never the reason a bay stops
        rt.log(f"[land] composing failed, leaving the candidates pending: {e!r}")
        return
    if outcome is not None and outcome.escalation:
        rt.log("[land] the composition needs a person; the candidates stay pending")


def _take_rework(rt: Runtime, state: RunState) -> dict | None:
    """A rejected candidate to put right, as the update that starts it; None
    when there is none this run can take.

    Asked before anything is drawn and before anything drawn is taken.
    Rejected work is closer to done than a stage the planner has yet to
    write, and it holds plan keys while it waits — a plan whose rejects are
    never picked up is a plan that slowly runs out of things to draw.

    It begins with a rebase, because the base the candidate was built
    against is not what the project branch holds any more and the failure
    it has to answer is a failure against the tree as it is now. From there
    it is an ordinary stage with its branch already carrying work: the
    executor is handed what the composition said, the gates run, and
    `advance` squashes it to a candidate again and offers to land it.
    """
    if rt.ledger is None or not rt.cfg.compose_landings:
        return None

    with rt.ledger.transaction():
        taken = _claim_rework(rt, state)
    if taken is None:
        return None

    rejection, stage = taken
    branch, sha = rejection.candidate.branch, rejection.candidate.sha
    try:
        rt.git.fetch()
        base = rt.git.reset_branch_to(branch, f"origin/{rt.cfg.project_branch}")
        conflicts = rt.git.apply_commit(sha)
    except GitError as e:
        # Given straight back rather than held by a bay that could not even
        # begin. A conflict is not this: a conflict is left in the tree to
        # be worked on, and only something that stopped the work starting
        # comes through here.
        rt.log(f"[rework] {stage.id} could not be set up, leaving it for a person: {e}")
        rt.ledger.append(
            REWORK_RELEASED, stage_id=stage.id, run_id=rt.paths.run_id,
            branch=branch, reason=f"could not be set up for rework: {e}",
        )
        return None

    rt.log(
        f"[rework] took {stage.id} on {branch}, re-applied onto {base[:12]}"
        + (f" with {len(conflicts)} conflicted file(s)" if conflicts else " cleanly")
    )
    return {
        **fresh_stage_fields(),
        "current": stage.model_dump(),
        "revision": 0,
        "stage_index": state.get("stage_index", 0),
        "stage_branch": rejection.candidate.branch,
        "stage_start_sha": base,
        "stage_started_at": time.time(),
        "stage_queue": [],
        "batch_notes": [],
        # What the composition found, as the thing to answer. The stage's
        # own instruction travels with it unchanged — this is the same
        # stage, still to be done — so what the executor is handed reads
        # like a stage with feedback, which is what it is.
        "last_failure": {"layer": "composition", "summary": _rework_feedback(rejection, conflicts)},
        "next_hop": "precheck",
    }


def _rework_feedback(rejection, conflicts: list[str]) -> str:
    """What the executor is told about a candidate that could not be landed.

    Both halves, because a candidate can have either or both: its changes
    would not apply beside what landed, and what it does is wrong beside
    what landed. Neither is a defect in the stage as it was drawn, and the
    stage is still the thing to do — so this says what changed underneath
    it, not that it did the wrong thing.
    """
    parts = [
        "This stage was finished and approved, and could not be landed. "
        "Work has landed on the project branch since, and your branch has "
        "been put back on top of it with your changes re-applied."
    ]
    if conflicts:
        parts.append(
            "These files could not be re-applied cleanly and are in the tree "
            "with conflict markers. Resolving them is part of the work:\n"
            + "\n".join(f"  {path}" for path in conflicts)
        )
    if rejection.reason:
        parts.append(f"What the composition found:\n{rejection.reason}")
    parts.append("The stage's own instruction is unchanged and is still what has to be done.")
    return "\n\n".join(parts)


def _claim_rework(rt: Runtime, state: RunState):
    """Pick a rejection nobody holds whose references are free, and hold it.
    Under the ledger's writer, like every other claim."""
    views = rt.views()
    for rejection in views.rework_waiting():
        candidate = rejection.candidate
        facts = candidate.landing or {}
        # A candidate carries its own stage. One written before it did can
        # still be recovered from its drawn record, which is what that
        # replaced — worth the one branch, because the alternative is a
        # rejection that is offered forever and can never be taken.
        fields = candidate.fields
        if not fields and facts.get("derived_id"):
            record = views.derived.get(facts["derived_id"])
            fields = record.fields if record is not None else None
        if not fields:
            continue
        if views.references_available(facts.get("keys") or [], facts.get("held") or []):
            continue
        try:
            stage = Stage.model_validate(fields)
        except Exception:  # noqa: BLE001 - a record nothing can read is not a rework
            continue
        rt.ledger.append(
            REWORK_TAKEN, stage_id=stage.id, run_id=rt.paths.run_id,
            branch=rejection.candidate.branch, pid=os.getpid(), bay=bay_id(rt),
        )
        _claim_references(rt, stage, state)
        return rejection, stage
    return None


def _take_derived(rt: Runtime, state: RunState) -> dict | None:
    """A stage already drawn and waiting whose references are available
    inside this run's scope, as the update that starts it; None when there is
    none. Taking it costs no planner call.

    Held under the ledger's own writer, which is what makes looking and
    claiming one act: the store refreshes inside it, so what is read is
    still true when it is written against, and no other bay anywhere can
    write between the two. Brief by construction — a read and a few
    appends — which is why it is this and not the planner semaphore, whose
    holder is away for the length of a planner call.
    """
    if rt.ledger is None:
        return None
    with rt.ledger.transaction():
        return _take_derived_locked(rt, state)


def _take_derived_locked(rt: Runtime, state: RunState) -> dict | None:
    views = rt.views()
    for record in views.derived_waiting():
        if views.references_available(record.keys, record.findings):
            continue
        try:
            stage = Stage.model_validate({**record.fields, "derived_id": record.id})
        except Exception as e:  # noqa: BLE001 - a record nothing can read is dropped, not fatal
            rt.ledger.append(STAGE_DROPPED, stage_id=record.stage_id, run_id=rt.paths.run_id, derived_id=record.id, reason=f"unreadable: {e}")
            continue
        # Taking *is* claiming, and both happen here because here is where
        # the planner semaphore is held. Two bays reaching this queue
        # together were stopped by nothing but the timing: each read the
        # record as waiting and each went on to `precheck`, which is where
        # the claim used to be written. `precheck` still re-asks and hands
        # a loser back to the planner, but that is a way of surviving the
        # race rather than of not having one.
        _claim_references(rt, stage, state)
        rt.log(f"[plan] took {stage.id} ({record.id}), drawn by {record.by_run or 'another run'}")
        return {
            **fresh_stage_fields(),
            "current": stage.model_dump(),
            "revision": 0,
            "stage_index": state.get("stage_index", 0),
            "stage_queue": [],
            "batch_notes": [],
            "next_hop": "precheck",
        }
    return None


def _record_derivation(rt: Runtime, head: Stage, queue: list[dict], state: RunState) -> tuple[Stage, list[dict]]:
    """Write the batch to the ledger as drawn stages, the head first, and hand
    back the head and queue carrying their record ids.

    Under the ledger's writer, and claiming the head's references before it
    lets go, for the same reason `_take_derived` does: drawing and holding
    are one act or they are a race. It is not enough that the planner
    semaphore is usually held here — a revision that comes back as a
    predecessor lands in this function having never taken it, because the
    run entered `plan` with a stage in hand.
    """
    if rt.ledger is None:
        return head, queue
    with rt.ledger.transaction():
        return _record_derivation_locked(rt, head, queue, state)


def _record_derivation_locked(rt: Runtime, head: Stage, queue: list[dict], state: RunState) -> tuple[Stage, list[dict]]:
    try:
        base = rt.git.rev_parse("HEAD")
    except GitError:  # pragma: no cover - defensive
        base = None
    run_id = rt.paths.run_id
    event = rt.ledger.append(
        STAGE_DERIVED, stage_id=head.id, run_id=run_id, sha=base,
        fields=head.model_dump(), keys=list(head.plan_keys), findings=list(head.resolves),
        batch=None, rank=0,
    )
    head = head.model_copy(update={"derived_id": event.derived_id})
    # The head is this run's to start: taken and its references held now, so
    # no other run takes either between the derivation and this run's
    # precheck.
    _claim_references(rt, head, state)
    recorded = []
    for rank, fields in enumerate(queue, start=1):
        sibling = rt.ledger.append(
            STAGE_DERIVED, stage_id=fields.get("id"), run_id=run_id,
            sha=fields.get("excerpt_base_sha") or base,
            fields=fields, keys=list(fields.get("plan_keys") or []),
            findings=list(fields.get("resolves") or []),
            batch=event.derived_id, rank=rank,
        )
        recorded.append({**fields, "derived_id": sibling.derived_id})
    return head, recorded


def _taken_key_failure(state: RunState, stage: Stage, taken: list[str]) -> dict:
    """Back to the plan node with no stage in hand: what this stage was drawn
    against is held elsewhere, so the run takes or draws another. Not a
    planner failure — nothing was drawn wrongly and no intervention is spent."""
    return {
        "current": None,
        "stage_queue": [],
        "batch_notes": [
            f"stage {stage.id!r} was not started: {', '.join(taken)}; "
            "another run holds it or it is closed"
        ],
        "last_failure": None,
        "next_hop": "plan",
    }


def _proposed_resolutions(rt: Runtime, stage: Stage) -> list[tuple[str, str]]:
    """The findings the planner proposed this stage settles, with their claims."""
    if rt.ledger is None or not stage.resolves:
        return []
    findings = rt.views().findings
    return [
        (fid, findings[fid].claim.splitlines()[0] if findings[fid].claim else "")
        for fid in stage.resolves
        if fid in findings
    ]


def _landing_trailers(rt: Runtime, stage: Stage, state: RunState, start_sha: str) -> list[tuple[str, str]]:
    return [
        ("Plan-Keys", " ".join(stage.plan_keys)),
        ("Resolves", " ".join(state.get("pending_resolved") or [])),
        ("Planner-Model", rt.cfg.planner.model),
        ("Executor-Model", state.get("stage_executor_model") or rt.cfg.executor.model),
        ("Reviewer-Model", rt.cfg.reviewer.model),
        ("Config", state.get("config_hash", "")),
        ("Stage-Base", start_sha),
        ("Bay", bay_id(rt)),
    ]


def bay_id(rt: Runtime) -> str:
    """The checkout this run occupies, on this host: `<origin>/<directory>`.
    Distinct across bays on one host, where the ledger origin alone is not."""
    host = rt.ledger.origin if rt.ledger is not None else ""
    return f"{host}/{Path(rt.cfg.target_repo).name}"


def landing_facts(stage: Stage, state: RunState) -> dict:
    """What a landing records, gathered from the run that did the work.

    Separated because the run that does the work and the run that lands it
    are no longer the same one: under `compose_landings` these travel in the
    candidate's event and are replayed by whichever bay composes it. One
    assembly rather than two, because a copy of an assembly is not a check
    on it.
    """
    return {
        "stage_id": stage.id,
        "derived_id": stage.derived_id,
        "keys": list(stage.plan_keys),
        "resolved": list(state.get("pending_resolved") or []),
        "held": list(stage.resolves),
        "summary": state.get("review_summary") or "",
        "observations": list(state.get("pending_observations") or []),
    }


def _record_landing(rt: Runtime, stage: Stage, state: RunState, merge_sha: str) -> None:
    record_landing(rt, landing_facts(stage, state), merge_sha, run_id=rt.paths.run_id)


def record_landing(rt: Runtime, facts: dict, merge_sha: str, *, run_id: str) -> None:
    """Landed keys, confirmed resolutions, and the reviewer's observations as
    findings. `run_id` is the run that did the work, which is not always the
    run writing this."""
    views = rt.views()
    summary = facts.get("summary") or ""
    stage_id = facts.get("stage_id") or ""
    for key in facts.get("keys") or []:
        current = views.state(key)
        if current.state == "landed" and current.sha == merge_sha:
            continue
        rt.ledger.append(
            LANDED, key=key, sha=merge_sha, stage_id=stage_id, run_id=run_id,
            evidence=summary,
        )
    resolved = list(facts.get("resolved") or [])
    for finding_id in resolved:
        rt.ledger.append(
            FINDING_RESOLVED, sha=merge_sha, stage_id=stage_id, run_id=run_id,
            finding_id=finding_id,
        )
    # A finding the stage held and the reviewer did not confirm goes back to
    # open rather than staying held by a stage that has finished.
    for finding_id in facts.get("held") or []:
        finding = views.findings.get(finding_id)
        if finding_id not in resolved and finding is not None and finding.claimed_run == run_id:
            rt.ledger.append(FINDING_RELEASED, stage_id=stage_id, run_id=run_id, finding_id=finding_id, reason="not confirmed by the reviewer")
    if facts.get("derived_id"):
        record = views.derived.get(facts["derived_id"])
        if record is not None and record.status == "taken":
            rt.ledger.append(STAGE_DONE, stage_id=stage_id, run_id=run_id, sha=merge_sha, derived_id=facts["derived_id"])
    for observation in facts.get("observations") or []:
        where = (observation.get("file") or "").strip()
        finding = (observation.get("finding") or "").strip()
        detail = (observation.get("detail") or "").strip()
        rt.ledger.open_finding(
            keys=[], by="reviewer",
            claim="\n\n".join(p for p in (f"{where}: {finding}" if where else finding, detail) if p),
            needs="human", at_sha=merge_sha, stage_id=stage_id, run_id=run_id,
        )
    rt.log(
        f"[landing] ledger: {len(facts.get('keys') or [])} key(s) landed, "
        f"{len(resolved)} finding(s) resolved, "
        f"{len(facts.get('observations') or [])} observation(s) opened"
    )


def _sync_project_branch(rt: Runtime) -> bool:
    """Bring the project branch up to origin's before a stage is cut. Returns
    whether the tip moved; False when there is no origin or no remote branch."""
    return rt.git.sync_branch(rt.cfg.project_branch)


def _push_candidate(rt: Runtime, stage: Stage, state: RunState, branch: str, start_sha: str, message: str) -> str | None:
    """Squash the stage to one commit on the base it was cut from, push that
    branch, and record it as waiting to be composed. Answers the candidate's
    sha, or None when the stage changed nothing.

    The project branch is not touched. Every bay pushing its own candidate
    is what makes a composing bay possible at all: the work travels between
    hosts on a branch of its own, so nothing has to move the one branch
    every bay is reading.

    The bay ends on the project branch as it found it, so the next stage is
    cut from the same base as this one rather than stacked on it.
    """
    candidate = rt.git.squash_to_candidate(branch, start_sha, message)
    rt.git.checkout(rt.cfg.project_branch)
    if candidate is None:
        rt.log(f"[advance] {stage.id}: nothing to land")
        rt.git.delete_branch(branch)
        return None

    # Moved once it is not the branch we are standing on, so the local
    # branch and the pushed one are the same commit and neither is a
    # rendering of the other.
    rt.git.set_branch(branch, candidate)
    # Replaced rather than pushed: a rework rebases the branch onto what has
    # landed since, so what goes up is not a descendant of what is there. A
    # candidate's branch is the pipeline's alone, which is what makes that
    # safe here and nowhere else.
    rt.git.replace_branch(branch)
    rt.log(f"[advance] {stage.id}: pushed candidate {candidate[:12]} as {branch}")

    if rt.ledger is not None:
        # Everything the landing will need, carried here because the run
        # that does the work and the run that lands it are no longer the
        # same one. Recorded as it happens — the candidate exists — and
        # replayed by whichever bay composes it, when that happens.
        rt.ledger.append(
            CANDIDATE_PUSHED, stage_id=stage.id, run_id=rt.paths.run_id,
            sha=candidate, branch=branch, base=start_sha,
            landing=landing_facts(stage, state),
            # The stage itself, so the candidate is self-contained. A bay
            # reworking one is not the bay that drew it and may be on
            # another machine: recovering the stage by joining against its
            # drawn record would make the rework depend on a second record
            # that a fold, a drop or a redraw can move.
            fields=stage.model_dump(),
        )
    # Kept at origin, which is where the composing bay reads it from.
    rt.git.delete_branch(branch)
    return candidate


def _publish_landing(rt: Runtime, stage: Stage) -> tuple[str, dict | None]:
    """pull --rebase, re-test if the tip moved, push fast-forward.

    Returns the branch's head after publication and an escalation when it
    could not be published. The landing is complete locally either way; the
    next precheck pulls again.
    """
    git, branch = rt.git, rt.cfg.project_branch
    if not git.remote_exists():
        return git.head_sha(), None
    # One landing at a time on this host, from the pull to the push: a bay
    # that lands quickly cannot keep moving origin under a neighbour's
    # re-test, and the suite the section runs re-enters the same lock.
    lock = rt.cfg.full_test_lock
    holding = (
        hostlock.hold(lock, f"landing {stage.id}, run {rt.paths.run_id}", rt.log)
        if lock else contextlib.nullcontext([0.0])
    )
    with holding as waited:
        if waited[0]:
            rt.log(f"[advance] {stage.id}: waited {waited[0]:.0f}s for the landing lock")
        return _publish_landing_locked(rt, stage)


def _publish_landing_locked(rt: Runtime, stage: Stage) -> tuple[str, dict | None]:
    git, branch = rt.git, rt.cfg.project_branch
    refused = None
    for _ in range(3):
        try:
            moved = git.remote_has_branch(branch) and git.pull_rebase(branch)
        except GitError as e:
            return git.head_sha(), _escalate(
                "remote_landing",
                f"{stage.id!r} landed on {branch!r} locally, but rebasing onto "
                f"origin's copy failed:\n{_clip(str(e))}\n\nResolve the branch "
                "against origin by hand and resume; nothing was pushed.",
            )
        if moved:
            rt.log(
                f"[advance] {stage.id}: origin moved under the landing; re-running "
                "the full suite on the rebased tree"
            )
            red = _suite_is_red(rt, stage)
            if red:
                return git.head_sha(), _escalate(
                    "remote_landing",
                    f"{stage.id!r} was approved on its own tree, but rebased onto "
                    f"what origin now holds the full suite is red:\n{red}\n\n"
                    "The two landings conflict. Nothing was pushed.",
                )
        try:
            git.push(branch)
        except GitError as e:
            refused = e
            continue
        rt.log(f"[advance] {stage.id}: pushed {git.head_sha()[:12]} to origin/{branch}")
        return git.head_sha(), None
    return git.head_sha(), _escalate(
        "remote_landing",
        f"origin refused the push of {branch!r} three times running:\n"
        f"{_clip(str(refused))}",
    )


def _suite_is_red(rt: Runtime, stage: Stage) -> str | None:
    """The full suite on the tree as it stands: None when green or flaked,
    else what failed."""
    command = rt.cfg.full_test_command
    if not command:
        return None
    result = rt.runner.run(command)
    if result.ok:
        return None
    verdict = adjudicate(output=result.output, command=command, cfg=rt.cfg, runner=rt.runner)
    if verdict.flaked:
        rt.log(f"[advance] {stage.id}: full suite flaked after the rebase — {verdict.summary}")
        _record_flakes(rt, stage.id, verdict.files, verdict.seeds, verdict.examples)
        return None
    return f"{result.summary()}\n{_clip(result.output)}"
