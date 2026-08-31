"""A runner's report is clipped toward its end, because that is where it answers.

`truncate_middle` splits the budget evenly on the reasoning that "command
output is informative at both ends" — the invocation and early errors at the
top, the summary at the bottom. True of a command that fails immediately. False
of a test runner and a linter, which both draw progress first and report last.

Measured on 495 test-failure feedbacks actually handed to an executor: 96% were
truncated, 53% still carried `N examples, M failures`, and only **20%** still
carried the `Failed examples:` block — the rerun commands, which are the most
actionable thing RSpec prints and live in the last few hundred bytes.

This codebase has already paid for the same defect one layer up. A suite emitted
334,143 characters with that block 146,285 characters from the end; the even
split dropped it, the flake gate found no failing files, fell back to re-running
everything, tripped an order-dependent spec, and reset a stage the reviewer had
approved. The fix then was to move truncation to the point of use. It left the
*weighting* alone, and the weighting is the half that was still biting.

RuboCop gains less and the tests say so, so that nobody reads more into this
than it does: its offence blocks are a median 257 bytes, so a 4,000-character
budget holds about fifteen however they are arranged. What the tail buys there
is the summary line and one unbroken run instead of two halves with a hole.
"""

from code_gantry.commands import (
    clip_for_model,
    clip_report_for_model,
    truncate_middle,
    truncate_to_tail,
)

# The shape RSpec actually emits, in the order it emits it.
RSPEC = (
    "Running 8 groups\n"
    + "." * 400
    + "F"
    + "." * 300
    + "\n\nFailures:\n\n"
    + "".join(
        f"  {i}) AdminController GET #index renders\n"
        f"     Failure/Error: expect(response).to have_http_status(:ok)\n"
        f"       expected the response to have status code :ok but it was 500\n"
        f"     # ./spec/requests/admin_spec.rb:{i * 7}:in `block (3 levels)'\n\n"
        for i in range(1, 40)
    )
    + "Finished in 41.2 seconds\n"
    + "312 examples, 39 failures\n\n"
    + "Failed examples:\n\n"
    + "".join(
        f"rspec ./spec/requests/admin_spec.rb:{i * 7} # AdminController renders\n"
        for i in range(1, 40)
    )
)

RUBOCOP = (
    "Inspecting 2041 files\n"
    + "." * 1800
    + "C"
    + "." * 140
    + "\n\nOffenses:\n\n"
    + "".join(
        f"spec/models/thing_{i}_spec.rb:{i}:121: C: "
        f"{'[Corrected] ' if i % 2 else ''}Layout/LineLength: Line is too long. [151/120]\n"
        f"        some_key_{i}: 'a fairly long literal standing in for real source',\n"
        "   " + "^" * 30 + "\n"
        for i in range(1, 30)
    )
    + "2041 files inspected, 29 offenses detected\n"
)

BUDGET = 4_000


class TestTheRerunListSurvives:
    """The single most actionable thing RSpec prints, and it is last."""

    def test_the_old_weighting_drops_it(self):
        # Not a hypothetical: this is the 80% case in the recorded runs, and it
        # is asserted here so the change has a measured baseline to move.
        old = clip_for_model(RSPEC, BUDGET)
        assert "Failed examples:" not in old

    def test_the_new_weighting_keeps_it(self):
        new = clip_report_for_model(RSPEC, BUDGET)
        assert "Failed examples:" in new
        assert "312 examples, 39 failures" in new

    def test_it_keeps_rerun_commands_a_model_can_use(self):
        new = clip_report_for_model(RSPEC, BUDGET)
        assert new.count("rspec ./spec/requests/admin_spec.rb:") >= 10


class TestTheSummaryLineSurvives:
    def test_rubocop_keeps_its_count(self):
        # 4 of 40 truncated RuboCop failures lost this line entirely, so the
        # model could not tell it was seeing a fraction.
        new = clip_report_for_model(RUBOCOP, BUDGET)
        assert "29 offenses detected" in new

    def test_rubocop_shows_no_fewer_offences_than_before(self):
        # The honest claim. Tail-weighting does not fit *more* offences into a
        # fixed budget — it stops them being split around a hole and keeps the
        # count. A test asserting a big gain here would be asserting something
        # the measurement does not support.
        import re

        pat = re.compile(r"^spec/models/thing_\d+_spec\.rb:", re.M)
        old = len(pat.findall(clip_for_model(RUBOCOP, BUDGET)))
        new = len(pat.findall(clip_report_for_model(RUBOCOP, BUDGET)))
        assert new >= old


