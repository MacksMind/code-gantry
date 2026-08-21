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

import time
from typing import Literal, TypedDict

Status = Literal["running", "complete", "escalated"]

FailureLayer = Literal[
    "precondition",
    "setup",
    "workspace",
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
PLANNING_FAILURES = frozenset({"precondition", "planner", "replan"})
# `replan` belongs here by definition rather than by observation: the executor
# raised it to say the stage is wrong or too small, so a resume that re-entered
# at `verify` would ask the gates a question nobody was waiting on an answer
# to, and re-running the executor would put it back where it stopped.
# `workspace` is deliberately in neither set. It is not a planning defect — the
# planner cannot commit somebody's files — and routing it to verify would diff
# against a stage branch that was never cut, because the check runs before
# precheck cuts one. Falling through sends the resume back to precheck, which
# re-runs the same check against the tree the human has since tidied.


class StageResult(TypedDict, total=False):
    id: str
    index: int
    revisions: int
    verify_retries: int
    rework_attempts: int
    flake_reruns_iteration: int
    flake_reruns_review_gate: int
    instruction: str
    # Peak context the executor held for this stage, from the provider's own
    # usage block. Surfaced to the planner so it sizes the next stage from what
    # the executor actually carried rather than from a file count.
    executor_context_tokens: int
    # What this stage's executor attempts cost, summed across them, when the
    # model was priced. Zero for a local endpoint — which is the truth, not a
    # missing reading. Accumulated rather than replaced: a stage that took four
    # attempts paid for four, and the figure an operator wants is the stage's.
    executor_cost_usd: float
    # Reference files the stage declared and the executor never received,
    # because they did not fit `max_read_lines`. Told to the planner so it can
    # choose what to drop, rather than having the tail of its list cut for it.
    withheld_reads: list[str]
    base_sha: str
    merge_sha: str
    wall_seconds: float
    test_seconds: float
    review_verdict: str | None
    review_summary: str | None
    review_record: str | None
    review_issues: list[dict]
    verify_failures: list[str]
    planner_notes: list[str]
    config_hash: str
    prompt_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    completion_tokens: int
    planner_prompt_tokens: int
    planner_cached_tokens: int
    planner_cache_write_tokens: int
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
    # Did the attempt that failed move the branch? A fact, measured from the
    # sha either side of it, and separate from what anything decides with it —
    # `plan` uses it to tell an exploratory replan from a stuck one. Here
    # rather than in `RunState` because a failure detail is replaced whole on
    # every failure, so it cannot go stale and no reset helper has to learn it.
    committed_work: bool


class RunState(TypedDict, total=False):
    run_id: str
    project_slug: str
    config_hash: str
    target_repo: str

    base_ref: str
    base_sha: str
    # The commit plan documents and the repo layout are read from: the project
    # branch at run start, not base_sha. Plan maintenance happens on the branch
    # and reaches base_ref only when the project merges, which on a long
    # migration is the end — so reading at the base shows a plan the run is not
    # executing. base_sha stays what the run is measured against.
    plan_sha: str
    project_branch: str
    stage_branch: str | None

    completed: list[StageResult]
    current: dict | None          # the pending Stage, as a plain dict
    stage_index: int
    revision: int
    verify_attempt: int
    rework_attempt: int
    # Whether approval has already refunded the rework budget this revision.
    # See `clear_rework_after_approval` — it is the bound on that refund.
    rework_refunded: bool

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
    plan_seconds: float

    last_failure: FailureDetail | None
    # The first failure since the stage was drawn or last redrawn, kept because
    # it is the diagnosis and `last_failure` is usually its consequence. A
    # stage whose tests fail, is reworked twice and then trips the no-progress
    # guard reaches the planner saying only that it repeated itself — true,
    # and no help in deciding what to draw instead. Observed twice: once
    # turning an `ArgumentError` naming a file and line into "the executor
    # timed out", once sending the planner to redraw a stage without the
    # assertion that broke it, which it then failed on again.
    #
    # Two, not a history. The failures in between are the same consequence
    # repeated, and every one of them would be re-billed on each planner call.
    opening_failure: FailureDetail | None
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
    # Fingerprint of the tree the full suite last passed on, set by verify when
    # it ran `full_test_command` itself. The merge gate compares it against the
    # tree in front of it and skips a second identical run. Declared here
    # because the graph drops keys the schema does not know: without this line
    # verify writes it, the schema discards it, and the gate silently never
    # skips — which is exactly how it shipped the first time.
    full_suite_digest: str
    # Observations the planner made about the plan going stale, held until the
    # stage lands. Written on advance rather than when the planner speaks: a
    # note about work that then fails review would record something that did
    # not happen.
    pending_plan_notes: list[dict]
    # The reviewer's out-of-scope findings, held until the stage lands, for the
    # same reason as above. Replaced rather than appended on each review: a
    # stage can be reviewed several times across rework attempts and every one
    # of them sees the whole cumulative diff, so accumulating would report one
    # finding once per attempt.
    pending_observations: list[dict]
    executor_context_tokens: int
    # What the executor loop proved green and against which tree, so the
    # gate can tell a question already answered from one it must ask.
    # Declared here or the schema drops it in transit, which is how four
    # previous values were lost.
    gate_records: dict
    executor_cost_usd: float
    withheld_reads: list[str]

    planner_interventions: int
    # Reset every time a stage lands; see Limits.max_interventions_without_landing.
    interventions_since_landing: int
    planner_notes: list[str]
    review_feedback: list[str]
    # What the executor said on an attempt that left the branch unchanged,
    # held until the next failure carries it. Declared here for the reason the
    # comment on `paused_before` gives: the driver filters every node's update
    # against this schema, so an undeclared key is written and silently
    # dropped. Not folded into `review_feedback`, which is composed with
    # `[-2:]` at both handoffs — a third kind of entry there pushes the failure
    # that actually ended the stage out of the window.
    executor_note: str | None
    review_verdict: str | None
    review_summary: str | None
    review_record: str | None

    stage_usage: dict[str, int]
    run_usage: dict[str, int]

    status: Status
    escalation_reason: str | None
    # The hop a pause interrupted, when a stage had been derived but not
    # started. Declared here because the driver filters every node's update
    # against this schema: undeclared, it would be written by `plan`, dropped
    # in the merge, and the resume would re-derive a stage it already had —
    # the same silent loss `full_suite_digest` shipped with.
    paused_before: str
    # Stages one derivation produced that have not run yet, in the planner's
    # order. Held rather than re-derived: the planner is the expensive
    # participant and a derivation is a third of a stage's wall clock.
    #
    # State rather than a local because it must survive a pause — which is why
    # the pause is checked immediately after the squash, so a run stops between
    # queued stages rather than mid-batch.
    stage_queue: list[dict]
    # What became of the last batch — stages dropped for overlap, or trimmed by
    # the cap. Held until the next planner call reads them, then cleared by the
    # node that consumed them, so a note is reported once rather than on every
    # derivation for the rest of the run.
    batch_notes: list[str]
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
    plan_sha: str = "",
) -> RunState:
    return RunState(
        run_id=run_id,
        project_slug=project_slug,
        config_hash=config_hash,
        target_repo=target_repo,
        base_ref=base_ref,
        base_sha=base_sha,
        plan_sha=plan_sha or base_sha,
        project_branch=project_branch,
        stage_branch=None,
        completed=[],
        current=None,
        stage_index=0,
        revision=0,
        verify_attempt=0,
        rework_attempt=0,
        rework_refunded=False,
        stage_start_sha="",
        stage_started_at=0.0,
        started_at=started_at,
        session_started_at=started_at,
        last_diff_digest="",
        full_suite_digest="",
        pending_plan_notes=[],
        pending_observations=[],
        last_failure=None,
        opening_failure=None,
        failure_layer=None,
        failed_stage_id=None,
        flake_reruns=0,
        flake_reruns_review_gate=0,
        flaky_files=[],
        test_seconds=0.0,
        planner_interventions=0,
        interventions_since_landing=0,
        planner_notes=[],
        review_feedback=[],
        executor_note=None,
        review_verdict=None,
        review_summary=None,
        review_record=None,
        stage_usage=zero_usage(),
        run_usage=zero_usage(),
        status="running",
        escalation_reason=None,
        stage_queue=[],
        batch_notes=[],
        resuming=False,
        next_hop="",
    )


