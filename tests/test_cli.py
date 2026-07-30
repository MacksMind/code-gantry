"""The CLI, driven end to end with a fake aider and a stubbed reviewer.

Exit codes are part of the contract: 0 complete, 1 failed, 2 waiting on a
human. A paused run is not a failure and must not read as one.
"""

import json
import os
import stat

import pytest
import yaml
from click.testing import CliRunner

from orchestrator import cli
from orchestrator.executor import AIDER_FLAGS
from orchestrator.reviewer import ReviewOutcome, TokenUsage


@pytest.fixture
def fake_aider(tmp_path, monkeypatch):
    """Answers `--help` with our flag list, otherwise applies queued edits."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    queue = tmp_path / "edits.json"
    queue.write_text(json.dumps([{"app.py": "edited by aider\n"}]))
    script = bindir / "aider"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"FLAGS = {AIDER_FLAGS!r}\n"
        "if '--help' in sys.argv:\n"
        "    print('\\n'.join(FLAGS))\n"
        "    sys.exit(0)\n"
        f"q = pathlib.Path({str(queue)!r})\n"
        "edits = json.loads(q.read_text())\n"
        "if edits:\n"
        "    step = edits.pop(0)\n"
        "    q.write_text(json.dumps(edits))\n"
        "    for name, text in step.items():\n"
        "        p = pathlib.Path(name)\n"
        "        p.parent.mkdir(parents=True, exist_ok=True)\n"
        "        p.write_text(text)\n"
        "print('aider done')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return queue


@pytest.fixture
def stub_reviewer(monkeypatch):
    outcomes = []

    def fake_make_reviewer(_cfg):
        class R:
            def review(self, messages):
                if outcomes:
                    return outcomes.pop(0)
                return ReviewOutcome(
                    verdict="approved", summary="fine", usage=TokenUsage(1000, 30, 900)
                )

        return R()

    monkeypatch.setattr(cli, "make_reviewer", fake_make_reviewer)
    return outcomes


@pytest.fixture
def workspace(tmp_path, repo, monkeypatch):
    """Run the CLI from a project directory so `runs/` lands in tmp_path."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


def write_config(project, repo, stages, **extra):
    """Build the config as data and dump it — hand-indented YAML in a test
    fixture is its own source of failures."""
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "branch": "refactor/thing",
        "test_command": "true",
        "executor": {"model": "openai/local"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": stages,
    }
    data.update(extra)
    path = project / "run.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


AGENT_STAGES = [
    {"id": "extract", "instruction": "Extract the thing.", "edit_files": ["app.py"]}
]

MANUAL_STAGES = [
    {
        "id": "bump",
        "kind": "manual",
        "human_steps": "Bump the runtime, then resume.",
        "checks": ["test -f bumped.txt"],
    },
    {"id": "after", "instruction": "Follow up.", "edit_files": ["app.py"]},
]


def invoke(args):
    return CliRunner().invoke(cli.main, args, catch_exceptions=False)


