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
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
            [_stage("one", edit=["app/a.rb"]), _stage("two", edit=["app/b.rb"])],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one", "two"]
        assert dropped == []

    def test_a_single_stage_is_always_safe(self):
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages([_stage("one", edit=["app/**"])], TRACKED)
        assert len(kept) == 1 and dropped == []

    def test_an_empty_batch_is_empty(self):
        from orchestrator.config import orthogonal_stages

        assert orthogonal_stages([], TRACKED) == ([], [])

    def test_stages_may_read_the_same_untouched_file(self):
        # Reading in common is fine. Only writing is what invalidates a spec.
        from orchestrator.config import orthogonal_stages

        kept, _ = orthogonal_stages(
            [
                _stage("one", edit=["spec/a_spec.rb"], read=["config/routes.rb"]),
                _stage("two", edit=["spec/b_spec.rb"], read=["config/routes.rb"]),
            ],
            TRACKED,
        )
        assert len(kept) == 2


class TestAConflictTruncates:
    def test_two_stages_writing_the_same_file(self):
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
            [_stage("one", edit=["app/a.rb"]), _stage("two", edit=["app/a.rb"])],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "two" in dropped[0] and "app/a.rb" in dropped[0]

    def test_a_later_stage_reading_what_an_earlier_one_writes(self):
        # The stale-spec case: stage two was drawn against the file as it is
        # now, and stage one is about to change it.
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
            [
                _stage("one", edit=["config/routes.rb"]),
                _stage("two", edit=["app/a.rb"], read=["config/routes.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "config/routes.rb" in dropped[0]

    def test_an_earlier_stage_reading_what_a_later_one_writes(self):
        # Symmetric, and the direction it is easy to forget: stage one is
        # drawn against a file stage two will rewrite. Running one first is
        # fine, but the batch is still unsafe if order ever changes, and the
        # rule is cheaper to state symmetrically than to reason about.
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
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
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
            [
                _stage("one", edit=["spec/support/helper.rb"]),
                _stage("two", edit=["spec/a_spec.rb"], excerpts=["spec/support/helper.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "spec/support/helper.rb" in dropped[0]

    def test_one_bad_pair_costs_one_stage_not_the_tail(self):
        """Five stages with one collision should yield four, not two.

        Truncating at the first conflict throws away every later stage for a
        collision they had nothing to do with. Filtering is sound because the
        rule removes ordering: a kept stage names nothing any other kept stage
        writes, so its spec is as true after the others run as before.
        """
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
            [
                _stage("one", edit=["app/a.rb"]),
                _stage("two", edit=["app/b.rb"]),
                _stage("three", edit=["app/c.rb"]),
                _stage("four", edit=["app/a.rb"]),      # collides with one
                _stage("five", edit=["spec/a_spec.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one", "two", "three", "five"]
        assert len(dropped) == 1 and "four" in dropped[0]

    def test_a_stage_is_judged_against_what_is_kept_not_what_was_dropped(self):
        # `three` is dropped for clashing with `one`. `four` clashes only with
        # `three`, which is not going to run — so it has nothing to clash with.
        from orchestrator.config import orthogonal_stages

        kept, _ = orthogonal_stages(
            [
                _stage("one", edit=["app/a.rb"]),
                _stage("three", edit=["app/a.rb", "app/b.rb"]),
                _stage("four", edit=["app/b.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one", "four"]

    def test_every_drop_is_reported(self):
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
            [
                _stage("one", edit=["app/a.rb"]),
                _stage("two", edit=["app/a.rb"]),
                _stage("three", edit=["app/a.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert len(dropped) == 2, "silence about a dropped stage is how one goes missing"


class TestGlobsAreResolvedNotCompared:
    def test_different_globs_selecting_the_same_file_conflict(self):
        # `app/*.rb` and `app/a.rb` share nothing as strings and everything as
        # files. Comparing the globs would call this batch safe.
        from orchestrator.config import orthogonal_stages

        kept, _ = orthogonal_stages(
            [_stage("one", edit=["app/*.rb"]), _stage("two", edit=["app/a.rb"])],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]

    def test_similar_globs_selecting_nothing_in_common_are_safe(self):
        from orchestrator.config import orthogonal_stages

        kept, _ = orthogonal_stages(
            [_stage("one", edit=["app/**"]), _stage("two", edit=["spec/**"])], TRACKED
        )
        assert [s.id for s in kept] == ["one", "two"]

    def test_a_glob_matching_no_tracked_file_conflicts_with_nothing(self):
        # A stage creating a new file names a path git has never seen. It
        # cannot collide with anything, and must not be treated as a wildcard.
        from orchestrator.config import orthogonal_stages

        kept, _ = orthogonal_stages(
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
        from orchestrator.config import orthogonal_stages

        kept, dropped = orthogonal_stages(
            [
                _stage("one", edit=["spec/brand_new_spec.rb"]),
                _stage("two", edit=["spec/brand_new_spec.rb"]),
            ],
            TRACKED,
        )
        assert [s.id for s in kept] == ["one"]
        assert "brand_new_spec" in dropped[0]


class TestTheReasonIsUsable:
    def test_it_names_the_stage_and_the_path(self):
        from orchestrator.config import orthogonal_stages

        _kept, dropped = orthogonal_stages(
            [
                _stage("first", edit=["config/routes.rb"]),
                _stage("second", edit=["app/a.rb"], read=["config/routes.rb"]),
            ],
            TRACKED,
        )
        assert "second" in dropped[0] and "first" in dropped[0]
        assert "config/routes.rb" in dropped[0]


class TestARevisedStageIsRecheckedAgainstTheQueue:
    """Rework is never batched, and the queue survives it.

    A stage that fails routes to the planner with its child branch intact, and
    the branch belongs to one stage — so the planner revises *that* stage and
    cannot answer with a batch. The queued stages behind it, though, are still
    good work: nothing about them has changed.

    What can change is the revised stage. A revision that widens `edit_files`
    to fix a scope violation may now name a file a queued stage reads, and the
    guarantee that made the batch safe would quietly stop holding. So the check
    is re-run rather than the queue discarded — the queued stages that survive
    are kept, and only those the revision actually collides with are dropped.

    No new mechanism: putting the revised stage at the head of the list and
    passing the queue behind it is the same question `orthogonal_stages`
    already answers. The revised stage is first so it is always kept, and the
    queue was already pairwise orthogonal, so the only drops that can appear are
    the ones the revision caused.
    """

    def test_a_revision_that_stays_in_scope_keeps_the_whole_queue(self):
        from orchestrator.config import orthogonal_stages

        revised = _stage("one", edit=["app/a.rb"], read=["spec/support/helper.rb"])
        queue = [_stage("two", edit=["app/b.rb"]), _stage("three", edit=["app/c.rb"])]
        kept, dropped = orthogonal_stages([revised, *queue], TRACKED)
        assert [s.id for s in kept] == ["one", "two", "three"]
        assert dropped == []

    def test_a_widened_revision_drops_only_what_it_now_collides_with(self):
        # The scope-violation case: `one` is revised to also edit `app/c.rb`,
        # which `three` was drawn against. `two` is untouched and survives.
        from orchestrator.config import orthogonal_stages

        widened = _stage("one", edit=["app/a.rb", "app/c.rb"])
        queue = [_stage("two", edit=["app/b.rb"]), _stage("three", edit=["app/c.rb"])]
        kept, dropped = orthogonal_stages([widened, *queue], TRACKED)
        assert [s.id for s in kept] == ["one", "two"]
        assert len(dropped) == 1 and "three" in dropped[0]

    def test_the_revised_stage_is_never_the_one_dropped(self):
        # It owns the branch. Whatever else goes, it stays.
        from orchestrator.config import orthogonal_stages

        widened = _stage("one", edit=["app/**"])
        queue = [_stage("two", edit=["app/b.rb"]), _stage("three", edit=["app/c.rb"])]
        kept, _ = orthogonal_stages([widened, *queue], TRACKED)
        assert [s.id for s in kept] == ["one"]
