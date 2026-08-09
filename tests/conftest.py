import subprocess

import pytest


def git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def run_git():
    """Raw git, for arranging and asserting outside the code under test."""
    return git


@pytest.fixture
def repo(tmp_path):
    """A git repo with one commit on `main`, ready to be operated on."""
    path = tmp_path / "target"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    (path / "app.py").write_text("def hello():\n    return 1\n")
    # `.code_gantry/` because the work dir now defaults inside the repo,
    # beside the plan documents, and preflight blocks a run whose data
    # directory is tracked — every stage would find a dirty tree and none
    # could cut a branch. Every real project needs this line too.
    (path / ".gitignore").write_text("ignored/\n*.local\n.code_gantry/\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "initial")
    return path
