"""What the planner is doing, while it is still doing it.

A derivation runs a tool loop of median 28 calls and up to 45, each an HTTP
round trip, and measured on this project it takes between 7 and 20 minutes. For
all of that it wrote nothing: `planner.json` is produced when the decision
completes, and the run log carried `[plan] deriving next stage` followed by
silence. Asked what a 19-minute derivation was doing, the only honest answers
were the elapsed time, that the process was blocked on I/O rather than
spinning, and that it held an established connection — nothing about the work.

This is the third artifact on this project written only at the end, after the
executor transcript and the startup banner. The planner's is the longest silent
window of the three.

The calls were already all logged; they were merely held until the end and then
emitted as one line. Streaming them is the same bytes earlier, so the dump is
replaced by a count rather than kept beside them — two renderings of one fact
is how the two drift, and there is a rule about that in this codebase already.
"""

import pytest

from orchestrator.repotools import ToolCall


class Reader:
    """Stands in for `RepoReader`: a ledger and nothing else."""

    def __init__(self):
        self.calls = []


def _planner(monkeypatch, turns, log):
    """A planner whose client returns `turns` in order."""
    from orchestrator.config import PlannerConfig
    from orchestrator.planner import AnthropicPlanner as Planner

    cfg = PlannerConfig(model="claude-opus-5")
    p = Planner.__new__(Planner)
    p.cfg = cfg
    p.log = log
    p.reader = Reader()
    p.semantic = None
    p.validate_stage_fields = None
    p._client = None
    return p


class TestEachCallIsLoggedWhenItHappens:
    def test_the_ledger_is_drained_turn_by_turn(self, monkeypatch):
        """The seam under test: calls made, then logged, before the next turn.

        Driven against the ledger rather than through a fake Anthropic client,
        because what is being pinned is that nothing accumulates — a client
        stub would prove the loop runs, not that it reports as it goes.
        """
        from orchestrator.planner import AnthropicPlanner as Planner

        lines = []
        p = _planner(monkeypatch, [], lines.append)

        seen = 0
        p.reader.calls.append(ToolCall(tool="search", detail="X in .", lines=3))
        seen = Planner._log_new_calls(p, seen)
        assert lines == ["[plan] search(X in .) -> 3 line(s)"]
        assert seen == 1

        # A second turn reports only what that turn added.
        p.reader.calls.append(ToolCall(tool="read_file", detail="a.rb:1-9", lines=9))
        seen = Planner._log_new_calls(p, seen)
        assert lines[-1] == "[plan] read_file(a.rb:1-9) -> 9 line(s)"
        assert len(lines) == 2
        assert seen == 2

    def test_nothing_new_logs_nothing(self, monkeypatch):
        from orchestrator.planner import AnthropicPlanner as Planner

        lines = []
        p = _planner(monkeypatch, [], lines.append)
        p.reader.calls.append(ToolCall(tool="search", detail="X", lines=1))
        seen = Planner._log_new_calls(p, 0)
        assert Planner._log_new_calls(p, seen) == seen
        assert len(lines) == 1

    def test_a_refusal_streams_as_a_refusal(self, monkeypatch):
        from orchestrator.planner import AnthropicPlanner as Planner

        lines = []
        p = _planner(monkeypatch, [], lines.append)
        p.reader.calls.append(
            ToolCall(tool="read_file", detail="gone.rb", lines=0, refusal="no such path")
        )
        Planner._log_new_calls(p, 0)
        assert lines == ["[plan] read_file(gone.rb) -> refused: no such path"]

    def test_a_planner_with_no_reader_is_silent(self, monkeypatch):
        # No repository access configured: the single-call planner it has
        # always been, and there is no ledger to drain.
        from orchestrator.planner import AnthropicPlanner as Planner

        lines = []
        p = _planner(monkeypatch, [], lines.append)
        p.reader = None
        assert Planner._log_new_calls(p, 0) == 0
        assert lines == []

    def test_no_log_is_not_an_error(self, monkeypatch):
        from orchestrator.planner import AnthropicPlanner as Planner

        p = _planner(monkeypatch, [], None)
        p.reader.calls.append(ToolCall(tool="search", detail="X", lines=1))
        assert Planner._log_new_calls(p, 0) == 1


class TestOneRenderingOfACall:
    """`_tool_log` and the live line must be the same string.

    Three copies of a format string is how the numbered-source renderer
    drifted while every test stayed green. These two describe the same ledger
    entry and are read side by side in the same file.
    """

    def test_the_streamed_line_matches_the_recorded_one(self, monkeypatch):
        from orchestrator.planner import AnthropicPlanner as Planner

        lines = []
        p = _planner(monkeypatch, [], lines.append)
        p.reader.calls.extend([
            ToolCall(tool="search", detail="X in .", lines=3),
            ToolCall(tool="read_file", detail="a.rb:1-9", lines=0, refusal="nope"),
        ])
        Planner._log_new_calls(p, 0)
        recorded = Planner._tool_log(p)
        assert [line.removeprefix("[plan] ") for line in lines] == recorded
