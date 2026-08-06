"""Dispatch is where a refusal becomes visible.

The reader raises; the caller records. `dispatch` is the only place that knows
both the tool's name and the arguments it was asked for, which is what a
refusal has to be labelled with — `_resolve` and `_require_readable` raise
without either.
"""

from __future__ import annotations

import subprocess

import pytest

from orchestrator.gitops import Git
from orchestrator.plannertools import dispatch
from orchestrator.repotools import ReadBudget, RepoReader


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app").mkdir(parents=True)
    (r / "app" / "order.rb").write_text("class Order\nend\n")

    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=r, check=True)
    return r


def reader(repo, **over):
    return RepoReader(Git(repo), repo, ReadBudget(**over) if over else ReadBudget())


class TestRefusalsAreRecorded:
    def test_an_exhausted_budget_is_recorded(self, repo):
        r = reader(repo, max_calls=1)
        dispatch("list_files", {}, r, None)
        out = dispatch("list_files", {}, r, None)

        assert "cannot do that" in out
        assert [c.refusal != "" for c in r.calls] == [False, True]
        assert "too many" in r.calls[1].refusal

    def test_a_bad_path_is_recorded_with_the_path_asked_for(self, repo):
        """The detail is the question, not the answer.

        A refused read has no content to describe it, so the only thing that
        makes it legible afterwards is what was asked for.
        """
        r = reader(repo)
        dispatch("read_file", {"path": "app/ghost.rb"}, r, None)

        assert len(r.calls) == 1
        assert r.calls[0].tool == "read_file"
        assert r.calls[0].detail == "app/ghost.rb"
        assert "does not exist" in r.calls[0].refusal

    def test_an_answered_call_carries_no_refusal(self, repo):
        r = reader(repo)
        dispatch("read_file", {"path": "app/order.rb"}, r, None)

        assert r.calls[0].refusal == ""
        assert r.calls[0].lines > 0

    def test_a_refused_search_is_labelled_with_its_pattern(self, repo):
        r = reader(repo)
        dispatch("search", {"pattern": ""}, r, None)

        assert r.calls[0].tool == "search"
        assert "no search pattern" in r.calls[0].refusal
