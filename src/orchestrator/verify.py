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
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum

from orchestrator.commands import CommandResult, CommandRunner, truncate_middle
from orchestrator.config import ProjectConfig, Stage
from orchestrator.flake import adjudicate
from orchestrator.gitops import Git, GitError
from orchestrator.globs import matches_any
from orchestrator.plandoc import resolve_plan_tree

# How much of a failing command's output to carry forward. Enough for a
# traceback and a summary; not so much that it crowds out the instruction.
FEEDBACK_OUTPUT_CHARS = 4_000

# Regex for a pytest/rspec-style failing file path in test output. Best effort:
# what the planner needs is a hint about where the damage is, and a wrong guess
# costs nothing because the full output travels with it.
_PATH_HINT = re.compile(r"([\w./-]+\.(?:rb|py|js|ts|tsx|go))")


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
    )

    outcome.diff_digest = diff_digest(git, stage_start_sha)

    for layer in (
        _layer_setup,
        _layer_branch,
        _layer_scope,
        _layer_progress,
        _layer_patterns,
        _layer_residue,
        _layer_tests,
        _layer_checks,
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

    out_of_scope = [p for p in changed if not matches_any(p, ctx.stage.edit_files)]
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
    if not ctx.stage.forbidden_patterns:
        return None

    added = ctx.git.added_lines(ctx.stage_start_sha)
    hits: list[str] = []
    exempt: list[str] = []
    for pattern in ctx.stage.forbidden_patterns:
        compiled = re.compile(pattern)
        for path, text in added:
            if not compiled.search(text):
                continue
            # A test proving the construct is gone has to name it. The gate
            # reads added lines, so `not_to include('new Ajax.Request')` and
            # reintroducing `new Ajax.Request` are the same text — nothing in
            # the line distinguishes them. Deadlocked a stage across all three
            # components: the planner prescribed the assertion, the reviewer
            # reworked the stage for omitting it, and this gate rejected every
            # attempt that included it.
            if matches_any(path, ctx.cfg.test_file_patterns):
                if path not in exempt:
                    exempt.append(path)
                continue
            hits.append(f"  {path}: {text.strip()}   [matches /{pattern}/]")

    outcome.exempt_pattern_files = exempt

    if not hits:
        return None

    return _fail(
        Layer.PATTERNS,
        Route.EXECUTOR,
        "the diff introduced a forbidden pattern",
        "These added lines match patterns this stage forbids:\n"
        + "\n".join(hits[:40])
        + "\nRemove them. They are out of bounds for this stage even if they "
        "would be correct elsewhere in the project.",
    )


# --- layer 4: residue ----------------------------------------------------


def _layer_residue(ctx: _Context, outcome: VerifyOutcome):
    """Nothing the stage promised to remove is still there.

    `forbidden_patterns` reads the diff's added lines, so it sees a construct
    arriving and is blind to one left behind — and "no occurrence of X should
    remain" is the shape of most migration work. An occurrence the executor
    simply missed produces no added line, so the diff cannot be asked about it.
    This reads the files instead.

    Scoped to `edit_files`, with the same globs the scope guard uses. That is
    the ground the stage claimed; a residue outside it belongs to work nobody
    authorised this stage to do, and failing on it would be unactionable.
    """
    if not ctx.stage.must_not_remain:
        return None

    compiled = [(p, re.compile(p)) for p in ctx.stage.must_not_remain]
    hits: list[str] = []

    for path in ctx.git.tracked_paths_now():
        if not matches_any(path, ctx.stage.edit_files):
            continue
        full = ctx.cfg.target_repo / path
        try:
            text = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Binary, unreadable, or deleted in this attempt. A regex over
            # source has nothing to say about any of those.
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, rx in compiled:
                if rx.search(line):
                    hits.append(
                        f"  {path}:{number}: {line.strip()}   [matches /{pattern}/]"
                    )

    if not hits:
        return None

    return _fail(
        Layer.RESIDUE,
        Route.EXECUTOR,
        "the stage left behind what it was meant to remove",
        "This stage declared that no occurrence of these patterns may remain "
        "in the files it owns, and these are still there:\n"
        + "\n".join(hits[:40])
        + (f"\n… and {len(hits) - 40} more" if len(hits) > 40 else "")
        + "\n\nThese are occurrences that were never edited, not ones you "
        "introduced — the work is incomplete rather than wrong. Convert the "
        "remaining sites the same way you converted the others, and change "
        "nothing else.",
    )


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

    result = ctx.runner.run(command)
    outcome.results.append(result)
    outcome.test_seconds += result.duration_seconds

    if result.ok:
        _record_full_suite(ctx, outcome, command)
        return None

    if result.signal is not None:
        # Something killed the suite from outside — the container stack going
        # down, an OOM kill, a Ctrl-C. Not a stage failure, so it must not spend
        # a retry, and re-running against the same dead environment would learn
        # nothing at whatever the suite costs.
        return _fail(
            Layer.TESTS,
            Route.HUMAN,
            f"the test command was killed by signal {result.signal}",
            f"The test command did not fail — it was killed by signal "
            f"{result.signal}.\n\nThat is an environment problem rather than a "
            "problem with this stage: the container stack going down, an "
            "out-of-memory kill, or an interrupt. Nothing the executor or the "
            "planner can do will fix it, so the run stops here rather than "
            "spending attempts.\n\nPut the environment back and resume; the "
            "gate re-runs from here.\n"
            f"{result.summary()}\n{_clip(result.output)}",
        )

    # One re-run before consuming a retry. Browser-driven and timing-sensitive
    # suites would otherwise spend the whole retry budget on noise.
    #
    # How to re-run depends on what just ran. A broad suite gets the failed
    # examples re-run on their own, which tests the order dependence directly
    # instead of re-rolling every other example in the suite. A command already
    # scoped to the stage's own specs has nothing broader to blame — every
    # failing example is one the stage owns — so it simply runs again.
    broad = command in (ctx.cfg.test_command, ctx.cfg.full_test_command)
    if broad:
        verdict = adjudicate(
            output=result.output,
            command=command,
            cfg=ctx.cfg,
            runner=ctx.runner,
        )
        outcome.results.extend(verdict.results)
        outcome.test_seconds += verdict.seconds
        detail = verdict.summary
        last_output = verdict.output or result.output
        flaked = verdict.flaked
        if flaked:
            outcome.flaky_files.extend(verdict.files)
            outcome.flaky_seeds.update(verdict.seeds)
    else:
        rerun = ctx.runner.run(command)
        outcome.results.append(rerun)
        outcome.test_seconds += rerun.duration_seconds
        detail = rerun.summary()
        last_output = rerun.output
        flaked = rerun.ok

    if flaked:
        outcome.flake_reruns += 1
        # A file that passes whole and standalone is green, which is the same
        # verdict the merge gate would reach — so this counts as the full suite
        # having passed on this tree, exactly as a first-try pass does.
        _record_full_suite(ctx, outcome, command)
        return None

    return _fail(
        Layer.TESTS,
        Route.EXECUTOR,
        "the test command failed",
        f"The test command failed.\n{detail}\n{_clip(last_output)}",
        failing_paths=_path_hints(last_output),
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
    """Which test command to run for this stage.

    When `scoped_test_command` is configured, iteration runs only the specs the
    stage actually affects — the paths from the diff, plus any the planner
    declared it expected to affect. The planner supplies paths; the operator
    supplies the command. That is how per-stage scoping happens without a model
    authoring shell.
    """
    if stage.test_command:
        return stage.test_command

    if cfg.scoped_test_command:
        paths = _scoped_test_paths(stage, cfg, git, stage_start_sha)
        if paths:
            command = cfg.scoped_test_command
            if cfg.directory_test_command and any(
                (cfg.target_repo / p).is_dir() for p in paths
            ):
                command = cfg.directory_test_command
            return command.format(paths=" ".join(paths))
        # Nothing identifiable to scope to: fall through to the full command
        # rather than running an empty selection and calling it green.

    return cfg.test_command


def _resolve_declared(paths: list[str], repo: Path) -> list[str]:
    """Turn the planner's declared paths into ones that exist.

    The planner writes globs, and it should: it cannot know the repository's
    file list, so `spec/controllers/**/*billing*` is a reasonable way to say
    "the billing controller specs". But an unmatched glob substituted into a
    command reaches the test runner as a literal asterisk, and rspec dies on it
    in three seconds — which verify then reads as the stage's tests failing and
    charges to the executor's retry budget. Three seconds is not a test run,
    and nothing noticed.

    So globs are expanded here and anything that does not exist is dropped.
    Dropping everything is fine: `resolve_test_command` falls back to the full
    command, which is slow but true.
    """
    out: list[str] = []
    for raw in paths:
        path = (raw or "").strip()
        if not path:
            continue
        if any(ch in path for ch in "*?["):
            out.extend(
                sorted(str(m.relative_to(repo)) for m in repo.glob(path))
            )
        elif (repo / path).exists():
            out.append(path)
    return out


def _scoped_test_paths(
    stage: Stage, cfg: ProjectConfig, git: Git, stage_start_sha: str
) -> list[str]:
    changed = git.diff_names(stage_start_sha)
    from_diff = [p for p in changed if matches_any(p, cfg.test_file_patterns)]
    declared = _resolve_declared(stage.test_paths, cfg.target_repo)
    # Deduplicate while preserving order, diff first: those definitely exist.
    seen: set[str] = set()
    out: list[str] = []
    for path in from_diff + declared:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


 # --- layer 5: checks -----------------------------------------------------




# --- layer 5: checks -----------------------------------------------------


def _layer_checks(ctx: _Context, outcome: VerifyOutcome):
    if not ctx.stage.checks:
        return None

    results = ctx.runner.run_all(ctx.stage.checks)
    outcome.results.extend(results)

    failed = next((r for r in results if not r.ok), None)
    if failed is None:
        return None

    if failed.signal is not None:
        # Checks run in the same environment as the suite and die with it.
        return _fail(
            Layer.CHECKS,
            Route.HUMAN,
            f"a required check was killed by signal {failed.signal}",
            f"A required check did not fail — it was killed by signal "
            f"{failed.signal}, which is an environment problem rather than a "
            "problem with this stage. Put the environment back and resume.\n"
            f"{failed.summary()}\n{_clip(failed.output)}",
        )

    return _fail(
        Layer.CHECKS,
        Route.EXECUTOR,
        "a required check failed",
        f"A required check failed.\n{failed.summary()}\n{_clip(failed.output)}",
        failing_paths=_path_hints(failed.output),
    )


# --- layer 6: new tests --------------------------------------------------


def _layer_new_tests(ctx: _Context, outcome: VerifyOutcome):
    if not ctx.stage.require_new_tests:
        return None

    changed = ctx.git.diff_names(ctx.stage_start_sha)
    if any(matches_any(p, ctx.cfg.test_file_patterns) for p in changed):
        return None

    patterns = ", ".join(ctx.cfg.test_file_patterns)
    return _fail(
        Layer.NEW_TESTS,
        Route.EXECUTOR,
        "the stage wrote no tests",
        "This stage requires tests, and the diff touches no test file. Write "
        "the tests for this behaviour, then the implementation that satisfies "
        f"them.\nRecognised test paths: {patterns}",
    )


def _path_hints(output: str) -> list[str]:
    """Source paths mentioned in failing output.

    What the planner needs at an intervention is where the damage is — that is
    what distinguishes "widen this stage by two files" from "we skipped a
    prerequisite". Best effort; the full output travels alongside it.
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in _PATH_HINT.findall(output or ""):
        if match not in seen:
            seen.add(match)
            out.append(match)
    return out[:20]


def _clip(text: str) -> str:
    return truncate_middle(text, FEEDBACK_OUTPUT_CHARS)
