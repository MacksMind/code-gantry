"""A repository that refuses a commit must not end the run.

Measured, not hypothesised: a run three stages deep died at
`nodes.verify`'s `commit_all` with a `GitError` traceback, because a
commit hook rejected the staged content. The cause was ours — the editor
normalises line endings on write, so a CRLF file came back as a whole-file
rewrite and every pre-existing trailing space became an *added* line for the
hook to find — but the shape is general. A hook is operator policy, it can fire
on anything, and CodeGantry's answer to "the repository said no" cannot be
a stack trace.

Two call sites, and they want opposite handling for the same reason. `verify`
commits what a *check* rewrote and has somewhere to route: the hook names the
file and line, which is exactly the feedback an executor can act on. The
executor loop's own commit has nowhere to route — it is already inside the
attempt — so it reports and leaves the work in the tree for the next commit to
sweep up. What it must not do is stay silent, which it did, under a comment
claiming it was "reported through the log".

The hook here is real rather than a patched `commit_all`, because the thing
being tested is a refusal arriving from git itself, and a stub raising
`GitError` would pass whether or not the real path ever reaches one.
"""

import time

import pytest

from code_gantry import nodes
from code_gantry.gitops import GitError

from test_nodes import make, with_stage


def refuse_commits(repo, message="Trailing whitespace found in staged changes."):
    """Install a pre-commit hook that rejects everything.

    `core.hooksPath` is set explicitly, and that is not tidiness. A global
    `core.hooksPath` — which is how the operator who hit this in production has
    theirs configured — makes git ignore `.git/hooks` entirely, so writing the
    file and trusting it to run is a test that passes or fails according to
    whose machine it is on. Setting it per repository pins the instrument.
    """
    import subprocess

    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(f'#!/bin/sh\necho "{message}" >&2\nexit 1\n')
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)],
        check=True,
        capture_output=True,
    )
    return hook


class TestTheHookIsReal:
    def test_the_fixture_actually_refuses(self, repo, tmp_path):
        # Guard on the instrument. A hook that silently does not run would
        # make every test below pass for the wrong reason.
        cfg, rt, state = make(repo, tmp_path)
        refuse_commits(repo)
        (repo / "app.py").write_text("x\n")
        with pytest.raises(GitError):
            rt.git.commit_all("should be refused")


class TestVerify:
    def _dirty_stage(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        rt.git.commit_all("[s] executor")
        # What a check rewrote: uncommitted when verify reaches the commit.
        (repo / "app.py").write_text("changed by a check\n")
        return cfg, rt, state

    def test_a_refused_commit_does_not_raise(self, repo, tmp_path):
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        out = nodes.verify(state, rt)  # must not raise
        assert out is not None

    def test_it_escalates_to_a_human(self, repo, tmp_path):
        """Not the executor, and the reason is what is being committed.

        This commit carries what the *checks* rewrote, not the model's work.
        Handing that back to the executor asks it to fight the linter — which
        rewrites the same bytes on the next cycle — and when the retry budget
        runs out the planner inherits a hook it can do nothing about either.
        A commit hook is repository policy, in the same family as the setup
        command failing, and the only participant who can satisfy it is a
        person.
        """
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        assert nodes.verify(state, rt)["next_hop"] == "escalate"

    def test_the_hook_output_reaches_the_operator(self, repo, tmp_path):
        # The hook names the file and line, and that is the whole diagnosis.
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo, "app.py:3: trailing whitespace.")
        out = nodes.verify(state, rt)
        assert "app.py:3" in out["escalation_reason"]

    def test_the_reason_claims_no_authorship_it_cannot_check(self, repo, tmp_path):
        """It said "not the model's edits" and that was false.

        The executor commits its own work before verify — unless the same hook
        refused *that* commit, silently, which is what happened: the attempt's
        `executor-loop.json` recorded `commits: []` against 15 edits. The
        uncommitted set is then the stage's work and the checks' together, so
        an escalation asserting either is guessing at the one fact an operator
        will act on.
        """
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        reason = nodes.verify(state, rt)["escalation_reason"].lower()
        assert "hook" in reason
        assert "not the model's edits" not in reason
        assert "uncommitted" in reason

    def test_no_executor_retry_is_consumed(self, repo, tmp_path):
        # An escalation that also spends a retry would let a second, unrelated
        # failure arrive at the planner one attempt short.
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        out = nodes.verify(state, rt)
        assert "verify_attempt" not in out or out["verify_attempt"] == state.get(
            "verify_attempt", 0
        )

    def test_the_layer_is_recorded_as_checks(self, repo, tmp_path):
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        assert nodes.verify(state, rt)["failure_layer"] == "checks"

    def test_it_says_what_to_do_about_it(self, repo, tmp_path):
        # An escalation stops an unattended run. What it costs an operator is
        # decided by whether the message names the next move or only the
        # symptom — the work is in the tree and resuming re-commits it.
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        reason = nodes.verify(state, rt)["escalation_reason"].lower()
        assert "resume" in reason

    def test_the_work_is_left_in_the_tree(self, repo, tmp_path):
        # Nothing may be discarded to make the commit succeed. The next
        # attempt's own commit sweeps it up once the hook is satisfied.
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        nodes.verify(state, rt)
        assert (repo / "app.py").read_text() == "changed by a check\n"

    def test_a_commit_that_succeeds_still_goes_to_review(self, repo, tmp_path):
        # The control. Without it, a bug that made every commit look refused
        # would pass every test above.
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        assert nodes.verify(state, rt)["next_hop"] == "review"


