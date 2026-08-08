"""The executor's non-loop surface: read budgets, excerpts, script stages.

The edit cycle itself is `test_executor_loop.py`; the provider call is
`test_executor_client.py`. What is left here is everything that shapes what
the executor is given before it runs, and the one stage kind that never
reaches a model.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from orchestrator.commands import CommandRunner
from orchestrator.config import Stage, parse_config
from orchestrator.executor import (
    Executor,
    ExcerptError,
    resolve_excerpts,
)
from orchestrator.gitops import Git
from orchestrator.repotools import SEPARATOR


BASE_STAGE ={"id": "s1", "instruction": "do it", "edit_files": ["app/**", "src/*.py"]}


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
        },
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(cfg_overrides)
    return parse_config(data), Stage(**stage)





class TestReadContextBudget:
    """Reference files are useful until they are the majority of the prompt.

    The planner passes previously-converted files as worked examples, which is
    sound and grows without bound: by the twelfth stage of one run it was
    sending 4,636 lines of context to change six lines, 69,000 tokens a call.
    Two costs, both measured on that run. Latency — attempts took 561s and 584s
    against an un-overridable 600s request timeout, so whether a stage
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
        from orchestrator.executor import _within_read_budget

        assert _within_read_budget(stage.read_files, cfg) == ["a.rb", "b.rb"]

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
        from orchestrator.executor import _within_read_budget

        assert _within_read_budget(stage.read_files, cfg) == ["small.rb", "mid.rb"]

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
        from orchestrator.executor import _within_read_budget

        assert _within_read_budget(stage.read_files, cfg) == []  # still too big
        got = resolve_excerpts(stage, cfg)
        assert len(got) == 1
        label, text = got[0]
        assert label == "huge.rb:40-44 — why"
        # Numbered from the range's own start, not from 1 — the executor is
        # told to match a line and a number that does not name the file's line
        # is worse than no number. The rendering itself is `number_lines`'
        # business and is pinned there.
        assert text.splitlines()[0].startswith(f"   40{SEPARATOR}")
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
        from orchestrator.executor import _within_read_budget

        assert _within_read_budget(stage.read_files, cfg) == []

    def test_the_edited_file_is_never_budgeted_away(self, tmp_path):
        # `edit_files` is the task. Only reference material is discretionary.
        self._repo(tmp_path, {"target.rb": 5000, "ref.rb": 5000})
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"edit_files": ["target.rb"], "read_files": ["ref.rb"]},
            executor={"model": "m", "max_read_lines": 10},
        )
        from orchestrator.executor import _within_read_budget

        # `edit_files` never enters the budget at all; only `read_files` does.
        assert _within_read_budget(stage.read_files, cfg) == []
        assert stage.edit_files == ["target.rb"]

    def test_an_unreadable_reference_is_kept(self, tmp_path):
        # Same rule as the auto-test paths: act on evidence, not on its absence.
        # A glob or a not-yet-created file has no line count, and guessing zero
        # would let it through while guessing huge would drop it silently.
        cfg, stage = cfg_with(
            target_repo=str(tmp_path),
            stage_overrides={"read_files": ["app/**/*.rb"]},
            executor={"model": "m", "max_read_lines": 10},
        )
        from orchestrator.executor import _within_read_budget

        assert _within_read_budget(stage.read_files, cfg) == ["app/**/*.rb"]






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

    def test_spends_no_model_tokens(self, repo):
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
        out = Executor(cfg, CommandRunner(cwd=repo, timeout=60)).run_script_stage(stage)
        # No model was reached, so nothing a model reports has a value.
        assert out.usage is None and out.model_turns == 0 and out.cycles == 0


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
