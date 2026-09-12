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


@pytest.fixture(autouse=True)
def _host_locks_in_tmp(tmp_path, monkeypatch):
    """Every test takes its host locks under its own directory, never the
    host's, so a suite leaves nothing behind and cannot wait on a live run.

    The daemon's state directory is pointed at the same place and left
    empty, so the semaphore finds no socket to ask and falls back. A suite
    that found the real one would queue behind a real derivation on
    another machine, and wait there for as long as that derivation takes.
    """
    monkeypatch.setenv("CODE_GANTRY_LOCK_DIR", str(tmp_path / "host-locks"))
    monkeypatch.setenv("CODE_GANTRY_DAEMON_STATE", str(tmp_path / "no-daemon"))
