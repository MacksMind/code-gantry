"""The repository's pre-commit hook refuses a staged name from the clone's
`.git/info/forbidden_names`. Driven in a repository of our own with no
account-wide git configuration in reach, so the hook's hand-over to the
account's hooks finds none."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / ".githooks" / "pre-commit"


def git(repo: Path, *args, env=None, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True, env=env,
    )


def make_repo(tmp_path: Path, home: Path) -> tuple[Path, dict]:
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / "none"),
           "GIT_CONFIG_NOSYSTEM": "1"}
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", env=env)
    git(repo, "config", "user.email", "t@example.com", env=env)
    git(repo, "config", "user.name", "t", env=env)
    hooks = repo / ".githooks"
    hooks.mkdir()
    shutil.copy(HOOK, hooks / "pre-commit")
    git(repo, "config", "core.hooksPath", ".githooks", env=env)
    return repo, env


def test_a_staged_name_is_refused_and_named(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo, env = make_repo(tmp_path, home)
    (repo / ".git" / "info" / "forbidden_names").write_text("# the client\nMyClient\n\n")
    (repo / "notes.md").write_text("The myclient migration.\n")
    git(repo, "add", "notes.md", env=env)
    r = git(repo, "hook", "run", "pre-commit", env=env, check=False)
    assert r.returncode == 1
    assert "notes.md:1:" in r.stderr and "forbidden names" in r.stderr


def test_a_clean_stage_passes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo, env = make_repo(tmp_path, home)
    (repo / ".git" / "info" / "forbidden_names").write_text("MyClient\n")
    (repo / "notes.md").write_text("Nothing of note.\n")
    git(repo, "add", "notes.md", env=env)
    assert git(repo, "hook", "run", "pre-commit", env=env, check=False).returncode == 0


def test_only_the_staged_blob_is_read(tmp_path):
    """A name in the working tree that is not staged is not this commit's;
    the sweep reads the index, which is what the commit takes."""
    home = tmp_path / "home"
    home.mkdir()
    repo, env = make_repo(tmp_path, home)
    (repo / ".git" / "info" / "forbidden_names").write_text("MyClient\n")
    (repo / "notes.md").write_text("clean\n")
    git(repo, "add", "notes.md", env=env)
    (repo / "notes.md").write_text("clean, then myclient in the tree only\n")
    assert git(repo, "hook", "run", "pre-commit", env=env, check=False).returncode == 0


def test_no_list_means_no_sweep(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    repo, env = make_repo(tmp_path, home)
    (repo / "notes.md").write_text("myclient\n")
    git(repo, "add", "notes.md", env=env)
    assert git(repo, "hook", "run", "pre-commit", env=env, check=False).returncode == 0
