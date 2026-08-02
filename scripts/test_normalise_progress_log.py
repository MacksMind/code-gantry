"""Rewriting a progress log into one shape, so its reader needs only one.

The entry format changed three times while the first long run was in flight,
and the file carries all of them: 199 entries in the current shape, 78 citing
plan text by line number under a `supersedes` bullet, 31 with no structured
citation at all, 3 transitional, and 1 recording a citation that would not
resolve. A consumer that has to interpret five shapes is five times as likely
to misread one, so the file is normalised once and the reader learns one.

Two rules govern every case here.

**Prose is never lost.** The observation is the thing worth keeping; the
citation is how you find what it is about. A normalisation that dropped a note
because its citation could not be resolved would discard the only part that
cannot be recomputed.

**A citation is derived, never trusted.** The old entries carry line numbers a
model wrote, and model-written line numbers are what the current format exists
to avoid — the first three under that scheme each named a real file and a real
in-range span pointing somewhere else entirely. Where an old entry also quotes
the text it is citing, that quote is an anchor and `locate` can find where it
actually is. Where it does not, the entry becomes explicitly unattributed
rather than silently mis-attributed.
"""

import pytest

from normalise_progress_log import (
    Entry,
    normalise_document,
    normalise_entry,
    parse_document,
    split_citations,
)

PLAN = """\
# Upgrade plan

## Mechanical conversions

Some preamble that is not the anchor.

- `render nothing: true` needs **36** occurrences across **18** controllers
- something else entirely

## Later section

More text.
"""


def read_plan(sha, path):
    return PLAN if path == "docs/plan.md" else None


CURRENT = """\
## Mechanical conversions — `docs/plan.md#L3` @ `abc1234`

- **observed** while landing `some-stage`
- **the plan says** ## Mechanical conversions

The prose of the observation.
"""

V1 = """\
## `render nothing: true` → `head :ok` (**36** sites)

- **observed** after `reconcile` landed as `a458cba2f283` (2026-07-31 14:37 UTC)
- **supersedes** docs/plan.md:189-190 ("**36** occurrences across **18** controllers"), docs/other.md:52 ("something else entirely")

The prose of the observation.
"""

V0 = """\
## `before_filter` → `before_action` — **152 filter macros total**

- **observed** while landing `reconcile`

The prose of the observation.
"""

V2 = """\
## Mechanical conversions — `docs/plan.md#L3` @ `abc1234`

- **observed** while landing `application-record-models-batch-3`
- **supersedes** ## Mechanical conversions

The prose of the observation.
"""


class TestParsing:
    def test_splits_on_entry_headings_and_keeps_the_preamble(self):
        doc = parse_document("# Plan addendum\n\nblurb\n\n" + CURRENT + "\n" + V1)
        assert doc.preamble.startswith("# Plan addendum")
        assert len(doc.entries) == 2

    def test_an_entry_carries_its_header_bullets_and_prose(self):
        doc = parse_document(CURRENT)
        entry = doc.entries[0]
        assert entry.header.startswith("Mechanical conversions")
        assert entry.bullets["observed"] == "while landing `some-stage`"
        assert entry.prose.strip() == "The prose of the observation."

    def test_a_document_with_no_entries_survives(self):
        doc = parse_document("# Plan addendum\n\nnothing yet\n")
        assert doc.entries == []
        assert "nothing yet" in doc.preamble

    def test_prose_containing_a_hash_is_not_mistaken_for_an_entry(self):
        # Only a `## ` at the start of a line opens an entry, and the prose of
        # a migration log is full of Ruby comments and Markdown fragments.
        doc = parse_document(CURRENT.rstrip() + "\n\nA line mentioning ## inline.\n")
        assert len(doc.entries) == 1
        assert "## inline" in doc.entries[0].prose


class TestClassification:
    @pytest.mark.parametrize(
        "text,expected",
        [(CURRENT, "current"), (V1, "v1"), (V0, "v0"), (V2, "v2")],
    )
    def test_each_shape_is_recognised(self, text, expected):
        assert parse_document(text).entries[0].shape == expected


