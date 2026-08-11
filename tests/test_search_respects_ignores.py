"""What `.gitignore` excludes must stay excluded, whatever glob the model writes.

`search`'s boundary is documented as ripgrep's ignore handling: "ignored files
out, untracked-but-not-ignored in", which is the old `--untracked` behaviour and
the reason the operator's convention of keeping identifiable values in ignored
files is safe. That claim held for a bare search and was false the moment the
model supplied a path, because a `-g` glob **overrides** ignore rules in
ripgrep — the flag is a filter applied over the walk, not a filter within it.

Measured on the live target repository at the run's tip, one literal:

    rg --hidden -g '**/*' -e resend_approval .
      41 hits  .code_gantry/runs/<run>/stages/010-.../executor-conversation.jsonl
      18 hits  .code_gantry/runs/<run>/tools.log
       …       log/test.log, coverage/index.html
    rg --hidden        -e resend_approval .
      source files only

So an executor searching `**/*` was handed its own transcript, a previous run's
rejected attempt, and `planner.json` for the very stage it was working on —
the planner's reasoning, which it is deliberately not given. Search output is
capped, and 31 of that run's 369 searches hit the cap: when the artifacts win
the first 12k the real hits are cut off, and the model searches again. Which is
how a leak shows up as repetition.

`git check-ignore` is asked instead of trusted to a flag, and asked of the
matched paths rather than the walk — one call on the answer, not on the tree.

It is asked *without* `--no-index`, so a tracked file matching an ignore rule
is not reported and survives. That case is reachable and worth having: ripgrep
reads ignore files and knows nothing of the index, so a tracked `app/keep.log`
is invisible to a bare search and visible under a glob, and git's answer — it
is tracked, a stage may edit it — is the better of the two. Which is why this
asks git rather than copying ripgrep's boundary.
"""

import subprocess

import pytest

from orchestrator.gitops import Git
from orchestrator.repotools import ReadBudget, RepoReader, ToolError


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app").mkdir(parents=True)
    (r / "docs" / ".artifacts" / "runs").mkdir(parents=True)
    (r / "log").mkdir()
    (r / "app" / "order.rb").write_text("class Order\n  TARGET = 1\nend\n")
    (r / "docs" / ".artifacts" / "runs" / "transcript.jsonl").write_text(
        '{"text": "TARGET was here"}\n'
    )
    (r / "log" / "test.log").write_text("TARGET in a log line\n")
    (r / "docs" / ".gitignore").write_text(".artifacts/\n")
    (r / ".gitignore").write_text("log/\n")
    # Tracked *and* matching an ignore rule. It must survive: the index wins,
    # and a search that dropped it would be narrower than the boundary claimed.
    (r / "log").mkdir(exist_ok=True)
    (r / "app" / "keep.log").write_text("TARGET tracked anyway\n")
    (r / ".gitignore").write_text("log/\n*.log\n")
    for args in (
        ["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        # Only this one is forced. `add -A -f` would track the artifacts and
        # the log too, and `check-ignore` does not report a tracked path — the
        # fixture would then pass by making the leak legitimate.
        ["add", "-f", "app/keep.log"],
        ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def _reader(repo, exclude=()):
    return RepoReader(
        Git(repo), repo, ReadBudget(), search_exclude_globs=list(exclude)
    )


def _paths(hits):
    return {h.split(":")[0] for h in hits}


class TestAModelsGlobCannotReopenAnIgnoredPath:
    def test_a_bare_search_never_saw_them(self, repo):
        # The behaviour the docstring was written about, pinned so the fix is
        # measured against something rather than asserted. `app/keep.log` is
        # tracked and still absent: ripgrep reads ignore files, not the index.
        assert _paths(_reader(repo).search("TARGET")) == {"app/order.rb"}

    def test_nor_does_one_with_a_universal_glob(self, repo):
        hits = _reader(repo).search("TARGET", "**/*")
        assert not any(".artifacts" in p for p in _paths(hits))
        assert "log/test.log" not in _paths(hits)

    @pytest.mark.parametrize("glob", ["docs/.artifacts/**/*", "log/**/*"])
    def test_nor_one_aimed_straight_at_the_ignored_directory(self, repo, glob):
        # Naming it explicitly is the strongest form of the same request, and
        # the one a model writes after a wider glob came back thin. Both
        # spellings of the rule are covered: `docs/.gitignore` sits next to
        # what it excludes, `.gitignore` at the root names `log/`.
        with pytest.raises(ToolError) as e:
            _reader(repo).search("TARGET", glob)
        assert "no files matched" in str(e.value)

    def test_what_survives_is_what_the_repository_does_not_ignore(self, repo):
        # Not "a subset of the bare walk": a glob genuinely widens it, because
        # ripgrep drops `app/keep.log` for matching `*.log` while git does not
        # — the file is tracked, and the index wins. Asking git rather than
        # copying ripgrep's answer is what makes a stage able to search a file
        # it is allowed to edit.
        wide = _paths(_reader(repo).search("TARGET", "**/*"))
        assert wide == {"app/order.rb", "app/keep.log"}

    def test_the_hits_that_remain_are_untouched(self, repo):
        hits = _reader(repo).search("TARGET", "**/*")
        assert "app/order.rb:2:  TARGET = 1" in hits
