"""Execution of operator-declared commands.

Every command CodeGantry runs passes through here: setup, tests,
checks, preconditions, context commands, and script-stage transforms. None
of them ever originate from model output — see PLAN.md's safety
requirements. `context_commands` push command output *into* a prompt; no
path exists in the other direction.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import signal
import subprocess
import tempfile
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

# `run_argv(log=...)` distinguishes "not asked" from "asked for silence", which
# `None` alone cannot: `None` is a legitimate sink meaning discard.
_INHERIT = object()


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


def clip_report_for_model(text: str, max_chars: int) -> str:
    """A *runner's report* made fit to hand to a model.

    The sibling of `clip_for_model`, and the difference is the content's shape
    rather than a preference. A reviewer's note is prose and its head is its
    point; a linter's or a test runner's output ends with what to do about it.
    Two functions rather than a flag at each call site, because which of the
    two a caller has is a property of what it is holding and not a decision to
    re-make: `gates` hands over runner output and nothing else does.

    The collapse still happens first, and for the reason it always did — a
    progress run truncated rather than collapsed spends the budget on dots.
    """
    return truncate_to_tail(collapse_progress_runs(text), max_chars)


# What survives from the front of a report: enough for the first line, which is
# the runner naming what it is about to do. Measured over 255 complete RuboCop
# listings, the text before the first offence — the `Inspecting N files` line
# and the collapsed progress run — is 143 characters at the median and 229 at
# the worst. So the head is nearly free, and everything else belongs to the end.
REPORT_HEAD_CHARS = 240


def truncate_to_tail(text: str, max_chars: int, head_chars: int = REPORT_HEAD_CHARS) -> str:
    """Keep a glimpse of the head and spend the rest of the budget on the tail.

    For output whose *answer is at the end*. `truncate_middle` splits the
    budget evenly because "command output is informative at both ends", which
    is true of a command that fails at the top and false of a runner that
    reports at the bottom — and both of this project's runners report at the
    bottom.

    Measured on 495 test-failure feedbacks actually handed to an executor: 96%
    were truncated, only 53% still carried `N examples, M failures`, and only
    **20%** still carried the `Failed examples:` list — the rerun commands,
    which are the most actionable thing RSpec prints and sit in the last few
    hundred bytes. The same defect is already recorded one layer up, where a
    334,143-character suite put that block 146,285 characters from the end and
    the even split dropped it; that was fixed by moving truncation to the point
    of use and left the *weighting* alone.

    RuboCop gains less, and the reason is worth stating so nobody expects more
    from this than it gives: its offence blocks are a median 257 bytes, so a
    4,000-character budget holds about fifteen of them however they are
    arranged. Splitting seven-and-seven or taking fifteen contiguously from the
    end shows the same number. What the tail buys there is the
    `N offenses detected` summary and an unbroken run rather than two halves
    with a hole between them.

    The tail is cut at a line boundary. Starting mid-line hands the model a
    fragment that looks like a line and is not, which is the same class of
    fault as a delimiter drawn from the content's own alphabet.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text

    marker_template = "\n... [{dropped} characters truncated] ...\n"
    reserve = len(marker_template.format(dropped=len(text)))
    keep = max(max_chars - reserve, 0)

    head_len = min(head_chars, keep)
    head = text[:head_len]
    # Not past the first newline: the allowance is a ceiling on one line, not a
    # licence to take several.
    if "\n" in head:
        head = head[: head.index("\n")]

    tail_len = keep - len(head)
    tail = text[len(text) - tail_len :] if tail_len > 0 else ""
    # Forward to the next line start, so the tail opens on a whole line. Only
    # when that costs little — a tail with no newline in its first stretch is
    # one long line, and half of it beats none of it.
    cut = tail.find("\n")
    if 0 <= cut < len(tail) // 4:
        tail = tail[cut + 1 :]

    dropped = len(text) - len(head) - len(tail)
    return head + marker_template.format(dropped=dropped) + tail


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
    # Time spent waiting for a host lock before the command started. Outside
    # `duration_seconds`, so a queue never reads as a slow suite.
    waited_seconds: float = 0.0

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


