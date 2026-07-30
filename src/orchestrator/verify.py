"""The layered verify gate.

`verify` is not one test command. It is an ordered sequence, cheapest gate
first, short-circuiting on the first failure. The ordering is economic: the
free deterministic checks run before anything that costs minutes of compute
or a paid API call.

Routing is the other half. Setup and scope failures escalate — they are
environment and containment problems, not defects an executor can be asked
to fix. Everything else retries with actionable feedback, because a retry
given no information about why the last attempt failed is a wasted retry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from orchestrator.commands import CommandResult, CommandRunner
from orchestrator.config import RunConfig, Stage
from orchestrator.gitops import Git
from orchestrator.globs import matches_any

# How much of a failing command's output to hand the executor. Enough for a
# traceback and a summary; not so much that it crowds out the instruction.
FEEDBACK_OUTPUT_CHARS = 4_000


class Layer(str, Enum):
    SETUP = "setup"
    SCOPE = "scope"
    PATTERNS = "patterns"
    TESTS = "tests"
    CHECKS = "checks"
    NEW_TESTS = "new_tests"


@dataclass
class VerifyOutcome:
    passed: bool
    failed_layer: Layer | None = None
    feedback: str = ""
    retryable: bool = False
    results: list[CommandResult] = field(default_factory=list)
    flake_reruns: int = 0
    test_seconds: float = 0.0


def run_verify(
    stage: Stage,
    cfg: RunConfig,
    git: Git,
    runner: CommandRunner,
    stage_start_sha: str,
) -> VerifyOutcome:
    outcome = VerifyOutcome(passed=True)

    for layer in (
        _layer_setup,
        _layer_scope,
        _layer_patterns,
        _layer_tests,
        _layer_checks,
        _layer_new_tests,
    ):
        failure = layer(stage, cfg, git, runner, stage_start_sha, outcome)
        if failure is not None:
            failure.results = outcome.results
            failure.flake_reruns = outcome.flake_reruns
            failure.test_seconds = outcome.test_seconds
            return failure

    return outcome


def _fail(
    layer: Layer, feedback: str, *, retryable: bool
) -> VerifyOutcome:
    return VerifyOutcome(
        passed=False, failed_layer=layer, feedback=feedback, retryable=retryable
    )


# --- layer 0: setup ------------------------------------------------------


def _layer_setup(stage, cfg, git, runner, sha, outcome):
    command = stage.effective_setup_command(cfg)
    if not command:
        return None

    result = runner.run(command)
    outcome.results.append(result)
    if result.ok:
        return None

    return _fail(
        Layer.SETUP,
        f"Environment setup failed.\n{result.summary()}\n"
        f"{_clip(result.output)}",
        # A broken environment is not something rework fixes.
        retryable=False,
    )


# --- layer 1: scope guard ------------------------------------------------


def _layer_scope(stage, cfg, git, runner, sha, outcome):
    changed = git.diff_names(sha)

    if not changed:
        # A stage that produced nothing has not been done, and a green suite
        # proves nothing about that. An executor can be asked to try again;
        # a human cannot be retried by the orchestrator.
        return _fail(
            Layer.SCOPE,
            "The previous attempt produced no changes at all. Nothing was "
            "edited, so the stage has not been done.",
            retryable=stage.kind != "manual",
        )

    if not stage.scope_guarded:
        return None

    out_of_scope = [p for p in changed if not matches_any(p, stage.edit_files)]
    if not out_of_scope:
        return None

    listed = "\n".join(f"  {p}" for p in sorted(out_of_scope))
    allowed = "\n".join(f"  {p}" for p in stage.edit_files)
    return _fail(
        Layer.SCOPE,
        f"Files were changed outside this stage's declared scope:\n{listed}\n"
        f"Only these globs are in scope:\n{allowed}",
        # Editing outside declared scope is a containment failure. The
        # operator should see it rather than have it silently reworked.
        retryable=False,
    )


# --- layer 2: forbidden patterns -----------------------------------------


def _layer_patterns(stage, cfg, git, runner, sha, outcome):
    if not stage.forbidden_patterns:
        return None

    added = git.added_lines(sha)
    hits: list[str] = []
    for pattern in stage.forbidden_patterns:
        compiled = re.compile(pattern)
        for path, text in added:
            if compiled.search(text):
                hits.append(f"  {path}: {text.strip()}   [matches /{pattern}/]")

    if not hits:
        return None

    return _fail(
        Layer.PATTERNS,
        "These added lines match patterns this stage forbids:\n"
        + "\n".join(hits[:40])
        + "\nRemove them. They are out of bounds for this stage even if they "
        "would be correct elsewhere.",
        retryable=True,
    )


# --- layer 3: tests ------------------------------------------------------


def _layer_tests(stage, cfg, git, runner, sha, outcome):
    command = stage.effective_test_command(cfg)
    if not command:
        return None

    result = runner.run(command)
    outcome.results.append(result)
    outcome.test_seconds += result.duration_seconds

    if result.ok:
        return None

    # One re-run before consuming a retry. Browser-driven and timing-sensitive
    # suites would otherwise spend the whole retry budget on noise.
    rerun = runner.run(command)
    outcome.results.append(rerun)
    outcome.test_seconds += rerun.duration_seconds

    if rerun.ok:
        outcome.flake_reruns += 1
        return None

    return _fail(
        Layer.TESTS,
        f"The test command failed.\n{rerun.summary()}\n{_clip(rerun.output)}",
        retryable=True,
    )


# --- layer 4: checks -----------------------------------------------------


def _layer_checks(stage, cfg, git, runner, sha, outcome):
    if not stage.checks:
        return None

    results = runner.run_all(stage.checks)
    outcome.results.extend(results)

    failed = next((r for r in results if not r.ok), None)
    if failed is None:
        return None

    return _fail(
        Layer.CHECKS,
        f"A required check failed.\n{failed.summary()}\n{_clip(failed.output)}",
        retryable=True,
    )


# --- layer 5: new tests --------------------------------------------------


def _layer_new_tests(stage, cfg, git, runner, sha, outcome):
    if not stage.require_new_tests:
        return None

    changed = git.diff_names(sha)
    if any(matches_any(p, cfg.test_file_patterns) for p in changed):
        return None

    patterns = ", ".join(cfg.test_file_patterns)
    return _fail(
        Layer.NEW_TESTS,
        "This stage requires tests, and the diff touches no test file. "
        "Write the tests for this behaviour, then the implementation that "
        f"satisfies them.\nRecognised test paths: {patterns}",
        retryable=True,
    )


def _clip(text: str) -> str:
    from orchestrator.commands import truncate_middle

    return truncate_middle(text, FEEDBACK_OUTPUT_CHARS)
