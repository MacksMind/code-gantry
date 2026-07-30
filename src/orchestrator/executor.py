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
from pathlib import Path

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
    "--no-gitignore",
    "--no-show-model-warnings",
    "--edit-format",
    "--model-metadata-file",
    "--chat-history-file",
    "--input-history-file",
    "--llm-history-file",
]

# A command, not a browser name: Python's webbrowser module treats an entry
# containing %s as a command line to run, so this consumes the URL and does
# nothing. Aider pairs --yes-always with prompts like "Open documentation url
# for more info?", and an unattended overnight run must not answer yes to that
# dozens of times.
NO_BROWSER = "/usr/bin/true %s"

# Aider's client library requires *some* key for an `openai/`-prefixed model,
# even when the endpoint it is pointed at serves without auth. The `sk-` prefix
# satisfies any naive format check along the way.
PLACEHOLDER_API_KEY = "sk-no-key-required"


@dataclass
class ExecutionResult:
    ok: bool
    log: str = ""
    timed_out: bool = False
    results: list[CommandResult] = field(default_factory=list)


def build_aider_argv(
    stage: Stage,
    cfg: ProjectConfig,
    prompt: str,
    history_dir: Path | None = None,
) -> list[str]:
    """Assemble the Aider invocation.

    The API key is deliberately absent: it goes through the environment, so it
    never appears in `ps` output or in our own run log.

    `history_dir` relocates Aider's own scratch files. It writes
    `.aider.chat.history.md` and `.aider.input.history` into the repository
    root by default, which no stage declares and which therefore fails the
    scope gate on the first attempt of every project. Pointing them at the
    attempt directory keeps the target repository clean — it receives product
    code and plan revisions, nothing else — and makes the model's actual
    conversation a per-attempt artifact worth reading afterwards.
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
        # Aider adds `.aider*` to .gitignore by default. No stage declares that
        # file, so it fails the scope gate on the first attempt of every
        # project. The orchestrator owns this repository's git; Aider does not
        # need to tidy it.
        "--no-gitignore",
        # A model warning becomes "Open documentation url for more info?",
        # which --yes-always answers for us.
        "--no-show-model-warnings",
    ]

    if ex.edit_format:
        argv += ["--edit-format", ex.edit_format]

    if ex.model_metadata_file:
        argv += ["--model-metadata-file", ex.model_metadata_file]

    if history_dir is not None:
        argv += [
            "--chat-history-file", str(history_dir / "aider-chat.md"),
            "--input-history-file", str(history_dir / "aider-input.txt"),
            "--llm-history-file", str(history_dir / "aider-llm.txt"),
        ]

    api_base = ex.resolve_api_base()
    if api_base:
        argv += ["--openai-api-base", api_base]

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

    def run_agent_stage(
        self, stage: Stage, prompt: str, history_dir: Path | None = None
    ) -> ExecutionResult:
        try:
            env = self._executor_env()
            argv = build_aider_argv(stage, self.cfg, prompt, history_dir=history_dir)
        except KeyError as e:
            # A missing key or endpoint variable. Failing here beats letting
            # Aider fail opaquely on auth, or calling the wrong endpoint.
            return ExecutionResult(ok=False, log=str(e.args[0]))
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
        variables, so a configured key env var is mapped onto OPENAI_API_KEY
        rather than passed as a flag: a key in argv shows up in `ps` output and
        in our own run log.

        `api_key_env` is optional because a local endpoint — llama-swap,
        llama.cpp, Ollama — serves without auth, so there is no key for an
        operator to name. Aider's client still refuses to make the call with
        none set at all, so a placeholder is supplied here. That is a
        client-side requirement, not the server's.
        """
        env: dict[str, str] = {"BROWSER": NO_BROWSER}
        name = self.cfg.executor.api_key_env
        api_base = self.cfg.executor.resolve_api_base()

        if name:
            if name not in os.environ:
                raise KeyError(
                    f"executor.api_key_env names {name}, which is not set in "
                    "the environment"
                )
            env["OPENAI_API_KEY"] = os.environ[name]
        elif api_base:
            # Only when an api_base redirects us somewhere local. With no
            # api_base this is the real OpenAI endpoint, where a placeholder
            # would turn a legible "you set no key" into a puzzling 401.
            env["OPENAI_API_KEY"] = PLACEHOLDER_API_KEY

        if api_base:
            env["OPENAI_API_BASE"] = api_base
        return env
