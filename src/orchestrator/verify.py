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

from orchestrator.commands import CommandResult, CommandRunner, truncate_middle
from orchestrator.config import ProjectConfig, Stage
from orchestrator.flake import adjudicate
from orchestrator.gitops import Git, GitError
from orchestrator.globs import matches_any

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
    previous_diff_digest: str | None = None,
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
        previous_diff_digest=previous_diff_digest,
    )

    outcome.diff_digest = _diff_digest(git, stage_start_sha)

    for layer in (
        _layer_setup,
        _layer_branch,
        _layer_scope,
        _layer_progress,
        _layer_patterns,
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
    base_sha: str | None
    previous_diff_digest: str | None = None


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


def _diff_digest(git: Git, stage_start_sha: str) -> str:
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
    """
    if not ctx.previous_diff_digest or not outcome.diff_digest:
        return None
    if outcome.diff_digest != ctx.previous_diff_digest:
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
    for pattern in ctx.stage.forbidden_patterns:
        compiled = re.compile(pattern)
        for path, text in added:
            if compiled.search(text):
                hits.append(f"  {path}: {text.strip()}   [matches /{pattern}/]")

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


# --- layer 4: tests ------------------------------------------------------


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
    else:
        rerun = ctx.runner.run(command)
        outcome.results.append(rerun)
        outcome.test_seconds += rerun.duration_seconds
        detail = rerun.summary()
        last_output = rerun.output
        flaked = rerun.ok

    if flaked:
        outcome.flake_reruns += 1
        return None

    return _fail(
        Layer.TESTS,
        Route.EXECUTOR,
        "the test command failed",
        f"The test command failed.\n{detail}\n{_clip(last_output)}",
        failing_paths=_path_hints(last_output),
    )


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


def _scoped_test_paths(
    stage: Stage, cfg: ProjectConfig, git: Git, stage_start_sha: str
) -> list[str]:
    changed = git.diff_names(stage_start_sha)
    from_diff = [p for p in changed if matches_any(p, cfg.test_file_patterns)]
    declared = [p for p in stage.test_paths if p]
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