class TestValidate:
    def test_accepts_a_good_config(self, workspace, repo, fake_aider):
        path = write_config(workspace, repo, AGENT_STAGES)
        result = invoke(["validate", str(path)])
        assert result.exit_code == 0
        assert "runnable" in result.output

    def test_rejects_a_config_with_problems(self, workspace, repo):
        path = workspace / "bad.yaml"
        path.write_text("target_repo: /tmp/x\nbranch: b\nstages: []\n")
        result = invoke(["validate", str(path)])
        assert result.exit_code == 1

    def test_reports_a_dirty_target_repo(self, workspace, repo, fake_aider):
        (repo / "app.py").write_text("uncommitted\n")
        path = write_config(workspace, repo, AGENT_STAGES)
        result = invoke(["validate", str(path)])
        assert result.exit_code == 1
        assert "working tree is clean" in result.output

    def test_reports_a_missing_aider_flag(self, workspace, repo, tmp_path, monkeypatch):
        # A renamed flag must fail validation, not stage 1 of a real run.
        bindir = tmp_path / "bin2"
        bindir.mkdir()
        script = bindir / "aider"
        script.write_text("#!/bin/sh\necho '--message --model'\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        path = write_config(workspace, repo, AGENT_STAGES)
        result = invoke(["validate", str(path)])
        assert result.exit_code == 1
        assert "--map-tokens" in result.output

    def test_skip_tests_flag(self, workspace, repo, fake_aider):
        path = write_config(workspace, repo, AGENT_STAGES, test_command="exit 1")
        assert invoke(["validate", str(path)]).exit_code == 1
        assert invoke(["validate", str(path), "--skip-tests"]).exit_code == 0


class TestRun:
    def test_completes_a_single_stage_run(self, workspace, repo, fake_aider, stub_reviewer):
        path = write_config(workspace, repo, AGENT_STAGES)
        result = invoke(["run", str(path), "--run-id", "r1"])
        assert result.exit_code == 0
        assert "complete" in result.output.lower()

    def test_creates_the_branch_and_commits_there(
        self, workspace, repo, fake_aider, stub_reviewer, run_git
    ):
        path = write_config(workspace, repo, AGENT_STAGES)
        invoke(["run", str(path), "--run-id", "r1"])
        assert run_git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "refactor/thing"
        assert "[extract]" in run_git(repo, "log", "--pretty=%s", "-1")

    def test_leaves_base_ref_untouched(self, workspace, repo, fake_aider, stub_reviewer, run_git):
        # The safety requirement: never modify any branch but the run's own.
        before = run_git(repo, "rev-parse", "main")
        path = write_config(workspace, repo, AGENT_STAGES)
        invoke(["run", str(path), "--run-id", "r1"])
        assert run_git(repo, "rev-parse", "main") == before

    def test_writes_run_artifacts(self, workspace, repo, fake_aider, stub_reviewer):
        path = write_config(workspace, repo, AGENT_STAGES)
        invoke(["run", str(path), "--run-id", "r1"])
        run_dir = workspace / "runs" / "r1"
        assert (run_dir / "report.md").exists()
        assert (run_dir / "run.log").exists()
        assert (run_dir / "state.db").exists()
        assert (run_dir / "run.json").exists()

    def test_target_repo_gets_no_orchestrator_artifacts(
        self, workspace, repo, fake_aider, stub_reviewer
    ):
        path = write_config(workspace, repo, AGENT_STAGES)
        invoke(["run", str(path), "--run-id", "r1"])
        assert not (repo / "runs").exists()
        assert not (repo / "report.md").exists()

    def test_refuses_to_start_on_a_dirty_repo(self, workspace, repo, fake_aider, stub_reviewer):
        (repo / "app.py").write_text("uncommitted\n")
        path = write_config(workspace, repo, AGENT_STAGES)
        result = invoke(["run", str(path), "--run-id", "r1"])
        assert result.exit_code == 1
        assert "nothing was run" in result.output

    def test_escalation_exits_nonzero(self, workspace, repo, fake_aider, stub_reviewer):
        stub_reviewer.append(ReviewOutcome(verdict="blocked", summary="Plan is wrong."))
        path = write_config(workspace, repo, AGENT_STAGES)
        result = invoke(["run", str(path), "--run-id", "r1"])
        assert result.exit_code == 1
        assert "Plan is wrong." in result.output


class TestGateAndResume:
    def test_gated_run_exits_with_the_waiting_code(
        self, workspace, repo, fake_aider, stub_reviewer
    ):
        # 2, not 1: waiting on a human is not a failure.
        path = write_config(workspace, repo, MANUAL_STAGES)
        result = invoke(["run", str(path), "--run-id", "r1"])
        assert result.exit_code == 2
        assert "Bump the runtime, then resume." in result.output

    def test_resume_completes_after_the_human_work(
        self, workspace, repo, fake_aider, stub_reviewer
    ):
        path = write_config(workspace, repo, MANUAL_STAGES)
        assert invoke(["run", str(path), "--run-id", "r1"]).exit_code == 2

        (repo / "bumped.txt").write_text("done\n")
        result = invoke(["resume", "r1"])
        assert result.exit_code == 0
        assert "complete" in result.output.lower()

    def test_resume_refuses_a_changed_stage_list(
        self, workspace, repo, fake_aider, stub_reviewer
    ):
        # Applying an edited plan to a half-finished run would make the report
        # describe work that never happened.
        path = write_config(workspace, repo, MANUAL_STAGES)
        invoke(["run", str(path), "--run-id", "r1"])
        write_config(workspace, repo, AGENT_STAGES)
        result = invoke(["resume", "r1"])
        assert result.exit_code == 1
        assert "stage list has changed" in result.output

    def test_resume_of_an_unknown_run_fails_cleanly(self, workspace, repo):
        result = CliRunner().invoke(cli.main, ["resume", "nope"])
        assert result.exit_code != 0


class TestStatus:
    def test_reports_a_finished_run(self, workspace, repo, fake_aider, stub_reviewer):
        path = write_config(workspace, repo, AGENT_STAGES)
        invoke(["run", str(path), "--run-id", "r1"])
        result = invoke(["status", "r1"])
        assert result.exit_code == 0
        assert "extract" in result.output
        assert "reset --hard" in result.output

    def test_reports_a_gated_run_with_the_waiting_code(
        self, workspace, repo, fake_aider, stub_reviewer
    ):
        path = write_config(workspace, repo, MANUAL_STAGES)
        invoke(["run", str(path), "--run-id", "r1"])
        result = invoke(["status", "r1"])
        assert result.exit_code == 2

    def test_unknown_run_fails_cleanly(self, workspace, repo):
        result = invoke(["status", "does-not-exist"])
        assert result.exit_code == 1
        assert "no such run" in result.output
