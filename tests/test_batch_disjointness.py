"""What makes a batch of stages from one derivation safe to run in order.

Step 10 lets the planner answer once with several stages. The saving is real —
the planner is $199 of a run against $3.30 for the executor, and a derivation
is 5 to 7 minutes of a ~13 minute stage — but a stage spec is a *prediction*,
and this project has paid for that twice: an unresolvable `read_excerpts` range
now fails the stage back to the planner, and "an authored edit stops being
satisfiable once part of it is already true on the branch" deadlocked a stage
into two redraws.

Stage 3 of 5 is drawn against a tree stages 1 and 2 have not touched yet. So
the batch is *constrained* rather than trusted, in this codebase's habit of
making the mistake unexpressible: **no stage's `edit_files` may intersect any
other batched stage's `edit_files`, `read_files` or `read_excerpts` paths.**
Under that rule nothing a batched stage does can invalidate a later one's spec,
because no later stage names anything an earlier one can write.

Intersection is decided by expanding globs against the repository's actual
files, not by comparing glob strings. Two different-looking globs can select
the same file and two similar-looking ones can select none in common; the only
honest question is which paths they resolve to.

Truncation rather than rejection: the longest safe prefix runs and the rest is
discarded, so heterogeneous work degrades to a single stage and today's
behaviour instead of failing.
"""

import pytest

from orchestrator.config import Excerpt, Stage


def _stage(sid, edit=(), read=(), excerpts=()):
    return Stage(
        id=sid,
        instruction="do it",
        edit_files=list(edit),
        read_files=list(read),
        read_excerpts=[Excerpt(path=p, start=1, end=2) for p in excerpts],
    )


TRACKED = [
    "app/a.rb", "app/b.rb", "app/c.rb",
    "spec/a_spec.rb", "spec/b_spec.rb",
    "config/routes.rb", "spec/support/helper.rb",
]


class TestDisjointBatchesSurvive:
    def test_stages_touching_different_files_all_run(self):
        from orchestrator.config import safe_batch_prefix

        kept, dropped = safe_batch_prefix(
            [_stage("one", edit=["app/a.rb"]), _stage("two", edit=["app/b.rb"])],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one", "two"]
        assert dropped == ""

    def test_a_single_stage_is_always_safe(self):
        from orchestrator.config import safe_batch_prefix

        kept, dropped = safe_batch_prefix([_stage("one", edit=["app/**"])], TRACKED)
        assert len(kept) == 1 and dropped == ""

    def test_an_empty_batch_is_empty(self):
        from orchestrator.config import safe_batch_prefix

        assert safe_batch_prefix([], TRACKED) == ([], "")

    def test_stages_may_read_the_same_untouched_file(self):
        # Reading in common is fine. Only writing is what invalidates a spec.
        from orchestrator.config import safe_batch_prefix

        kept, _ = safe_batch_prefix(
            [
                _stage("one", edit=["spec/a_spec.rb"], read=["config/routes.rb"]),
                _stage("two", edit=["spec/b_spec.rb"], read=["config/routes.rb"]),
            ],
            TRACKED,
        )
        assert len(kept) == 2


class TestAConflictTruncates:
    def test_two_stages_writing_the_same_file(self):
        from orchestrator.config import safe_batch_prefix

        kept, dropped = safe_batch_prefix(
            [_stage("one", edit=["app/a.rb"]), _stage("two", edit=["app/a.rb"])],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "two" in dropped and "app/a.rb" in dropped

    def test_a_later_stage_reading_what_an_earlier_one_writes(self):
        # The stale-spec case: stage two was drawn against the file as it is
        # now, and stage one is about to change it.
        from orchestrator.config import safe_batch_prefix

        kept, dropped = safe_batch_prefix(
            [
                _stage("one", edit=["config/routes.rb"]),
                _stage("two", edit=["app/a.rb"], read=["config/routes.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "config/routes.rb" in dropped

    def test_an_earlier_stage_reading_what_a_later_one_writes(self):
        # Symmetric, and the direction it is easy to forget: stage one is
        # drawn against a file stage two will rewrite. Running one first is
        # fine, but the batch is still unsafe if order ever changes, and the
        # rule is cheaper to state symmetrically than to reason about.
        from orchestrator.config import safe_batch_prefix

        kept, dropped = safe_batch_prefix(
            [
                _stage("one", edit=["app/a.rb"], read=["config/routes.rb"]),
                _stage("two", edit=["config/routes.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]

    def test_an_excerpt_quoting_what_another_stage_writes(self):
        """The case the constraint exists for.

        Nearly every batch in the live run quoted
        `spec/support/migrated_controller_inventory.rb`. A batch that also
        *edited* it would hand every later stage a range read at a commit that
        no longer describes the file.
        """
        from orchestrator.config import safe_batch_prefix

        kept, dropped = safe_batch_prefix(
            [
                _stage("one", edit=["spec/support/helper.rb"]),
                _stage("two", edit=["spec/a_spec.rb"], excerpts=["spec/support/helper.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "spec/support/helper.rb" in dropped

    def test_it_keeps_the_prefix_before_the_conflict(self):
        from orchestrator.config import safe_batch_prefix

        kept, _ = safe_batch_prefix(
            [
                _stage("one", edit=["app/a.rb"]),
                _stage("two", edit=["app/b.rb"]),
                _stage("three", edit=["app/a.rb"]),
                _stage("four", edit=["app/c.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one", "two"], "stops at the first conflict"


class TestGlobsAreResolvedNotCompared:
    def test_different_globs_selecting_the_same_file_conflict(self):
        # `app/*.rb` and `app/a.rb` share nothing as strings and everything as
        # files. Comparing the globs would call this batch safe.
        from orchestrator.config import safe_batch_prefix

        kept, _ = safe_batch_prefix(
            [_stage("one", edit=["app/*.rb"]), _stage("two", edit=["app/a.rb"])],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]

    def test_similar_globs_selecting_nothing_in_common_are_safe(self):
        from orchestrator.config import safe_batch_prefix

        kept, _ = safe_batch_prefix(
            [_stage("one", edit=["app/**"]), _stage("two", edit=["spec/**"])], TRACKED
        )
        assert [s.id for s in kept] == ["one", "two"]

    def test_a_glob_matching_no_tracked_file_conflicts_with_nothing(self):
        # A stage creating a new file names a path git has never seen. It
        # cannot collide with anything, and must not be treated as a wildcard.
        from orchestrator.config import safe_batch_prefix

        kept, _ = safe_batch_prefix(
            [
                _stage("one", edit=["spec/brand_new_spec.rb"]),
                _stage("two", edit=["spec/also_new_spec.rb"]),
            ],
            TRACKED,
        )
        assert len(kept) == 2

    def test_two_stages_creating_the_same_new_file_still_conflict(self):
        # Untracked, so glob expansion finds nothing — the literal paths have
        # to be compared as well, or the one case globs cannot see is missed.
        from orchestrator.config import safe_batch_prefix

        kept, dropped = safe_batch_prefix(
            [
                _stage("one", edit=["spec/brand_new_spec.rb"]),
                _stage("two", edit=["spec/brand_new_spec.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "brand_new_spec" in dropped


class TestTheReasonIsUsable:
    def test_it_names_the_stage_and_the_path(self):
        from orchestrator.config import safe_batch_prefix

        _kept, dropped = safe_batch_prefix(
            [
                _stage("first", edit=["config/routes.rb"]),
                _stage("second", edit=["app/a.rb"], read=["config/routes.rb"]),
            ],
            TRACKED,
        )
        assert "second" in dropped and "first" in dropped
        assert "config/routes.rb" in dropped
