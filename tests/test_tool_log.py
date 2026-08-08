"""A second log, for what the three roles read, kept out of the timeline.

`run.log` is documented as "a human-readable timeline of node transitions and
decisions", and its reader is a person reconstructing why a run stopped. The
planner's reads alone are ~25 lines a decision, which roughly doubles it and
turns the timeline into a feed.

But the reads have to be somewhere live: a derivation runs 3 to 20 minutes and
a review up to a minute, and until they returned there was nothing to look at.
The executor already worked this way — its transcript is a per-attempt file,
because sixty calls a cycle could never go in a shared timeline — so the answer
is the executor's answer applied to all three.

One file per run rather than per decision, because that is what `tail -f`
wants: a per-decision file means finding the new one each time. And no terminal
echo, because the terminal is where the timeline goes.
"""

from pathlib import Path

import pytest

from orchestrator.repotools import ToolCall


class Reader:
    def __init__(self, calls=None):
        self.calls = calls if calls is not None else []


class TestTheSinkItself:
    def test_it_appends_across_openings(self, tmp_path):
        # A resumed run continues one file, for the same reason `run.log` does:
        # truncating discards the history of why it paused.
        from orchestrator.runlog import RunLog

        for message in ("first", "second"):
            log = RunLog(tmp_path / "tools.log", echo=None)
            log(f"[plan] {message}")
            log.close()
        body = (tmp_path / "tools.log").read_text()
        assert "first" in body and "second" in body

    def test_it_does_not_echo_to_the_terminal(self, tmp_path, capsys):
        from orchestrator.runlog import RunLog

        log = RunLog(tmp_path / "tools.log", echo=None)
        log("[plan] quiet")
        log.close()
        captured = capsys.readouterr()
        assert "quiet" not in captured.out + captured.err


class TestEachRoleWritesThere:
    def _bound(self, tmp_path):
        """A planner and a reviewer with a tool log bound, as `build_runtime` does."""
        from orchestrator.config import PlannerConfig, ReviewerConfig
        from orchestrator.planner import AnthropicPlanner
        from orchestrator.reviewer import OpenAIReviewer
        from orchestrator.runlog import RunLog

        sink = RunLog(tmp_path / "tools.log", echo=None)
        timeline = []

        p = AnthropicPlanner.__new__(AnthropicPlanner)
        p.cfg = PlannerConfig(model="claude-opus-5")
        p.log, p.tool_log, p.reader, p.semantic = timeline.append, sink, Reader(), None

        r = OpenAIReviewer.__new__(OpenAIReviewer)
        r.cfg = ReviewerConfig(model="gpt-5.6-sol")
        r.log, r.tool_log, r.reader, r.semantic = timeline.append, sink, Reader(), None
        return p, r, sink, timeline

    def test_the_planners_reads_go_to_the_sink_not_the_timeline(self, tmp_path):
        from orchestrator.planner import AnthropicPlanner

        p, _r, sink, timeline = self._bound(tmp_path)
        p.reader.calls.append(ToolCall(tool="search", detail="X in .", lines=3))
        AnthropicPlanner._log_new_calls(p, 0)
        sink.close()

        assert "search(X in .) -> 3 line(s)" in (tmp_path / "tools.log").read_text()
        assert timeline == [], "the timeline must stay a timeline"

    def test_the_reviewers_reads_go_there_too(self, tmp_path):
        from orchestrator.reviewer import OpenAIReviewer

        _p, r, sink, timeline = self._bound(tmp_path)
        r.reader.calls.append(ToolCall(tool="read_file", detail="a.rb:1-9", lines=9))
        OpenAIReviewer._log_new_calls(r, 0)
        sink.close()

        assert "read_file(a.rb:1-9) -> 9 line(s)" in (tmp_path / "tools.log").read_text()
        assert timeline == []

    def test_both_roles_are_distinguishable_in_one_file(self, tmp_path):
        # One file for three roles is only useful if you can tell them apart.
        from orchestrator.planner import AnthropicPlanner
        from orchestrator.reviewer import OpenAIReviewer

        p, r, sink, _t = self._bound(tmp_path)
        p.reader.calls.append(ToolCall(tool="search", detail="P", lines=1))
        r.reader.calls.append(ToolCall(tool="search", detail="R", lines=1))
        AnthropicPlanner._log_new_calls(p, 0)
        OpenAIReviewer._log_new_calls(r, 0)
        sink.close()

        body = (tmp_path / "tools.log").read_text()
        assert "[plan] search(P)" in body
        assert "[review] search(R)" in body

    def test_without_a_sink_it_falls_back_to_the_run_log(self, tmp_path):
        # A planner built outside `build_runtime` — the tests do this — must
        # not lose its reporting because nothing bound the second log.
        from orchestrator.planner import AnthropicPlanner

        timeline = []
        p = AnthropicPlanner.__new__(AnthropicPlanner)
        p.log, p.tool_log, p.reader, p.semantic = timeline.append, None, Reader(), None
        p.reader.calls.append(ToolCall(tool="search", detail="X", lines=1))
        AnthropicPlanner._log_new_calls(p, 0)
        assert timeline == ["[plan] search(X) -> 1 line(s)"]


class TestTheTimelineIsOutputNotDiagnostics:
    """The run log echoes to stdout; errors keep stderr.

    It echoed to stderr, so `orchestrator run <project> > out.txt` captured the
    final report and lost the entire timeline — the timeline being the only
    thing the command actually produces while it works. stderr is for what went
    wrong, and `cli.py` already uses `click.echo(..., err=True)` for that in
    fourteen places; nothing was left for stdout but the last few lines.

    The report no longer goes into `run.log` at all. It was put there on the
    argument that a durable per-run record should not stop before the
    conclusion, and that was answered by the file it names: `report.md` is
    equally durable and sits in the same directory, so the copy in the timeline
    was a second thing to keep in sync — and the one that cannot be re-read as
    markdown, since a report interleaved with stamped events is neither. The
    timeline gets the event instead: a report was written, and where.

    `record` survives for the start banner, which is a document-shaped line that
    must not carry a clock: it *is* the header the clocked lines follow.
    """

    def test_the_timeline_goes_to_stdout(self, tmp_path, capsys):
        from orchestrator.runlog import RunLog

        log = RunLog(tmp_path / "run.log")
        log("[plan] deriving next stage")
        log.close()
        captured = capsys.readouterr()
        assert "deriving next stage" in captured.out
        assert "deriving next stage" not in captured.err

    def test_a_file_only_write_does_not_echo(self, tmp_path, capsys):
        # So the report can reach `run.log` without appearing twice on the
        # terminal, now that the timeline shares stdout with it.
        from orchestrator.runlog import RunLog

        log = RunLog(tmp_path / "run.log")
        log.record("## Report\n\nlanded 3 stages")
        log.close()
        captured = capsys.readouterr()
        assert captured.out == "" and captured.err == ""
        assert "landed 3 stages" in (tmp_path / "run.log").read_text()

    def test_a_file_only_write_is_not_timestamped(self, tmp_path):
        # A markdown report prefixed line by line with a clock is not a report.
        from orchestrator.runlog import RunLog

        log = RunLog(tmp_path / "run.log")
        log.record("## Report")
        log.close()
        assert (tmp_path / "run.log").read_text().startswith("## Report")
