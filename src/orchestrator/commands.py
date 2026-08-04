"""Execution of operator-declared commands.

Every command the orchestrator runs passes through here: setup, tests,
checks, preconditions, context commands, and script-stage transforms. None
of them ever originate from model output — see PLAN.md's safety
requirements. `context_commands` push command output *into* a prompt; no
path exists in the other direction.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

# Enough to see a test summary and a stack trace without carrying a whole
# suite's chatter into graph state or a prompt.
# A memory guard, not a display limit. Output feeds two kinds of consumer:
# things that parse it, which need all of it, and things that put it in a
# prompt or a log line, which must be bounded — and those bound it themselves,
# at the point of use.
#
# It used to be 20,000, which served the second need and silently broke the
# first. A real suite emitted 334,143 characters with its `Failed examples:`
# block 146,285 characters from the end, under per-worker summaries, a coverage
# report and deprecation tallies. `truncate_middle` keeps the head and tail, so
# the block fell in the dropped middle, the flake gate found nothing to re-run,
# and a stage the reviewer had approved was reset over someone else's flaky
# spec.
DEFAULT_MAX_OUTPUT_CHARS = 5_000_000


# Long enough that no meaningful line reaches it, short enough to catch a
# progress bar early. A suite reporting `....F....` says something with every
# character; a thousand identical ones say only "still going".
_PROGRESS_RUN = re.compile(r"(.)\1{39,}")


def collapse_progress_runs(text: str) -> str:
    """Replace a long run of one repeated character with a note of its length.

    Command-line tools draw progress as a stream of identical characters and
    print the finding afterwards. Measured on a live stage: 1,575 unbroken dots
    were 66% of the feedback the executor received, ahead of the two lines that
    said what to fix — and that offence then survived four passes untouched.

    Deliberately written about repetition rather than about any tool's
    alphabet. A rule naming dots, or `F` and `E`, or a linter, would be one
    ecosystem's vocabulary compiled into a framework that ships to every
    project.

    Honest rather than merely shorter: the count survives, so a reader can tell
    a long run from a short one, and nothing that varies is discarded.
    """
    return _PROGRESS_RUN.sub(
        lambda m: f"{m.group(1) * 3}[{len(m.group(0)):,} repeated characters]",
        text or "",
    )


def clip_for_model(text: str, max_chars: int) -> str:
    """Command output made fit to hand to a model.

    One function because the order matters and was getting decided twice.
    `truncate_middle` keeps the head and tail on the reasoning that output is
    informative at both ends — true of most commands, false of a progress
    reporter, which puts its noise first and its findings after. Truncate an
    uncollapsed run and the dots are the head that survives while the offences
    are the middle that goes.
    """
    return truncate_middle(collapse_progress_runs(text), max_chars)


def truncate_middle(text: str, max_chars: int) -> str:
    """Keep the head and tail, drop the middle.

    Command output is informative at both ends — the invocation and early
    errors at the top, the failure summary at the bottom — and least
    informative in between.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    marker_template = "\n... [{dropped} characters truncated] ...\n"
    # Reserve room for the marker itself so the result honours max_chars.
    reserve = len(marker_template.format(dropped=len(text)))
    keep = max(max_chars - reserve, 0)
    head_len = keep // 2
    tail_len = keep - head_len

    head = text[:head_len]
    tail = text[len(text) - tail_len :] if tail_len else ""
    dropped = len(text) - head_len - tail_len
    return head + marker_template.format(dropped=dropped) + tail


