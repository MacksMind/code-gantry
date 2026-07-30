"""The executor: Aider for agent stages, a declared command for script stages.

Aider's flag surface changes between releases and the spec says to verify it
rather than trust a list. That check lives in preflight; here we pin the argv
we intend to build, and use a fake `aider` on PATH so the wiring is exercised
without the real tool.
"""

import json
import os
import stat

import pytest

from orchestrator.commands import CommandRunner
from orchestrator.config import parse_config
from orchestrator.executor import Executor, build_aider_argv


def cfg_with(stage_overrides=None, **cfg_overrides):
    stage = {"id": "s1", "instruction": "do it", "edit_files": ["app/**", "src/*.py"]}
    stage.update(stage_overrides or {})
    data = {
        "target_repo": "/tmp/x",
        "branch": "work",
        "test_command": "pytest -q",
        "executor": {
            "model": "openai/local-model",
            "api_base": "http://spark:8080/v1",
            "lint_command": "ruff check --fix",
            "map_tokens": 0,
        },
        "reviewer": {"model": "gpt-5.5"},
        "stages": [stage],
    }
    data.update(cfg_overrides)
    return parse_config(data)


@pytest.fixture
def fake_aider(tmp_path, monkeypatch):
    """An `aider` on PATH that records its argv and environment."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "argv.json"
    script = bindir / "aider"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
        "print('aider ran')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return record


class TestAiderArgv:
    def test_passes_the_prompt_as_message(self):
        cfg = cfg_with()
        argv = build_aider_argv(cfg.stages[0], cfg, "PROMPT TEXT")
        assert "--message" in argv
        assert argv[argv.index("--message") + 1] == "PROMPT TEXT"

    def test_runs_unattended(self):
        # A subprocess that stops to ask a question would hang the run.
        cfg = cfg_with()
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert "--yes-always" in argv
        assert "--no-stream" in argv

    def test_passes_the_model_and_api_base(self):
        cfg = cfg_with()
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert argv[argv.index("--model") + 1] == "openai/local-model"
        assert argv[argv.index("--openai-api-base") + 1] == "http://spark:8080/v1"

    def test_passes_the_stage_test_command_for_auto_test(self):
        cfg = cfg_with(stage_overrides={"test_command": "pytest tests/unit"})
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "pytest tests/unit"
        assert "--auto-test" in argv

    def test_falls_back_to_the_global_test_command(self):
        cfg = cfg_with()
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "pytest -q"

    def test_omits_auto_test_when_there_is_no_test_command(self):
        # Greenfield: nothing exists to run yet. Passing --auto-test with no
        # command would fail every edit round.
        cfg = cfg_with(stage_overrides={"checks": ["true"]}, test_command=None)
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert "--auto-test" not in argv
        assert "--test-cmd" not in argv

    def test_passes_the_lint_command(self):
        cfg = cfg_with()
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert argv[argv.index("--lint-cmd") + 1] == "ruff check --fix"

    def test_passes_map_tokens(self):
        cfg = cfg_with()
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert argv[argv.index("--map-tokens") + 1] == "0"

    def test_scopes_editable_files(self):
        # Not optional on a large repo: an unscoped repo map consumes the
        # context window before the task is stated.
        cfg = cfg_with()
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        files = [argv[i + 1] for i, a in enumerate(argv) if a == "--file"]
        assert files == ["app/**", "src/*.py"]

    def test_passes_read_only_context_files(self):
        cfg = cfg_with(stage_overrides={"read_files": ["config/routes.rb"]})
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert argv[argv.index("--read") + 1] == "config/routes.rb"

    def test_extra_args_are_appended(self):
        # Escape hatch: aider's flags change between releases, and an
        # operator must be able to correct them without a code change.
        cfg = cfg_with(executor={"model": "m", "extra_args": ["--no-gitignore"]})
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert argv[-1] == "--no-gitignore"

    def test_never_puts_a_key_in_argv(self):
        cfg = cfg_with(executor={"model": "m", "api_key_env": "SOME_KEY"})
        argv = build_aider_argv(cfg.stages[0], cfg, "p")
        assert not any("key" in a.lower() for a in argv)


class TestRunAgentStage:
    def test_invokes_aider_and_reports_success(self, repo, fake_aider):
        cfg = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            cfg.stages[0], "PROMPT"
        )
        assert result.ok
        assert "aider ran" in result.log

    def test_prompt_reaches_aider(self, repo, fake_aider):
        cfg = cfg_with(target_repo=str(repo))
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            cfg.stages[0], "DISTINCTIVE PROMPT"
        )
        recorded = json.loads(fake_aider.read_text())
        assert "DISTINCTIVE PROMPT" in recorded["argv"]

    def test_api_key_is_passed_by_environment_not_argv(self, repo, fake_aider, monkeypatch):
        # A key in argv shows up in `ps` output and in our own run log.
        monkeypatch.setenv("LOCAL_KEY", "sk-secret-value")
        cfg = cfg_with(
            target_repo=str(repo),
            executor={"model": "m", "api_key_env": "LOCAL_KEY"},
        )
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            cfg.stages[0], "p"
        )
        recorded = json.loads(fake_aider.read_text())
        assert "sk-secret-value" not in " ".join(recorded["argv"])
        assert recorded["env"]["OPENAI_API_KEY"] == "sk-secret-value"

    def test_missing_api_key_env_var_is_an_error(self, repo, fake_aider):
        cfg = cfg_with(
            target_repo=str(repo),
            executor={"model": "m", "api_key_env": "DEFINITELY_NOT_SET_ANYWHERE"},
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            cfg.stages[0], "p"
        )
        assert not result.ok
        assert "DEFINITELY_NOT_SET_ANYWHERE" in result.log

    def test_nonzero_exit_is_a_failed_attempt(self, repo, tmp_path, monkeypatch):
        bindir = tmp_path / "bin2"
        bindir.mkdir()
        script = bindir / "aider"
        script.write_text("#!/bin/sh\necho broke >&2\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        cfg = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            cfg.stages[0], "p"
        )
        assert not result.ok
        assert "broke" in result.log

    def test_timeout_is_a_failed_attempt(self, repo, tmp_path, monkeypatch):
        bindir = tmp_path / "bin3"
        bindir.mkdir()
        script = bindir / "aider"
        script.write_text("#!/bin/sh\nsleep 30\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        cfg = cfg_with(target_repo=str(repo), limits={"aider_timeout_seconds": 1})
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            cfg.stages[0], "p"
        )
        assert not result.ok
        assert result.timed_out


class TestRunScriptStage:
    def test_runs_the_declared_command(self, repo):
        cfg = cfg_with(
            target_repo=str(repo),
            stage_overrides={
                "id": "annotate",
                "kind": "script",
                "command": "echo transforming",
                "instruction": None,
            },
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_script_stage(
            cfg.stages[0]
        )
        assert result.ok
        assert "transforming" in result.log

    def test_failing_command_fails_the_stage(self, repo):
        cfg = cfg_with(
            target_repo=str(repo),
            stage_overrides={
                "id": "annotate",
                "kind": "script",
                "command": "exit 4",
                "instruction": None,
            },
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_script_stage(
            cfg.stages[0]
        )
        assert not result.ok

    def test_spends_no_model_tokens(self, repo, fake_aider):
        # A scripted transform across hundreds of files must not invoke a model.
        cfg = cfg_with(
            target_repo=str(repo),
            stage_overrides={
                "id": "annotate",
                "kind": "script",
                "command": "true",
                "instruction": None,
            },
        )
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_script_stage(cfg.stages[0])
        assert not fake_aider.exists()


class TestContextCommands:
    def test_collects_stdout_for_the_prompt(self, repo):
        cfg = cfg_with(
            target_repo=str(repo),
            stage_overrides={"context_commands": ["echo index; echo show"]},
        )
        collected, results = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).gather_context(
            cfg.stages[0]
        )
        assert collected[0][0] == "echo index; echo show"
        assert "show" in collected[0][1]
        assert results[0].ok

    def test_a_failing_context_command_is_reported(self, repo):
        # The work list feeding the prompt would otherwise be silently empty,
        # and the executor would invent one.
        cfg = cfg_with(
            target_repo=str(repo),
            stage_overrides={"context_commands": ["echo partial; exit 1"]},
        )
        collected, results = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).gather_context(
            cfg.stages[0]
        )
        assert not results[0].ok

    def test_no_context_commands_yields_nothing(self, repo):
        cfg = cfg_with(target_repo=str(repo))
        collected, results = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).gather_context(
            cfg.stages[0]
        )
        assert collected == []
        assert results == []