def zero_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "cached_tokens": 0,
        # Billed above the base input rate — 6.25e-06 against 5e-06 on Opus —
        # so this cannot be folded into the uncached remainder without
        # understating by a quarter of whatever was just written. Both clients
        # have computed it per call for as long as they have existed; it
        # stopped at the per-call artifact because the run totals had no key
        # for it, which is the fourth value to be computed correctly, written
        # correctly, and lost crossing a schema.
        "cache_write_tokens": 0,
        "completion_tokens": 0,
        "planner_prompt_tokens": 0,
        # Not a total; see `accumulate_usage`. The largest single call of a
        # derivation's tool loop, which is what the read budgets bound and
        # what the summed figure cannot show.
        "planner_peak_prompt_tokens": 0,
        "planner_cached_tokens": 0,
        "planner_cache_write_tokens": 0,
        "planner_completion_tokens": 0,
        # The executor had no keys here at all while it was a subprocess:
        # its usage was scraped from a console line that omitted reasoning
        # tokens and could not see this provider's cache fields, so there
        # was nothing true to accumulate. In-process there is, and without
        # these the hit rate is computable per attempt and nowhere for the
        # run — which is the fifth value in this file to be computed
        # correctly and lost crossing a schema.
        "executor_prompt_tokens": 0,
        "executor_cached_tokens": 0,
        "executor_cache_write_tokens": 0,
        "executor_completion_tokens": 0,
    }