class TestTheHeadIsStillThere:
    def test_the_first_line_survives(self):
        # Measured at 143 characters median across 255 complete listings, so
        # keeping it costs almost nothing and losing it would leave the model
        # unable to see what ran.
        assert clip_report_for_model(RUBOCOP, BUDGET).startswith("Inspecting 2041 files")
        assert clip_report_for_model(RSPEC, BUDGET).startswith("Running 8 groups")

    def test_the_head_is_one_line_and_not_several(self):
        text = "first\nsecond\nthird\n" + "x" * 10_000
        assert truncate_to_tail(text, 500).startswith("first\n...")

    def test_no_raw_progress_run_survives_clipping(self):
        out = clip_report_for_model(RUBOCOP, BUDGET)
        assert "." * 100 not in out

    def test_the_collapse_still_happens_where_it_is_observable(self):
        # Under tail-weighting the collapsed marker itself lands in the dropped
        # middle, so the truncating case cannot show that collapse ran. It is
        # still load-bearing and this is where it shows: output short enough to
        # survive whole, which is most output. It also keeps `path_hints` from
        # backtracking quadratically over an unbroken run.
        short = "Inspecting 3 files\n" + "." * 900 + "\n1 offense detected\n"
        out = clip_report_for_model(short, BUDGET)
        assert "repeated characters" in out
        assert "." * 100 not in out
        assert "1 offense detected" in out


class TestTheTailIsWholeLines:
    def test_it_does_not_open_mid_line(self):
        text = "head\n" + "".join(f"line {i} with some content\n" for i in range(500))
        out = truncate_to_tail(text, 400)
        body = out.split("...\n", 1)[1]
        assert body.startswith("line ")

    def test_one_enormous_line_is_not_discarded_to_find_a_boundary(self):
        # Half of a long line beats none of it, so the boundary search gives up
        # rather than eating the whole tail.
        text = "head\n" + "y" * 10_000
        out = truncate_to_tail(text, 500)
        assert "y" * 100 in out


class TestItHonoursTheBudget:
    def test_the_result_fits(self):
        for text in (RSPEC, RUBOCOP, "z" * 100_000):
            assert len(truncate_to_tail(text, BUDGET)) <= BUDGET

    def test_short_text_is_untouched(self):
        assert truncate_to_tail("all of it", 4_000) == "all of it"

    def test_it_says_how_much_went(self):
        assert "characters truncated" in truncate_to_tail("q" * 9_000, 500)


class TestTheOtherClipperIsUnchanged:
    """Prose and diffs still keep both ends — the shapes differ, not the taste."""

    def test_middle_truncation_still_keeps_the_head(self):
        out = truncate_middle("A" * 500 + "B" * 500, 200)
        assert out.startswith("A")
        assert out.rstrip().endswith("B")

    def test_a_reviewer_note_keeps_its_opening(self):
        # A note is prose: its point is its first sentence. Tail-weighting it
        # would be the same mistake in the other direction.
        note = "The change is wrong because " + "detail. " * 2_000
        assert clip_for_model(note, 300).startswith("The change is wrong because")


class TestTheGateActuallyUsesIt:
    """The seam, tested end to end — because testing the helper is not testing
    the caller.

    Written after a falsification pass failed to fail: reverting `gates.clip`
    to the middle-weighted clipper broke nothing, because every test above
    calls `clip_report_for_model` directly. Four of this codebase's recorded
    defects are a value computed correctly and lost in transit, and each of
    them passed its unit tests. A helper with no caller test is that shape
    waiting to happen.
    """

    def test_gate_feedback_keeps_the_rerun_list(self):
        from code_gantry import gates

        out = gates.clip(RSPEC)
        assert "Failed examples:" in out
        assert "312 examples, 39 failures" in out

    def test_check_feedback_keeps_the_offence_count(self):
        from code_gantry import gates

        assert "29 offenses detected" in gates.clip(RUBOCOP)

    def test_it_is_still_bounded_by_the_shared_budget(self):
        from code_gantry import gates

        assert len(gates.clip("x" * 200_000)) <= gates.FEEDBACK_OUTPUT_CHARS
