"""Deciding whether a red suite is the stage's fault or the suite's.

The merge gate is reviewer approval AND a green full suite. That second half is
only as trustworthy as the suite, and large legacy suites are rarely
order-independent: on the first real target, four runs of 2,335 examples
produced four failures, all different, all passing when run alone.

Re-running the whole suite — the original rule — tests the wrong thing. It
re-rolls every order-dependent example in the suite, so the second run is
roughly as likely to trip over a different one, and the stage is blamed for a
property of the repository. Re-running *only the examples that failed* tests
the property actually in question: does this example pass when the rest of the
suite is not running?

Two guards keep that from becoming a way to launder real failures:

- **Ownership.** A failure in a file the stage edited or named in `test_paths`
  is never credited as a flake. Order dependence in a file the stage just
  touched is at least as likely to be new as pre-existing.
- **Volume.** Thirty examples do not flake simultaneously. Past a small
  threshold the failure is treated as real without spending the re-run.

Everything here is driven by operator-supplied config: the regex that finds the
failed examples and the command template they are substituted into. The
orchestrator never composes a shell command out of a model's output, and this
is no exception — the locators come from the test runner's own stdout.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from orchestrator.commands import CommandResult, CommandRunner

if TYPE_CHECKING:  # `config` imports the default pattern from here.
    from orchestrator.config import ProjectConfig

# RSpec's end-of-run block:
#
#     Failed examples:
#
#     rspec './spec/requests/checkout_spec.rb[1:1:1:1]' # Checkout Storefront …
#
# The quotes are optional (they appear when the locator contains brackets), and
# the trailing `# description` is free text that may itself contain quotes, so
# the capture stops at the first whitespace or quote.
DEFAULT_FAILED_EXAMPLE_PATTERN = r"^\s*rspec\s+'?([^'\s]+)'?"


@dataclass
class FlakeVerdict:
    """What a re-run established, and what it cost."""

    flaked: bool
    seconds: float = 0.0
    results: list[CommandResult] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def output(self) -> str:
        return self.results[-1].output if self.results else ""


def failed_examples(output: str, pattern: str | None) -> list[str]:
    """Re-runnable locators for the examples that failed, in order, deduplicated.

    A parallel runner concatenates one block per worker, and a worker that
    retries can name the same example twice, so duplicates are expected.
    """
    if not pattern or not output:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in re.findall(pattern, output, re.MULTILINE):
        locator = match if isinstance(match, str) else match[0]
        if locator and locator not in seen:
            seen.add(locator)
            out.append(locator)
    return out


def locator_file(locator: str) -> str:
    """The file part of `./spec/a_spec.rb[1:2:3]` or `./spec/a_spec.rb:12`."""
    path = locator.split("[", 1)[0]
    path = re.sub(r":\d+(:\d+)*$", "", path)
    return normalize(path)


def normalize(path: str) -> str:
    """Test runners print `./spec/a_spec.rb`; git prints `spec/a_spec.rb`."""
    return path[2:] if path.startswith("./") else path


def _owner(failing: str, owned: set[str]) -> str | None:
    """Which owned path claims this failing file, if any.

    A stage's `test_paths` are paths, and a planner naming coverage will often
    name a directory: the first real stage declared `spec/controllers` and the
    gate then failed inside it. Exact matching excused that as a flake, which
    inverts the rule — the stage had just pointed at that directory as the
    thing that proves it.

    Compared segment-wise so `spec/controllers` claims
    `spec/controllers/admin/orders_spec.rb` without also claiming
    `spec/controllers_helper/a_spec.rb`.
    """
    if failing in owned:
        return failing
    for candidate in owned:
        if candidate and failing.startswith(candidate.rstrip("/") + "/"):
            return candidate
    return None


def adjudicate(
    *,
    output: str,
    command: str,
    cfg: ProjectConfig,
    runner: CommandRunner,
    owned_paths: set[str],
) -> FlakeVerdict:
    """Re-run what failed and decide whether the failure was real.

    `owned_paths` is what the stage may not blame on the suite: the files its
    diff touched plus the specs it declared. Callers pass repo-relative paths.
    """
    examples = (
        failed_examples(output, cfg.failed_example_pattern)
        if cfg.flake_rerun_examples
        else []
    )
    if not examples or not cfg.scoped_test_command:
        return _whole_suite_rerun(command, runner)

    if len(examples) > cfg.flake_rerun_max_examples:
        return FlakeVerdict(
            flaked=False,
            examples=examples,
            summary=(
                f"{len(examples)} failing examples is too many to be a flake "
                f"(limit {cfg.flake_rerun_max_examples}); not re-run"
            ),
        )

    owned_here = {normalize(p) for p in owned_paths}
    owned = sorted({o for e in examples for o in (_owner(locator_file(e), owned_here),) if o})
    if owned:
        return FlakeVerdict(
            flaked=False,
            examples=examples,
            summary=(
                "this stage touches " + ", ".join(owned) + ", so its failure is "
                "the stage's own however it re-runs; not re-run"
            ),
        )

    paths = " ".join(shlex.quote(e) for e in examples)
    rerun = runner.run(cfg.scoped_test_command.format(paths=paths))
    listed = ", ".join(examples)
    return FlakeVerdict(
        flaked=rerun.ok,
        seconds=rerun.duration_seconds,
        results=[rerun],
        examples=examples,
        summary=(
            f"{len(examples)} failing example(s) passed when re-run alone "
            f"({listed}); recorded as a suite flake"
            if rerun.ok
            else f"{len(examples)} failing example(s) failed again when re-run "
            f"alone ({listed}); the failure is real"
        ),
    )


def _whole_suite_rerun(command: str, runner: CommandRunner) -> FlakeVerdict:
    """The original rule, kept for runners we cannot read.

    Weak, but strictly better than nothing, and it is what every project
    without a `failed_example_pattern` match still gets.
    """
    rerun = runner.run(command)
    return FlakeVerdict(
        flaked=rerun.ok,
        seconds=rerun.duration_seconds,
        results=[rerun],
        summary=(
            "could not tell which examples failed; the whole suite passed on "
            "re-run" if rerun.ok
            else "could not tell which examples failed; the whole suite failed "
            "again"
        ),
    )
