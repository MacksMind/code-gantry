"""`run` reads its config from the project branch, not from whatever branch
the checkout was left on.

A bay is a checkout, and a placement can move it to a project whose branch
is not the one checked out. The config's `project_branch` is the one fact
the config on any branch can be trusted for, so a run takes it from the
config it finds, puts the checkout on that branch, and reads the config
again from there before preflight. Otherwise preflight proves a config
another branch holds — three bays refused a project this way, each
reading the old branch's copy of the new project's config.
"""

import subprocess

import pytest
from click.testing import CliRunner

from test_config import SCOPED_TOOL_YAML

from code_gantry import cli


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _config(planner_model, remote_landing=False):
    return (
        f"""
base_ref: main
project_branch: work
plan_root: PLAN.md
full_test_command: "true"
remote_landing: {str(remote_landing).lower()}
"""
        + SCOPED_TOOL_YAML
        + f"""
executor:
  model: m
planner:
  model: {planner_model}
reviewer:
  model: gpt-5.5
"""
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """`work` carries the config the project means; `other`, checked out,
    carries an older copy that differs in a field preflight can see."""
    repo = tmp_path / "target"
    (repo / "docs" / "p").mkdir(parents=True)
    (repo / "PLAN.md").write_text("# Plan\n\n1. Do it.\n")
    (repo / "docs" / "p" / "code_gantry.yaml").write_text(_config("claude-opus-5"))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "branch", "work")
    _git(repo, "checkout", "-q", "-b", "other")
    (repo / "docs" / "p" / "code_gantry.yaml").write_text(_config("claude-sonnet-5"))
    _git(repo, "commit", "-q", "-am", "an older copy of the config")
    monkeypatch.chdir(tmp_path)
    return repo


def _current_branch(repo):
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def test_preflight_sees_the_project_branch_and_its_config(repo, monkeypatch):
    seen = {}

    def boom(cfg, **kwargs):
        seen["branch"] = _current_branch(repo)
        seen["planner"] = cfg.planner.model
        raise RuntimeError("preflight reached")

    monkeypatch.setattr(cli, "run_preflight", boom)
    result = CliRunner().invoke(
        cli.main, ["run", "target/docs/p/code_gantry.yaml"], catch_exceptions=True,
    )
    assert isinstance(result.exception, RuntimeError), result.output
    assert seen == {"branch": "work", "planner": "claude-opus-5"}


def test_a_dirty_checkout_is_left_for_preflight_to_report(repo, monkeypatch):
    # A checkout with changes is not switched under them; preflight refuses
    # a dirty tree and says so, which is the diagnosis a person needs.
    (repo / "PLAN.md").write_text("# Plan\n\n1. Do it.\n2. And this.\n")
    seen = {}

    def boom(cfg, **kwargs):
        seen["branch"] = _current_branch(repo)
        raise RuntimeError("preflight reached")

    monkeypatch.setattr(cli, "run_preflight", boom)
    CliRunner().invoke(cli.main, ["run", "target/docs/p/code_gantry.yaml"], catch_exceptions=True)
    assert seen["branch"] == "other"


@pytest.fixture
def origin(repo, tmp_path):
    """A bare origin whose `work` carries a newer config than the checkout's
    own `work`: another host landed a field change the run must honour."""
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main", "work")
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", "-b", "work", str(bare), str(other))
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "O")
    _git(other, "config", "commit.gpgsign", "false")
    return bare, other


def _other_lands(other, planner_model, remote_landing):
    (other / "docs" / "p" / "code_gantry.yaml").write_text(_config(planner_model, remote_landing))
    _git(other, "commit", "-q", "-am", "the planner changes")
    _git(other, "push", "-q", "origin", "work")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=other, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _reach_preflight(repo, monkeypatch, seen):
    def boom(cfg, **kwargs):
        seen["branch"] = _current_branch(repo)
        seen["tip"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
        ).stdout.strip()
        seen["planner"] = cfg.planner.model
        raise RuntimeError("preflight reached")

    monkeypatch.setattr(cli, "run_preflight", boom)
    result = CliRunner().invoke(
        cli.main, ["run", "target/docs/p/code_gantry.yaml"], catch_exceptions=True,
    )
    assert isinstance(result.exception, RuntimeError), result.output


def test_under_remote_landing_the_config_read_is_origins_copy(repo, origin, monkeypatch):
    # Three bays behind origin each read the config their own `work` held,
    # then preflight pulled the branch under them: the run planned with the
    # old models while its checkpoint named the new blob. The pull comes
    # before the read.
    _git(repo, "checkout", "-q", "work")
    (repo / "docs" / "p" / "code_gantry.yaml").write_text(_config("claude-opus-5", remote_landing=True))
    _git(repo, "commit", "-q", "-am", "remote landing on")
    _git(repo, "push", "-q", "origin", "work")
    _git(repo, "checkout", "-q", "other")
    bare, other = origin
    _git(other, "pull", "-q", "--rebase", "origin", "work")
    theirs = _other_lands(other, "claude-fable-5-1", remote_landing=True)
    seen = {}
    _reach_preflight(repo, monkeypatch, seen)
    assert seen == {"branch": "work", "tip": theirs, "planner": "claude-fable-5-1"}


def test_without_remote_landing_the_branch_is_this_hosts_own(repo, origin, monkeypatch):
    bare, other = origin
    theirs = _other_lands(other, "claude-fable-5-1", remote_landing=False)
    seen = {}
    _reach_preflight(repo, monkeypatch, seen)
    assert seen["branch"] == "work"
    assert seen["tip"] != theirs
    assert seen["planner"] == "claude-opus-5"


def test_a_resume_with_nothing_to_resume_says_so_with_its_own_code(repo):
    # A run killed or crashed in preflight left no checkpoint. The daemon
    # decides what to do next from the exit code alone, and "nothing to
    # resume" wants a fresh run where "failed" wants a person; they were
    # one code, and a killed bay showed `failed` until asked twice.
    from code_gantry.cli import EXIT_NOTHING_TO_RESUME

    result = CliRunner().invoke(
        cli.main, ["resume", "target/docs/p/code_gantry.yaml", "20260101-000000-bay1"],
    )
    assert result.exit_code == EXIT_NOTHING_TO_RESUME, result.output
    assert "no checkpoint for run 20260101-000000-bay1" in result.output
