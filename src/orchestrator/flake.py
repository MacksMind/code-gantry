"""Deciding whether a red suite is the stage's fault or the suite's.

The merge gate is reviewer approval AND a green full suite. That second half is
only as trustworthy as the suite, and large legacy suites are rarely
order-independent: on the first real target, four runs of 2,335 examples
produced four failures, all different, all passing when run alone.

Re-running the whole suite — the original rule — tests the wrong thing. It
re-rolls every order-dependent example in the suite, so the second run is
roughly as likely to trip over a different one, and the stage is blamed for a
property of the repository.

So: **a file that passes whole and standalone is green.** Take the files that
failed and run them on their own. That is stronger than re-running a single
example — it also proves the example is independent of its siblings — and it is
what "this file is green" has to mean.

Note what is deliberately *not* here: an ownership rule. An earlier design
refused to excuse a spec the stage had edited, reasoning that the stage might
have introduced the order dependence. But a file that passes whole and
standalone has been proven green *including* the stage's edits to it. Whatever
makes it fail in the group is a property of the suite, to be cleaned up as its
own work rather than charged to whichever stage happened to be in flight.

One guard remains, on volume: thirty files do not flake simultaneously. Past a
small threshold the failure is treated as real without spending the re-run.

Nothing here knows what a test runner is. The regex that finds failing files
and the command they are substituted into are both operator-supplied, because
they are properties of a project rather than of this tool — and because the
orchestrator never composes a shell command out of a model's output.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from orchestrator.commands import CommandResult, CommandRunner

if TYPE_CHECKING:
    from orchestrator.config import ProjectConfig


@dataclass
class FlakeVerdict:
    """What a re-run established, and what it cost."""

    flaked: bool
    seconds: float = 0.0
    results: list[CommandResult] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    summary: str = ""
    # file -> the ordering seed that produced the failure, where the runner
    # said. Read from the run that failed, never from the re-runs: the whole
    # point is the ordering that broke, and the re-runs deliberately use a
    # different one.
    seeds: dict[str, str] = field(default_factory=dict)

    @property
    def output(self) -> str:
        return self.results[-1].output if self.results else ""


# CSI sequences: colour, cursor moves, anything a runner emits for a terminal.
# Stripped before matching because a pattern anchored at the start of a line
# cannot see past them, and the failure is silent — the gate just stops finding
# anything and falls back to re-running everything.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def failed_files(output: str, pattern: str | None) -> list[str]:
    """The distinct files that failed, in order, deduplicated.

    `pattern` is the operator's: group 1 must capture a repo-relative path.
    Deduplication matters twice over — a parallel runner concatenates one block
    per worker, and two failures in the same file must cost one re-run rather
    than two.
    """
    if not pattern or not output:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in re.findall(pattern, strip_ansi(output), re.MULTILINE):
        path = normalize(match if isinstance(match, str) else match[0])
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def normalize(path: str) -> str:
    """Test runners print `./spec/a_spec.rb`; git prints `spec/a_spec.rb`."""
    return path[2:] if path.startswith("./") else path


def seeds_by_file(
    output: str, failed_pattern: str | None, seed_pattern: str | None
) -> dict[str, str]:
    """The ordering seed to reproduce each failing file, where one is findable.

    Excusing a flake and recording only its name leaves the operator with a
    file and no way to make it fail again, which is most of the distance
    between "known flaky" and "fixed". The seed is the rest of it.

    Taken as the *first seed reported after* each failing file rather than a
    single seed for the run, because a parallel runner is many independent
    orderings. The run that motivated this printed fourteen, and each worker's
    summary ends with its own — so the seed that follows a failure is the one
    belonging to the worker that produced it. Anything else records a number
    that reproduces a different worker's ordering.

    A file with no seed after it is simply absent: half an answer is better
    reported as none than as a seed that does not reproduce anything.
    """
    if not output or not failed_pattern or not seed_pattern:
        return {}

    text = strip_ansi(output)
    try:
        failures = list(re.finditer(failed_pattern, text, re.MULTILINE))
        seeds = list(re.finditer(seed_pattern, text, re.MULTILINE))
    except re.error:  # pragma: no cover - config validation rejects these
        return {}

    found: dict[str, str] = {}
    for failure in failures:
        path = normalize(failure.group(1))
        if not path or path in found:
            continue
        after = next((s for s in seeds if s.start() > failure.end()), None)
        if after:
            found[path] = after.group(1)
    return found


def adjudicate(
    *,
    output: str,
    command: str,
    cfg: ProjectConfig,
    runner: CommandRunner,
) -> FlakeVerdict:
    """Re-run the files that failed and decide whether the failure was real."""
    files = (
        failed_files(output, cfg.failed_file_pattern)
        if cfg.flake_rerun_failed_files
        else []
    )
    if not files or not cfg.scoped_test_command:
        return _whole_suite_rerun(command, runner)

    if len(files) > cfg.flake_rerun_max_files:
        return FlakeVerdict(
            flaked=False,
            files=files,
            summary=(
                f"{len(files)} failing files is too many to be a flake "
                f"(limit {cfg.flake_rerun_max_files}); not re-run"
            ),
        )

    paths = " ".join(shlex.quote(f) for f in files)
    command_text = cfg.scoped_test_command.format(paths=paths)
    listed = ", ".join(files)
    seeds = seeds_by_file(output, cfg.failed_file_pattern, cfg.seed_pattern)

    # Three strikes: the group run that got us here, then up to two alone. One
    # isolated attempt proved too few — a spec failed in the suite, failed
    # alone, then passed on the next full run, and the stage it condemned was
    # innocent. Stop at the first pass; there is nothing to learn from
    # confirming one.
    results: list[CommandResult] = []
    for _ in range(max(cfg.flake_rerun_attempts, 1)):
        rerun = runner.run(command_text)
        results.append(rerun)
        if rerun.ok:
            break

    passed = results[-1].ok
    attempts = len(results)
    seconds = sum(r.duration_seconds for r in results)
    return FlakeVerdict(
        flaked=passed,
        seconds=seconds,
        results=results,
        files=files,
        seeds=seeds,
        summary=(
            f"{len(files)} failing file(s) passed when re-run whole and alone "
            f"({listed}); recorded as a suite flake"
            if passed
            else f"{len(files)} failing file(s) failed {attempts} time(s) when "
            f"re-run whole and alone ({listed}); the failure is real"
        ),
    )


def _whole_suite_rerun(command: str, runner: CommandRunner) -> FlakeVerdict:
    """The fallback when we cannot tell which files failed.

    Weak, but strictly better than nothing, and it is what a project without a
    `failed_file_pattern` still gets.
    """
    rerun = runner.run(command)
    return FlakeVerdict(
        flaked=rerun.ok,
        seconds=rerun.duration_seconds,
        results=[rerun],
        summary=(
            "could not tell which files failed; the whole suite passed on "
            "re-run" if rerun.ok
            else "could not tell which files failed; the whole suite failed again"
        ),
    )


@dataclass
class BaselineVerdict:
    """Whether a real failure was already there before the stage ran."""

    predates: bool = False
    checked: bool = False
    seconds: float = 0.0
    # The subset that also failed at the base. Empty when nothing did, which is
    # the "the stage caused this" answer.
    files: list[str] = field(default_factory=list)
    summary: str = ""
    output: str = ""


def predates_stage(
    *,
    files: list[str],
    base_sha: str,
    cfg: ProjectConfig,
    runner: CommandRunner,
    git,
    setup_command: str | None = None,
) -> BaselineVerdict:
    """Did these files already fail before this stage touched anything?

    `adjudicate` answers "flake or real". That is one question short of what
    the merge gate has to decide, because a real failure is not necessarily
    *this stage's* failure. A suite can go red between one stage and the next
    for reasons no diff explains — a spec that depends on the calendar is the
    cleanest example, and it is what motivated this: a stage that renamed four
    macros in one controller was sent back three times, with a byte-identical
    diff approved by the reviewer each time, because a commission-report spec
    in a file it never touched began failing when the clock crossed into the
    31st. The executor was scoped to that one controller and could not have
    fixed the spec under any instruction, so all three attempts were spent to
    learn nothing.

    So: re-run the same files against the tree as it stood at `base_sha`. Red
    there too and the stage did not cause it, which makes rework the wrong
    route no matter how many attempts remain — only the planner can act on it.

    Costs one scoped run, and only on a failure that has already been ruled a
    real one. The tree is returned to where it was found: both directions go
    through `reset_hard`, so untracked files are treated the same coming and
    going, and every commit involved is on the stage branch.

    Two limits, stated rather than papered over. A stage that changes the
    schema or the test environment can make the base run fail for reasons of
    its own, which reads as "pre-existing" — the planner sees the output and
    can tell. And `predates` requires *every* failing file to fail at the base;
    a partial overlap is reported but still routed as the stage's problem,
    because part of it is.

    Skipped outright on a dirty tree. Restoring by sha restores what is
    committed, so uncommitted work would be destroyed rather than put back —
    and the caller may be about to rework *forward* from it, with
    `rework_reset` off. The executor commits its own work, so the normal case
    is clean; declining to answer is the right move when it is not.

    `setup_command` runs after each reset, not just the first. Moving the tree
    moves the environment with it: a stage that touched the Dockerfile, the
    Gemfile, or the schema leaves containers built for the tip, and running the
    base tree's specs against them measures the wrong thing in the direction
    that produces a false "pre-existing". The restoring run matters for the
    same reason — the environment is left matching the tree, as it was found.
    If setup cannot be made to work at the base, the question goes unanswered
    rather than being answered wrongly.
    """
    if not files or not cfg.scoped_test_command or not base_sha:
        return BaselineVerdict(summary="no baseline comparison available")

    try:
        if not git.is_clean():
            return BaselineVerdict(
                summary=(
                    "the tree has uncommitted changes, so it cannot be put "
                    "back after a baseline run; not compared"
                )
            )
        restore_sha = git.head_sha()
    except Exception:  # pragma: no cover - a broken repo fails louder elsewhere
        return BaselineVerdict(summary="could not read HEAD to compare a baseline")

    paths = " ".join(shlex.quote(f) for f in files)
    command_text = cfg.scoped_test_command.format(paths=paths)
    listed = ", ".join(files)

    spent = [0.0]

    def timed(command: str):
        outcome = runner.run(command)
        spent[0] += outcome.duration_seconds
        return outcome

    git.reset_hard(base_sha)
    try:
        setup = timed(setup_command) if setup_command else None
        result = timed(command_text) if setup is None or setup.ok else None
    finally:
        git.reset_hard(restore_sha)
        if setup_command:
            timed(setup_command)

    seconds = spent[0]

    if result is None:
        return BaselineVerdict(
            seconds=seconds,
            output=setup.output,
            summary=(
                f"the environment could not be set up at {base_sha[:8]}, so "
                "the baseline was not compared"
            ),
        )

    if result.ok:
        return BaselineVerdict(
            checked=True,
            seconds=result.duration_seconds,
            output=result.output,
            summary=(
                f"the same file(s) passed at {base_sha[:8]}, before this stage "
                f"({listed}); the failure is this stage's"
            ),
        )

    also = failed_files(result.output, cfg.failed_file_pattern) or list(files)
    every = all(f in also for f in files)
    return BaselineVerdict(
        predates=every,
        checked=True,
        seconds=result.duration_seconds,
        files=also,
        output=result.output,
        summary=(
            f"{len(files)} failing file(s) failed the same way at "
            f"{base_sha[:8]}, before this stage ran ({listed}); the failure "
            "predates the stage"
            if every
            else f"{len(also)} of {len(files)} failing file(s) also failed at "
            f"{base_sha[:8]} ({', '.join(also)}); the rest are this stage's"
        ),
    )


# A red suite excused is a bug deferred, and the deferral is only honest if it
# leaves something to act on. `flaky_files` in the run state answers "which
# files", which is enough to notice a pattern and not enough to chase one — and
# it dies with the run, so the two-sighting bar for filing a bug is a question
# nobody can answer without grepping an old log. This file is the answer:
# append-only, one line per excusal, every run of every stage, small enough to
# read whole and structured enough to count.
FLAKES_FILENAME = "flakes.md"


def append_flakes(
    project_dir: Path | str,
    stage_id: str,
    files: list[str],
    seeds: dict[str, str],
    now: str,
) -> Path:
    """Record what was excused, when, and under which ordering.

    The arguments to a reproduction, not a reproduction — whoever chases this
    knows how to invoke the repo's own bisect tool, and a formatted command
    line would be one more thing to keep correct as that tool changes.

    `now` is the reason this is not just a seed. A suite can go red for reasons
    no ordering explains: the failure that motivated the baseline check was a
    spec asserting `Time.zone.today - 1.month`, which broke the instant the
    clock crossed into the 31st and would have gone on breaking every 29th
    through 31st. Nothing about the file name or the seed says that. A column
    of timestamps does, at a glance, the first time two of them cluster after
    midnight.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / FLAKES_FILENAME

    lines = []
    for name in files:
        seed = seeds.get(name)
        line = f"- flake `{now}` `{stage_id}` `{name}`"
        if seed:
            line += f" seed `{seed}`"
        else:
            # Said plainly rather than left blank. "No seed" is a fact about
            # the runner's output that the operator can go fix in
            # `seed_pattern`; a silently short line looks like the flake had
            # no ordering, which is never true.
            line += " — no seed reported"
        lines.append(line + "\n")

    with path.open("a") as fh:
        fh.writelines(lines)
    return path


def recent_flakes(path: Path | str) -> list[dict]:
    """Every excusal recorded so far, oldest first.

    Deliberately not deduplicated: two sightings of one file is the signal
    that it is worth filing, and collapsing them destroys exactly that.
    """
    path = Path(path)
    if not path.is_file():
        return []
    found = []
    for when, stage_id, name, seed in _FLAKE.findall(path.read_text()):
        found.append(
            {
                "at": when,
                "stage_id": stage_id,
                "file": name,
                "seed": seed or None,
            }
        )
    return found


_FLAKE = re.compile(
    r"^- flake `([^`]*)` `([^`]*)` `([^`]*)`(?: seed `([^`]*)`)?", re.MULTILINE
)
