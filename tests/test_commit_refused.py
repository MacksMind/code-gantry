"""A repository that refuses a commit must not end the run.

Measured, not hypothesised: a run three stages deep died at
`nodes.verify`'s `commit_all` with a `GitError` traceback, because a
commit hook rejected the staged content. The cause was ours — the editor
normalises line endings on write, so a CRLF file came back as a whole-file
rewrite and every pre-existing trailing space became an *added* line for the
hook to find — but the shape is general. A hook is operator policy, it can fire
on anything, and the orchestrator's answer to "the repository said no" cannot be
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

from orchestrator import nodes
from orchestrator.gitops import GitError

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

    def test_it_routes_to_the_executor(self, repo, tmp_path):
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        assert nodes.verify(state, rt)["next_hop"] == "execute"

    def test_the_hook_output_reaches_the_executor(self, repo, tmp_path):
        # The hook names the file and line. Feedback that omits it leaves the
        # model guessing at a gate it cannot see.
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo, "app.py:3: trailing whitespace.")
        out = nodes.verify(state, rt)
        assert any(
            "app.py:3" in note for note in (out.get("review_feedback") or [])
        )

    def test_the_feedback_says_it_is_not_one_of_the_stage_gates(
        self, repo, tmp_path
    ):
        # Handed an unattributed complaint, a model reads it as its own gate
        # failing and re-runs the work rather than fixing the file.
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        out = nodes.verify(state, rt)
        assert any(
            "hook" in note.lower() for note in (out.get("review_feedback") or [])
        )

    def test_the_layer_is_recorded_as_checks(self, repo, tmp_path):
        cfg, rt, state = self._dirty_stage(repo, tmp_path)
        refuse_commits(repo)
        assert nodes.verify(state, rt)["failure_layer"] == "checks"

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


class TestExecutorLoop:
    def test_a_refused_commit_is_logged_rather_than_swallowed(self, repo, tmp_path):
        """The comment claimed the log carried this and there was no log."""
        from orchestrator.executor import ExecutionResult
        from orchestrator.executorloop import _commit_if_dirty
        from orchestrator.gitops import Git
        from orchestrator.config import Stage

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
        from orchestrator.executor import ExecutionResult
        from orchestrator.executorloop import _commit_if_dirty
        from orchestrator.gitops import Git
        from orchestrator.config import Stage

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