def fresh_stage_fields() -> dict:
    """Counters and per-stage scratch that reset when a stage completes.

    Not `revision`: that belongs to the stage being replaced, and `plan` sets it
    when it derives or revises.

    Not `plan_seconds`, for the same reason and found the same way: `plan`
    spreads this reset over its own return *after* setting it, so putting the
    zero here made every landed stage record no planning time at all while both
    halves' unit tests passed. `advance` clears it, being the node that ends
    the stage the time belongs to.

    Not `pending_plan_notes` either, and that one cost two stages to find. The
    notes are written by `advance`, which then clears them explicitly — but
    `plan` also spreads this reset over its own return value, *after* the notes
    it just accumulated. So every note the planner produced while deriving a
    stage was zeroed within the same function call, and `advance` never saw
    one. A field cleared by whoever finishes with it, rather than by a
    catch-all, cannot be swallowed that way.
    """
    return {
        "stage_branch": None,
        "stage_start_sha": "",
        "stage_started_at": 0.0,
        "verify_attempt": 0,
        "rework_attempt": 0,
        "rework_refunded": False,
        "flake_reruns": 0,
        "flake_reruns_review_gate": 0,
        "test_seconds": 0.0,
        "last_diff_digest": "",
        # Never cleared until now, and `advance` copies it onto the landed
        # StageResult and into `stage-costs.md`. `execute` writes it only when
        # usage came back, so a stage whose attempts reported none carried the
        # previous stage's figure into a record keyed by a merge sha it had
        # nothing to do with — and that file is what the planner sizes the next
        # batch against.
        "executor_context_tokens": 0,
        # Cleared for the same reason as the line above. These say "this
        # exact command was green on this exact tree"; carried into a new
        # stage they would be answers to a question about a different one,
        # and although the sha comparison would reject them, a value that
        # is never cleared is invisible until it is wrong.
        "gate_records": {},
        "executor_cost_usd": 0.0,
        # A new stage has a new tree; nothing has been proven about it yet.
        "full_suite_digest": "",
        "last_failure": None,
        "opening_failure": None,
        "failure_layer": None,
        "failed_stage_id": None,
        "review_feedback": [],
        "executor_note": None,
        "review_verdict": None,
        "review_summary": None,
        "review_record": None,
        "planner_notes": [],
        # Belongs to the attempt that was truncated. Carried into the next
        # stage it would report a withholding that stage never suffered, and
        # the planner would trim a reference list that fits.
        "withheld_reads": [],
        # `stage_usage` is deliberately absent, and cleared by `advance`
        # instead — exactly where `plan_seconds` is, for the same reason. The
        # planner's derivation is the first thing a stage costs, and `plan`
        # spreads this dict *over* its own update, so a reset here would zero
        # the figure the derivation had just recorded. That is the defect
        # `plan_seconds` was moved out to fix; the second value through the
        # same door does not need to rediscover it.
    }