def host_lock_dir() -> Path:
    """Where this host's command locks live: one directory per user, outside
    every checkout, so two runs in two checkouts contend for the same file.
    `CODE_GANTRY_LOCK_DIR` overrides it."""
    override = os.environ.get("CODE_GANTRY_LOCK_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"code-gantry-{os.getuid()}" / "locks"


class CommandRunner:
    """Runs shell command strings in a target repo, with a timeout that takes
    the whole process group with it.

    `exclusive` maps a command to the name of a host lock it must hold while
    it runs. The full suite is the case: it takes every core, so a second
    copy on the host buys no throughput and doubles the memory, and every
    run on the host serialises on the one name. The lock is an advisory
    file lock, released by the kernel when its holder exits, so a crashed
    run leaves nothing to clean up. The wait has no ceiling of its own: the
    holder's command timeout bounds it.
    """

    def __init__(
        self,
        cwd: Path | str,
        timeout: int,
        env: dict[str, str] | None = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        log: Callable[[str], None] | None = None,
        exclusive: dict[str, str] | None = None,
        lock_dir: Path | None = None,
    ):
        self.cwd = Path(cwd)
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._extra_env = env or {}
        self._log = log
        self.exclusive = dict(exclusive or {})
        self._lock_dir = lock_dir

    @contextlib.contextmanager
    def _holding(self, label: str, log):
        """Hold the host lock `label` maps to, if any, for the block. Yields a
        one-element list that carries the seconds spent waiting."""
        waited = [0.0]
        name = self.exclusive.get(label)
        if not name:
            yield waited
            return
        lock_dir = self._lock_dir or host_lock_dir()
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = lock_dir / f"{name}.lock"
        with open(path, "a+") as handle:
            started = time.monotonic()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.seek(0)
                holder = handle.read().strip() or "another process"
                if log:
                    log(f"waiting for the {name!r} lock, held by {holder}")
                fcntl.flock(handle, fcntl.LOCK_EX)
                waited[0] = time.monotonic() - started
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid {os.getpid()}: {' '.join(label.split())}")
            handle.flush()
            try:
                yield waited
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

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
        log: Callable[[str], None] | None = _INHERIT,
    ) -> CommandResult:
        """Run an argv list with no shell.

        The counterpart to `run`, and the difference is the whole safety story
        for `project_tools`: those commands carry values the model supplied, and
        with no shell in the path there is no metacharacter to escape — a value
        of `rails; rm -rf /` is one argument that makes the program error.

        `label` is the joined form because that is what an operator reads in the
        log and what the denylist scanned; the list is what actually runs, and
        the two can only disagree by whitespace in an element.

        `log=None` sends the `$ command` line nowhere. A declared tool is a
        model's tool call and belongs in the tool log with the others; the run
        log is the timeline, and the loop's own commands — lint, the suite,
        setup — are what belongs there. Measured on one run: 27 declared calls
        put 54 lines into a 140-line timeline, every one already recorded in
        `tools.log`. Defaulted to the runner's own sink rather than to `None`,
        so nothing that does not ask loses its logging.
        """
        argv = list(argv)
        return self._spawn(
            argv, shell=False, label=" ".join(argv), timeout=timeout, log=log
        )

    def _spawn(
        self,
        target,
        shell: bool,
        label: str,
        timeout: int | None = None,
        extra_env: dict[str, str] | None = None,
        log=_INHERIT,
    ) -> CommandResult:
        effective_timeout = self.timeout if timeout is None else timeout
        sink = self._log if log is _INHERIT else log

        env = self._env()
        if extra_env:
            env.update(extra_env)

        with self._holding(label, sink) as waited:
            started = time.monotonic()
            proc, stdout, stderr, timed_out = self._communicate(
                target, shell, env, effective_timeout
            )
            duration = time.monotonic() - started

        result = CommandResult(
            command=label,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=truncate_middle(stdout or "", self.max_output_chars),
            stderr=truncate_middle(stderr or "", self.max_output_chars),
            duration_seconds=duration,
            timed_out=timed_out,
            waited_seconds=waited[0],
        )

        if sink:
            # Collapsed for the log line only. The run log is one line per
            # event and is read by skimming, and an argv element may hold a
            # whole shell script. `result.command` keeps the command whole:
            # this is a rendering, not a record.
            one_line = " ".join(label.split())
            queued = f" after waiting {waited[0]:.1f}s" if waited[0] else ""
            if timed_out:
                sink(f"$ {one_line}\n  timed out after {duration:.1f}s{queued}")
            else:
                sink(f"$ {one_line}\n  exit {result.exit_code} in {duration:.1f}s{queued}")

        return result

    def _communicate(self, target, shell: bool, env: dict, timeout: float):
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
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_group(proc)
            # Reap and collect whatever the process managed to emit.
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                stdout, stderr = "", ""
        return proc, stdout, stderr, timed_out

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
