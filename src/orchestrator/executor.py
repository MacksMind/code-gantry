"""The executor: Aider for agent stages, a declared command for script stages.

Aider is invoked as a subprocess in headless mode, not imported as a library.
The orchestrator hands it a task and waits; Aider owns its own
edit → lint → test → fix cycle inside a single attempt.

Two things worth knowing:

- **File scoping is mandatory, not an optimisation.** Handing a local model an
  unscoped repository means the repo map alone consumes the context window
  before the task is stated. Every agent stage declares what it may edit and
  what it needs to read.
- **Flag names are unverified against a live Aider.** The spec says to check
  them, and `preflight` does exactly that by parsing `aider --help`, because
  Aider's CLI surface changes between releases. `executor.extra_args` is the
  escape hatch when it has moved again.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from orchestrator.commands import CommandResult, CommandRunner
from orchestrator.config import ProjectConfig, Stage

# The flags we build. preflight checks each of these against `aider --help`
# so a release that renamed one fails validation instead of stage 1.
AIDER_FLAGS = [
    "--message",
    "--yes-always",
    "--no-stream",
    "--model",
    "--openai-api-base",
    "--test-cmd",
    "--auto-test",
    "--lint-cmd",
    "--map-tokens",
    "--file",
    "--read",
]


@dataclass
class ExecutionResult:
    ok: bool
    log: str = ""
    timed_out: bool = False
    results: list[CommandResult] = field(default_factory=list)


def build_aider_argv(stage: Stage, cfg: ProjectConfig, prompt: str) -> list[str]:
    """Assemble the Aider invocation.

    The API key is deliberately absent: it goes through the environment, so it
    never appears in `ps` output or in our own run log.
    """
    ex = cfg.executor
    argv = [
        "aider",
        "--message",
        prompt,
        "--yes-always",
        "--no-stream",
        "--model",
        ex.model,
    ]

    if ex.api_base:
        argv += ["--openai-api-base", ex.api_base]

    test_command = stage.effective_test_command(cfg)
    if test_command:
        argv += ["--test-cmd", test_command, "--auto-test"]

    if ex.lint_command:
        argv += ["--lint-cmd", ex.lint_command]

    argv += ["--map-tokens", str(ex.map_tokens)]

    for glob in stage.edit_files:
        argv += ["--file", glob]
    for glob in stage.read_files:
        argv += ["--read", glob]

    argv += list(ex.extra_args)
    return argv


class Executor:
    def __init__(self, cfg: ProjectConfig, runner: CommandRunner):
        self.cfg = cfg
        self.runner = runner

    def gather_context(
        self, stage: Stage
    ) -> tuple[list[tuple[str, str]], list[CommandResult]]:
        """Run the stage's context commands and collect their stdout.

        These are declared by the operator, never produced by a model. Their
        output flows into the prompt; nothing flows the other way.
        """
        collected: list[tuple[str, str]] = []
        results: list[CommandResult] = []
        for command in stage.context_commands:
            result = self.runner.run(command)
            results.append(result)
            collected.append((command, result.output))
        return collected, results

    def run_agent_stage(self, stage: Stage, prompt: str) -> ExecutionResult:
        try:
            env = self._executor_env()
        except KeyError as e:
            # Failing here beats letting Aider fail opaquely on auth.
            return ExecutionResult(ok=False, log=str(e.args[0]))

        argv = build_aider_argv(stage, self.cfg, prompt)
        result = self.runner.run_argv(
            argv,
            timeout=self.cfg.limits.aider_timeout_seconds,
            env=env,
        )
        return ExecutionResult(
            ok=result.ok,
            log=result.output,
            timed_out=result.timed_out,
            results=[result],
        )

    def run_script_stage(self, stage: Stage) -> ExecutionResult:
        result = self.runner.run(stage.command or "")
        return ExecutionResult(
            ok=result.ok,
            log=result.output,
            timed_out=result.timed_out,
            results=[result],
        )

    def _executor_env(self) -> dict[str, str]:
        """Environment additions for the Aider subprocess.

        Aider talks to the local endpoint through the OpenAI-compatible
        variables, so the configured key env var is mapped onto
        OPENAI_API_KEY rather than passed as a flag.
        """
        env: dict[str, str] = {}
        name = self.cfg.executor.api_key_env
        if name:
            if name not in os.environ:
                raise KeyError(
                    f"executor.api_key_env names {name}, which is not set in "
                    "the environment"
                )
            env["OPENAI_API_KEY"] = os.environ[name]
        if self.cfg.executor.api_base:
            env["OPENAI_API_BASE"] = self.cfg.executor.api_base
        return env
