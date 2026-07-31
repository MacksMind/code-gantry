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
