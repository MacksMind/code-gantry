"""Dispatch is where a refusal becomes visible.

The reader raises; the caller records. `dispatch` is the only place that knows
both the tool's name and the arguments it was asked for, which is what a
refusal has to be labelled with — `_resolve` and `_require_readable` raise
without either.
"""

from __future__ import annotations

import subprocess

import pytest

from code_gantry.gitops import Git
from code_gantry.plannertools import dispatch
from code_gantry.repotools import ReadBudget, RepoReader


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


class TestCallDetailNamesTheRangeToo:
    """The other half of the ledger, so one call is named one way.

    `RepoReader` records the range it served; `call_detail` names a call the
    reviewer and executor render and the one a *refusal* is recorded under.
    A refused read has no served range — there is nothing to serve — so the
    only range it can carry is the one that was asked for, which is also the
    only thing worth knowing about it.

    Left alone for every other tool. `search` and `list_files` take no range,
    and `call_detail` picks whichever single argument names the call.
    """

    def test_a_ranged_read_carries_the_range(self):
        from code_gantry.plannertools import call_detail

        assert call_detail({"path": "a.rb", "start": 5, "end": 9}) == "a.rb:5-9"

    def test_an_open_ended_range_says_so(self):
        from code_gantry.plannertools import call_detail

        assert call_detail({"path": "a.rb", "start": 5}) == "a.rb:5-"
        assert call_detail({"path": "a.rb", "end": 9}) == "a.rb:-9"

    def test_a_whole_file_stays_the_bare_path(self):
        from code_gantry.plannertools import call_detail

        assert call_detail({"path": "a.rb"}) == "a.rb"

    def test_a_null_range_is_a_whole_file(self):
        # Strict mode makes every property required and optional ones
        # nullable, so an omitted range arrives as an explicit null.
        from code_gantry.plannertools import call_detail

        assert call_detail({"path": "a.rb", "start": None, "end": None}) == "a.rb"

    def test_other_tools_are_untouched(self):
        from code_gantry.plannertools import call_detail

        assert call_detail({"pattern": "render", "glob": "app/**"}) == "render"
        assert call_detail({"glob": "app/**"}) == "app/**"


class TestSemanticNamesBothQuestionsItAnswers:
    """It described one use, and the other is the one that pays.

    The description called sweep completeness — "have I found every kind of
    this?" — its best use. That is an enumeration question. The case that
    demonstrated the tool's value was a mechanism question: "how are admin
    controller action permissions determined, and where are allowed action
    names configured?" returned `Admin::ApplicationController#authorized?`,
    `User#has_authority_to` and `Role`, none of them named in the question and
    no one `search` reaching all three without already knowing the names.

    Worth the words because the alternative is not a slower search, it is not
    finding it. And because a model reaches for a tool on the strength of its
    description alone: nothing carries a good result from one run into the
    next, so improving what the index returns cannot make anything ask for it
    more often. Only this string can.
    """

    def _text(self):
        from code_gantry.plannertools import SEMANTIC_TOOL

        return SEMANTIC_TOOL["description"].lower()

    def test_it_names_the_enumeration_question(self):
        assert "every kind of this" in self._text()

    def test_it_names_the_mechanism_question(self):
        text = self._text()
        assert "how does this work" in text or "how it works" in text
        assert "chain" in text or "several files" in text or "spread over" in text

    def test_the_limits_survive(self):
        # All three were expensive to learn and none of them changed.
        text = self._text()
        assert "not an existence check" in text
        assert "pointer" in text
        assert "not evidence" in text

    def test_it_still_says_to_prefer_search_for_a_known_name(self):
        assert "if you can spell" in self._text()

    def test_it_carries_no_project_vocabulary(self):
        # This ships to every project's planner and reviewer. The example that
        # prompted the change is a Rails one and must not travel with it.
        text = self._text()
        for word in ("rails", "ruby", "controller", "role", "rspec", ".rb", "gem"):
            assert word not in text, f"{word!r} is project knowledge in a tool description"
