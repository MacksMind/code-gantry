"""The executor: Aider for agent stages, a declared command for script stages.

Aider's flag surface changes between releases and the spec says to verify it
rather than trust a list. That check lives in preflight; here we pin the argv
we intend to build, and use a fake `aider` on PATH so the wiring is exercised
without the real tool.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.commands import CommandRunner
from orchestrator.config import Stage, parse_config
from orchestrator.executor import PLACEHOLDER_API_KEY, Executor, build_aider_argv


BASE_STAGE = {"id": "s1", "instruction": "do it", "edit_files": ["app/**", "src/*.py"]}


def cfg_with(stage_overrides=None, **cfg_overrides):
    stage = dict(BASE_STAGE)
    stage.update(stage_overrides or {})
    data = {
        "target_repo": "/tmp/x",
        "project_branch": "work",
        "plan_root": "PLAN.md",
        "planner": {"model": "claude-opus-5"},
        "test_command": "pytest -q",
        "executor": {
            "model": "openai/local-model",
            "api_base": "http://spark:8080/v1",
            "lint_command": "ruff check --fix",
            "map_tokens": 0,
        },
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(cfg_overrides)
    return parse_config(data), Stage(**stage)


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
        cfg, stage = cfg_with()
        argv = build_aider_argv(stage, cfg, "PROMPT TEXT")
        assert "--message" in argv
        assert argv[argv.index("--message") + 1] == "PROMPT TEXT"

    def test_runs_unattended(self):
        # A subprocess that stops to ask a question would hang the run.
        cfg, stage = cfg_with()
        argv = build_aider_argv(stage, cfg, "p")
        assert "--yes-always" in argv
        assert "--no-stream" in argv

    def test_passes_the_model_and_api_base(self):
        cfg, stage = cfg_with()
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--model") + 1] == "openai/local-model"
        assert argv[argv.index("--openai-api-base") + 1] == "http://spark:8080/v1"

    def test_aider_does_not_run_the_tests_by_default(self):
        """`--auto-test` is off unless the operator asks for it.

        Measured on the first real stage: a correct two-line edit took ~90
        seconds, and the attempt took 609. The rest was Aider running the
        project's full suite, ingesting 7,167 lines of output, and — because
        `--yes-always` answers "yes" to "Attempt to fix test errors?" — trying
        to repair two order-dependent specs it had no business touching, on a
        stage whose declared scope was two controllers.

        The orchestrator runs the tests itself, at the layer that knows about
        scope and flakes. Doing it twice buys nothing and hands the executor a
        mandate to edit outside its box.
        """
        cfg, stage = cfg_with(stage_overrides={"test_command": "pytest tests/unit"})
        argv = build_aider_argv(stage, cfg, "p")
        assert "--auto-test" not in argv
        assert "--test-cmd" not in argv

    def test_auto_test_can_be_switched_on(self):
        cfg, stage = cfg_with(
            stage_overrides={"test_command": "pytest tests/unit"},
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "pytest tests/unit"
        assert "--auto-test" in argv

    def test_falls_back_to_the_global_test_command(self):
        cfg, stage = cfg_with(executor={"model": "m", "auto_test": True})
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "pytest -q"

    def test_omits_auto_test_when_there_is_no_test_command(self):
        # Greenfield: nothing exists to run yet. Passing --auto-test with no
        # command would fail every edit round.
        cfg, stage = cfg_with(
            stage_overrides={"checks": ["true"]},
            test_command=None,
            stage_defaults={"checks": ["true"]},
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--auto-test" not in argv
        assert "--test-cmd" not in argv

    def test_passes_the_lint_command(self):
        cfg, stage = cfg_with()
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--lint-cmd") + 1] == "ruff check --fix"

    def test_passes_map_tokens(self):
        cfg, stage = cfg_with()
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--map-tokens") + 1] == "0"

    def test_scopes_editable_files(self):
        # Not optional on a large repo: an unscoped repo map consumes the
        # context window before the task is stated.
        cfg, stage = cfg_with()
        argv = build_aider_argv(stage, cfg, "p")
        files = [argv[i + 1] for i, a in enumerate(argv) if a == "--file"]
        assert files == ["app/**", "src/*.py"]

    def test_passes_read_only_context_files(self):
        cfg, stage = cfg_with(stage_overrides={"read_files": ["config/routes.rb"]})
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--read") + 1] == "config/routes.rb"

    def test_extra_args_are_appended(self):
        # Escape hatch: aider's flags change between releases, and an
        # operator must be able to correct them without a code change.
        cfg, stage = cfg_with(executor={"model": "m", "extra_args": ["--no-gitignore"]})
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[-1] == "--no-gitignore"

    def test_aider_may_not_touch_gitignore(self):
        # Aider adds `.aider*` to .gitignore by default. That is a file no stage
        # declares, so it fails the scope gate on the first attempt of every
        # project — as it did on the first real run.
        cfg, stage = cfg_with(executor={"model": "m"})
        assert "--no-gitignore" in build_aider_argv(stage, cfg, "p")

    def test_model_warnings_are_suppressed(self):
        # Paired with --yes-always, a warning becomes "Open documentation url
        # for more info?" answered yes — which opens a browser tab mid-run.
        cfg, stage = cfg_with(executor={"model": "m"})
        assert "--no-show-model-warnings" in build_aider_argv(stage, cfg, "p")

    def test_edit_format_is_passed_when_configured(self):
        # Local models frequently cannot produce Aider's default diff format.
        cfg, stage = cfg_with(executor={"model": "m", "edit_format": "whole"})
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--edit-format") + 1] == "whole"

    def test_no_edit_format_flag_when_unset(self):
        # Aider's per-model default is better than a guess of ours.
        cfg, stage = cfg_with(executor={"model": "m"})
        assert "--edit-format" not in build_aider_argv(stage, cfg, "p")

    def test_history_files_are_written_outside_the_repo(self, tmp_path):
        # Aider writes .aider.chat.history.md and .aider.input.history into the
        # repo root. No stage declares them, so they fail the scope gate on the
        # first attempt of every project — which is exactly what happened, and
        # cost a planner intervention to work around.
        cfg, stage = cfg_with(executor={"model": "m"})
        argv = build_aider_argv(stage, cfg, "p", history_dir=tmp_path)
        for flag in ("--chat-history-file", "--input-history-file", "--llm-history-file"):
            assert flag in argv, flag
            assert argv[argv.index(flag) + 1].startswith(str(tmp_path))

    def test_paths_handed_to_aider_are_absolute(self, monkeypatch, tmp_path):
        # Aider runs with its cwd set to the target repo, so a relative path
        # resolves *inside the repository under test* — which is how the
        # history files ended up back in the repo they were moved out of.
        monkeypatch.chdir(tmp_path)
        cfg, stage = cfg_with(
            executor={"model": "m", "model_metadata_file": "meta/models.json"}
        )
        argv = build_aider_argv(
            stage, cfg, "p", history_dir=Path("projects/p/runs/r/stages/000")
        )
        for flag in (
            "--chat-history-file",
            "--input-history-file",
            "--llm-history-file",
            "--model-metadata-file",
        ):
            value = argv[argv.index(flag) + 1]
            assert Path(value).is_absolute(), f"{flag} got a relative path: {value}"

    def test_no_history_flags_without_a_destination(self):
        cfg, stage = cfg_with(executor={"model": "m"})
        assert "--chat-history-file" not in build_aider_argv(stage, cfg, "p")

    def test_never_puts_a_key_in_argv(self):
        cfg, stage = cfg_with(executor={"model": "m", "api_key_env": "SOME_KEY"})
        argv = build_aider_argv(stage, cfg, "p")
        assert not any("key" in a.lower() for a in argv)


class TestRunAgentStage:
    def test_invokes_aider_and_reports_success(self, repo, fake_aider):
        cfg, stage = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "PROMPT"
        )
        assert result.ok
        assert "aider ran" in result.log

    def test_prompt_reaches_aider(self, repo, fake_aider):
        cfg, stage = cfg_with(target_repo=str(repo))
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "DISTINCTIVE PROMPT"
        )
        recorded = json.loads(fake_aider.read_text())
        assert "DISTINCTIVE PROMPT" in recorded["argv"]

    def test_api_key_is_passed_by_environment_not_argv(self, repo, fake_aider, monkeypatch):
        # A key in argv shows up in `ps` output and in our own run log.
        monkeypatch.setenv("LOCAL_KEY", "sk-secret-value")
        cfg, stage = cfg_with(
            target_repo=str(repo),
            executor={"model": "m", "api_key_env": "LOCAL_KEY"},
        )
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        recorded = json.loads(fake_aider.read_text())
        assert "sk-secret-value" not in " ".join(recorded["argv"])
        assert recorded["env"]["OPENAI_API_KEY"] == "sk-secret-value"

    def test_no_browser_can_be_opened(self, repo, fake_aider):
        # Belt and braces with --no-show-model-warnings: an edit-format error
        # prints a docs URL too, and --yes-always answers yes to opening it.
        # An unattended overnight run must not accumulate browser tabs.
        cfg, stage = cfg_with(target_repo=str(repo))
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(stage, "p")
        recorded = json.loads(fake_aider.read_text())
        assert "%s" in recorded["env"]["BROWSER"], "must be a no-op command, not a name"

    def test_a_local_endpoint_needs_no_configured_key(self, repo, fake_aider):
        # llama-swap and llama.cpp serve without auth, so there is nothing for
        # an operator to put in a key env var. Aider's client still refuses to
        # make the call with no key set at all, so the placeholder is supplied
        # here rather than being one more thing to export.
        cfg, stage = cfg_with(
            target_repo=str(repo),
            executor={"model": "openai/local", "api_base": "http://spark:8080/v1"},
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert result.ok
        recorded = json.loads(fake_aider.read_text())
        assert recorded["env"]["OPENAI_API_BASE"] == "http://spark:8080/v1"
        assert recorded["env"]["OPENAI_API_KEY"] == PLACEHOLDER_API_KEY

    def test_api_base_env_reaches_aider(self, repo, fake_aider, monkeypatch):
        monkeypatch.setenv("SPARK_API_BASE", "http://spark.internal:8080/v1")
        cfg, stage = cfg_with(
            target_repo=str(repo),
            executor={"model": "openai/local", "api_base_env": "SPARK_API_BASE"},
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert result.ok
        recorded = json.loads(fake_aider.read_text())
        assert recorded["env"]["OPENAI_API_BASE"] == "http://spark.internal:8080/v1"
        argv = recorded["argv"]
        assert argv[argv.index("--openai-api-base") + 1] == "http://spark.internal:8080/v1"
        # Resolving an api_base is still not a reason to demand a key.
        assert recorded["env"]["OPENAI_API_KEY"] == PLACEHOLDER_API_KEY

    def test_an_unset_api_base_env_is_a_legible_failure(self, repo, fake_aider, monkeypatch):
        monkeypatch.delenv("SPARK_API_BASE", raising=False)
        cfg, stage = cfg_with(
            target_repo=str(repo),
            executor={"model": "openai/local", "api_base_env": "SPARK_API_BASE"},
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert not result.ok
        assert "SPARK_API_BASE" in result.log

    def test_a_configured_key_beats_the_placeholder(self, repo, fake_aider, monkeypatch):
        # An endpoint that does want auth must still get the real key.
        monkeypatch.setenv("GATEWAY_KEY", "sk-real")
        cfg, stage = cfg_with(
            target_repo=str(repo),
            executor={
                "model": "openai/m",
                "api_base": "https://gateway.example/v1",
                "api_key_env": "GATEWAY_KEY",
            },
        )
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(stage, "p")
        recorded = json.loads(fake_aider.read_text())
        assert recorded["env"]["OPENAI_API_KEY"] == "sk-real"

    def test_no_placeholder_without_an_api_base(self, repo, fake_aider):
        # No api_base means the real OpenAI endpoint, which genuinely needs a
        # key. Injecting a placeholder there would turn a legible "you set no
        # key" into a puzzling 401 from a paid service.
        cfg, stage = cfg_with(target_repo=str(repo), executor={"model": "gpt-4o"})
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(stage, "p")
        recorded = json.loads(fake_aider.read_text())
        assert "OPENAI_API_KEY" not in recorded["env"]

    def test_missing_api_key_env_var_is_an_error(self, repo, fake_aider):
        cfg, stage = cfg_with(
            target_repo=str(repo),
            executor={"model": "m", "api_key_env": "DEFINITELY_NOT_SET_ANYWHERE"},
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert not result.ok
        assert "DEFINITELY_NOT_SET_ANYWHERE" in result.log

    def test_an_unapplied_edit_is_a_failed_attempt(self, repo, tmp_path, monkeypatch):
        # Aider exits 0 even when the model's reply was unparseable and no edit
        # was applied. Left alone, that surfaces two gates later as "the attempt
        # produced no changes" — which is false, and useless as feedback: the
        # model produced plenty, in the wrong shape.
        bindir = tmp_path / "bin3"
        bindir.mkdir()
        script = bindir / "aider"
        script.write_text(
            "#!/bin/sh\n"
            "echo 'The LLM did not conform to the edit format.'\n"
            "echo 'Only 3 reflections allowed, stopping.'\n"
            "exit 0\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        cfg, stage = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert not result.ok
        assert "edit format" in result.log

    def test_a_clean_aider_run_stays_successful(self, repo, fake_aider):
        cfg, stage = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert result.ok

    def test_nonzero_exit_is_a_failed_attempt(self, repo, tmp_path, monkeypatch):
        bindir = tmp_path / "bin2"
        bindir.mkdir()
        script = bindir / "aider"
        script.write_text("#!/bin/sh\necho broke >&2\nexit 1\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        cfg, stage = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
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
        cfg, stage = cfg_with(target_repo=str(repo), limits={"aider_timeout_seconds": 1})
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert not result.ok
        assert result.timed_out


class TestRunScriptStage:
    def test_runs_the_declared_command(self, repo):
        cfg, stage = cfg_with(
            target_repo=str(repo),
            stage_overrides={
                "id": "annotate",
                "kind": "script",
                "command": "echo transforming",
                "instruction": None,
            },
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_script_stage(
            stage
        )
        assert result.ok
        assert "transforming" in result.log

    def test_failing_command_fails_the_stage(self, repo):
        cfg, stage = cfg_with(
            target_repo=str(repo),
            stage_overrides={
                "id": "annotate",
                "kind": "script",
                "command": "exit 4",
                "instruction": None,
            },
        )
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_script_stage(
            stage
        )
        assert not result.ok

    def test_spends_no_model_tokens(self, repo, fake_aider):
        # A scripted transform across hundreds of files must not invoke a model.
        cfg, stage = cfg_with(
            target_repo=str(repo),
            stage_overrides={
                "id": "annotate",
                "kind": "script",
                "command": "true",
                "instruction": None,
            },
        )
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_script_stage(stage)
        assert not fake_aider.exists()


class TestContextCommands:
    def test_collects_stdout_for_the_prompt(self, repo):
        cfg, stage = cfg_with(
            target_repo=str(repo),
            stage_overrides={"context_commands": ["echo index; echo show"]},
        )
        collected, results = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).gather_context(
            stage
        )
        assert collected[0][0] == "echo index; echo show"
        assert "show" in collected[0][1]
        assert results[0].ok

    def test_a_failing_context_command_is_reported(self, repo):
        # The work list feeding the prompt would otherwise be silently empty,
        # and the executor would invent one.
        cfg, stage = cfg_with(
            target_repo=str(repo),
            stage_overrides={"context_commands": ["echo partial; exit 1"]},
        )
        collected, results = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).gather_context(
            stage
        )
        assert not results[0].ok

    def test_no_context_commands_yields_nothing(self, repo):
        cfg, stage = cfg_with(target_repo=str(repo))
        collected, results = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).gather_context(
            stage
        )
        assert collected == []
        assert results == []


class TestAiderCommitsAreNotSigned:
    """Aider makes its own commits, in a subprocess we do not drive.

    The orchestrator's own commits already pass `-c commit.gpgsign=false`, but
    that does nothing for Aider's, which inherit the operator's global config.
    With signing on — as it is on the first real target — every executor
    attempt would try to reach a GPG agent. If the passphrase is cached it
    works; over a fourteen-hour run it will not stay cached, and then each
    attempt either fails or waits on a pinentry dialog nobody is there to
    answer.

    Injected through the environment rather than by editing the operator's
    config or the target repo's: nothing to remember to restore, nothing left
    behind if the run dies, and no change to how that repo behaves for anyone
    else.
    """

    def _git_env(self, recorded):
        env = recorded["env"]
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        return {
            env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
            for i in range(count)
        }

    def test_signing_is_disabled_for_the_executor(self, repo, fake_aider):
        cfg, stage = cfg_with(target_repo=str(repo))
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(stage, "p")
        assert self._git_env(json.loads(fake_aider.read_text()))["commit.gpgsign"] == "false"

    def test_tag_signing_too(self, repo, fake_aider):
        cfg, stage = cfg_with(target_repo=str(repo))
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(stage, "p")
        assert self._git_env(json.loads(fake_aider.read_text()))["tag.gpgsign"] == "false"

    def test_the_operators_own_config_is_untouched(self, repo, fake_aider):
        # The mechanism is environment-only. Nothing writes to a config file.
        import subprocess

        before = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--list"],
            capture_output=True, text=True,
        ).stdout
        cfg, stage = cfg_with(target_repo=str(repo))
        Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(stage, "p")
        after = subprocess.run(
            ["git", "-C", str(repo), "config", "--local", "--list"],
            capture_output=True, text=True,
        ).stdout
        assert before == after


class TestPartiallyAppliedEdits:
    """Some blocks failing is not the same as nothing applying.

    Aider prints "The LLM did not conform to the edit format" when *any*
    SEARCH/REPLACE block fails to match, even when others applied and were
    committed. Treating that as a failed attempt sent the run round
    execute -> execute -> execute without ever reaching verify, while each pass
    landed more edits.

    Observed live on an 18-site sweep: attempt 0 applied 11 sites across three
    files, committed them, and was recorded as having produced nothing.

    Applied edits mean the attempt did something, so it goes to verify — where
    the scope guard, the tests and the reviewer are equipped to judge whether
    it did *enough*. That judgement was never the executor wrapper's to make.
    """

    def result(self, log):
        from orchestrator.commands import CommandResult

        return CommandResult(
            command="aider", exit_code=0, stdout=log, stderr="", duration_seconds=1.0
        )

    def test_applied_edits_alongside_failures_are_not_a_failed_attempt(self):
        from orchestrator.executor import _classify_execution

        log = (
            "Applied edit to app/controllers/a.rb\n"
            "Commit bcf7276 refactor: ...\n"
            "The LLM did not conform to the edit format.\n"
            "# 7 SEARCH/REPLACE blocks failed to match!\n"
        )
        outcome = _classify_execution(self.result(log))
        assert outcome.ok
        assert not outcome.unapplied_edit

    def test_nothing_applied_is_still_a_failed_attempt(self):
        from orchestrator.executor import _classify_execution

        log = (
            "The LLM did not conform to the edit format.\n"
            "# 2 SEARCH/REPLACE blocks failed to match!\n"
        )
        outcome = _classify_execution(self.result(log))
        assert not outcome.ok
        assert outcome.unapplied_edit

    def test_a_clean_run_is_unremarkable(self):
        from orchestrator.executor import _classify_execution

        outcome = _classify_execution(self.result("Applied edit to a.rb\n"))
        assert outcome.ok
        assert not outcome.unapplied_edit

    def test_exhausted_reflections_with_no_edits_still_fails(self):
        from orchestrator.executor import _classify_execution

        log = "Only 3 reflections allowed, stopping.\n"
        outcome = _classify_execution(self.result(log))
        assert not outcome.ok
