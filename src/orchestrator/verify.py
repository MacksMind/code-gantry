"""The layered pre-review gate.

An ordered sequence, cheapest gate first, short-circuiting on the first
failure. The ordering is economic: free deterministic checks run before
anything costing minutes of compute or a paid API call.

Routing is the other half, and it is now three-way rather than two. A failure
either goes back to the executor, back to the planner, or to a human:

- **executor** — something the executor can plausibly fix inside its declared
  scope: a failing test, a forbidden pattern, a missing test file.
- **planner** — evidence the *stage* was drawn wrongly: a scope violation, or
  retries exhausted. The planner can widen scope or insert a predecessor.
- **human** — setup failure and branch-identity failure. A broken environment
  is not a planning defect, and a containment breach does not negotiate.

Every failure carries feedback, not an exit code. A retry given no information
about why the last attempt failed is a wasted retry, and that applies with more
force to a planner intervention, which costs more than an executor attempt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

from orchestrator import gates
from orchestrator.commands import CommandResult, CommandRunner
from orchestrator.config import ProjectConfig, Stage
from orchestrator.gitops import Git, GitError
from orchestrator.globs import matches_any
from orchestrator.plandoc import resolve_plan_tree



class Layer(str, Enum):
    SETUP = "setup"
    BRANCH = "branch"
    SCOPE = "scope"
    PROGRESS = "progress"
    PATTERNS = "patterns"
    RESIDUE = "residue"
    TESTS = "tests"
    CHECKS = "checks"
    NEW_TESTS = "new_tests"


class Route(str, Enum):
    EXECUTOR = "executor"
    PLANNER = "planner"
    HUMAN = "human"


@dataclass
class VerifyOutcome:
    passed: bool
    failed_layer: Layer | None = None
    route: Route | None = None
    summary: str = ""
    feedback: str = ""
    results: list[CommandResult] = field(default_factory=list)
    flake_reruns: int = 0
    # Files excused as suite flakes, for the run-level list in the report.
    flaky_files: list[str] = field(default_factory=list)
    # And the ordering seed each of them failed under, so an excusal leaves
    # something reproducible behind rather than only a name.
    flaky_seeds: dict[str, str] = field(default_factory=dict)
    flaky_examples: dict[str, list[str]] = field(default_factory=dict)
    test_seconds: float = 0.0
    # Populated on a scope violation. The planner decides whether to adopt these
    # paths into the stage or have them reverted; the stage's other work is
    # never discarded for them.
    out_of_scope_paths: list[str] = field(default_factory=list)
    failing_paths: list[str] = field(default_factory=list)
    # Fingerprint of this attempt's diff, carried forward so the next attempt
    # can tell whether the executor actually moved.
    diff_digest: str = ""
    # The tests layer fell back to the whole suite because nothing identified
    # which specs this stage affects. Correct, but expensive enough on a real
    # project to be worth surfacing rather than looking like a slow scoped run.
    unscoped_tests: bool = False
    # Test files whose added lines matched a forbidden pattern and were excused
    # for being tests. Reported rather than skipped in silence: a gate that
    # quietly stops checking is indistinguishable from a gate that found
    # nothing.
    exempt_pattern_files: list[str] = field(default_factory=list)
    # Set when this layer ran `full_test_command` itself and it came back green:
    # a fingerprint of the tree that passed. The merge gate re-runs the full
    # suite after review, and when verify has already run that exact command on
    # this exact tree the second run is the same command against the same bytes
    # — the reviewer reads a diff, it does not edit. Keyed to the tree rather
    # than to a sha so uncommitted work counts, and compared rather than
    # trusted, so any drift falls back to running it.
    full_suite_digest: str = ""


def run_verify(
    stage: Stage,
    cfg: ProjectConfig,
    git: Git,
    runner: CommandRunner,
    stage_start_sha: str,
    stage_branch: str | None = None,
    project_branch: str | None = None,
    base_ref: str | None = None,
    base_sha: str | None = None,
    plan_sha: str | None = None,
    previous_diff_digest: str | None = None,
    previous_failure_layer: str | None = None,
    resuming: bool = False,
    green_records: dict[str, dict] | None = None,
) -> VerifyOutcome:
    outcome = VerifyOutcome(passed=True)
    context = _Context(
        stage=stage,
        cfg=cfg,
        git=git,
        runner=runner,
        stage_start_sha=stage_start_sha,
        stage_branch=stage_branch,
        project_branch=project_branch,
        base_ref=base_ref,
        base_sha=base_sha,
        plan_sha=plan_sha,
        previous_diff_digest=previous_diff_digest,
        previous_failure_layer=previous_failure_layer,
        resuming=resuming,
        green_records=green_records or {},
    )

    outcome.diff_digest = diff_digest(git, stage_start_sha)

    for layer in (
        _layer_setup,
        _layer_branch,
        _layer_scope,
        _layer_progress,
        _layer_patterns,
        _layer_residue,
        # Checks before tests, because a check may *write*: `rubocop -A` and
        # its kin exit zero after rewriting files, and `checks_commit_changes`
        # commits what they rewrote. Run the suite first and its green
        # describes bytes that are not the ones landing — caught eventually by
        # the full suite at review, a whole round trip later. The executor's
        # loop has always had this order; the gate had the legacy one, from
        # when nothing between the suite and the merge could change a file.
        _layer_checks,
        _layer_tests,
        _layer_new_tests,
    ):
        failure = layer(context, outcome)
        if failure is not None:
            failure.results = outcome.results
            failure.flake_reruns = outcome.flake_reruns
            failure.test_seconds = outcome.test_seconds
            failure.diff_digest = outcome.diff_digest
            failure.unscoped_tests = outcome.unscoped_tests
            return failure

    return outcome


@dataclass
class _Context:
    stage: Stage
    cfg: ProjectConfig
    git: Git
    runner: CommandRunner
    stage_start_sha: str
    stage_branch: str | None
    project_branch: str | None
    base_ref: str | None
    # What the run is measured against. Only the branch-identity check uses it.
    base_sha: str | None
    # Which commit the plan documents are read from: the project branch, where
    # plan maintenance actually happens. Distinct from base_sha because a long
    # migration branch carries months of plan edits that never reach base_ref.
    plan_sha: str | None = None
    previous_diff_digest: str | None = None
    previous_failure_layer: str | None = None
    resuming: bool = False
    # What the executor's loop already proved green, and against which
    # tree. See `_already_answered`.
    green_records: dict = field(default_factory=dict)


def _already_answered(ctx: "_Context", layer: str, command: str | None) -> bool:
    """True when the loop answered this and the answer was yes."""
    record = _recorded_answer(ctx, layer, command)
    return record is not None and not record.get("failed")


def _recorded_answer(ctx: "_Context", layer: str, command: str | None) -> dict | None:
    """Has this exact question already been answered on this exact tree?

    The executor's loop runs the layers it can act on, and until now the gate
    ran them again — same command, same bytes, same answer. Measured on one
    stage: 16.7s of specs and 1.5s of formatter, twice per attempt.

    That duplication was never the design. It existed because the subprocess
    editor could not be made to run the set we wanted, so the gate had to run
    the authoritative one itself. Controlling the executor is what removes the
    need, and this is where the need is removed.

    Not trust — two facts compared. The command must be the one this gate would
    run, and HEAD must be where it was when the answer was obtained. Anything
    that moves the tree, including a human's commit on a resume or a check that
    rewrote a file, fails the comparison and the layer runs. Which is why the
    resume-into-verify path — the whole human-in-the-loop tier — still tests
    everything: a hand-edit moves HEAD.
    """
    if layer not in (getattr(ctx.cfg, "trust_executor_gates", None) or ()):
        # The operator has not said the loop may answer for *this* layer. See
        # `ProjectConfig.trust_executor_gates` for why that is the default, and
        # why it is granted per layer rather than wholesale.
        return None
    record = (ctx.green_records or {}).get(layer)
    if not record:
        return None
    if record.get("command") != (command or ""):
        return None
    try:
        if record.get("head_sha") and record["head_sha"] == ctx.git.head_sha():
            return record
    except GitError:  # pragma: no cover - a broken repo fails louder elsewhere
        return None
    return None


def _fail(
    layer: Layer, route: Route, summary: str, feedback: str, **extra
) -> VerifyOutcome:
    return VerifyOutcome(
        passed=False,
        failed_layer=layer,
        route=route,
        summary=summary,
        feedback=feedback,
        **extra,
    )


# --- layer 0: setup ------------------------------------------------------


def _layer_setup(ctx: _Context, outcome: VerifyOutcome):
    command = ctx.stage.effective_setup_command(ctx.cfg)
    if not command:
        return None

    # Precheck runs this before the executor, and the executor's own tests
    # need the same stack up — so on the agent path it has already run, on
    # this tree, minutes ago. Keyed to HEAD like the others rather than to
    # "did anything run it": the environment question is only settled while
    # nothing has moved, and a resume where a human restarted Docker moves
    # HEAD with their commit.
    if ctx.green_records and _already_answered(ctx, "checks", ""):
        return None

    result = ctx.runner.run(command)
    outcome.results.append(result)
    if result.ok:
        return None

    return _fail(
        Layer.SETUP,
        Route.HUMAN,
        "the environment could not be prepared",
        f"Environment setup failed.\n{result.summary()}\n{_clip(result.output)}",
    )


# --- layer 1: branch identity --------------------------------------------


def _layer_branch(ctx: _Context, outcome: VerifyOutcome):
    """Nothing moved that should not have.

    `script` stages, `checks`, `preconditions`, and `setup_command` are all
    arbitrary operator-authored shell, any of which could contain a stray
    `git checkout` or rewrite a branch. Over a ten-hour unattended run that is
    the failure you would least like to discover afterward — and it is free to
    check, so it runs every time.
    """
    if not (ctx.stage_branch and ctx.project_branch and ctx.base_ref and ctx.base_sha):
        return None

    problems = ctx.git.branch_identity_problems(
        ctx.stage_branch, ctx.project_branch, ctx.base_ref, ctx.base_sha
    )
    if not problems:
        return None

    return _fail(
        Layer.BRANCH,
        Route.HUMAN,
        "the repository is not where the run left it",
        "Branch identity check failed — the run cannot safely continue:\n"
        + "\n".join(f"- {p}" for p in problems),
    )


# --- layer 2: scope guard ------------------------------------------------


def out_of_scope_paths(changed: list[str], ctx) -> list[str]:
    """Changed paths the stage was not allowed to touch.

    Named and separated so the exemption below is testable without a git
    repository, a stage branch and an executor attempt in front of it.

    `scope_exempt_globs` is the operator's list of paths that move on their own
    — see its config comment. It is checked *after* `edit_files` rather than
    merged into it, because the two mean different things: `edit_files` grants
    the executor permission, and an exemption only says that a change here is
    not evidence of wandering. Merging them would let an exemption widen what a
    stage may deliberately rewrite.

    Plan documents are unreachable from here: that check runs earlier and
    returns before this is called, so no exemption can excuse one.
    """
    exempt = getattr(ctx.cfg, "scope_exempt_globs", None) or []
    return [
        p
        for p in changed
        if not matches_any(p, ctx.stage.edit_files) and not matches_any(p, exempt)
    ]


def _is_plan_document(path: str, ctx: _Context) -> bool:
    """Is this one of the documents the planner is drawing from?

    Exactly the resolved tree — root plus linked children — rather than a
    directory glob, because a plan root commonly sits in `docs/` beside
    unrelated files that stages may legitimately touch.

    The addendum counts as one, even though a run does add to it. It is
    written by the orchestrator from the planner's structured output, at
    advance time, outside any stage's diff — so it never appears here legally.
    An executor edit to it is the executor wandering into the record of its own
    work, which is exactly the thing to catch.
    """
    addendum = ctx.cfg.plan_addendum_path
    if addendum and (path == addendum or path.startswith(addendum.rstrip("/") + "/")):
        return True

    # The config itself, which now lives inside the repository it describes.
    # `checks`, `test_command` and `setup_command` are arbitrary operator shell
    # that runs unattended; before the move they sat in a repository no stage
    # could reach, and after it they are one `edit_files` glob away. The same
    # sentence as the agent-context documents below, with the most force it
    # gets: a stage able to edit this one chooses what the machine runs.
    config_rel = ctx.cfg.config_rel_path
    if config_rel and path == config_rel:
        return True

    # The agent-context documents, for the same reason and with more force: the
    # planner reads them for what the machine can do, so a stage able to edit
    # one could retire its own constraints — "the pipeline cannot run bundle
    # install" is exactly the kind of sentence that lives in them.
    if path in ctx.cfg.effective_agent_context:
        return True

    root = ctx.cfg.plan_root
    if path == root:
        return True

    # Resolving the tree costs a `git show` per document, and verify runs on
    # every attempt. Almost every stage touches only code, so skip the work
    # unless a changed path is even in the right directory.
    root_dir = root.rsplit("/", 1)[0] if "/" in root else ""
    if root_dir and not path.startswith(root_dir + "/"):
        return False
    if not ctx.plan_sha:
        return False

    tree = resolve_plan_tree(ctx.git, root, ctx.plan_sha)
    return any(child.path == path for child in tree.children)


def _layer_scope(ctx: _Context, outcome: VerifyOutcome):
    changed = ctx.git.diff_names(ctx.stage_start_sha)

    if not changed:
        # A stage that produced nothing has not been done, and a green suite
        # proves nothing about that.
        return _fail(
            Layer.SCOPE,
            Route.EXECUTOR,
            "the attempt produced no changes",
            "The previous attempt produced no changes at all. Nothing was "
            "edited, so the stage has not been done.",
        )

    # Checked before the declared scope, because this one is not the planner's
    # to widen. The planner reads the plan tree and also chooses `edit_files`,
    # so without this it can put the plan in scope and have the executor amend
    # the instructions it will be judged against next cycle — goalpost drift
    # with a green suite behind it, in an unattended loop.
    #
    # Plan maintenance is a separate pass driven by git history: a human
    # decision about what the work has become, not a side effect of doing it.
    plan_edits = [p for p in changed if _is_plan_document(p, ctx)]
    if plan_edits:
        listed = "\n".join(f"  {p}" for p in sorted(plan_edits))
        return _fail(
            Layer.SCOPE,
            Route.PLANNER,
            "the stage edited the plan it is being drawn from",
            f"These are plan documents and no stage may change them:\n{listed}\n\n"
            "The plan states what the work is; a stage that rewrites it while "
            "doing the work removes the only fixed thing it is measured "
            "against. Redraw the stage without them. If the plan is genuinely "
            "wrong, say so in `reasoning` and block — correcting it is a "
            "human's decision, not this run's.",
            out_of_scope_paths=sorted(plan_edits),
        )

    out_of_scope = out_of_scope_paths(changed, ctx)
    if not out_of_scope:
        return None

    listed = "\n".join(f"  {p}" for p in sorted(out_of_scope))
    allowed = "\n".join(f"  {p}" for p in ctx.stage.edit_files)
    return _fail(
        Layer.SCOPE,
        # To the planner, not a human: there are two possible causes — the
        # executor wandered, or it correctly concluded the fix lies outside its
        # box — and they are indistinguishable from the diff. The planner can
        # widen the scope; a human is not needed to tell them apart.
        Route.PLANNER,
        "the stage touched files outside its declared scope",
        f"Files were changed outside this stage's declared scope:\n{listed}\n"
        f"In-scope globs are:\n{allowed}\n\n"
        "Either the stage was drawn too narrowly and these files belong in it, "
        "or the executor wandered. If they belong, widen edit_files and the "
        "existing work stands. If not, they will be reverted and the rest of "
        "the stage's work is kept.",
        out_of_scope_paths=sorted(out_of_scope),
    )


# --- layer 3: no progress ------------------------------------------------


def diff_digest(git: Git, stage_start_sha: str) -> str:
    try:
        return hashlib.sha256(git.diff(stage_start_sha).encode()).hexdigest()
    except GitError:  # pragma: no cover - a broken repo fails louder elsewhere
        return ""


def _layer_progress(ctx: _Context, outcome: VerifyOutcome):
    """The attempt reproduced the previous one exactly.

    Retrying an executor that has just demonstrated it cannot move spends money
    to learn nothing. Observed live: three rework attempts produced identical
    diffs and drew three identical reviewer verdicts, and with a real local
    model each of those is minutes of inference as well as a paid review.

    Routed to the planner rather than the executor for the same reason: another
    identical attempt is not a fix. Only a redrawn stage is.

    Exempt after a merge-gate failure. There the reviewer had already approved
    the diff and only the full suite was red, so reproducing it is the correct
    response rather than evidence of being stuck — and on a suite with
    order-dependent specs it is the *expected* response. Observed live: one
    flaky spec elsewhere in the suite cost a reset, a rework, and a planner
    intervention that set about reshaping work which was already right.
    """
    if not ctx.previous_diff_digest or not outcome.diff_digest:
        return None
    if outcome.diff_digest != ctx.previous_diff_digest:
        return None
    if ctx.previous_failure_layer == "full_suite":
        return None
    if ctx.resuming:
        # Re-entering at verify is re-checking the tree, not repeating an
        # attempt — a repository-state resume exists so a human's fix is
        # checked rather than discarded, and the tree being unchanged since the
        # last verify is the normal case, not evidence of a stalled executor.
        return None

    return _fail(
        Layer.PROGRESS,
        Route.PLANNER,
        "the attempt reproduced the previous diff exactly",
        "This attempt produced a byte-identical diff to the one before it, so "
        "the feedback from that attempt changed nothing. The executor cannot "
        "make progress on this stage as drawn — retrying it again would cost "
        "another attempt and another review for the same result. Redraw the "
        "stage: narrow it, widen its scope, give it a clearer instruction, or "
        "insert a predecessor that makes it achievable.",
    )


# --- layer 4: forbidden patterns -----------------------------------------


def _layer_patterns(ctx: _Context, outcome: VerifyOutcome):
    """The gate's spelling of `gates.check_patterns`.

    The finding lives in `gates.py` so the executor's loop can ask the same
    question before paying for a round trip; the routing lives here, because
    which of executor, planner or human a failure belongs to is the graph's
    decision and not a gate's.
    """
    found = gates.check_patterns(ctx.stage, ctx.cfg, ctx.git, ctx.stage_start_sha)
    outcome.exempt_pattern_files = found.exempt_pattern_files
    if found.ok:
        return None
    return _fail(Layer.PATTERNS, Route.EXECUTOR, found.summary, found.feedback)


# --- layer 4: residue ----------------------------------------------------


def _layer_residue(ctx: _Context, outcome: VerifyOutcome):
    """The gate's spelling of `gates.check_residue`. See `_layer_patterns`."""
    found = gates.check_residue(ctx.stage, ctx.cfg, ctx.git)
    if found.ok:
        return None
    return _fail(Layer.RESIDUE, Route.EXECUTOR, found.summary, found.feedback)


