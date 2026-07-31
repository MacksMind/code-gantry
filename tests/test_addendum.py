"""The append-only record of what the work turned out to be.

Plan documents are written before the work and go stale during it. On the first
long run the operator hand-wrote a paragraph of `planner.guidance` describing
three landed stages, because a fresh run starts with an empty history and would
otherwise re-derive work already done. That paragraph is the thing this
replaces — written by the planner from what it read, rather than by a human
from memory.

What it is not: a plan edit. Nothing here changes a plan document. A later pass
folds these observations in, which is a judgement about what the work has become
and does not belong in the middle of doing it.
"""

from pathlib import Path

from orchestrator.addendum import append_notes


def note(step="item 17", observation="8 of 9 controllers are clean", **over):
    n = {"plan_step": step, "observation": observation, "supersedes": ""}
    n.update(over)
    return n


def write(tmp_path, notes, path="docs/addendum", **over):
    kw = {"stage_id": "plain-order-render-text", "when": "2026-07-31 12:00 UTC"}
    kw.update(over)
    return append_notes(tmp_path, path, notes, **kw)


class TestWritingIsOptional:
    def test_no_configured_path_writes_nothing(self, tmp_path):
        assert write(tmp_path, [note()], path=None) is None
        assert not list(tmp_path.rglob("*.md"))

    def test_no_notes_writes_nothing(self, tmp_path):
        # Most calls have none. A note is for when the plan and the repository
        # disagree, not for narrating every stage.
        assert write(tmp_path, []) is None
        assert not list(tmp_path.rglob("*.md"))


class TestContent:
    def test_records_the_observation_and_where_it_came_from(self, tmp_path):
        written = write(tmp_path, [note(observation="`search` found 0 remaining")])
        body = written.read_text()
        assert "item 17" in body
        assert "`search` found 0 remaining" in body
        # Attribution: which stage, and when. Without it a reader cannot tell
        # an observation from an assertion.
        #
        # No commit sha, deliberately: the entry is written on the stage branch
        # before the squash, so it lands *inside* the commit it describes. That
        # commit's sha does not exist yet, and citing it from within itself
        # would be circular anyway.
        assert "plain-order-render-text" in body
        assert "2026-07-31 12:00 UTC" in body

    def test_records_what_the_plan_currently_claims(self, tmp_path):
        written = write(
            tmp_path, [note(supersedes="checklist says 24 sites across 9 controllers")]
        )
        assert "24 sites across 9 controllers" in written.read_text()

    def test_the_header_says_it_is_not_a_plan_change(self, tmp_path):
        # A reader finding this inside the plan directory must not mistake it
        # for the plan.
        body = write(tmp_path, [note()]).read_text()
        assert "not changes to the plan" in body


class TestAppendOnly:
    def test_later_notes_do_not_disturb_earlier_ones(self, tmp_path):
        write(tmp_path, [note(observation="first observation")])
        write(tmp_path, [note(observation="second observation")], stage_id="later")
        body = (tmp_path / "docs" / "addendum" / "plan-addendum.md").read_text()
        assert "first observation" in body
        assert "second observation" in body
        assert body.index("first observation") < body.index("second observation")

    def test_a_wrong_note_is_corrected_by_a_later_one_not_a_rewrite(self, tmp_path):
        # The record is the record. Correction happens in the open.
        write(tmp_path, [note(observation="9 controllers clean")])
        write(tmp_path, [note(observation="miscounted: 8 clean, order_controller remains")])
        body = (tmp_path / "docs" / "addendum" / "plan-addendum.md").read_text()
        assert "9 controllers clean" in body
        assert "miscounted" in body

    def test_the_preamble_is_written_once(self, tmp_path):
        write(tmp_path, [note()])
        write(tmp_path, [note()])
        body = (tmp_path / "docs" / "addendum" / "plan-addendum.md").read_text()
        assert body.count("# Plan addendum") == 1


class TestPathHandling:
    def test_a_directory_collects_notes_in_a_file(self, tmp_path):
        written = write(tmp_path, [note()], path="docs/plan/addendum")
        assert written == tmp_path / "docs" / "plan" / "addendum" / "plan-addendum.md"

    def test_a_markdown_path_is_used_as_the_file(self, tmp_path):
        written = write(tmp_path, [note()], path="docs/plan/notes.md")
        assert written == tmp_path / "docs" / "plan" / "notes.md"

    def test_missing_directories_are_created(self, tmp_path):
        assert not (tmp_path / "docs").exists()
        written = write(tmp_path, [note()], path="docs/deep/nested/addendum")
        assert written.exists()
