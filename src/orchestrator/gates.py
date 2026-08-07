"""What to run, decided once for both places that run it.

Two callers ask "which tests does this stage need": the executor's inner loop,
before it has finished editing, and the verify gate, after. They asked
separately for most of this project's life and drifted, which is the failure
this module exists to make impossible — not because either answer was wrong,
but because both were right and neither knew about the other.

The divergences are real and each has an incident behind it, so they are
carried here as one function with a documented flag rather than smoothed away.
`for_loop=True` is the executor's question, `for_loop=False` is the gate's.
They differ in five ways, all recorded below at the point where they differ.

The rule this serves is the one about spelling a command the same way in both
places: run `rubocop -A` one way inside the loop and another at the gate and
the exit code stops describing the artifacts. The same is true of a test
selection — a loop that runs a different set from the gate reports a green
that the gate then contradicts, and the disagreement reads as flakiness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.commands import CommandResult, CommandRunner, clip_for_model
from orchestrator.config import ProjectConfig, Stage
from orchestrator.flake import adjudicate
from orchestrator.gitops import Git
from orchestrator.globs import matches_any


@dataclass
class GateResult:
    """What one gate found, in terms both callers can act on.

    Deliberately not `VerifyOutcome`. That type carries routing — which of
    executor, planner or human a failure belongs to — and routing is the
    graph's business, not a gate's. A gate answers "is this true of the tree";
    `verify.py` maps that onto a `Route`. Separating what was found from what
    should happen next is the same rule the reviewer's contract is built on,
    and the executor's inner loop needs the first half without the second.

    `command` and `head_sha` record what ran and against what. A caller with a
    green record for the current HEAD, from the command it would have run
    itself, already has the answer and does not need to ask again — which is
    how the gate stops duplicating the loop on the agent path while still
    testing a tree a human edited by hand.
    """

    ok: bool
    summary: str = ""
    feedback: str = ""
    command: str = ""
    head_sha: str = ""
    exempt_pattern_files: list[str] = field(default_factory=list)
    results: list[CommandResult] = field(default_factory=list)
    failing_paths: list[str] = field(default_factory=list)
    flaky_files: list[str] = field(default_factory=list)
    flaky_seeds: dict[str, str] = field(default_factory=dict)
    flake_reruns: int = 0
    test_seconds: float = 0.0
    # Set when a command was killed from outside rather than failing. Neither
    # caller can fix it, so both stop; only the gate knows that means a human.
    signal: int | None = None


def expand_globs(path: str, repo: Path) -> list[str]:
    """A glob against the working tree, or nothing.

    The planner writes globs, and it should: it cannot know the repository's
    file list, so a pattern is a reasonable way to name a group of specs. But
    an unmatched glob substituted into a command reaches the test runner as a
    literal asterisk and dies in three seconds — which the gate then reads as
    the stage's tests failing and charges to the executor's retry budget. Three
    seconds is not a test run, and nothing noticed.

    Both callers expand identically. It is only *plain* paths they disagree
    about.
    """
    return sorted(str(m.relative_to(repo)) for m in repo.glob(path))


def is_glob(path: str) -> bool:
    return any(ch in path for ch in "*?[")


def runnable(path: str, stage: Stage, cfg: ProjectConfig) -> bool:
    """Is this declared path worth putting in front of the inner loop?

    Dropped only on positive evidence that it is not: a readable repository
    that does not contain it, and a stage that cannot create it. A stage can
    create it if the path is inside what it is allowed to write, or if it is
    obliged to add tests and so may write specs it was not handed by name.

    The repository check is deliberately a precondition rather than an
    assumption. If `target_repo` cannot be read there is no evidence either
    way, and inventing some by treating every path as absent would silently
    switch the inner loop off for a whole project on the strength of a check
    that never ran.
    """
    if not cfg.target_repo.is_dir():
        return True
    if (cfg.target_repo / path).exists():
        return True
    return stage.require_new_tests or matches_any(path, stage.edit_files)


def tests_the_stage_may_edit(stage: Stage, cfg: ProjectConfig) -> list[str]:
    """The stage's own tests, added to whatever it declared.

    Measured over one run of 35 stages: 12 declared no `test_paths` at all, so
    a third of the run ran with no inner loop and paid a whole round trip — a
    fresh process, re-reading the files — for every failure it could have fixed
    in place. Those 12 averaged 1.42 attempts against 0.91 for the rest. In
    every one of them the tests were already listed in `edit_files`, because a
    coverage stage edits the spec it is proving.

    Added rather than used as a fallback, which is the correction to the first
    version of this. Declaring paths does not mean declaring the right ones: of
    the 17 stages that declared some *and* edited a test file, 11 named a
    different file than the one they were editing — including the worst stage
    of that run, which reached attempt 4 editing a controller spec while its
    inner loop ran two request specs. A loop that tests everything except the
    file being rewritten is worse than none, because it reports green while the
    edit is unverified. Between them the two shapes covered 23 of 35 stages.

    So this reads a fact the stage already carries rather than asking the
    planner to restate it. What counts as a test comes from
    `test_file_patterns`, the same config the new-tests gate reads, so no
    project's vocabulary reaches this file.

    Plain paths only. A glob in `edit_files` may name a whole spec tree, and
    expanding it would hand the executor most of the suite — which it must
    never have, because it knows nothing of `edit_files` and will edit whatever
    is red to make it green. A stage whose tests are only reachable by glob
    keeps the old behaviour of no inner loop, which is worse than a scoped one
    and much better than a wrong one.
    """
    return [
        path
        for path in stage.edit_files
        if not is_glob(path) and matches_any(path, cfg.test_file_patterns)
    ]


def resolve_test_paths(
    stage: Stage,
    cfg: ProjectConfig,
    git: Git | None = None,
    since_sha: str = "",
    *,
    for_loop: bool,
) -> list[str]:
    """The paths, in the order the command will list them.

    **First divergence — where the paths come from.** The gate adds the test
    files in the diff, because after the work exists the tree itself says what
    was touched. The loop cannot: it is asked before the edits are made, so it
    has only what the stage declared plus what the stage is allowed to edit.

    **Second — a plain path that does not exist.** The gate drops it, because
    a path that is not there is not a test and running the full suite instead
    is slow but true. The loop keeps it when `runnable` says the stage could
    create it, because the loop runs *after* the edits and a spec the stage was
    told to write will be there by then.

    Kept unconditionally, a path the stage cannot create is a command that can
    never pass, and the executor reads the runner's "no such file" as a failing
    test and spends its budget repairing a file that will never exist. Observed
    live: a planner that could not grep the spec tree declared two spec paths
    for a repository containing neither, and the attempt hung on a
    77,000-token fix. The gate dropped the same two paths and ran the whole
    suite, which passed — so the edit had been right the entire time and only
    the inner loop was chasing a phantom.

    **Third — the union with `tests_the_stage_may_edit`.** The loop adds them;
    the gate does not need to, because they are already in the diff if the
    stage touched them.
    """
    paths: list[str] = []

    if not for_loop and git is not None:
        changed = git.diff_names(since_sha)
        paths.extend(p for p in changed if matches_any(p, cfg.test_file_patterns))

    for raw in stage.test_paths:
        path = (raw or "").strip()
        if not path:
            continue
        if is_glob(path):
            resolved = expand_globs(path, cfg.target_repo)
        elif for_loop:
            resolved = [path] if runnable(path, stage, cfg) else []
        else:
            resolved = [path] if (cfg.target_repo / path).exists() else []
        paths.extend(resolved)

    if for_loop:
        paths.extend(tests_the_stage_may_edit(stage, cfg))

    # Deduplicate while preserving order. Diff-derived paths come first on the
    # gate's side because those definitely exist.
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def resolve_test_command(
    stage: Stage,
    cfg: ProjectConfig,
    git: Git | None = None,
    since_sha: str = "",
    *,
    for_loop: bool,
) -> str | None:
    """Which test command to run, or `None` when there is nothing worth running.

    **Fourth divergence — the template.** The gate honours `stage.test_command`
    first, because an operator-declared per-stage command is the whole point of
    the field; then `scoped_test_command`. The loop uses `auto_test_command` if
    the operator set one, else `scoped_test_command`, and never the project's
    full suite: the executor has no notion of `edit_files`, so faced with a red
    spec outside the stage it will edit that spec, and a full suite gives it
    minutes per pass in which to do so.

    **Fifth — what happens when nothing resolves.** The gate falls back to the
    full command, which is slow but true. The loop returns `None` and runs no
    inner loop at all, because a command that can never pass is worse than no
    command: the attempt ends believing it succeeded.

    The directory swap is common to both, and the loop's extra condition —
    only when `auto_test_command` was not set — is preserved: an operator who
    named the loop's command meant that command.
    """
    if not for_loop and stage.test_command:
        return stage.test_command

    template_base = (
        (cfg.auto_test_command or cfg.scoped_test_command)
        if for_loop
        else cfg.scoped_test_command
    )
    if not template_base:
        return None if for_loop else cfg.test_command

    paths = resolve_test_paths(stage, cfg, git, since_sha, for_loop=for_loop)
    if not paths:
        # Nothing identifiable to scope to. The gate falls through to the full
        # command; the loop declines to run rather than running an empty
        # selection and calling it green.
        return None if for_loop else cfg.test_command

    template = template_base
    swap_allowed = (not for_loop) or cfg.auto_test_command is None
    if (
        swap_allowed
        and cfg.directory_test_command
        and any((cfg.target_repo / p).is_dir() for p in paths)
    ):
        template = cfg.directory_test_command
    return template.format(paths=" ".join(paths))


# --- the layers a caller can fix by editing -------------------------------
#
# Everything below routes to the executor when it fails, which is what makes it
# the executor's own business: each one names something wrong that editing can
# put right. The layers that route to the planner or a human — scope, progress,
# branch identity, setup — stay in `verify.py`, because they judge whether the
# executor stayed inside its remit, and a check the subject can iterate against
# is one it will route around rather than satisfy.


def check_patterns(stage: Stage, cfg: ProjectConfig, git: Git, since_sha: str) -> GateResult:
    """Nothing forbidden was *introduced*.

    Reads the diff's added lines, so it sees a construct arriving and is blind
    to one left behind. `check_residue` is the other half; the two read almost
    identically in prose and are opposites in a diff.
    """
    if not stage.forbidden_patterns:
        return GateResult(ok=True, head_sha=git.head_sha())

    added = git.added_lines(since_sha)
    hits: list[str] = []
    exempt: list[str] = []
    for pattern in stage.forbidden_patterns:
        compiled = re.compile(pattern)
        for path, text in added:
            if not compiled.search(text):
                continue
            # A test proving the construct is gone has to name it. This gate
            # reads added lines, so an assertion that the construct is absent
            # and the construct itself are the same text — nothing in the line
            # distinguishes them. Deadlocked a stage across all three
            # components: the planner prescribed the assertion, the reviewer
            # reworked the stage for omitting it, and this gate rejected every
            # attempt that included it.
            if matches_any(path, cfg.test_file_patterns):
                if path not in exempt:
                    exempt.append(path)
                continue
            hits.append(f"  {path}: {text.strip()}   [matches /{pattern}/]")

    if not hits:
        return GateResult(ok=True, exempt_pattern_files=exempt, head_sha=git.head_sha())

    return GateResult(
        ok=False,
        summary="the diff introduced a forbidden pattern",
        feedback=(
            "These added lines match patterns this stage forbids:\n"
            + "\n".join(hits[:40])
            + "\nRemove them. They are out of bounds for this stage even if they "
            "would be correct elsewhere in the project."
        ),
        exempt_pattern_files=exempt,
        head_sha=git.head_sha(),
    )


def check_residue(stage: Stage, cfg: ProjectConfig, git: Git) -> GateResult:
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
    if not stage.must_not_remain:
        return GateResult(ok=True, head_sha=git.head_sha())

    compiled = [(p, re.compile(p)) for p in stage.must_not_remain]
    hits: list[str] = []

    for path in git.tracked_paths_now():
        if not matches_any(path, stage.edit_files):
            continue
        full = cfg.target_repo / path
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
        return GateResult(ok=True, head_sha=git.head_sha())

    return GateResult(
        ok=False,
        summary="the stage left behind what it was meant to remove",
        feedback=(
            "This stage declared that no occurrence of these patterns may remain "
            "in the files it owns, and these are still there:\n"
            + "\n".join(hits[:40])
            + (f"\n… and {len(hits) - 40} more" if len(hits) > 40 else "")
            + "\n\nThese are occurrences that were never edited, not ones you "
            "introduced — the work is incomplete rather than wrong. Convert the "
            "remaining sites the same way you converted the others, and change "
            "nothing else."
        ),
        head_sha=git.head_sha(),
    )


# Enough for a traceback and a summary; not so much that it crowds out the
# instruction it is attached to.
FEEDBACK_OUTPUT_CHARS = 4_000

# Regex for a pytest/rspec-style failing file path in test output. Best effort:
# what the planner needs is a hint about where the damage is, and a wrong guess
# costs nothing because the full output travels with it.
_PATH_HINT = re.compile(r"([\w./-]+\.(?:rb|py|js|ts|tsx|go))")


def clip(text: str) -> str:
    return clip_for_model(text, FEEDBACK_OUTPUT_CHARS)


def path_hints(output: str) -> list[str]:
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


def run_checks(stage: Stage, runner: CommandRunner) -> GateResult:
    """The operator's declared checks, in order, stopping at the first failure.

    These may write as well as report — `rubocop -A`, `eslint --fix`, `gofmt -w`
    are the useful ones — which is why the executor runs them inside its loop
    and commits afterwards. Run only at the gate, a check's rewrite is swept up
    silently when the stage lands and orphaned when it does not, and the next
    stage's precheck then refuses to cut a branch over changes it cannot
    attribute.
    """
    if not stage.checks:
        return GateResult(ok=True)

    results = runner.run_all(stage.checks)
    failed = next((r for r in results if not r.ok), None)
    if failed is None:
        return GateResult(ok=True, results=results)

    if failed.signal is not None:
        # Checks run in the same environment as the suite and die with it.
        return GateResult(
            ok=False,
            summary=f"a required check was killed by signal {failed.signal}",
            feedback=(
                f"A required check did not fail — it was killed by signal "
                f"{failed.signal}, which is an environment problem rather than a "
                "problem with this stage. Put the environment back and resume.\n"
                f"{failed.summary()}\n{clip(failed.output)}"
            ),
            results=results,
            signal=failed.signal,
            command=failed.command,
        )

    return GateResult(
        ok=False,
        summary="a required check failed",
        feedback=f"A required check failed.\n{failed.summary()}\n{clip(failed.output)}",
        results=results,
        failing_paths=path_hints(failed.output),
        command=failed.command,
    )


def run_tests(
    stage: Stage,
    cfg: ProjectConfig,
    git: Git,
    runner: CommandRunner,
    since_sha: str = "",
    *,
    for_loop: bool,
) -> GateResult:
    """Run the stage's tests once, with one re-run before calling it a failure.

    Flake adjudication lives here rather than in either caller, because it
    belongs wherever tests are *run* — the executor's loop and the gate would
    otherwise reach different verdicts about the same intermittent spec, and
    the disagreement would read as the gate contradicting the loop.

    How to re-run depends on what just ran. A broad suite gets its failed
    examples re-run on their own, which tests the order dependence directly
    instead of re-rolling every other example. A command already scoped to the
    stage's own specs has nothing broader to blame — every failing example is
    one the stage owns — so it simply runs again.

    `ok=True` with an empty `command` means there was nothing worth running,
    which the loop treats as "no inner test loop" and the gate as "fall through
    to the full suite". That distinction is `resolve_test_command`'s, not this
    function's.
    """
    command = resolve_test_command(stage, cfg, git, since_sha, for_loop=for_loop)
    if not command:
        return GateResult(ok=True, head_sha=git.head_sha())

    result = runner.run(command)
    out = GateResult(
        ok=True,
        command=command,
        head_sha=git.head_sha(),
        results=[result],
        test_seconds=result.duration_seconds,
    )

    if result.ok:
        return out

    if result.signal is not None:
        # Something killed the suite from outside — the container stack going
        # down, an OOM kill, a Ctrl-C. Not a stage failure, so it must not spend
        # a retry, and re-running against the same dead environment would learn
        # nothing at whatever the suite costs.
        out.ok = False
        out.signal = result.signal
        out.summary = f"the test command was killed by signal {result.signal}"
        out.feedback = (
            f"The test command did not fail — it was killed by signal "
            f"{result.signal}.\n\nThat is an environment problem rather than a "
            "problem with this stage: the container stack going down, an "
            "out-of-memory kill, or an interrupt. Nothing the executor or the "
            "planner can do will fix it, so the run stops here rather than "
            "spending attempts.\n\nPut the environment back and resume; the "
            f"gate re-runs from here.\n{result.summary()}\n{clip(result.output)}"
        )
        return out

    # One re-run before consuming a retry. Browser-driven and timing-sensitive
    # suites would otherwise spend the whole retry budget on noise.
    broad = command in (cfg.test_command, cfg.full_test_command)
    if broad:
        verdict = adjudicate(
            output=result.output, command=command, cfg=cfg, runner=runner
        )
        out.results.extend(verdict.results)
        out.test_seconds += verdict.seconds
        detail = verdict.summary
        last_output = verdict.output or result.output
        flaked = verdict.flaked
        if flaked:
            out.flaky_files.extend(verdict.files)
            out.flaky_seeds.update(verdict.seeds)
    else:
        rerun = runner.run(command)
        out.results.append(rerun)
        out.test_seconds += rerun.duration_seconds
        detail = rerun.summary()
        last_output = rerun.output
        flaked = rerun.ok

    if flaked:
        out.flake_reruns += 1
        return out

    out.ok = False
    out.summary = "the test command failed"
    out.feedback = f"The test command failed.\n{detail}\n{clip(last_output)}"
    out.failing_paths = path_hints(last_output)
    return out


def check_new_tests(
    stage: Stage, cfg: ProjectConfig, git: Git, since_sha: str
) -> GateResult:
    """A test file the stage wrote has to contain something.

    Two rules, and only the second is conditional. Whether a stage *must* write
    tests is the operator's and the planner's business; whether a test file it
    did write is worth anything is not a matter of opinion, and an empty one is
    worthless however the stage was configured.

    This was originally written entirely behind the flag. One stage later, a
    stage whose whole output was a spec file left it at zero bytes with the flag
    unset, so the gate never ran and a review turn paid for it.
    """
    changed = git.diff_names(since_sha)
    touched = [p for p in changed if matches_any(p, cfg.test_file_patterns)]

    # Content, not just a path. The editor creates any file it is handed, so a
    # stage naming a not-yet-existing spec in `edit_files` gets that file
    # whether or not the model's reply was applied — and a reply that returned
    # the spec body as a plain fenced block instead of an edit leaves it at zero
    # bytes. Observed: the scoped suite passed in five seconds because there
    # were no examples to run, the diff really had added a test file, and the
    # reviewer was the only thing between an empty file and a landed stage.
    #
    # A path in the diff that is missing from the worktree was deleted, which is
    # not what this gate is about; it simply does not count towards the
    # requirement.
    root = Path(cfg.target_repo)
    substantial = []
    for path in touched:
        try:
            if (root / path).read_text().strip():
                substantial.append(path)
        except (OSError, UnicodeDecodeError):
            # Unreadable or binary — not this gate's business to adjudicate,
            # and a binary fixture is content by any reading.
            substantial.append(path)
    if substantial:
        return GateResult(ok=True, head_sha=git.head_sha())

    patterns = ", ".join(cfg.test_file_patterns)
    if touched:
        listed = ", ".join(touched)
        plural = len(touched) > 1
        return GateResult(
            ok=False,
            head_sha=git.head_sha(),
            summary=(
                "the stage's test files are empty"
                if plural
                else "the stage's test file is empty"
            ),
            feedback=(
                f"This stage touched {listed}, but "
                + ("every one of them is empty" if plural else "that file is empty")
                + ", so they assert nothing and the suite passes them in no time at "
                "all.\n\nThe editor creates a file named in your scope before you "
                "edit it, so an empty one means your reply was not applied as an "
                "edit. Write the file's contents as a proper edit rather than as a "
                "quoted block, and check the file is not empty before you finish."
            ),
        )
    if not stage.require_new_tests:
        # Nothing empty, and nothing required. A stage that legitimately writes
        # no tests reaches here and is none of this gate's business.
        return GateResult(ok=True, head_sha=git.head_sha())
    return GateResult(
        ok=False,
        head_sha=git.head_sha(),
        summary="the stage wrote no tests",
        feedback=(
            "This stage requires tests, and the diff touches no test file. Write "
            "the tests for this behaviour, then the implementation that satisfies "
            f"them.\nRecognised test paths: {patterns}"
        ),
    )
