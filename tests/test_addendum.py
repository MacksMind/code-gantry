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

from orchestrator.addendum import append_notes, append_outcome


def note(observation="8 of 9 controllers are clean", **over):
    n = {"plan_path": "docs/PLAN.md", "anchor": "", "observation": observation}
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
        assert "`search` found 0 remaining" in body
        # Which stage produced it, and nothing else about provenance. The sha
        # and the date belong to the commit this entry lands inside, and git
        # answers both — correctly after a rebase, where the same facts in
        # append-only prose would go quietly wrong.
        assert "plain-order-render-text" in body
        assert "UTC" not in body

    def test_records_what_the_plan_currently_claims(self, tmp_path):
        written = write(
            tmp_path, [note(anchor="checklist says 24 sites across 9 controllers")]
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
    so nothing could tell which was still true.

    Now the planner quotes a passage, the tool finds it, and the heading comes
    from the document. Two stages working the same section agree by
    construction rather than by the model phrasing it the same way twice.
    """

    def plan(self, path):
        return PLAN if path == "docs/PLAN.md" else None

    def test_the_header_and_the_lines_come_from_the_document(self, tmp_path):
        written = write(
            tmp_path,
            [note(observation="zero sites remain",
                  anchor="`before_filter` is deprecated. 152 sites.")],
            read_plan=self.plan, plan_sha="a3f19c2bcd45",
        )
        # Line 7 of PLAN, found by the tool rather than counted by the planner.
        assert "## Filter macros — `docs/PLAN.md#L7` @ `a3f19c2bcd45`" in written.read_text()

    def test_two_stages_quoting_one_section_get_one_heading(self, tmp_path):
        # The entire point. Different prose, different quotes from the same
        # section, and they must still collate.
        for anchor in ("`before_filter` is deprecated. 152 sites.",
                       "Line about something else."):
            written = write(
                tmp_path, [note(anchor=anchor)],
                read_plan=self.plan, plan_sha="a3f19c2bcd45",
            )
        headings = [
            line.split(" — ")[0] for line in written.read_text().splitlines()
            if line.startswith("## ")
        ]
        assert headings == ["## Filter macros", "## Filter macros"]

    def test_it_finds_the_section_the_quote_is_inside(self, tmp_path):
        written = write(
            tmp_path,
            [note(anchor="The gem must come out before Rails 5.")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "## Deprecated finders" in written.read_text()

    def test_a_quote_spanning_lines_gets_a_range(self, tmp_path):
        written = write(
            tmp_path,
            [note(anchor="`before_filter` is deprecated. 152 sites. Line about something else.")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "`docs/PLAN.md#L7-L9`" in written.read_text()

    def test_a_preamble_falls_under_the_documents_title(self, tmp_path):
        # Above every `##` but below the `#`. The title is the section it is
        # in, and a better answer than refusing to name one.
        written = write(
            tmp_path,
            [note(anchor="Preamble that belongs to no section.")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "## Rails 5 migration — `docs/PLAN.md#L3`" in written.read_text()

    def test_a_quote_that_is_not_there_never_costs_the_observation(self, tmp_path):
        """The entry is written regardless, with the problem named.

        Three defects on this project were plan notes computed correctly and
        lost on the way to disk. The progress record is what the next run reads
        to know what is done; a quotation the tool cannot find is worth a
        missing heading, not a missing fact.
        """
        written = write(
            tmp_path,
            [note(observation="zero remain", anchor="a passage that is nowhere in this document")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        text = written.read_text()
        assert "zero remain" in text
        assert "the quoted text was not found" in text

    def test_a_document_that_is_not_in_the_plan_commit(self, tmp_path):
        written = write(
            tmp_path,
            [note(plan_path="docs/NOPE.md", anchor="The gem must come out before Rails 5.")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        assert "not readable in the plan at this commit" in written.read_text()

    def test_a_literal_escape_becomes_the_character_it_meant(self, tmp_path):
        # A planner that double-escapes emits `\\u2014` in its JSON, which
        # decodes to a literal backslash-u and lands in committed Markdown
        # looking like a bug in this tool. Seen twice in one stage of fifty-two.
        written = write(
            tmp_path,
            [note(observation="Stage 1 \\u2192 Stage 2 is done \\u2014 fully")],
            read_plan=self.plan, plan_sha="a3f19c2",
        )
        text = written.read_text()
        assert "Stage 1 → Stage 2 is done — fully" in text
        assert "u2192" not in text


class TestTheFindingIsItsOwnLine:
    """What the planner found, as distinct from what the plan says.

    The heading is lifted from the plan document, which is right — two stages
    working one section then group together necessarily rather than usually.
    But a plan heading says what a passage is *about* and nothing about what
    this stage discovered, and before the heading was lifted that one line was
    the only summary an entry had. Reading a log of four hundred entries, the
    prose is the substance and the heading is a filing label; without this
    there is nothing in between.

    Optional. Not every observation has a one-line form worth separating from
    its prose, and an empty bullet is worse than an absent one.
    """

    def test_it_is_rendered_as_its_own_bullet(self, tmp_path):
        text = write(
            tmp_path,
            [note(finding="7 of 24 sites remain, all inline `<script>` renders")],
        ).read_text()
        assert "- **found** 7 of 24 sites remain, all inline `<script>` renders" in text

    def test_it_sits_after_the_plan_quote(self, tmp_path):
        # The plan first, then what this stage found about it — the order a
        # reader needs and the order the migrated entries already use.
        text = write(
            tmp_path, [note(anchor="**36** occurrences", finding="now 25")]
        ).read_text()
        assert text.index("the plan says") < text.index("found")

    def test_the_prose_still_follows(self, tmp_path):
        text = write(
            tmp_path, [note(observation="the sweep is complete", finding="0 remain")]
        ).read_text()
        assert text.index("found") < text.index("the sweep is complete")

    def test_an_absent_finding_renders_no_bullet(self, tmp_path):
        assert "**found**" not in write(tmp_path, [note()]).read_text()

    def test_an_empty_finding_renders_no_bullet(self, tmp_path):
        assert "**found**" not in write(tmp_path, [note(finding="   ")]).read_text()


class TestWhatLanded:
    """The one entry in this file that is a claim about the past.

    Everything else here is written before the work. The planner's notes are
    produced when a stage is *derived* and held until it lands; the reviewer's
    observations are about code the stage did not touch. Nothing recorded what
    the stage actually did — and the planner reads this log back as history on
    every later derivation, so an intention published on landing became the
    account of record.

    Observed: an entry saying a controller "now permits" two fields "with a
    two-shop controller-spec example reading the values back from the
    database", written before a line of it existed. The stage happened to
    deliver it. The log had no way to know that.
    """

    def test_it_records_the_reviewers_words(self, tmp_path):
        target = append_outcome(
            tmp_path,
            "log.md",
            stage_id="drop-whitelists",
            summary="Removes the three declarations and adds a spec that reads "
            "the persisted value back.",
        )
        text = target.read_text()
        assert "## What `drop-whitelists` landed" in text
        assert "reads the persisted value back" in text

    def test_it_says_when_it_was_written(self, tmp_path):
        # The distinction the file exists to make. A reader has to be able to
        # tell a report from a prediction, and the two sit next to each other.
        text = append_outcome(
            tmp_path, "log.md", stage_id="s", summary="did the thing"
        ).read_text()
        assert "**reviewed** after the diff was written" in text

    def test_a_stage_with_no_review_writes_nothing(self, tmp_path):
        # Nothing landed, so there is nothing to report.
        assert append_outcome(tmp_path, "log.md", stage_id="s", summary="") is None
        assert append_outcome(tmp_path, "log.md", stage_id="s", summary="  ") is None

    def test_planner_notes_no_longer_claim_to_be_observations_of_landing(
        self, tmp_path
    ):
        text = write(tmp_path, [note(finding="something")]).read_text()
        assert "**observed** while planning" in text
        assert "while landing" not in text
