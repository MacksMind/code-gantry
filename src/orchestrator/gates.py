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

from pathlib import Path

from orchestrator.config import ProjectConfig, Stage
from orchestrator.gitops import Git
from orchestrator.globs import matches_any


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