def resume_input(
    saved: RunState, *, stage_has_work: bool, reset_progress_budget: bool
) -> dict:
    """The state a resumed run actually starts from.

    `resume_fields` has always said it was "what a resume merges over the saved
    checkpoint" and the merge did not exist: `cli.resume` handed the delta to
    `_drive` on its own, so every resumed run began with a four-key state. Its
    seven tests all passed, because each asserts what the delta *contains* —
    `CLAUDE.md`'s "a test that pins where a value lives passes while the value
    is lost", and the lesson landed one function short of the defect it was
    written about.

    Measured on a 14-hour run. `stage_index` restarts at zero on every resume,
    which is visible in the artifact tree as `000…030` followed by `000…003`
    twice more. `completed` restarts with it — and that list is documented here
    as the cacheable prefix of both paid prompts, so a resume rebuilds it from
    empty. `run_usage`, `executor_cost_usd`, `flaky_files` and the rest of the
    accounting restart too, which is why `report.md` renders `(unknown)` and
    `None` for a run whose checkpoint holds all of it.

    Worse, and silent: `drive` writes a checkpoint only `if state.get("run_id")`,
    so a resumed run writes none at all. That database froze 31 stages ago while
    the run went on landing work. A crash after a resume would come back to a
    checkpoint naming a stage that landed hours earlier.

    What kept it working is that the continuity lives elsewhere — the project
    branch holds the code and the progress log holds the history the planner
    reads — so the loss is in the record rather than the work. That is exactly
    what made it survive fourteen hours of being watched.
    """
    return {
        **saved,
        **resume_fields(
            stage_has_work=stage_has_work,
            reset_progress_budget=reset_progress_budget,
        ),
    }


def resume_fields(*, stage_has_work: bool, reset_progress_budget: bool) -> dict:
    """What a resume merges over the saved checkpoint.

    This was inline in `cli.py` until a live checkpoint was found reporting
    `escalated`, with an hour-old reason, while the run was actively landing
    work at revision 2. `status` and `escalation_reason` are written when a run
    stops and nothing cleared them when it started again, so the operator's
    primary question answered with the stop that had already been fixed — for
    the whole of the next session. It lives here so the merge has a seam to be
    tested at.
    """
    return {
        # How the entry router re-enters: verify for a repository-state
        # failure, so a human's fix is checked rather than discarded; plan for
        # a planning failure.
        "resuming": True,
        "next_hop": "",
        # `wall_clock_hours` bounds one unattended stretch. The hours between
        # an escalation and a human reaching it were not spent working, and
        # measuring from the original start would make a run escalated
        # overnight impossible to resume.
        "session_started_at": time.time(),
        # Did the interrupted stage get far enough to commit? If so the resume
        # verifies that work rather than asking the executor to redo it — asked
        # to redo a finished stage it has nothing to produce and no way to say
        # so.
        "stage_has_work": stage_has_work,
        # The run is running again. Left as they were, these describe the stop
        # this resume exists to undo.
        "status": "running",
        "escalation_reason": None,
        # Only when asked. Merging an empty dict leaves the counter where it
        # was, so the default resume cannot clear it by accident.
        **({"interventions_since_landing": 0} if reset_progress_budget else {}),
    }


def fresh_revision_fields() -> dict:
    """Counters that reset when the planner revises a stage.

    The stage keeps its identity and its accumulated review feedback history is
    cleared, because the instruction it was rejected against no longer applies.
    """
    return {
        "verify_attempt": 0,
        "rework_attempt": 0,
        "rework_refunded": False,
        # A redrawn stage is a different instruction, so reproducing the old
        # diff under it is not evidence of being stuck.
        "last_diff_digest": "",
        "full_suite_digest": "",
        "last_failure": None,
        "opening_failure": None,
        "failure_layer": None,
        "review_feedback": [],
        "executor_note": None,
        "review_verdict": None,
        "review_summary": None,
        "review_record": None,
    }


def evidence_surviving_a_revision(previous: dict | None, keep_branch: bool) -> dict:
    """What a revised stage inherits from the stage it replaces.

    A revised stage is rebuilt from the planner's fields, and the planner may
    not write `suite_failing_paths` — the suite said which files failed, and a
    model naming them would be a claim where there is already a fact. So the
    rebuild dropped them, and on an `extend` that is exactly backwards: `extend`
    *keeps the branch*, which means it keeps the failures, and it discards the
    only record of what they were.

    That record is what the routing depends on. `plan` sends an `extend`
    straight to `verify` rather than to the executor, on the argument that the
    work may already be done and the gates read state rather than intent — "if
    the revision did add work, residue or the tests fail and route to the
    executor then, with the gap named". The tests can only fail if the gate is
    given them to run. Measured on `remove-non-admin-catch-all-retry` revision
    1: the branch carried ~30 specs the full suite had attributed to this
    stage, the rebuild dropped them, the revised spec declared one path, and
    verify passed it in 5.2s. The reviewer approved on that, and a 228.5s suite
    and a 120.9s baseline re-established the failure before the executor was
    reached at all — six minutes after the evidence to route it had been
    thrown away.

    Not carried when the branch is not: `restart` re-cuts from the project tip,
    so the diff that caused those failures no longer exists and naming them
    would send the next attempt after somebody else's problem.
    """
    if not keep_branch or not previous:
        return {}
    paths = [p for p in (previous.get("suite_failing_paths") or []) if p]
    return {"suite_failing_paths": sorted(set(paths))} if paths else {}


