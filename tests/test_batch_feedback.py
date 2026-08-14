"""What the planner is told about a batch that did not survive intact.

A single cycle can produce several facts at once: one stage landed, another
was rejected by the reviewer, the ones behind it are still queued, and one was
dropped for sharing a file. `_failure_block` presents one failure about one
stage, which is the right shape for a single derivation and the wrong shape
for a batch.

Without this the planner re-derives blind and can return the same collision,
and each round trip is a whole derivation. Telling it costs a paragraph.
"""

import pytest

from code_gantry.prompts import build_planner_messages
from test_prompts import _cfg, a_plan, all_text


class TestTheBatchOutcomeReachesThePlanner:
    def test_a_dropped_stage_is_reported_with_its_reason(self):
        text = all_text(build_planner_messages(
            _cfg(), a_plan(), [],
            batch_notes=["stage 'three' was dropped: it shares 'app/a.rb' with stage 'one'"],
        ))
        assert "three" in text and "app/a.rb" in text

    def test_the_queue_still_waiting_is_named(self):
        text = all_text(build_planner_messages(
            _cfg(), a_plan(), [], stage_queue=[{"id": "two"}, {"id": "three"}],
        ))
        assert "two" in text and "three" in text

    def test_it_says_not_to_redraw_what_is_queued(self):
        # The failure this prevents: the planner deriving a stage that is
        # already waiting, and the pair then colliding.
        text = all_text(build_planner_messages(
            _cfg(), a_plan(), [], stage_queue=[{"id": "two"}],
        )).lower()
        assert "already" in text and "queue" in text

    def test_nothing_is_said_when_there_is_nothing_to_say(self):
        # A run not using batching must not carry a paragraph about it, and
        # the cached prefix must not change shape for every other project.
        text = all_text(build_planner_messages(_cfg(), a_plan(), []))
        assert "queued" not in text.lower()

    def test_the_notes_sit_after_the_cache_breakpoint(self):
        """They change every derivation; the plan and history do not.

        Ordering is the caching strategy rather than presentation, and a block
        that churns placed before a breakpoint re-bills everything ahead of it.
        """
        messages = build_planner_messages(
            _cfg(), a_plan(), [], batch_notes=["stage 'three' was dropped"],
        )
        blocks = messages[0]["content"]
        cached = [b for b in blocks if "cache_control" in b]
        assert cached, "the prefix must still be cached"
        assert all("three" not in b["text"] for b in cached)
