"""The read budget is per step, and one field of it was never reset.

`plan()` clears `calls` and `_lines_used` before each decision, with a comment
saying why: "Reset per decision, not per run: the log line answers what did it
look at to draw *this* stage". `max_total_chars` was added underneath
`max_total_lines` later — because a line is not a unit of size — and the reset
was never taught about it. So `_chars_used` accumulated for the life of the
process, and once it passed the configured ceiling **every planner call was
refused on its first read, permanently**.

Measured on one live run: 31 planner calls, 14 of which asked for something and
got nothing. The transition is a cliff, not a slope — stage 025 got 3 of 7
reads, and every call after it got zero. The planner correctly reported that it
could not read and misdiagnosed the cause as its context being too large, then
recommended folding the log, which would not have moved this by a byte.

The reviewer had it worse: it never reset anything at all. Its `calls` list
accumulated across every review in a run, so `review.json` recorded 685 calls
for a review that made a handful, the per-review line and call budgets were
really per-run, and reviews late in a run were starved by reads their
predecessors had done.

Hence `reset()` on the reader rather than three assignments at two call sites.
A reset that has to be *remembered* field by field is the thing that has now
gone wrong twice: once when a field was added, once when a whole call site was
written without one.
"""

import pytest

from orchestrator.repotools import ReadBudget, RepoReader, Spend, ToolError


def _reader(tmp_path, **budget):
    from orchestrator.gitops import Git

    defaults = dict(
        max_lines_per_call=100,
        max_total_lines=1000,
        max_total_chars=200,
        max_calls=50,
    )
    defaults.update(budget)
    return RepoReader(Git(tmp_path), tmp_path, ReadBudget(**defaults))


class TestResetClearsEveryCounter:
    def test_it_clears_the_character_counter(self, tmp_path):
        # The one that was missed, and the only one whose absence is invisible
        # until a long run crosses the ceiling.
        r = _reader(tmp_path)
        r.spend.chars = 10_000
        r.spend = Spend()
        assert r.spend.chars == 0

    def test_it_clears_the_line_counter(self, tmp_path):
        r = _reader(tmp_path)
        r.spend.lines = 999
        r.spend = Spend()
        assert r.spend.lines == 0

    def test_it_clears_the_ledger(self, tmp_path):
        from orchestrator.repotools import ToolCall

        r = _reader(tmp_path)
        r.calls.append(ToolCall(tool="read_file", detail="a", lines=1))
        r.spend = Spend()
        assert r.calls == []

    def test_the_ledger_is_replaced_not_emptied(self, tmp_path):
        """This assertion is the reverse of what it used to be, deliberately.

        The first fix cleared the list in place, because `SemanticSearch` was
        handed `reader.calls` and rebinding would have left it appending to an
        orphan. That constraint is what forced the field-by-field clearing this
        whole file exists to remove — so it was cut the other way: the reader
        replaces its `Spend` wholesale, and `SemanticSearch` reaches through the
        reader for the current ledger instead of holding a list.

        Pinned because in-place clearing would still pass every other test here
        while quietly reintroducing the shape.
        """
        from orchestrator.repotools import ToolCall

        r = _reader(tmp_path)
        before = r.calls
        r.calls.append(ToolCall(tool="search", detail="x", lines=1))
        r.spend = Spend()
        assert r.calls is not before
        assert r.calls == []

    def test_a_reader_that_reset_can_read_again(self, tmp_path):
        (tmp_path / "f.txt").write_text("x" * 500)
        r = _reader(tmp_path, max_total_chars=200)
        r.spend.chars = 10_000
        with pytest.raises(ToolError):
            r._charge_call("read_file")
        r.spend = Spend()
        r._charge_call("read_file")  # must not raise


class TestBothRolesReset:
    """Pinned at the call sites, because that is where it went wrong twice."""

    def test_the_planner_resets_before_each_decision(self, tmp_path):
        from orchestrator.planner import AnthropicPlanner

        planner = AnthropicPlanner.__new__(AnthropicPlanner)
        planner.reader = _reader(tmp_path)
        planner.semantic = None
        planner.reader.spend.chars = 10_000
        planner.reader.spend.lines = 500

        AnthropicPlanner._reset_reads(planner)
        assert planner.reader.spend.chars == 0
        assert planner.reader.spend.lines == 0

    def test_the_reviewer_resets_before_each_review(self, tmp_path):
        from orchestrator.reviewer import OpenAIReviewer

        reviewer = OpenAIReviewer.__new__(OpenAIReviewer)
        reviewer.reader = _reader(tmp_path)
        reviewer.semantic = None
        reviewer.reader.spend.chars = 10_000

        OpenAIReviewer._reset_reads(reviewer)
        assert reviewer.reader.spend.chars == 0

    def test_a_role_without_repo_access_does_not_raise(self, tmp_path):
        # `repo_access` off leaves `reader` as None on both roles.
        from orchestrator.planner import AnthropicPlanner
        from orchestrator.reviewer import OpenAIReviewer

        for cls in (AnthropicPlanner, OpenAIReviewer):
            role = cls.__new__(cls)
            role.reader = None
            role.semantic = None
            cls._reset_reads(role)
