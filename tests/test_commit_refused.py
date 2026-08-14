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
