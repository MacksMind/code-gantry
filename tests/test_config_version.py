"""A run reads one config, and reads it for its whole life.

This replaces `orchestrator approve`. Approval was a sha256 of the bytes an
operator confirmed they had read, kept beside the config and invalidated by any
edit — and its module was explicit about why there was no `approved: true`
field: "such a field could be set by anything."

Once the config lives in the repository it describes, git hashes the same bytes
and its hash carries an author, a message, a parent, and whatever review the
repository requires. The approval hash with provenance attached.

What that buys is a stronger property than approval had, for less ceremony.
Approval let an edited-and-re-approved config take over a run already deep in
its work, so the commands behind stage 1 and stage 40 could differ with nothing
in the record saying where. Pinning refuses instead: the run in front of you is
not the run that config describes.
"""

import subprocess

import pytest

from orchestrator.configversion import (
    ConfigVersionError,
    blob_sha,
    problem_resuming,
    problem_starting,
)


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "target"
    (path / "docs").mkdir(parents=True)
    git_init = ["init", "-q", "-b", "main"]
    path.mkdir(exist_ok=True)
    subprocess.run(["git", "-C", str(path), *git_init], check=True, capture_output=True)
    for pair in (("user.email", "t@e.com"), ("user.name", "T"),
                 ("commit.gpgsign", "false")):
        git(path, "config", *pair)
    (path / "docs" / "orchestrator.yaml").write_text("test_command: true\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "config")
    return path


class TestTheShaIsGits:
    def test_it_matches_what_git_would_say(self, repo):
        # Not a sha256 of our own: the point of using git's is that an operator
        # can look the value up. `git cat-file` must answer to it.
        cfg = repo / "docs" / "orchestrator.yaml"
        got = blob_sha(cfg)
        out = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-p", got],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout == cfg.read_text()

    def test_an_unreadable_path_says_so(self, tmp_path):
        with pytest.raises(ConfigVersionError):
            blob_sha(tmp_path / "nope.yaml")


class TestStarting:
    def test_a_committed_config_is_fine(self, repo):
        assert problem_starting(repo / "docs" / "orchestrator.yaml", repo, "main") == ""

    def test_an_uncommitted_edit_refuses(self, repo):
        cfg = repo / "docs" / "orchestrator.yaml"
        cfg.write_text("test_command: false\n")
        problem = problem_starting(cfg, repo, "main")
        assert "uncommitted" in problem
        # Both shas named, so the operator can see which is which rather than
        # being told something differs.
        assert blob_sha(cfg)[:12] in problem

    def test_a_config_the_branch_has_never_seen_refuses(self, repo):
        cfg = repo / "docs" / "other.yaml"
        cfg.write_text("test_command: true\n")
        problem = problem_starting(cfg, repo, "main")
        assert "not committed" in problem

    def test_a_config_outside_the_repo_refuses(self, repo, tmp_path):
        # There is no commit to cite, so there is no version. Reported as the
        # same class of problem rather than passing silently.
        outside = tmp_path / "loose.yaml"
        outside.write_text("test_command: true\n")
        assert problem_starting(outside, repo, "main") != ""


class TestResuming:
    def test_an_unchanged_config_continues(self, repo):
        cfg = repo / "docs" / "orchestrator.yaml"
        assert problem_resuming(cfg, blob_sha(cfg)) == ""

    def test_a_changed_config_refuses(self, repo):
        cfg = repo / "docs" / "orchestrator.yaml"
        was = blob_sha(cfg)
        cfg.write_text("test_command: false\n")
        problem = problem_resuming(cfg, was)
        assert "changed since this run started" in problem
        # And says what to do, with the reason it is safe: the work is not in
        # the checkpoint, it is on the project branch.
        assert "Start a new run" in problem
        assert "project branch" in problem

    def test_a_committed_change_refuses_too(self, repo):
        # The point is not "uncommitted". Committing an edit mid-run is exactly
        # the case approval could not catch, because re-approving made it legal.
        cfg = repo / "docs" / "orchestrator.yaml"
        was = blob_sha(cfg)
        cfg.write_text("test_command: false\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "change the suite")
        assert problem_resuming(cfg, was) != ""

    def test_a_run_with_no_recorded_sha_is_not_blocked(self, repo):
        # Runs started before this existed have nothing to compare against, and
        # refusing them all would strand work that is perfectly resumable.
        cfg = repo / "docs" / "orchestrator.yaml"
        assert problem_resuming(cfg, "") == ""