# --- layer 5: tests ------------------------------------------------------


def _layer_tests(ctx: _Context, outcome: VerifyOutcome):
    command = resolve_test_command(ctx.stage, ctx.cfg, ctx.git, ctx.stage_start_sha)
    if not command:
        return None

    outcome.unscoped_tests = bool(ctx.cfg.scoped_test_command) and (
        command == ctx.cfg.test_command
    )

    if outcome.unscoped_tests and ctx.stage.require_scoped_tests:
        # Before the suite runs, not after. The cost of a redraw is one planner
        # call; the cost of letting this through is the whole suite on this
        # attempt and on every reviewer round trip that follows it.
        return _fail(
            Layer.TESTS,
            Route.PLANNER,
            "the stage does not identify which specs prove it",
            "Nothing identified the specs this stage affects: its diff touched "
            "no test files and it declared no `test_paths`, so the only sound "
            "check left is the entire suite — on this attempt and again on "
            "every rework.\n\n"
            "Redraw it with `test_paths` naming the specs that exercise the "
            "code it changes, even though it does not modify them. If nothing "
            "covers this code, widen `edit_files` and have the stage add a "
            "spec instead.",
        )

    known = _recorded_answer(ctx, "tests", command)
    if known is not None:
        if not known.get("failed"):
            # The loop ran this command on this tree and it passed.
            _record_full_suite(ctx, outcome, command)
            return None
        # And it ran the same command on the same tree and it failed. Running
        # it again to watch it fail identically is the duplication this whole
        # mechanism exists to remove, and it falls hardest on the stages that
        # need the most attempts.
        return _fail(
            Layer.TESTS,
            Route.EXECUTOR,
            known.get("summary") or "the test command failed",
            known.get("feedback") or "",
            failing_paths=list(known.get("failing_paths") or []),
        )

    # The running, the one re-run and the flake adjudication all live in
    # `gates.py`, shared with the executor's loop. What stays here is the
    # routing and the full-suite bookkeeping: a signal is a human's problem, a
    # failure is the executor's, and neither is a gate's decision to make.
    found = gates.run_tests(
        ctx.stage, ctx.cfg, ctx.git, ctx.runner, ctx.stage_start_sha, for_loop=False
    )
    outcome.results.extend(found.results)
    outcome.test_seconds += found.test_seconds
    outcome.flake_reruns += found.flake_reruns
    outcome.flaky_files.extend(found.flaky_files)
    outcome.flaky_seeds.update(found.flaky_seeds)
    outcome.flaky_examples.update(found.flaky_examples)

    if found.ok:
        # A file that passes whole and standalone is green, which is the same
        # verdict the merge gate would reach — so a flake counts as the full
        # suite having passed on this tree, exactly as a first-try pass does.
        _record_full_suite(ctx, outcome, command)
        return None

    if found.signal is not None:
        return _fail(Layer.TESTS, Route.HUMAN, found.summary, found.feedback)

    return _fail(
        Layer.TESTS,
        Route.EXECUTOR,
        found.summary,
        found.feedback,
        failing_paths=found.failing_paths,
    )


