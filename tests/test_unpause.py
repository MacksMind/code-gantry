"""`code-gantry unpause` — taking back a pause the run has not read yet.

Written the way `test_reconcile` argues for: through the real `click` entry
point, because a CLI command with no test is untested however green the suite
is.

The command exists because `resume` was the only thing that cleared the flag,
and resume is for a run that has stopped. Cancelling a pause on a *live* run
that way starts a second process on the same repository. The operation wanted
is the one `resume` performs at its own start and nothing else: delete a file.
"""

import subprocess

from pathlib import Path

import pytest
from click.testing import CliRunner

from code_gantry import cli
from code_gantry.runtime import RunPaths


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A target repo, a config pointing at it, and a run directory."""
    repo = tmp_path / "target"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "plan.md").write_text("# Plan\n\n1. Do the thing.\n")
    # Production puts `.code_gantry/` beside the plan and gitignores it by
    # construction. A fixture without that line makes `git status` report the
    # run directory, so a test asking "was the tree touched" answers a
    # question about our own artifacts instead — and passes or fails on
    # whether git happens to be looking at an empty directory.
    (repo / ".gitignore").write_text(".code_gantry/\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        ["commit", "-q", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)

    projects = tmp_path / "projects"
    (projects / "demo").mkdir(parents=True)
    (projects / "demo" / "config.yaml").write_text(
        f"""
target_repo: {repo}
base_ref: main
project_branch: work
plan_root: docs/plan.md
full_test_command: "true"
executor:
  model: m
planner:
  model: claude-opus-5
reviewer:
  model: gpt-5.5
scoped_test_tool: scoped_suite
project_tools:
  - name: scoped_suite
    description: The suite, taking a selection.
    command: ['true', '{{paths}}']
    arguments:
      - {{name: paths, description: Files or examples., repeated: true}}
    roles: ['executor']
"""
    )
    monkeypatch.chdir(tmp_path)
    return repo, projects


CONFIG = "projects/demo/config.yaml"
RUN_ID = "20260831-000000-work"


def make_run(config_path=CONFIG, run_id=RUN_ID):
    """The run directory `pause` and `unpause` both address."""
    _, project = cli._project_for(Path(config_path))
    paths = RunPaths(project, run_id)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    return paths


class TestItWithdrawsAPause:
    def test_the_flag_is_gone_afterwards(self, project):
        paths = make_run()
        paths.pause_flag.write_text("changed my mind")
        result = CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        assert result.exit_code == 0, result.output
        assert not paths.pause_flag.exists()

    def test_it_says_which_run_and_names_resume_for_a_stopped_one(self, project):
        paths = make_run()
        paths.pause_flag.write_text("")
        result = CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        assert RUN_ID in result.output
        # A run that already stopped is not restarted by this, and the output
        # has to say so or the caller waits for a process that is not there.
        assert "resume" in result.output

    def test_a_pause_can_be_taken_back_and_set_again(self, project):
        paths = make_run()
        CliRunner().invoke(cli.main, ["pause", CONFIG, RUN_ID])
        assert paths.pause_flag.exists()
        CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        assert not paths.pause_flag.exists()
        CliRunner().invoke(cli.main, ["pause", CONFIG, RUN_ID, "--note", "again"])
        assert paths.pause_flag.read_text() == "again"


class TestItIsSafeToRunAnyway:
    def test_no_pause_is_not_an_error(self, project):
        """"Make sure this is not paused" is what a caller means."""
        make_run()
        result = CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        assert result.exit_code == 0, result.output
        assert "no pause" in result.output

    def test_running_it_twice_is_the_same_as_running_it_once(self, project):
        paths = make_run()
        paths.pause_flag.write_text("")
        first = CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        second = CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        assert first.exit_code == 0 and second.exit_code == 0
        assert not paths.pause_flag.exists()

    def test_an_unknown_run_fails_rather_than_reporting_success(self, project):
        make_run()
        result = CliRunner().invoke(cli.main, ["unpause", CONFIG, "20990101-000000-nope"])
        assert result.exit_code != 0
        assert "no such run" in result.output


class TestItTouchesNothingElse:
    def test_the_run_directory_survives(self, project):
        """It withdraws a request; it does not discard the run."""
        paths = make_run()
        paths.pause_flag.write_text("")
        (paths.run_dir / "run.json").write_text("{}")
        CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        assert paths.run_dir.is_dir()
        assert (paths.run_dir / "run.json").read_text() == "{}"

    def test_the_target_repo_is_not_touched(self, project):
        """No process is started, so the tree and HEAD are exactly as found."""
        repo, _ = project
        paths = make_run()
        paths.pause_flag.write_text("")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout
        CliRunner().invoke(cli.main, ["unpause", CONFIG, RUN_ID])
        after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
        ).stdout
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert after == head
        assert status == ""
