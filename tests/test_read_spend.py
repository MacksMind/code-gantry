"""The reader's spent budget is one replaceable structure, not N fields.

The defect: `plan()` cleared `calls` and zeroed `_lines_used`. `max_total_chars`
was added underneath `max_total_lines` later, nothing taught the reset about it,
and the character counter then accumulated for the life of the process — every
planner call past the ceiling refused on its first read, 14 of 31 on one
measured run, each drawing a stage with no way to check a premise against the
code.

The fix is not a tidier reset. A reset that names its fields is a list somebody
maintains, and the next counter added is one more line to forget in a place
whose omission stays invisible until a long run crosses a ceiling. So the spent
state is a single object and clearing it is replacing it: whatever `Spend`
grows, a new one starts empty.

The one hazard that came with it is pinned below. `SemanticSearch` used to be
handed `reader.calls` — the list itself — so the two shared one ledger and the
tool log stayed chronological. Replacing the container rebinds that list, and a
semantic object still holding the old one would keep appending to an orphan:
every semantic call missing from the log, in order-dependent ways, with nothing
raising. It reaches through the reader now.
"""

import pytest

from code_gantry.repotools import ReadBudget, RepoReader, Spend, ToolCall


def _reader(tmp_path, **over):
    from code_gantry.gitops import Git

    budget = dict(
        max_lines_per_call=100, max_total_lines=1000,
        max_total_chars=200, max_calls=50,
    )
    budget.update(over)
    return RepoReader(Git(tmp_path), tmp_path, ReadBudget(**budget))


class TestOneStructure:
    def test_replacing_it_clears_everything_it_holds(self, tmp_path):
        r = _reader(tmp_path)
        r.spend.lines = 500
        r.spend.chars = 90_000
        r.spend.calls.append(ToolCall(tool="read_file", detail="a", lines=1))
        r.spend = Spend()
        assert (r.spend.lines, r.spend.chars, r.spend.calls) == (0, 0, [])

    def test_a_new_counter_is_cleared_without_anyone_remembering(self):
        # The property that makes this structural rather than tidier. Every
        # field of a fresh Spend is at its default, whatever fields it has.
        from dataclasses import fields

        fresh, used = Spend(), Spend()
        used.lines, used.chars = 7, 7
        used.calls.append(ToolCall(tool="search", detail="x", lines=1))
        for f in fields(Spend):
            assert getattr(fresh, f.name) == getattr(Spend(), f.name), f.name

    def test_the_reader_reads_its_counters_through_the_structure(self, tmp_path):
        (tmp_path / "f.txt").write_text("x" * 500)
        r = _reader(tmp_path, max_total_chars=200)
        r.spend.chars = 10_000
        with pytest.raises(Exception):
            r._charge_call("read_file")
        r.spend = Spend()
        r._charge_call("read_file")  # a fresh structure spends nothing


class TestTheSharedLedgerSurvivesReplacement:
    """The hazard the container introduced, and the reason for reaching through."""

    def test_semantic_search_sees_the_current_ledger(self, tmp_path):
        from code_gantry.semantic import SemanticSearch, SemanticSearchConfig

        r = _reader(tmp_path)
        s = SemanticSearch(
            SemanticSearchConfig(api_base="http://x", qdrant_url="http://q", embedding_model="m", collection="c"), reader=r
        )
        r.spend = Spend()
        s.calls.append(ToolCall(tool="semantic_search", detail="q", lines=1))
        assert len(r.spend.calls) == 1, "the semantic call must land in the reader"

    def test_the_two_are_the_same_list_not_a_copy(self, tmp_path):
        from code_gantry.semantic import SemanticSearch, SemanticSearchConfig

        r = _reader(tmp_path)
        s = SemanticSearch(
            SemanticSearchConfig(api_base="http://x", qdrant_url="http://q", embedding_model="m", collection="c"), reader=r
        )
        assert s.calls is r.spend.calls
        r.spend = Spend()
        assert s.calls is r.spend.calls, "it must follow the replacement"

    def test_semantic_search_still_works_with_no_reader(self, tmp_path):
        # Every existing test constructs it bare, and the executor's semantic
        # tool is built the same way.
        from code_gantry.semantic import SemanticSearch, SemanticSearchConfig

        s = SemanticSearch(SemanticSearchConfig(api_base="http://x", qdrant_url="http://q", embedding_model="m", collection="c"))
        s.calls.append(ToolCall(tool="semantic_search", detail="q", lines=1))
        assert len(s.calls) == 1


class TestTheSpendIsVisibleFromOutside:
    """A budget whose consumption is never printed cannot be seen to leak.

    The counter climbed past its ceiling for a whole run and the only outward
    sign was the planner saying, in prose, that it could not read. Printing
    what a step spent makes the next leak a number that stops going back to
    zero, and answers the tuning question besides: whether a ceiling is
    anywhere near binding in ordinary operation.
    """

    def _role(self, tmp_path, chars):
        from types import SimpleNamespace

        r = _reader(tmp_path, max_total_chars=800_000)
        r.spend.chars = chars
        return SimpleNamespace(reader=r)

    def test_it_reports_used_and_limit(self, tmp_path):
        from code_gantry.nodes import _spent

        assert _spent(self._role(tmp_path, 120_000)) == " (120k/800k chars)"

    def test_a_fresh_step_reports_zero(self, tmp_path):
        # What the fix looks like from outside: this number returns to zero
        # every step. If it climbs across steps, the reset is not happening.
        from code_gantry.nodes import _spent

        assert _spent(self._role(tmp_path, 0)) == " (0k/800k chars)"

    def test_a_role_without_repo_access_prints_nothing(self, tmp_path):
        from types import SimpleNamespace

        from code_gantry.nodes import _spent

        assert _spent(SimpleNamespace(reader=None)) == ""