def _record_full_suite(ctx: _Context, outcome: VerifyOutcome, command: str) -> None:
    """Remember a green full suite so the merge gate need not repeat it.

    Only when the command *is* `full_test_command`. `test_command` may be the
    same string on some projects and a cheaper unscoped run on others, and the
    gate's contract is about the full suite specifically — so this compares the
    command rather than inferring from `unscoped_tests`.

    The digest is taken after the suite ran, so anything the run itself left in
    the tree is already part of the fingerprint the gate will compare against.
    """
    if command and command == ctx.cfg.full_test_command:
        outcome.full_suite_digest = diff_digest(ctx.git, ctx.stage_start_sha)


def resolve_test_command(
    stage: Stage, cfg: ProjectConfig, git: Git, stage_start_sha: str
) -> str | None:
    """Which test command the gate runs for this stage.

    The selection itself lives in `gates.py`, shared with the executor's inner
    loop, because the two used to decide separately and drifted. This wrapper
    is the gate's spelling of the question — `for_loop=False` — and exists so
    the call sites and their tests keep naming what they have always named.
    """
    return gates.resolve_test_command(
        stage, cfg, git, stage_start_sha, for_loop=False
    )


# --- layer 6: checks -----------------------------------------------------


def _layer_checks(ctx: _Context, outcome: VerifyOutcome):
    """The gate's spelling of `gates.run_checks`. See `_layer_patterns`.

    These are the checks that may *write* — `rubocop -A` and its kin — which
    is why the executor runs them inside its own loop and commits after. Run
    only here, a check's rewrite is swept up silently when the stage lands and
    orphaned when it does not.
    """
    if _already_answered(ctx, "checks", ""):
        return None

    found = gates.run_checks(ctx.stage, ctx.runner)
    outcome.results.extend(found.results)
    if found.ok:
        return None
    if found.signal is not None:
        return _fail(Layer.CHECKS, Route.HUMAN, found.summary, found.feedback)
    return _fail(
        Layer.CHECKS,
        Route.EXECUTOR,
        found.summary,
        found.feedback,
        failing_paths=found.failing_paths,
    )


# --- layer 6: new tests --------------------------------------------------


def _layer_new_tests(ctx: _Context, outcome: VerifyOutcome):
    """The gate's spelling of `gates.check_new_tests`. See `_layer_patterns`."""
    found = gates.check_new_tests(
        ctx.stage, ctx.cfg, ctx.git, ctx.stage_start_sha
    )
    if found.ok:
        return None
    return _fail(Layer.NEW_TESTS, Route.EXECUTOR, found.summary, found.feedback)


def _clip(text: str) -> str:
    # The same clipping the gates use, so a setup failure and a test failure
    # are truncated the same way. `collapse_progress_runs` before truncation is
    # the load-bearing half: a progress reporter puts its dots first and its
    # findings after, and 1,575 unbroken dots once made up 66% of the feedback
    # handed to an executor.
    return gates.clip(text)
