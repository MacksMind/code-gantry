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
    kw = {"stage_id": "plain-order-render-text"}
    kw.update(over)
    return append_notes(tmp_path, path, notes, **kw)


class TestWritingIsOptional:
    def test_no_configured_path_writes_nothing(self, tmp_path):
        assert write(tmp_path, [note()], path=None) is None
        assert not list(tmp_path.rglob("*.md"))

    def test_no_notes_writes_nothing(self, tmp_path):
        # A stage that advances a plan step should carry an entry, but not
        # every stage maps to one, and an empty note is worse than none.
        assert write(tmp_path, []) is None
        assert not list(tmp_path.rglob("*.md"))


class TestContent:
    def test_records_the_observation_and_where_it_came_from(self, tmp_path):
        written = write(tmp_path, [note(observation="`search` found 0 remaining")])
        body = written.read_text()
        assert "item 17" in body
        assert "`search` found 0 remaining" in body
        # Which stage produced it, and nothing else about provenance. The sha
        # and the date belong to the commit this entry lands inside, and git
        # answers both — correctly after a rebase, where the same facts in
        # append-only prose would go quietly wrong.
        assert "plain-order-render-text" in body
        assert "UTC" not in body

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


PLAN = """# Rails 5 migration

Preamble that belongs to no section.

## Filter macros

`before_filter` is deprecated. 152 sites.

Line about something else.

## Deprecated finders

The gem must come out before Rails 5.
"""


class TestTheHeadingIsLiftedFromThePlan:
    """Entries group by the plan section they are about, not by their wording.

    The log this replaces had one heading repeated seventeen times — the
    superseded plan text, quoted verbatim by seventeen stages sweeping one
    item. Identical, and distinguishable only by a count buried in the prose,
    so nothing could tell which was still true. Keying on a line reference and
    lifting the heading from the document makes stages working the same section
    agree by construction rather than by the model phrasing it the same way
    twice.
    """

    def plan(self, path):
        return PLAN if path == "docs/PLAN.md" else None

    def test_the_header_comes_from_the_document(self, tmp_path):
        written = write(
            tmp_path,
            [note(observation="zero sites remain", plan_ref="docs/PLAN.md#L7-L9")],
            read_plan=self.plan,
            plan_sha="a3f19c2bcd45",
        )
        text = written.read_text()
        assert "## Filter macros — `docs/PLAN.md#L7-L9` @ `a3f19c2bcd45`" in text

    def test_two_stages_citing_one_section_get_one_heading(self, tmp_path):
        # The entire point. These are written by separate stages with different
        # prose, and they must still collate.
        for observation in ("seven remain", "zero remain"):
            written = write(
                tmp_path,
                [note(observation=observation, plan_ref="docs/PLAN.md#L9")],
                read_plan=self.plan,
                plan_sha="a3f19c2bcd45",
            )
        headings = [
            line for line in written.read_text().splitlines()
            if line.startswith("## ")
        ]
        assert len(headings) == 2
        assert headings[0] == headings[1]

    def test_it_finds_the_section_the_line_is_inside(self, tmp_path):
        written = write(
            tmp_path,
            [note(plan_ref="docs/PLAN.md#L13")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "## Deprecated finders" in written.read_text()

    def test_a_preamble_falls_under_the_documents_title(self, tmp_path):
        # Line 3 sits above every `##` but below the `#`. The title is the
        # section it is in, and is a better answer than refusing to name one.
        written = write(
            tmp_path,
            [note(step="", plan_ref="docs/PLAN.md#L3")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "## Rails 5 migration — `docs/PLAN.md#L3`" in written.read_text()

    def test_a_bad_citation_never_costs_the_observation(self, tmp_path):
        """The entry is written regardless, with the problem named.

        Three defects on this project were plan notes computed correctly and
        lost on the way to disk. The progress record is what the next run reads
        to know what is done; a malformed line reference is worth a missing
        header, not a missing fact.
        """
        written = write(
            tmp_path,
            [note(observation="zero remain", plan_ref="docs/PLAN.md#L900")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        text = written.read_text()
        assert "zero remain" in text
        assert "citation" in text and "past the end" in text

    def test_an_unparseable_reference_says_so(self, tmp_path):
        written = write(
            tmp_path,
            [note(observation="zero remain", plan_ref="item 17, the filter bit")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "zero remain" in written.read_text()
        assert "not a `path#Lstart-Lend` reference" in written.read_text()

    def test_a_document_that_is_not_in_the_plan_commit(self, tmp_path):
        written = write(
            tmp_path,
            [note(plan_ref="docs/NOPE.md#L1-L2")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "not readable in the plan at this commit" in written.read_text()


class TestTheCitationAndTheQuotationMustAgree:
    """A reference is only better than a quotation because it is checkable.

    Nothing checked it. The first three notes written under the reference
    format all cited a real file and a real in-range line span, and all three
    pointed somewhere else — one aimed the `ApplicationRecord` item at a
    `render text:` bullet forty lines away. Every one passed the only test
    there was, "does this range exist".

    The note carries the evidence to catch it: `supersedes` is what the planner
    says the plan states, `plan_ref` is where it says so, and they have to
    agree.
    """

    PLAN = (
        "# Plan\n\n## Models\n\n"
        "Add `ApplicationRecord` base class, migrate models in batches.\n\n"
        "## Rendering\n\n"
        "`render text:` becomes `render plain:` across 9 controllers.\n"
    )

    def plan(self, path):
        return self.PLAN if path == "docs/PLAN.md" else None

    def test_a_quote_from_another_section_is_flagged(self, tmp_path):
        written = write(
            tmp_path,
            [note(
                plan_ref="docs/PLAN.md#L8-L9",
                observation="the sweep is done",
                supersedes="Add `ApplicationRecord` base class, migrate models in batches.",
            )],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        text = written.read_text()
        assert "reference and the quotation disagree" in text
        assert "the sweep is done" in text, "the observation survives regardless"

    def test_a_quote_that_is_there_passes(self, tmp_path):
        written = write(
            tmp_path,
            [note(
                plan_ref="docs/PLAN.md#L5",
                supersedes="Add `ApplicationRecord` base class, migrate models in batches.",
            )],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "disagree" not in written.read_text()

    def test_rewording_and_emphasis_do_not_trip_it(self, tmp_path):
        # The failure being caught is gross. Flagging a lightly reworded quote
        # would train whoever folds these to ignore the warning.
        written = write(
            tmp_path,
            [note(
                plan_ref="docs/PLAN.md#L5",
                supersedes="add **ApplicationRecord** base class, migrate models in batches",
            )],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "disagree" not in written.read_text()

    def test_an_ellipsis_splits_the_quote(self, tmp_path):
        written = write(
            tmp_path,
            [note(
                plan_ref="docs/PLAN.md#L3-L5",
                supersedes="## Models ... migrate models in batches.",
            )],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "disagree" not in written.read_text()

    def test_a_note_with_no_quotation_is_not_flagged(self, tmp_path):
        # `supersedes` is optional — the plan may simply be silent. With
        # nothing to compare, there is no disagreement to report.
        written = write(
            tmp_path,
            [note(plan_ref="docs/PLAN.md#L5", supersedes="")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "disagree" not in written.read_text()