def usage_deltas(prefix: str, usage) -> dict:
    """Every field a usage record carries, prefixed for its role.

    Walks the dataclass rather than naming fields, because the four call sites
    that named them by hand are four places to forget the next one — and this
    codebase has lost `cache_write_tokens`, `peak_prompt_tokens` and a cost
    figure that way already, each computed correctly at both ends and dropped
    crossing a schema. A field has to be *excluded* on purpose now.

    `prefix` is empty for the reviewer, whose keys are unprefixed because it
    was the first role to write here.
    """
    import dataclasses

    if usage is None or not dataclasses.is_dataclass(usage):
        return {}
    return {
        f"{prefix}{f.name}": getattr(usage, f.name)
        for f in dataclasses.fields(usage)
    }


def accumulate_usage(current: dict[str, int] | None, **deltas: int) -> dict[str, int]:
    """Add every figure except the ones that are not totals.

    A key naming a peak takes the maximum. Two calls do not make a larger
    call than either of them, and summing high-water marks produces a number
    that is not a reading of anything — it looks like a context figure, grows
    monotonically, and would be compared against a window it never approached.

    Decided here rather than at each call site because there are four of
    those, in two modules, and the fifth is one feature away. A field whose
    arithmetic depends on the caller remembering is the shape of thing this
    codebase has already lost twice.
    """
    out = dict(current or zero_usage())
    for key, value in deltas.items():
        if "peak" in key:
            out[key] = max(out.get(key, 0), value)
        elif "cost" in key:
            # Summed like a total, but *through* `None`, which is not the same
            # as summing zeros: unreported plus unreported is still
            # unreported, and only a provider saying so makes a zero real. A
            # gateway reports what it billed; a first-party endpoint reports
            # nothing and the figure comes from the rate table instead.
            prior = out.get(key)
            out[key] = value if prior is None else (
                prior if value is None else prior + value
            )
        else:
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
    if layer == "paused" and state.get("paused_before"):
        # A stage was derived and never started. Run it: nothing about it is
        # stale, and re-deriving would pay a second planner call for an answer
        # already in hand. Distinguished by a recorded fact rather than by the
        # shape of the state, because "a stage is present and nothing failed"
        # also describes the case below.
        return state["paused_before"]
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


def clear_rework_after_approval(state) -> dict:
    """Refund the rework budget once the reviewer has approved the diff.

    Approval is a real milestone: as far as the reviewer can tell, the diff is
    right. What the full suite finds *after* it is a different question from
    "this diff is not there yet", and the budget spent reaching approval should
    not decide how the run answers it.

    Measured on `order-edit-item-personalization-explicit-scope`. Two reviewer
    reworks — a stale explanatory comment, and a route id that should have been
    optional — spent `max_rework_retries: 2`. The stage was then approved, the
    full suite failed on one spec that failed twice more when re-run alone, and
    with no budget left it escalated to the planner, which spent 884 seconds and
    chose `restart` on an approved diff. Neither rework had anything to do with
    what the suite found.

    Deliberately not a judgement about how serious a finding was. Severity is
    not decidable mechanically, and a rule that tried would be wrong in the
    cases that matter.

    **Once per revision, and that is the bound.** Without it: rework, approve,
    red suite, refund, rework, approve, red suite … a cycle that never reaches
    the planner and pays for a full suite run every lap. With it, a revision is
    worth at most two attempts to reach approval and two to answer the suite.
    `fresh_stage_fields` clears the flag, so a redraw starts over.
    """
    if state.get("rework_refunded"):
        return {}
    return {"rework_attempt": 0, "rework_refunded": True}