@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def signal(self) -> int | None:
        """The signal that killed this command, if one did.

        A shell reports a signalled child as 128+N. That is a convention rather
        than a guarantee — a program may choose to exit 137 — but no test runner
        does, and the cost of being wrong is asymmetric: treating a real failure
        as an environment problem stops the run for a human, which is safe,
        while treating a SIGKILL as a test failure spends a retry on something
        no retry can fix.

        Two encodings reach us. Python reports a *direct* child killed by a
        signal as a negative return code, and we run through a shell, so that is
        the shell itself dying. When the shell survives and its own child is
        killed — the live case: the container stack went down under a running
        suite — the shell exits 128+N instead.

        Our own timeout kills the process group with SIGKILL, so that case is
        excluded here: a timeout is the stage's problem and already has its own
        flag, whereas a signal from outside is the environment's.
        """
        if self.timed_out:
            return None
        if self.exit_code < 0:
            return -self.exit_code
        if self.exit_code > 128:
            return self.exit_code - 128
        return None

    @property
    def output(self) -> str:
        """Both streams, for logs and for executor feedback."""
        parts = [self.stdout.rstrip()]
        if self.stderr.strip():
            parts.append(self.stderr.rstrip())
        return "\n".join(p for p in parts if p)

    def summary(self) -> str:
        if self.timed_out:
            return f"$ {self.command}\ntimed out after {self.duration_seconds:.0f}s"
        if self.signal is not None:
            return (
                f"$ {self.command}\nkilled by signal {self.signal} "
                f"(exit {self.exit_code}) after {self.duration_seconds:.0f}s"
            )
        return f"$ {self.command}\nexit {self.exit_code}"


class CommandRunner:
    """Runs shell command strings in a target repo, with a timeout that takes
    the whole process group with it."""

    def __init__(
        self,
        cwd: Path | str,
        timeout: int,
        env: dict[str, str] | None = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        log: Callable[[str], None] | None = None,
    ):
        self.cwd = Path(cwd)
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._extra_env = env or {}
        self._log = log

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._extra_env)
        return env

    def run(self, command: str, timeout: int | None = None) -> CommandResult:
        """Run a shell command string. Config commands use shell syntax
        (`a && b`, `! grep -q x`), so a shell is required."""
        return self._spawn(command, shell=True, label=command, timeout=timeout)

    def run_argv(
        self,
        argv: Sequence[str],
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Run an argument vector with no shell.

        Used for the executor, whose prompt is a multi-line string full of
        backticks and quotes. Passing that through a shell would be a
        quoting minefield for no benefit.
        """
        return self._spawn(
            list(argv),
            shell=False,
            label=" ".join(argv[:2]) + " ...",
            timeout=timeout,
            extra_env=env,
        )

    def _spawn(
        self,
        target,
        shell: bool,
        label: str,
        timeout: int | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> CommandResult:
        effective_timeout = self.timeout if timeout is None else timeout
        started = time.monotonic()

        env = self._env()
        if extra_env:
            env.update(extra_env)

        # start_new_session puts the child in its own process group so a
        # timeout can kill everything it spawned. Killing only the shell
        # leaves docker/bundler/pytest children holding resources.
        proc = subprocess.Popen(
            target,
            shell=shell,
            cwd=str(self.cwd),
            env=env,
            # Nothing here may read from the terminal. A command that waits on
            # stdin in an unattended run does not fail, it hangs — silently,
            # until the timeout kills it an hour later — and if the operator
            # happens to be at the terminal it eats their keystrokes instead.
            # Closed rather than inherited, so a prompt gets EOF and fails fast.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            start_new_session=True,
        )

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(proc)
            # Reap and collect whatever the process managed to emit.
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                stdout, stderr = "", ""

        duration = time.monotonic() - started
        result = CommandResult(
            command=label,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=truncate_middle(stdout or "", self.max_output_chars),
            stderr=truncate_middle(stderr or "", self.max_output_chars),
            duration_seconds=duration,
            timed_out=timed_out,
        )

        if self._log:
            if timed_out:
                self._log(f"$ {label}\n  timed out after {duration:.1f}s")
            else:
                self._log(f"$ {label}\n  exit {result.exit_code} in {duration:.1f}s")

        return result

    def run_all(
        self, commands: Sequence[str], timeout: int | None = None
    ) -> list[CommandResult]:
        """Run in order, stopping at the first failure.

        These are gates, not a report. Once one has failed the stage is
        failing, and running the rest only costs time.
        """
        results: list[CommandResult] = []
        for command in commands:
            result = self.run(command, timeout=timeout)
            results.append(result)
            if not result.ok:
                break
        return results

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # pragma: no cover
            proc.kill()