class TestTheAttemptDoesNotReportSuccess:
    """A loop that could not record its work has not succeeded.

    Measured on the run that produced all of this: `executor-loop.json` held
    `commits: []` against `edits_applied: 15` and `in_loop_failures: []`. The
    attempt reported clean. `executorloop.py`'s own docstring calls "the
    executor committed before verify" a *guarantee* — the in-process loop
    cannot be killed mid-write, so `git.is_clean()` need not be consulted — and
    a commit hook falsifies it without a word to anyone.

    Escalated rather than retried, on the same grounds as the setup command:
    the hook will refuse the next attempt identically, and a broken environment
    is not a planning defect.
    """

    def test_the_result_carries_the_refusal(self, repo, tmp_path):
        from code_gantry.executor import ExecutionResult
        from code_gantry.executorloop import _commit_if_dirty
        from code_gantry.gitops import Git
        from code_gantry.config import Stage

        (repo / "app.py").write_text("work\n")
        refuse_commits(repo, "app.py:1: trailing whitespace.")
        out = ExecutionResult(ok=True)
        _commit_if_dirty(
            Git(repo),
            Stage(id="s", instruction="do", edit_files=["app.py"]),
            out,
        )
        assert out.commit_refused
        assert "app.py:1" in out.commit_refused

    def test_a_successful_commit_leaves_it_unset(self, repo, tmp_path):
        # The control: without it, a bug setting this always would pass above.
        from code_gantry.executor import ExecutionResult
        from code_gantry.executorloop import _commit_if_dirty
        from code_gantry.gitops import Git
        from code_gantry.config import Stage

        (repo / "app.py").write_text("work\n")
        out = ExecutionResult(ok=True)
        _commit_if_dirty(
            Git(repo),
            Stage(id="s", instruction="do", edit_files=["app.py"]),
            out,
        )
        assert not out.commit_refused
        assert out.commits

    def test_execute_escalates(self, repo, tmp_path):
        from test_nodes import StubExecutor

        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)

        original = ex.run_agent_stage

        def refusing(*a, **kw):
            out = original(*a, **kw)
            out.commit_refused = "app.py:1: trailing whitespace."
            return out

        ex.run_agent_stage = refusing
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "escalate"
        assert "app.py:1" in out["escalation_reason"]

    def test_it_does_not_consume_an_executor_retry(self, repo, tmp_path):
        from test_nodes import StubExecutor

        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        original = ex.run_agent_stage

        def refusing(*a, **kw):
            out = original(*a, **kw)
            out.commit_refused = "refused"
            return out

        ex.run_agent_stage = refusing
        out = nodes.execute(state, rt)
        assert out.get("verify_attempt", 0) == state.get("verify_attempt", 0)


class TestExecutorLoop:
    def test_a_refused_commit_is_logged_rather_than_swallowed(self, repo, tmp_path):
        """The comment claimed the log carried this and there was no log."""
        from code_gantry.executor import ExecutionResult
        from code_gantry.executorloop import _commit_if_dirty
        from code_gantry.gitops import Git
        from code_gantry.config import Stage

        (repo / "app.py").write_text("work\n")
        refuse_commits(repo)
        lines = []
        sha = _commit_if_dirty(
            Git(repo),
            Stage(id="s", instruction="do", edit_files=["app.py"]),
            ExecutionResult(ok=True),
            log=lines.append,
        )
        assert sha is None
        assert lines, "a refused commit must not be silent"
        assert "refused" in lines[0]

    def test_it_still_returns_none_rather_than_raising(self, repo, tmp_path):
        from code_gantry.executor import ExecutionResult
        from code_gantry.executorloop import _commit_if_dirty
        from code_gantry.gitops import Git
        from code_gantry.config import Stage

        (repo / "app.py").write_text("work\n")
        refuse_commits(repo)
        assert (
            _commit_if_dirty(
                Git(repo),
                Stage(id="s", instruction="do", edit_files=["app.py"]),
                ExecutionResult(ok=True),
            )
            is None
        )


