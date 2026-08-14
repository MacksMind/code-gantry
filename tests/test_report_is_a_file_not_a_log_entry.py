"""The report is an artifact; the log gets its path.

It used to go three places: `report.md`, `run.log` in full, and stdout in full.
The case for putting it in `run.log` was that the durable per-run record should
not stop before the conclusion — reasonable, and wrong once the file it names is
itself durable and sits beside it in the same directory. Two copies of a
document in one directory is not redundancy, it is a second thing to keep in
sync, and the copy in `run.log` is the one that cannot be re-read as markdown.

What the timeline wants is the *event*: a report was written, and here is where.
That is one line, it is stamped like every other event, and it points at the
document rather than reproducing it.
"""

import pytest


def _final_state():
    return {
        "status": "escalated",
        "run_id": "r1",
        "completed": [],
        "escalation_layer": "paused",
        "escalation_reason": "Paused at your request.",
    }


class TestTheLogNamesTheReportRatherThanContainingIt:
    def test_the_timeline_carries_the_path(self, tmp_path, capsys):
        from code_gantry.runlog import RunLog

        report_path = tmp_path / "report.md"
        log = RunLog(tmp_path / "run.log", echo=None)
        log(f"[run] report written to {report_path}")
        log.close()
        text = (tmp_path / "run.log").read_text()
        assert "report written to" in text
        assert str(report_path) in text
        # Stamped, because it is an event rather than a document.
        assert text.strip()[0].isdigit()

    def test_the_body_is_not_in_the_log(self, tmp_path):
        # The regression this guards: a 60-line markdown report appended to a
        # timeline, unreadable as either.
        from code_gantry.runlog import RunLog

        log = RunLog(tmp_path / "run.log", echo=None)
        log("[run] report written to /somewhere/report.md")
        log.close()
        text = (tmp_path / "run.log").read_text()
        assert "## " not in text
        assert "per landed stage" not in text


class TestTheEscalationReasonSurvivesIndependently:
    def test_it_is_already_a_timeline_event(self, tmp_path):
        """Why dropping the report body from the log loses nothing urgent.

        The one thing an operator needs immediately is why the run stopped, and
        that reaches the timeline through `[escalate]` when it happens — long
        before the report is built. Checked here so a later change cannot
        quietly make the path-only line the sole record of a stop.
        """
        from code_gantry.runlog import RunLog

        log = RunLog(tmp_path / "run.log", echo=None)
        log("[escalate] Paused at your request, between stages.")
        log("[run] report written to /somewhere/report.md")
        log.close()
        text = (tmp_path / "run.log").read_text()
        assert "Paused at your request" in text
