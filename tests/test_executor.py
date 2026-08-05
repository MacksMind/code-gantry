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
from orchestrator.executor import (
    AIDER_FLAGS,
    PLACEHOLDER_API_KEY,
    Executor,
    ExcerptError,
    attached_by_mention,
    build_aider_argv,
    resolve_excerpts,
    shield_path_mentions,
)
from orchestrator.gitops import Git


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
            stage_overrides={"test_paths": ["tests/unit/test_a.py"]},
            scoped_test_command="pytest {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "pytest tests/unit/test_a.py"
        assert "--auto-test" in argv

    def test_it_never_falls_back_to_the_global_test_command(self):
        # It used to, and that is how a 90-second edit became a 609-second
        # attempt: --test-cmd got `bin/parallel_rspec`, the whole suite, inside
        # a loop that could run it three times.
        cfg, stage = cfg_with(
            scoped_test_command="pytest {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--test-cmd" not in argv

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

class TestReadContextBudget:
    """Reference files are useful until they are the majority of the prompt.

    The planner passes previously-converted files as worked examples, which is
    sound and grows without bound: by the twelfth stage of one run it was
    sending 4,636 lines of context to change six lines, 69,000 tokens a call.
    Two costs, both measured on that run. Latency — attempts took 561s and 584s
    against Aider's un-overridable 600s request timeout, so whether a stage
    landed or appeared to hang turned on the generation rate that minute. And
    accuracy — the same stage converted four of six sites, then three of six,
    losing the task inside the reference material.

    The controlled comparison is stage 10. Revision 0 carried 2,818 lines of
    `--read` and stalled six times across three hours; the planner's redraw
    passed one file, 39k tokens, and it landed in 120 seconds.
    """

    def _repo(self, tmp_path, sizes):
        for name, lines in sizes.items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x\n" * lines)
        return tmp_path

    def test_unset_budget_keeps_every_read_file(self, tmp_path):
        # The default must not change behaviour for projects that are fine.
        self._repo(tmp_path, {"a.rb": 100, "b.rb": 5000})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"read_files": ["a.rb", "b.rb"]},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert [argv[i + 1] for i, a in enumerate(argv) if a == "--read"] == ["a.rb", "b.rb"]

    def test_the_largest_reference_goes_first(self, tmp_path):
        # Dropping the biggest recovers the most context per file dropped, and
        # the small ones are likelier to be the base class or the routes file
        # that the stage genuinely needs.
        self._repo(tmp_path, {"small.rb": 50, "mid.rb": 300, "huge.rb": 2000})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"read_files": ["small.rb", "mid.rb", "huge.rb"]},
            executor={"model": "m", "max_read_lines": 400},
        )
        argv = build_aider_argv(stage, cfg, "p")
        kept = [argv[i + 1] for i, a in enumerate(argv) if a == "--read"]
        assert kept == ["small.rb", "mid.rb"]

    def test_an_excerpt_reaches_a_file_the_budget_would_drop(self, tmp_path):
        # The point of the field. `read_files` is whole files, so one past the
        # budget contributes nothing at all; a range of it contributes the part
        # that mattered, which is what the planner had already read.
        self._repo(tmp_path, {"huge.rb": 2000})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "read_files": ["huge.rb"],
                "read_excerpts": [
                    {"path": "huge.rb", "start": 40, "end": 44, "note": "why"}
                ],
            },
            executor={"model": "m", "max_read_lines": 400},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--read" not in argv  # the whole file is still too big
        got = resolve_excerpts(stage, cfg)
        assert len(got) == 1
        label, text = got[0]
        assert label == "huge.rb:40-44 — why"
        assert text.splitlines()[0].startswith("   40  ")
        assert len(text.splitlines()) == 5

    def test_reference_files_and_excerpts_share_one_budget(self, tmp_path):
        # Not one budget each. Excerpts exist so a file too large to send whole
        # can still contribute the part that matters — not so a stage can carry
        # twice what the operator allowed by splitting it across two fields.
        self._repo(tmp_path, {"ref.rb": 90, "other.rb": 500})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "read_files": ["ref.rb"],
                "read_excerpts": [{"path": "other.rb", "start": 1, "end": 500}],
            },
            executor={"model": "m", "max_read_lines": 100},
        )
        # ref.rb takes 90 of the 100, so the excerpt gets the remaining 10.
        assert len(resolve_excerpts(stage, cfg)[0][1].splitlines()) == 10

    def test_excerpts_are_charged_against_the_same_budget(self, tmp_path):
        self._repo(tmp_path, {"huge.rb": 2000})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "read_excerpts": [{"path": "huge.rb", "start": 1, "end": 900}]
            },
            executor={"model": "m", "max_read_lines": 10},
        )
        # Clipped, not dropped: a clipped range still carries its beginning.
        assert len(resolve_excerpts(stage, cfg)[0][1].splitlines()) == 10

    def test_an_unreadable_excerpt_fails_loudly(self, tmp_path):
        self._repo(tmp_path, {"a.rb": 10})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "read_excerpts": [
                    {"path": "gone.rb", "start": 1, "end": 5},
                    {"path": "a.rb", "start": 1, "end": 2},
                ]
            },
            executor={"model": "m", "max_read_lines": 400},
        )
        # It used to be skipped, on the reasoning that an excerpt is help and
        # help that fails should not fail the stage. That held while the
        # instruction also carried the literal. It does not now: the planner
        # writes no code, so the excerpt *is* the code, and skipping one hands
        # the executor an instruction referring to lines it was never shown.
        with pytest.raises(ExcerptError) as excinfo:
            resolve_excerpts(stage, cfg)
        assert "gone.rb" in str(excinfo.value)


    def test_a_budget_smaller_than_everything_drops_everything(self, tmp_path):
        self._repo(tmp_path, {"a.rb": 900, "b.rb": 900})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"read_files": ["a.rb", "b.rb"]},
            executor={"model": "m", "max_read_lines": 10},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--read" not in argv

    def test_the_edited_file_is_never_budgeted_away(self, tmp_path):
        # `edit_files` is the task. Only reference material is discretionary.
        self._repo(tmp_path, {"target.rb": 5000, "ref.rb": 5000})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"edit_files": ["target.rb"], "read_files": ["ref.rb"]},
            executor={"model": "m", "max_read_lines": 10},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--file") + 1] == "target.rb"
        assert "--read" not in argv

    def test_an_unreadable_reference_is_kept(self, tmp_path):
        # Same rule as the auto-test paths: act on evidence, not on its absence.
        # A glob or a not-yet-created file has no line count, and guessing zero
        # would let it through while guessing huge would drop it silently.
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"read_files": ["app/**/*.rb"]},
            executor={"model": "m", "max_read_lines": 10},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--read") + 1] == "app/**/*.rb"