class TestCitationSplitting:
    def test_pulls_the_path_and_the_quoted_anchor(self):
        cites = split_citations(
            'docs/plan.md:189-190 ("**36** occurrences"), docs/other.md:52 ("x")'
        )
        assert cites[0] == ("docs/plan.md", "**36** occurrences")
        assert cites[1] == ("docs/other.md", "x")

    def test_an_anchor_below_the_minimum_length_does_not_resolve(self):
        # `locate` refuses anything under 12 characters, because a short quote
        # matches in too many places to mean anything. A citation carrying one
        # is therefore uncheckable, exactly like a citation carrying none.
        from normalise_progress_log import Entry, normalise_entry

        entry = Entry(
            header="H",
            bullets={
                "observed": "while landing `s`",
                "supersedes": 'docs/plan.md:1 ("**36**")',
            },
            prose="p\n",
        )
        assert normalise_entry(entry, read_plan).startswith("## (unattributed)")

    def test_a_citation_without_a_quote_still_yields_its_path(self):
        assert split_citations("docs/plan.md:189-190 and its table row at :263") == [
            ("docs/plan.md", "")
        ]

    def test_nothing_citable_yields_nothing(self):
        assert split_citations("some prose with no path in it") == []


class TestNormalisingCurrentEntries:
    def test_a_current_entry_is_returned_byte_identical(self):
        # The 199 that are already right must not be churned. Rewriting them
        # would put the whole file in a diff and hide the entries that changed.
        doc = parse_document(CURRENT)
        assert normalise_entry(doc.entries[0], read_plan) == CURRENT

    def test_the_unresolved_citation_form_is_also_left_alone(self):
        text = CURRENT.replace(
            "- **the plan says**",
            "- **citation** unresolved: the quoted text was not found\n"
            "- **the plan says**",
        )
        doc = parse_document(text)
        assert normalise_entry(doc.entries[0], read_plan) == text


class TestNormalisingV2:
    def test_the_bullet_is_renamed_and_nothing_else_moves(self):
        # It already has the derived ref; only the bullet key predates the
        # rename. Re-deriving would risk changing a citation that is correct.
        doc = parse_document(V2)
        out = normalise_entry(doc.entries[0], read_plan)
        assert "- **the plan says** ## Mechanical conversions" in out
        assert "supersedes" not in out
        assert "`docs/plan.md#L3` @ `abc1234`" in out


class TestNormalisingV1:
    def test_the_quoted_anchor_is_located_and_becomes_the_reference(self):
        doc = parse_document(V1)
        out = normalise_entry(doc.entries[0], read_plan)
        # The anchor is on line 7 of PLAN; the model claimed 189-190.
        assert "`docs/plan.md#L7`" in out
        assert "189" not in out.splitlines()[0]

    def test_the_heading_is_lifted_from_the_document(self):
        doc = parse_document(V1)
        out = normalise_entry(doc.entries[0], read_plan)
        assert out.splitlines()[0].startswith("## Mechanical conversions — ")

    def test_the_sha_is_carried_over_from_the_observed_line(self):
        doc = parse_document(V1)
        assert "@ `a458cba2f283`" in normalise_entry(doc.entries[0], read_plan)

    def test_the_observed_bullet_is_rewritten_to_the_current_wording(self):
        doc = parse_document(V1)
        out = normalise_entry(doc.entries[0], read_plan)
        assert "- **observed** while landing `reconcile`" in out

    def test_the_anchor_becomes_the_plan_says_bullet(self):
        doc = parse_document(V1)
        out = normalise_entry(doc.entries[0], read_plan)
        assert "- **the plan says** **36** occurrences across **18** controllers" in out

    def test_the_prose_survives_verbatim(self):
        doc = parse_document(V1)
        assert "The prose of the observation." in normalise_entry(
            doc.entries[0], read_plan
        )

    def test_the_original_header_is_preserved_when_it_is_replaced(self):
        # The lifted heading names the section; the planner's original title
        # said what it found there. Losing it would lose the only summary the
        # entry has.
        doc = parse_document(V1)
        out = normalise_entry(doc.entries[0], read_plan)
        assert "`render nothing: true` → `head :ok` (**36** sites)" in out

    def test_an_unfindable_quote_becomes_unattributed_rather_than_wrong(self):
        def missing(sha, path):
            return "# Plan\n\nnothing matching here at all.\n"

        doc = parse_document(V1)
        out = normalise_entry(doc.entries[0], missing)
        assert out.startswith("## (unattributed)")
        assert "- **citation** unresolved" in out
        assert "The prose of the observation." in out

    def test_a_missing_document_becomes_unattributed(self):
        doc = parse_document(V1)
        out = normalise_entry(doc.entries[0], lambda sha, path: None)
        assert out.startswith("## (unattributed)")
        assert "The prose of the observation." in out

    def test_the_second_citation_is_tried_when_the_first_does_not_resolve(self):
        def only_other(sha, path):
            return PLAN if path == "docs/other.md" else None

        text = V1.replace('("**36** occurrences across **18** controllers")', '("no")')
        doc = parse_document(text)
        out = normalise_entry(doc.entries[0], only_other)
        assert "docs/other.md" in out