class TestAskingTheHookBeforeCommitting:
    """The refusal is feedback, not an escalation, if you ask in time.

    `commit_refused` was the right answer to "the repository said no" and the
    wrong place to stop. Measured: an overnight run landed 16 stages and then
    ended on three lines of trailing whitespace in an `.erb` file — content no
    declared check could have repaired, because `_gate_cycle` commits the
    model's raw work *before* it runs `checks`, so the commit the hook rejects
    happens upstream of every autocorrecting tool the operator has.

    Asking the hook first turns that into an ordinary cycle of feedback. It is
    also general over whatever the hook checks, which is the reason not to
    special-case whitespace: a hook is operator policy and the next rule it
    grows is not ours to predict.

    Run through `git hook run`, so git invokes it exactly as a commit would —
    the "same command in both places, spelled the same way" property, for free,
    rather than a reimplementation that can disagree with the thing it stands
    for.
    """

    def test_it_reports_what_the_hook_said(self, repo, tmp_path):
        from code_gantry.gitops import Git

        refuse_commits(repo, message="line 62: trailing whitespace.")
        (repo / "app.py").write_text("x = 1   \n")
        got = Git(repo).run_pre_commit_hook()
        assert got is not None, "a hook is installed, so it must have run"
        ok, output = got
        assert not ok
        assert "line 62: trailing whitespace." in output

    def test_a_clean_tree_passes(self, repo, tmp_path):
        import subprocess

        from code_gantry.gitops import Git

        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        (hooks / "pre-commit").chmod(0o755)
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)],
            check=True, capture_output=True,
        )
        (repo / "app.py").write_text("x = 1\n")
        assert Git(repo).run_pre_commit_hook() == (True, "")

    def test_no_hook_is_not_a_failure(self, repo, tmp_path):
        # `git hook run` exits 1 with "cannot find a hook named pre-commit"
        # when there is none, which is indistinguishable from a refusal if you
        # read the exit code. Answered from the hook file's existence instead —
        # a fact about the repository rather than a label parsed out of an
        # error message.
        import subprocess

        from code_gantry.gitops import Git

        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", str(tmp_path / "none")],
            check=True, capture_output=True,
        )
        assert Git(repo).run_pre_commit_hook() is None

    def test_it_finds_the_hook_a_global_hookspath_points_at(self, repo, tmp_path):
        # The operator who hit this in production has `core.hooksPath` set
        # globally, so `.git/hooks` is ignored entirely. Resolving the path
        # ourselves would have to reimplement that; `git rev-parse --git-path`
        # already knows.
        import subprocess

        from code_gantry.gitops import Git

        elsewhere = tmp_path / "shared-hooks"
        elsewhere.mkdir()
        (elsewhere / "pre-commit").write_text("#!/bin/sh\necho far away >&2\nexit 1\n")
        (elsewhere / "pre-commit").chmod(0o755)
        stale = repo / ".git" / "hooks"
        stale.mkdir(parents=True, exist_ok=True)
        (stale / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
        (stale / "pre-commit").chmod(0o755)
        subprocess.run(
            ["git", "-C", str(repo), "config", "core.hooksPath", str(elsewhere)],
            check=True, capture_output=True,
        )
        (repo / "app.py").write_text("x = 1\n")
        ok, output = Git(repo).run_pre_commit_hook()
        assert not ok and "far away" in output

    def test_the_gate_hands_the_hook_a_staged_tree(self, repo, tmp_path):
        # A pre-commit hook reads the *index*, so an unstaged edit is invisible
        # to it and the gate would pass on work the commit then refuses.
        from code_gantry.gitops import Git

        refuse_commits(repo, message="staged content was seen")
        (repo / "app.py").write_text("x = 1   \n")
        ok, output = Git(repo).run_pre_commit_hook()
        assert not ok and "staged content was seen" in output


class TestTheGateLayer:
    def test_it_passes_when_there_is_no_hook(self, repo, tmp_path):
        from code_gantry import gates
        from code_gantry.gitops import Git

        assert gates.check_commit_hook(Git(repo)).ok

    def test_a_refusal_becomes_feedback_that_names_the_hook(self, repo, tmp_path):
        from code_gantry import gates
        from code_gantry.gitops import Git

        refuse_commits(repo, message="app.py:1: trailing whitespace.")
        (repo / "app.py").write_text("x = 1   \n")
        got = gates.check_commit_hook(Git(repo))
        assert not got.ok
        assert "app.py:1: trailing whitespace." in got.feedback
        # Attribution, in as many words. Handed a complaint with no author, a
        # model reads it as a test failure or a reviewer note and edits the
        # wrong thing.
        assert "hook" in got.feedback.lower()
        assert "commit" in got.summary.lower()