class TestExtraArgs:
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

    def test_urls_are_never_fetched(self):
        # Aider offers to add URLs it sees to the chat, and --yes-always
        # accepts. The URL it sees is usually its own: on a repo this size the
        # startup banner prints a large-mono-repo warning citing
        # aider.chat/docs/faq.html, so it offers to scrape its own
        # documentation. Two of the nine attempt timeouts across all runs so
        # far end on the line "Scraping https://aider.chat/docs/faq.html…" with
        # nothing after it — a network fetch, in an unattended run, with a
        # fifteen-minute timeout as its only bound.
        #
        # --no-show-model-warnings already covers the same hazard for model
        # warnings. This closes the general case rather than the next specific
        # one.
        cfg, stage = cfg_with(executor={"model": "m"})
        assert "--no-detect-urls" in build_aider_argv(stage, cfg, "p")

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


class TestScopedAutoTest:
    """Aider runs the tests again — but the stage's, never the whole suite.

    Turning --auto-test off was the right emergency fix and the wrong permanent
    design. What was wrong was pointing it at `bin/parallel_rspec`: 3.5 minutes
    per pass, inside a loop that could run three times, which is how a
    90-second edit became a 609-second attempt.

    The inner loop is the cheapest feedback available — the model already has
    the files in context, so a failure it just caused costs one exchange to fix
    rather than a fresh attempt re-sending 18k of context. What it must never
    have is the full suite, because Aider knows nothing about the stage's scope
    and will happily edit `spec/features` to make a red spec green.

    So the auto-test command is built from the stage's declared test_paths and
    from nothing else. No declared paths, no --auto-test.
    """

    def test_the_command_is_scoped_to_declared_paths(self, tmp_path):
        cfg, stage = cfg_with(
            stage_overrides={"test_paths": ["spec/a_spec.rb"]},
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/a_spec.rb"
        assert "--auto-test" in argv

    def test_the_full_suite_is_never_used(self):
        # The whole point. A stage with nothing declared gets no inner loop
        # rather than a 3.5-minute one.
        cfg, stage = cfg_with(
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--auto-test" not in argv
        assert "--test-cmd" not in argv

    def test_a_spec_the_stage_will_create_is_still_named(self, tmp_path):
        """Test-first work: the planner says "write this spec", so run it.

        The file does not exist when the command is built, but Aider runs
        --test-cmd *after* applying edits, so it will by then. And if the stage
        was supposed to create it and did not, rspec says so immediately —
        which is the fastest possible signal for exactly that mistake.
        """
        cfg, stage = cfg_with(
            stage_overrides={"test_paths": ["spec/models/brand_new_spec.rb"]},
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "spec/models/brand_new_spec.rb" in argv[argv.index("--test-cmd") + 1]

    def test_a_glob_matching_nothing_is_dropped(self):
        # A glob asks about files that exist; it cannot name one that does not
        # yet. Left in, it reaches rspec as a literal and kills the inner loop.
        cfg, stage = cfg_with(
            stage_overrides={"test_paths": ["spec/**/*nothing*"]},
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--auto-test" not in argv

    def test_auto_test_off_means_no_test_command(self):
        cfg, stage = cfg_with(
            stage_overrides={"test_paths": ["spec/a_spec.rb"]},
            scoped_test_command="rspec {paths}",
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--test-cmd" not in argv


class TestTheInnerLoopFallsBackToTheTestsTheStageMayEdit:
    """`test_paths` empty does not mean the stage has no tests.

    Measured over one run of 35 stages: 12 declared no `test_paths`, so a third
    of the run had no inner loop at all and every failure cost a full round
    trip — a fresh Aider process re-reading the files to fix what it had just
    broken. Those 12 averaged 1.42 attempts against 0.91 for the rest.

    In all 12 the tests were sitting in `edit_files`, because a coverage stage
    edits the spec it is proving. So the information was already present and
    the planner was being asked to restate it, which is the declaration the
    repository already knows — the same argument as not having a stage name the
    plan item it advances.

    Bounded deliberately: only plain paths, never globs. A glob in `edit_files`
    can be `spec/**`, and expanding it would hand Aider something close to the
    full suite — the one thing this command must never be, since Aider knows
    nothing of `edit_files` and will edit whatever is red.
    """

    def test_a_test_in_edit_files_is_used_when_nothing_is_declared(self):
        cfg, stage = cfg_with(
            stage_overrides={"edit_files": ["spec/models/order_spec.rb"]},
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/models/order_spec.rb"
        assert "--auto-test" in argv

    def test_a_declared_path_still_wins(self):
        # The planner's choice is not second-guessed when it made one.
        cfg, stage = cfg_with(
            stage_overrides={
                "test_paths": ["spec/a_spec.rb"],
                "edit_files": ["spec/models/order_spec.rb"],
            },
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/a_spec.rb"

    def test_a_glob_is_not_expanded_into_the_suite(self):
        # `spec/**` is a legal edit_files entry and would be most of the suite.
        cfg, stage = cfg_with(
            stage_overrides={"edit_files": ["spec/**"]},
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--auto-test" not in argv
        assert "--test-cmd" not in argv

    def test_a_stage_editing_no_tests_still_gets_no_inner_loop(self):
        cfg, stage = cfg_with(
            stage_overrides={"edit_files": ["app/models/order.rb"]},
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--auto-test" not in argv

    def test_what_counts_as_a_test_comes_from_config(self):
        # Project knowledge belongs in config. This reads the same
        # `test_file_patterns` the new_tests gate does, so a project whose
        # tests are not Ruby specs is served without touching this code.
        cfg, stage = cfg_with(
            stage_overrides={"edit_files": ["pkg/order_test.go", "pkg/order.go"]},
            test_file_patterns=["**/*_test.go"],
            scoped_test_command="go test {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "go test pkg/order_test.go"

    def test_every_test_in_edit_files_is_named(self):
        cfg, stage = cfg_with(
            stage_overrides={
                "edit_files": ["spec/a_spec.rb", "app/x.rb", "spec/b_spec.rb"]
            },
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/a_spec.rb spec/b_spec.rb"


class TestAutoTestHasItsOwnCommand:
    """The inner loop wants quiet; verify wants verbose. Same run, opposite needs.

    Verify parses the runner's output to find which files failed — that is how
    the flake gate works at all — so it needs the full `Failed examples:` block.
    Aider's inner loop needs the opposite: its test output lands in the model's
    context, and a directory-scoped run put 138,000 to 152,000 tokens into a
    single request, at roughly 165 seconds of prefill each before a token was
    generated.

    So `auto_test_command` is separate, and falls back to the scoped command
    when unset — quiet is an optimisation, not a requirement.
    """

    def test_the_auto_test_command_is_used_when_set(self):
        cfg, stage = cfg_with(
            stage_overrides={"test_paths": ["spec/a_spec.rb"]},
            scoped_test_command="rspec {paths}",
            auto_test_command="rspec --fail-fast -f progress {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == (
            "rspec --fail-fast -f progress spec/a_spec.rb"
        )

    def test_it_falls_back_to_the_scoped_command(self):
        cfg, stage = cfg_with(
            stage_overrides={"test_paths": ["spec/a_spec.rb"]},
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/a_spec.rb"

    def test_a_spec_the_stage_cannot_create_is_dropped(self, tmp_path):
        # A plain path that does not exist is kept only when the stage could
        # plausibly create it. Otherwise the inner loop runs a command that is
        # guaranteed to fail, Aider reads the runner's "no such file" as a test
        # failure, and it burns reflections repairing a file that will never
        # exist. Observed live: the planner declared `spec/requests/godata_spec.rb`
        # and `spec/controllers/godata_controller_spec.rb` for a repo with no
        # godata specs at all, and the attempt hung on a 77k-token fix.
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "test_paths": ["spec/imaginary_spec.rb"],
                "edit_files": ["app/controllers/godata_controller.rb"],
                "require_new_tests": False,
            },
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--test-cmd" not in argv, (
            "no runnable spec means no inner loop, not a loop that cannot pass"
        )

    def test_a_spec_the_stage_will_write_is_kept(self, tmp_path):
        # The original reasoning still holds where it applies: Aider runs this
        # after its edits, so a spec the stage was told to create will be there
        # by the time the command runs.
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "test_paths": ["spec/new_spec.rb"],
                "edit_files": ["spec/new_spec.rb", "app/thing.rb"],
                "require_new_tests": False,
            },
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/new_spec.rb"

    def test_require_new_tests_also_keeps_a_missing_spec(self, tmp_path):
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "test_paths": ["spec/new_spec.rb"],
                "edit_files": ["app/thing.rb"],
                "require_new_tests": True,
            },
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/new_spec.rb"

    def test_an_existing_spec_is_always_kept(self, tmp_path):
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "real_spec.rb").write_text("x\n")
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={
                "test_paths": ["spec/real_spec.rb"],
                "edit_files": ["app/thing.rb"],
                "require_new_tests": False,
            },
            scoped_test_command="rspec {paths}",
            executor={"model": "m", "auto_test": True},
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--test-cmd") + 1] == "rspec spec/real_spec.rb"

    def test_verify_is_unaffected_by_it(self, repo):
        # The parsing side must keep the verbose command whatever the inner
        # loop uses, or the flake gate stops finding failing files.
        from orchestrator.config import Stage, parse_config
        from orchestrator.gitops import Git
        from orchestrator.verify import resolve_test_command

        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "a_spec.rb").write_text("x\n")
        g = Git(repo)
        g.commit_all("spec")
        sha = g.head_sha()
        (repo / "app.py").write_text("changed\n")

        cfg = parse_config(
            {
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "test_command": "rspec-all",
                "scoped_test_command": "rspec {paths}",
                "auto_test_command": "rspec --fail-fast {paths}",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        stage = Stage(
            id="s", instruction="i", edit_files=["app.py"],
            test_paths=["spec/a_spec.rb"],
        )
        assert resolve_test_command(stage, cfg, g, sha) == "rspec spec/a_spec.rb"


class TestTheExecutorsContextCostIsMeasured:
    """How much the executor actually had to hold, per attempt.

    Stage sizing is guesswork without it. Two stages that both edit "one file"
    differed by 3.4x in what the executor loaded — 14k tokens for a small leaf
    controller, 47k for a 1,935-line one — and nothing recorded the difference,
    so a planner batching "up to ten files" was sizing by a number that does
    not describe the constraint.

    Aider prints the figure on every attempt and it was being discarded.
    """

    def test_tokens_sent_are_parsed_from_the_log(self):
        from orchestrator.executor import context_tokens_from_log

        assert context_tokens_from_log("> Tokens: 14k sent, 268 received.") == 14_000

    def test_a_plain_count_is_read_exactly(self):
        from orchestrator.executor import context_tokens_from_log

        assert context_tokens_from_log("> Tokens: 8,192 sent, 41 received.") == 8_192

    def test_the_last_report_wins(self):
        # Aider prints one per exchange; a reflection produces several, and the
        # largest context the attempt reached is the one that matters.
        from orchestrator.executor import context_tokens_from_log

        log = "> Tokens: 9k sent, 1 received.\n...\n> Tokens: 31k sent, 2 received.\n"
        assert context_tokens_from_log(log) == 31_000

    def test_a_log_without_the_line_reports_nothing(self):
        from orchestrator.executor import context_tokens_from_log

        assert context_tokens_from_log("no usage here") == 0


class TestExecutorCostIsCaptured:
    """What the executor costs, for the first time.

    Nothing priced it because nothing needed to: a local model on a Spark is
    free, and the economics the architecture rests on — planner at 91% of
    tokens, executor at 2.2% of prompt volume — assumed that. The moment the
    executor is a hosted model those figures are wrong and there is no line in
    `stage-costs.md` that would say so.

    Aider already reports it, and only when it can: `base_coder` returns early
    with the tokens report alone unless `input_cost_per_token` is known for the
    model. That is why no log in this project has ever carried a `Cost:` line,
    and why one will appear the day the model changes without anything else
    being touched.

    The session figure, not the message figure. Both are printed and both are
    cumulative within an invocation — `total_cost` and `message_cost` are each
    `+=` — so the largest session value is what the attempt actually spent.
    """

    def test_the_session_total_is_taken(self):
        from orchestrator.executor import cost_from_log

        log = "Tokens: 12k sent, 1.1k received.\nCost: $0.03 message, $0.11 session."
        assert cost_from_log(log) == 0.11

    def test_the_largest_session_figure_wins(self):
        # One report per exchange, and a reflection produces several. The last
        # one is the total, but ordering in a captured log is not guaranteed.
        from orchestrator.executor import cost_from_log

        log = (
            "Cost: $0.03 message, $0.03 session.\n"
            "Cost: $0.04 message, $0.07 session.\n"
        )
        assert cost_from_log(log) == 0.07

    def test_sub_cent_precision_survives(self):
        # `format_cost` widens the decimals below $0.01, so a naive two-place
        # parse would read a real cost as zero.
        from orchestrator.executor import cost_from_log

        assert cost_from_log("Cost: $0.00021 message, $0.00042 session.") == 0.00042

    def test_a_log_without_pricing_reports_nothing(self):
        # The local model's case, and it must read as "free", not "unknown" —
        # a stage that spent nothing should not be indistinguishable from one
        # whose figure was lost.
        from orchestrator.executor import cost_from_log

        assert cost_from_log("Tokens: 225k sent, 1.8k received.") == 0.0

    def test_cache_activity_is_captured_too(self):
        # How the cache strategy is judged rather than assumed. Aider adds
        # these to the tokens line only when the provider actually cached.
        from orchestrator.executor import cache_tokens_from_log

        log = "Tokens: 12k sent, 8.0k cache write, 4.0k cache hit, 1.1k received."
        assert cache_tokens_from_log(log) == {"write": 8000, "hit": 4000}

    def test_no_cache_activity_reads_as_zero(self):
        from orchestrator.executor import cache_tokens_from_log

        assert cache_tokens_from_log("Tokens: 12k sent, 1.1k received.") == {
            "write": 0,
            "hit": 0,
        }


class TestPromptCachingIsOperatorControlled:
    """Off by default, because it is a property of the endpoint.

    Aider's own default is `--no-cache-prompts`. Against a local endpoint that
    prices nothing and caches nothing, turning it on buys nothing and adds a
    keepalive ping loop; against a hosted model it is most of the saving. So
    the orchestrator declares neither — the operator does, in the config that
    already carries every other fact about where the executor runs.
    """

    def test_it_is_absent_unless_asked_for(self):
        cfg, stage = cfg_with(executor={"model": "m"})
        argv = build_aider_argv(stage, cfg, "p")
        assert "--cache-prompts" not in argv
        assert "--cache-keepalive-pings" not in argv

    def test_it_is_passed_when_configured(self):
        cfg, stage = cfg_with(executor={"model": "m", "cache_prompts": True})
        assert "--cache-prompts" in build_aider_argv(stage, cfg, "p")

    def test_keepalive_is_passed_when_set(self):
        # A stage's attempts are separated by a scoped suite and sometimes a
        # full one, which is minutes — long enough for a five-minute cache
        # window to lapse between the attempts that would have reused it.
        cfg, stage = cfg_with(
            executor={"model": "m", "cache_prompts": True, "cache_keepalive_pings": 3}
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert argv[argv.index("--cache-keepalive-pings") + 1] == "3"

    def test_an_explicit_effort_overrides_aiders_own_capability_check(self):
        """Aider drops the flag when its metadata says the model refuses it.

        It said exactly that for `gpt-5.6-luna` — "does not support
        'reasoning_effort', ignoring" — and the provider's own API disagrees:
        a live call with `xhigh` is accepted, and the error for an unsupported
        value names the supported set. So the check was reading stale metadata
        and silently discarding the operator's setting.

        Bypassed only when an effort is actually configured. Aider's check is a
        reasonable default for someone who set nothing; it is the wrong
        authority once the operator has stated a value that the provider
        accepts.
        """
        cfg, stage = cfg_with(executor={"model": "m", "reasoning_effort": "xhigh"})
        argv = build_aider_argv(stage, cfg, "p")
        assert "--no-check-model-accepts-settings" in argv

    def test_the_bypass_is_absent_when_no_effort_is_set(self):
        cfg, stage = cfg_with(executor={"model": "m"})
        assert "--no-check-model-accepts-settings" not in build_aider_argv(stage, cfg, "p")

    def test_keepalive_alone_does_nothing(self):
        # Pinging to keep a cache warm that was never enabled is pure cost.
        cfg, stage = cfg_with(executor={"model": "m", "cache_keepalive_pings": 3})
        assert "--cache-keepalive-pings" not in build_aider_argv(stage, cfg, "p")


class TestConventionsReachAiderAsReadOnlyFiles:
    """Through `--read`, and never through `--message`.

    They went into the prompt first, and it broke the run inside four minutes.
    Aider scans the user message for anything that looks like a path and offers
    to attach it; `--yes-always` answers yes. The repository's agent-facing
    document is dense with paths, so a single stage attached `config/routes.rb`,
    `db/structure.sql`, `docker-compose.yml` and more, reaching 258,854 tokens
    against a 229,376 limit. Aider exited in three seconds having written
    nothing, verify correctly reported no changes, and the loop repeated.

    `check_for_file_mentions` is called on the user message and on the model's
    reply, and nowhere else — files supplied through `--read` are rendered as
    context and never scanned. There is no flag to disable the behaviour;
    `--detect-urls` covers URLs only. Read from the installed 0.86.2 source
    rather than recalled.
    """

    def _cfg(self, tmp_path, **over):
        (tmp_path / "AGENTS.md").write_text("# conventions\n" * 20)
        (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n")
        return cfg_with(target_repo=str(tmp_path), **over)

    def test_the_documents_are_passed_as_read_only(self, tmp_path):
        cfg, stage = self._cfg(tmp_path, executor={"model": "m"})
        argv = build_aider_argv(stage, cfg, "p")
        reads = [argv[i + 1] for i, a in enumerate(argv) if a == "--read"]
        assert "AGENTS.md" in reads

    def test_they_are_never_named_in_the_message(self, tmp_path):
        # The whole point. A path in the message is a path Aider will attach.
        cfg, stage = self._cfg(tmp_path, executor={"model": "m"})
        argv = build_aider_argv(stage, cfg, "the prompt text")
        assert argv[argv.index("--message") + 1] == "the prompt text"

    def test_a_document_that_does_not_exist_is_skipped(self, tmp_path):
        # The defaults name two files and most projects have one. Passing a
        # missing path makes Aider warn and, worse, offer to create it.
        (tmp_path / "AGENTS.md").write_text("# conventions\n")
        cfg, stage = cfg_with(target_repo=str(tmp_path), executor={"model": "m"})
        argv = build_aider_argv(stage, cfg, "p")
        reads = [argv[i + 1] for i, a in enumerate(argv) if a == "--read"]
        assert reads == ["AGENTS.md"]

    def test_an_operator_who_declares_none_gets_none(self, tmp_path):
        # `[]` means the operator looked and decided there is no such file.
        (tmp_path / "AGENTS.md").write_text("# conventions\n")
        cfg, stage = cfg_with(
            target_repo=str(tmp_path), agent_context=[], executor={"model": "m"}
        )
        argv = build_aider_argv(stage, cfg, "p")
        assert "--read" not in argv

    def test_they_do_not_displace_the_stage_s_own_reference_files(self, tmp_path):
        # Charged first would let a large conventions file evict the file the
        # stage actually needs. The stage's choices are budgeted; these are the
        # operator's standing context and sit outside that accounting.
        (tmp_path / "AGENTS.md").write_text("x\n" * 500)
        (tmp_path / "ref.rb").write_text("y\n" * 100)
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"read_files": ["ref.rb"]},
            executor={"model": "m", "max_read_lines": 200},
        )
        argv = build_aider_argv(stage, cfg, "p")
        reads = [argv[i + 1] for i, a in enumerate(argv) if a == "--read"]
        assert "ref.rb" in reads
        assert "AGENTS.md" in reads


class TestPathMentionsAreShielded:
    """A path the message merely *names* must not become a file Aider reads.

    Keeping the conventions out of `--message` fixed the document that broke a
    run; it did not fix the mechanism. The planner writes prose, prose names
    files, and `check_for_file_mentions` runs on the message either way. It
    attached `.rubocop_todo.yml` — 453,480 bytes — on every one of the 117
    executor inputs that named it, taking one stage from ~20k tokens a message
    to 137k. Measured across the project's run history: 1,713 attachments,
    ~30.8M tokens, led by that file, `config/routes.rb` and `db/structure.sql`.

    It is invisible from both ends. `base_coder.py:919` discards the return of
    the input scan, so unlike the reply scan it emits no "I added these files"
    line into the history, and nothing tells the planner that naming a file
    costs anything.

    So the message is shielded on the way out instead: a word that would
    resolve to a tracked path gets `./`, which the executor reads as the same
    file and Aider's matcher does not. Aider compares against the repo-relative
    path verbatim (`normalized_rel_fname in normalized_words`), having stripped
    trailing `,.!;:?` and surrounding `"'`*_` — so the prefix survives to defeat
    the comparison and nothing else about the sentence changes.
    """

    def test_a_backticked_path_becomes_unmatchable(self):
        out = shield_path_mentions("see `.rubocop_todo.yml` first", [".rubocop_todo.yml"])
        assert out == "see `./.rubocop_todo.yml` first"

    def test_trailing_punctuation_is_preserved(self):
        # Aider rstrips these before comparing, so they do not protect a path
        # and must not be lost when one is rewritten.
        out = shield_path_mentions("edit `config/routes.rb`.", ["config/routes.rb"])
        assert out == "edit `./config/routes.rb`."

    def test_bold_and_italic_wrappers_are_preserved(self):
        out = shield_path_mentions("**`db/structure.sql`** is truth", ["db/structure.sql"])
        assert out == "**`./db/structure.sql`** is truth"

    def test_rewriting_is_idempotent(self):
        # The shield runs on every attempt of every stage. A prefix applied
        # twice would walk the path out of the repository.
        once = shield_path_mentions("see `app.py`", ["app.py"])
        assert shield_path_mentions(once, ["app.py"]) == once

    def test_a_word_that_is_not_a_tracked_path_is_untouched(self):
        text = "rename the model and update app.pyc and the docs"
        assert shield_path_mentions(text, ["app.py"]) == text

    def test_a_path_carrying_a_line_range_is_left_alone(self):
        # Already immune — the range makes the word differ from the path, which
        # is why `read_excerpts` labels have never attached anything. Rewriting
        # it would be churn on the one form that was already safe.
        text = "quoted from `db/structure.sql:1752-1775` above"
        assert shield_path_mentions(text, ["db/structure.sql"]) == text

    def test_fenced_blocks_are_left_alone(self):
        # Excerpts, context-command output and the cumulative diff all arrive
        # fenced, and they are quoted from the repository rather than authored.
        # Rewriting inside one would corrupt the only copy of the code the
        # executor is told to treat as current.
        text = "before\n```\nexclude: app.py\n```\nafter app.py"
        out = shield_path_mentions(text, ["app.py"])
        assert "exclude: app.py" in out
        assert out.endswith("after ./app.py")

    def test_files_already_supplied_to_aider_are_exempt(self):
        # `--file` and `--read` paths are in the chat already, so Aider excludes
        # them from `get_addable_relative_files` and cannot re-add them. Marking
        # them up would be noise in the sentence that names the stage's own work.
        out = shield_path_mentions(
            "change `app.py` and read `lib.py`", ["app.py", "lib.py"], exempt=["app.py"]
        )
        assert out == "change `app.py` and read `./lib.py`"

    def test_a_bare_basename_is_a_recorded_residual(self):
        # Aider also matches a unique basename, so this one still attaches. It
        # is left alone deliberately: `./routes.rb` would name a file that does
        # not exist, and expanding it to the full path rewrites more of the
        # sentence than the fault justifies. Measured on the run history, the
        # full-path form outnumbers this one 71 inputs to 5.
        text = "the whitelist in routes.rb"
        assert shield_path_mentions(text, ["config/routes.rb"]) == text

    def test_runs_of_whitespace_survive(self):
        out = shield_path_mentions("a  `app.py`\tb", ["app.py"])
        assert out == "a  `./app.py`\tb"

    def test_the_message_is_shielded_before_it_reaches_aider(self):
        cfg, stage = cfg_with()
        argv = build_aider_argv(
            stage, cfg, "look at `lib/x.rb`", tracked=["lib/x.rb"]
        )
        assert argv[argv.index("--message") + 1] == "look at `./lib/x.rb`"

    def test_without_a_tracked_list_the_message_is_passed_through(self):
        # The shield needs the repository to know what a path is. Callers that
        # have no git — every argv test below this one — must not silently get
        # a different message than they built.
        cfg, stage = cfg_with()
        argv = build_aider_argv(stage, cfg, "look at `lib/x.rb`")
        assert argv[argv.index("--message") + 1] == "look at `lib/x.rb`"

    def test_the_executor_shields_from_the_live_repository(self, repo, fake_aider):
        # End to end, because the tracked list crosses from git through the
        # Executor into argv, and each end passing its own unit test is exactly
        # how four earlier values were lost in transit.
        cfg, stage = cfg_with(target_repo=str(repo))
        Executor(
            cfg, CommandRunner(cwd=repo, timeout=60), git=Git(repo)
        ).run_agent_stage(stage, "the bug is in `app.py` somewhere")
        recorded = json.loads(fake_aider.read_text())
        assert "the bug is in `./app.py` somewhere" in recorded["argv"]


class TestARepliedMentionCostsTheReply:
    """Aider discards a reply's edits if that reply also names a file.

    `check_for_file_mentions` runs on the model's answer at `base_coder.py:1561`
    and returns at `:1567` when it attached something; `apply_updates()` is at
    `:1585` and is never reached. So a reply carrying a correct edit *and* a
    path loses the edit, reflects to ask about the file, and the second reply
    answers a question about files rather than editing anything.

    Measured: the model emitted three SEARCH/REPLACE blocks and the transcript
    records zero `Applied edit to` lines. Across the run history every one of
    the 29 attempts reported as producing no changes was preceded by an attach.

    Aider asks for it. `coders/shell.py` instructs the model to "suggest any
    shell commands the user might want to run", with "if you added a test,
    suggest how to run it" among the examples — so on a stage that requires
    tests, the reply that follows instructions is the reply that gets thrown
    away. `--no-suggest-shell-commands` removes that section, and the command
    was never run anyway: that confirm is `explicit_yes_required`, which
    `--yes-always` answers *no*.

    None of it reaches stdout, which is why it stayed invisible for 344
    attempts. The confirmation is a prompt_toolkit call; only the chat history
    Aider writes for us records it.
    """

    CHAT = (
        "> Added app/models/x.rb to the chat.  \n"
        "> Tokens: 16k sent, 7.5k received.  \n"
        "> bin/rspec  \n"
        "> Add file to the chat? (Y)es/(N)o/(D)on't ask again [Yes]: y  \n"
    )

    def test_the_attached_file_is_recovered_from_the_chat_history(self):
        assert attached_by_mention(self.CHAT) == ["bin/rspec"]

    def test_a_declined_mention_is_not_an_attachment(self):
        # Nothing was added, so Aider does not return early and the edits
        # apply. Counting it would report a loss that did not happen.
        declined = self.CHAT.replace("[Yes]: y", "[Yes]: n")
        assert attached_by_mention(declined) == []

    def test_an_ordinary_session_reports_nothing(self):
        assert attached_by_mention("> Added app/models/x.rb to the chat.  \n") == []

    def test_each_file_is_reported_once(self):
        assert attached_by_mention(self.CHAT + self.CHAT) == ["bin/rspec"]

    def test_the_shell_command_suggestion_is_turned_off(self):
        # The clause that generates the mention, removed rather than argued
        # with. A rule asking the model for restraint would be the weaker fix,
        # and this instruction is not ours to argue with — it is Aider's.
        cfg, stage = cfg_with()
        assert "--no-suggest-shell-commands" in build_aider_argv(stage, cfg, "p")

    def test_the_flag_is_pinned_for_preflight(self):
        # preflight greps `aider --help` for everything in AIDER_FLAGS, so a
        # release that renames this one fails validation instead of quietly
        # restoring the behaviour it suppresses.
        assert "--no-suggest-shell-commands" in AIDER_FLAGS

    def test_the_execution_reports_what_was_attached(self, repo, fake_aider, tmp_path):
        history = tmp_path / "hist"
        history.mkdir()
        (history / "aider-chat.md").write_text(self.CHAT)
        cfg, stage = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p", history_dir=history
        )
        assert result.attached_files == ["bin/rspec"]

    def test_a_missing_history_is_not_an_error(self, repo, fake_aider, tmp_path):
        # `history_dir` is optional and several callers pass none. This must
        # not be the thing that fails an attempt that otherwise worked.
        cfg, stage = cfg_with(target_repo=str(repo))
        result = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_agent_stage(
            stage, "p"
        )
        assert result.ok
        assert result.attached_files == []


class TestExcerptsResolveAtACommit:
    """One baseline for the three participants, not three.

    The reviewer judges the cumulative diff from `stage_start_sha`; the planner
    is now shown the same diff; and an excerpt read from the working tree would
    be the odd one out. It matters most on a rework, where the executor's own
    prior attempt has already moved the lines the planner picked — a range
    chosen against one state and read against another silently yields the wrong
    code, and with no literal in the instruction there is nothing to notice it.
    """

    def _stage_at(self, repo, excerpts):
        return cfg_with(
            target_repo=str(repo),
            stage_overrides={"read_excerpts": excerpts},
            executor={"model": "m", "max_read_lines": 400},
        )

    def test_it_reads_the_commit_not_the_working_tree(self, repo, run_git):
        (repo / "app.py").write_text("FROM_COMMIT\n")
        run_git(repo, "commit", "-aqm", "pin it")
        sha = run_git(repo, "rev-parse", "HEAD")
        (repo / "app.py").write_text("FROM_TREE\n")

        cfg, stage = self._stage_at(repo, [{"path": "app.py", "start": 1, "end": 1}])
        text = resolve_excerpts(stage, cfg, git=Git(repo), sha=sha)[0][1]
        assert "FROM_COMMIT" in text
        assert "FROM_TREE" not in text

    def test_a_path_absent_at_that_commit_fails_loudly(self, repo, run_git):
        sha = run_git(repo, "rev-parse", "HEAD")
        cfg, stage = self._stage_at(repo, [{"path": "gone.rb", "start": 1, "end": 5}])
        with pytest.raises(ExcerptError) as excinfo:
            resolve_excerpts(stage, cfg, git=Git(repo), sha=sha)
        assert "gone.rb" in str(excinfo.value)

    def test_a_clipped_range_says_so_in_its_own_label(self, repo, run_git):
        # The other way an excerpt fails to arrive. An unreadable one now fails
        # the stage; a budget-clipped one still gets through, and used to get
        # through silently — the executor was handed the first N lines of a
        # range under a label claiming the whole of it. That was survivable
        # while the instruction also carried the code. It is not now: the
        # excerpt is the code, so a partial one has to announce itself to the
        # only participant that could be misled by it.
        (repo / "app.py").write_text("".join(f"line{i}\n" for i in range(1, 21)))
        run_git(repo, "commit", "-aqm", "twenty lines")
        sha = run_git(repo, "rev-parse", "HEAD")

        cfg, stage = cfg_with(
            target_repo=str(repo),
            stage_overrides={
                "read_excerpts": [{"path": "app.py", "start": 1, "end": 20}]
            },
            executor={"model": "m", "max_read_lines": 5},
        )
        label, text = resolve_excerpts(stage, cfg, git=Git(repo), sha=sha)[0]
        assert len(text.splitlines()) == 5
        assert "clipped" in label
        assert "20" in label  # what was asked for, not only what arrived

    def test_a_range_that_fits_carries_no_clip_note(self, repo, run_git):
        # The note has to mean something when it appears.
        sha = run_git(repo, "rev-parse", "HEAD")
        cfg, stage = cfg_with(
            target_repo=str(repo),
            stage_overrides={"read_excerpts": [{"path": "app.py", "start": 1, "end": 2}]},
            executor={"model": "m", "max_read_lines": 400},
        )
        label, _ = resolve_excerpts(stage, cfg, git=Git(repo), sha=sha)[0]
        assert "clipped" not in label

    def test_a_symlink_is_not_passed_off_as_its_target_s_name(self, repo, run_git):
        # `git show <sha>:<path>` on a symlink returns the link's target — a
        # path, not the file it names. Read as content that is a "file" whose
        # entire body is a filename, and as an excerpt it would be a numbered
        # line of nonsense presented as the code to edit.
        (repo / "link.py").symlink_to("app.py")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "link")
        sha = run_git(repo, "rev-parse", "HEAD")

        cfg, stage = self._stage_at(repo, [{"path": "link.py", "start": 1, "end": 1}])
        with pytest.raises(ExcerptError) as excinfo:
            resolve_excerpts(stage, cfg, git=Git(repo), sha=sha)
        assert "symlink" in str(excinfo.value).lower()
