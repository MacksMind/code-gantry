"""A batched stage's excerpts are checked against the tree, not predicted.

The static orthogonality pass this replaces asked whether an earlier stage
*was permitted* to touch a file a later stage quotes. Three things were wrong
with that, and the third is the one that ends the argument:

- It predicted. A stage spec is a prediction already; layering a prediction
  about the prediction compounds rather than checks.
- It only knew about batch-mates. A file can move between two stages because a
  human edited it, because `rubocop -A` reflowed it, or because the run was
  resumed onto a branch that advanced — and none of those were visible.
- **`edit_files` is a permission, not a record.** A stage declaring a file
  editable very often does not edit it, so the check fired over a superset of
  what actually happened and dropped usable stages for edits that never
  occurred.

Comparing the blob the planner read against the blob at the stage's start is a
measurement of the only thing that matters: did the bytes under this line range
move. It is the same principle as `git blame` on the progress log answering
what a stage advanced — prefer the fact to the label.
"""

import subprocess

import pytest


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app").mkdir(parents=True)
    (r / "app" / "a.rb").write_text("one\ntwo\nthree\n")
    (r / "app" / "b.rb").write_text("bee\n")
    for args in (
        ["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"],
        ["add", "-A"], ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def _commit(repo, path, text):
    (repo / path).write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "x"],
        cwd=repo, check=True, capture_output=True,
    )


def _stage(sid, excerpts=(), base=""):
    from orchestrator.config import Excerpt, Stage

    return Stage(
        id=sid,
        instruction="do it",
        read_excerpts=[Excerpt(path=p, start=1, end=2) for p in excerpts],
        excerpt_base_sha=base,
    )


class TestStaleExcerptsAreDetected:
    def test_an_unchanged_file_is_fresh(self, repo):
        from orchestrator.gitops import Git
        from orchestrator.nodes import stale_excerpts

        git = Git(repo)
        base = git.rev_parse("HEAD")
        _commit(repo, "app/b.rb", "bee two\n")  # a different file moved
        assert stale_excerpts(git, _stage("s", ["app/a.rb"], base)) == []

    def test_a_changed_file_is_named(self, repo):
        from orchestrator.gitops import Git
        from orchestrator.nodes import stale_excerpts

        git = Git(repo)
        base = git.rev_parse("HEAD")
        _commit(repo, "app/a.rb", "one\nCHANGED\nthree\n")
        assert stale_excerpts(git, _stage("s", ["app/a.rb"], base)) == ["app/a.rb"]

    def test_a_deleted_file_is_stale_not_an_error(self, repo):
        from orchestrator.gitops import Git
        from orchestrator.nodes import stale_excerpts

        git = Git(repo)
        base = git.rev_parse("HEAD")
        subprocess.run(["git", "rm", "-q", "app/a.rb"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "rm"],
            cwd=repo, check=True, capture_output=True,
        )
        assert stale_excerpts(git, _stage("s", ["app/a.rb"], base)) == ["app/a.rb"]

    def test_no_base_sha_means_nothing_to_compare(self, repo):
        # The stage being started now was derived against the tree as it
        # stands, so there is no window in which anything could have moved.
        from orchestrator.gitops import Git
        from orchestrator.nodes import stale_excerpts

        _commit(repo, "app/a.rb", "changed\n")
        assert stale_excerpts(Git(repo), _stage("s", ["app/a.rb"], "")) == []

    def test_a_stage_with_no_excerpts_is_never_stale(self, repo):
        from orchestrator.gitops import Git
        from orchestrator.nodes import stale_excerpts

        git = Git(repo)
        base = git.rev_parse("HEAD")
        _commit(repo, "app/a.rb", "changed\n")
        assert stale_excerpts(git, _stage("s", [], base)) == []

    def test_a_content_preserving_commit_is_not_stale(self, repo):
        """The point of comparing blobs rather than commits.

        A stage in front may land without touching this file at all, and under
        the old rule merely *declaring* it editable was enough to drop the
        later stage. Only the bytes decide.
        """
        from orchestrator.gitops import Git
        from orchestrator.nodes import stale_excerpts

        git = Git(repo)
        base = git.rev_parse("HEAD")
        _commit(repo, "app/b.rb", "unrelated\n")
        _commit(repo, "app/b.rb", "unrelated again\n")
        assert stale_excerpts(git, _stage("s", ["app/a.rb"], base)) == []