class TestNormalisingV0:
    def test_it_becomes_unattributed_with_its_prose_intact(self):
        # No path, no quote, nothing to resolve. Inventing a citation from the
        # header text would be exactly the mis-attribution this avoids.
        doc = parse_document(V0)
        out = normalise_entry(doc.entries[0], read_plan)
        assert out.startswith("## (unattributed)")
        assert "The prose of the observation." in out

    def test_the_original_header_survives(self):
        doc = parse_document(V0)
        out = normalise_entry(doc.entries[0], read_plan)
        assert "**152 filter macros total**" in out

    def test_the_stage_is_kept(self):
        doc = parse_document(V0)
        assert "while landing `reconcile`" in normalise_entry(doc.entries[0], read_plan)


class TestNormalisingTheWholeDocument:
    def test_the_preamble_and_entry_order_are_preserved(self):
        text = "# Plan addendum\n\nblurb\n\n" + V1 + "\n" + CURRENT
        out, _ = normalise_document(text, read_plan)
        assert out.startswith("# Plan addendum\n\nblurb")
        assert out.index("render nothing") < out.index("The prose of the observation.")

    def test_every_entry_survives(self):
        text = "# Plan addendum\n\n" + "\n".join([V1, V0, CURRENT, V2])
        out, report = normalise_document(text, read_plan)
        assert out.count("\n## ") == 4
        assert report.total == 4

    def test_the_report_counts_what_happened(self):
        text = "# Plan addendum\n\n" + "\n".join([V1, V0, CURRENT, V2])
        _, report = normalise_document(text, read_plan)
        assert report.unchanged == 1          # CURRENT
        assert report.resolved == 1           # V1, via its quoted anchor
        assert report.renamed == 1            # V2
        assert report.unattributed == 1       # V0, nothing to cite
        assert report.total == 4

    def test_it_is_idempotent(self):
        # It will be run more than once — during a pause, then again after the
        # next batch of entries. A second pass must be a no-op, or the file
        # churns and every re-run looks like a change.
        text = "# Plan addendum\n\n" + "\n".join([V1, V0, CURRENT, V2])
        once, _ = normalise_document(text, read_plan)
        twice, report = normalise_document(once, read_plan)
        assert twice == once
        assert report.unchanged == report.total

    def test_no_prose_is_lost_across_the_whole_document(self):
        text = "# Plan addendum\n\n" + "\n".join([V1, V0, CURRENT, V2])
        out, _ = normalise_document(text, read_plan)
        assert out.count("The prose of the observation.") == 4

    def test_the_result_parses_as_one_shape(self):
        text = "# Plan addendum\n\n" + "\n".join([V1, V0, CURRENT, V2])
        out, _ = normalise_document(text, read_plan)
        shapes = {e.shape for e in parse_document(out).entries}
        assert shapes == {"current"}, f"a consumer would still see {shapes}"


class TestEntryIsDataNotBehaviour:
    def test_an_entry_can_be_built_directly(self):
        # Keeps the parser testable apart from the file it usually reads.
        entry = Entry(
            header="H", bullets={"observed": "while landing `s`"}, prose="p\n"
        )
        assert entry.shape == "v0"


class TestSayingWhyACitationFailed:
    """The reason is the useful part when the location is unrecoverable.

    On the project this was written for, 107 of 312 entries cannot be cited at
    all: 65 quote nothing, 42 cite by line number only, and the handful that do
    quote something name documents that a plan reorganisation deleted. None of
    that is recoverable, so the entry should say which of those happened rather
    than a single flat "unresolved" that reads like a bug.
    """

    def test_a_reorganised_document_says_so(self):
        text = V1.replace("docs/other.md:52", "docs/gone.md:52")
        doc = parse_document(text)
        out = normalise_entry(doc.entries[0], lambda sha, path: None)
        assert "does not exist at this revision" in out
        assert "reorganised" in out

    def test_a_line_only_citation_says_it_was_never_checkable(self):
        entry = Entry(
            header="H",
            bullets={
                "observed": "while landing `s`",
                "supersedes": "docs/plan.md:189-190 and its table row at :263",
            },
            prose="p\n",
        )
        out = normalise_entry(entry, read_plan)
        assert "by line number only" in out

    def test_no_citation_at_all_says_it_predates_them(self):
        doc = parse_document(V0)
        assert "predates derived citations" in normalise_entry(doc.entries[0], read_plan)
