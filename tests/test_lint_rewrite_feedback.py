"""What the linter changed, told to the model that did not change it.

`checks` run with autocorrection — `rubocop -A`, `eslint --fix`, `gofmt -w` —
and they run *after* the model has stopped asking for things. So the tree moves
underneath a conversation that is already finished, and the next cycle opens
with the model holding file contents that are no longer what is on disk. It
cannot see that its edit was rewritten; from where it sits, it made the change
and the gate is complaining anyway.

Measured on `customer-service-automations-reminder-dates`, which took three
planner revisions and about thirty-five minutes to escape. The stage required
the template to read `Date.today` and forbade `Time.zone.today`. The repository
loads `rubocop-rails`, whose `Rails/Date` cop rewrites `Date.today` into
`Time.zone.today` — so every attempt made the edit, the linter undid it, the
patterns gate saw the forbidden spelling still present, and the same diff came
back twice. The planner eventually worked it out from the repetition alone and
withdrew the instruction:

    That could never be satisfied: the repository's linter runs over this
    stage's output with autocorrection enabled and rewrites `Date.today` into
    `Time.zone.today`, so the change was undone by tooling after the executor
    made it, and the same diff came back twice.

It reached that by inference from a repeated diff. Nothing told it, and nothing
told the executor either.

The mechanism is the cheap half: commit the model's work before the checks run,
and whatever the checks then change is the unstaged remainder. That is the
linter's diff exactly, and committing it separately is what makes it survive —
the first attempt staged instead of committed, which isolated the diff in
memory and then folded both halves into one commit, so the rewrite existed only
for as long as the variable holding it. On the success path it was computed,
used for nothing, and dropped; `git` could not recover it afterwards because
nothing had ever written it down.

Its own commit answers three questions the fold could not: whether a rewrite
happened at all, what it was, and — through `git blame` on the stage branch —
whose it was. All of it is squashed on landing, so the project branch is
unaffected either way.
"""

import subprocess

import pytest

from test_config import as_test_tools

from code_gantry.config import Stage, parse_config
from code_gantry.gitops import Git


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "target"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    for pair in (("user.email", "t@e.com"), ("user.name", "T"),
                 ("commit.gpgsign", "false")):
        git(path, "config", *pair)
    (path / "app.rb").write_text("x = Date.today\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "initial")
    return path


class TestTheLintersDiffIsIsolated:
    def test_committing_first_separates_the_two(self, repo):
        """The whole mechanism, in the shape the loop uses it.

        The model's edit is committed; the linter's rewrite lands on top as the
        unstaged remainder, measured against a tree that already contains the
        model's work.
        """
        g = Git(repo)
        (repo / "app.rb").write_text("x = Date.today\ny = 1\n")   # the model
        g.commit_all("the model's work")
        (repo / "app.rb").write_text("x = Time.zone.today\ny = 1\n")  # the linter
        diff = g.diff_unstaged()
        assert "Time.zone.today" in diff
        assert "-x = Date.today" in diff
        # The model's own edit must not appear: it is committed, so it is the
        # baseline the remainder is measured against.
        assert "+y = 1" not in diff

    def test_no_rewrite_is_an_empty_diff(self, repo):
        g = Git(repo)
        (repo / "app.rb").write_text("x = Date.today\ny = 1\n")
        g.commit_all("the model's work")
        assert g.diff_unstaged() == ""

    def test_an_untracked_file_the_model_added_is_committed_too(self, repo):
        # Otherwise a new spec the model wrote reads as the linter's work.
        g = Git(repo)
        (repo / "new_spec.rb").write_text("describe X do\nend\n")
        g.commit_all("the model's work")
        assert g.diff_unstaged() == ""


class TestItReachesTheModel:
    def _cfg(self, repo):
        return parse_config(as_test_tools({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "full_test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        }))

    def test_the_rewrite_is_appended_to_the_failure(self, repo):
        from code_gantry.executorloop import _with_lint_rewrite

        failure = type("F", (), {"feedback": "the patterns gate failed"})()
        out = _with_lint_rewrite(
            failure, "--- a/app.rb\n+++ b/app.rb\n-Date.today\n+Time.zone.today\n"
        )
        assert "the patterns gate failed" in out.feedback
        assert "Time.zone.today" in out.feedback
        # Named as the tool's doing, not the model's. Without that the model
        # reads it as its own mistake and tries the same edit again.
        assert "checks" in out.feedback.lower()

    def test_nothing_is_appended_when_nothing_was_rewritten(self, repo):
        from code_gantry.executorloop import _with_lint_rewrite

        failure = type("F", (), {"feedback": "the tests failed"})()
        assert _with_lint_rewrite(failure, "").feedback == "the tests failed"

    def test_a_huge_rewrite_is_clipped(self, repo):
        # A formatter that reflows a whole file must not evict the failure it
        # is attached to. `clip_for_model` collapses before it truncates.
        from code_gantry.executorloop import _with_lint_rewrite

        failure = type("F", (), {"feedback": "the patterns gate failed"})()
        out = _with_lint_rewrite(failure, "+line\n" * 20_000)
        assert len(out.feedback) < 20_000
        assert "the patterns gate failed" in out.feedback


class TestAcheckThatCorrectedNothing:
    """An autocorrecting check that fails having changed no file.

    `rubocop -A` fixes what it can and reports what it cannot, and both arrive
    in one run. When the offence it reports has no autocorrection — this
    project's conventions name the case, a cop flagging a strong-parameter
    permit list — the check exits non-zero and rewrites nothing, and
    `_layer_checks` routes that to the executor as "a required check failed".

    Told nothing more, the model cannot tell that from a check that fixed
    nothing because something was broken. That distinction has cost this
    project 42 minutes of an attempt being told its work was wrong by an
    environment that was not there.
    """

    def test_the_model_is_told_the_checks_changed_nothing(self, repo):
        from code_gantry.executorloop import _note_uncorrectable

        failure = type("F", (), {"feedback": "A required check failed.\nrubocop"})()
        out = _note_uncorrectable(failure, "")
        assert "changed no file" in out.feedback
        assert "not something they can correct for you" in out.feedback

    def test_it_says_nothing_when_the_checks_did_rewrite(self, repo):
        # There the diff is the message, and `_with_lint_rewrite` carries it.
        from code_gantry.executorloop import _note_uncorrectable

        failure = type("F", (), {"feedback": "A required check failed."})()
        out = _note_uncorrectable(failure, "--- a/x.rb\n+++ b/x.rb\n-a\n+b\n")
        assert out.feedback == "A required check failed."

    def test_it_is_not_applied_to_a_test_failure(self, repo):
        # The defect a test caught. `_with_lint_rewrite` runs on every gate
        # failure in the cycle, so putting this inside it would tell a stage
        # whose *tests* failed that the checks changed no file — true,
        # irrelevant, and about a gate that passed.
        import ast
        import pathlib

        src = pathlib.Path("src/code_gantry/executorloop.py").read_text()
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_note_uncorrectable"
        ]
        assert len(calls) == 1, "it must be applied on exactly one branch"
        # And that branch is the one guarded by the checks result.
        line = src.splitlines()[calls[0].lineno - 2]
        assert "gate_records.pop(\"checks\"" in line
