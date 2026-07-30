"""Prompt construction, and specifically where the cache breakpoints go.

Anthropic caching is explicit: content without a `cache_control` marker is
re-billed in full on every call. The planner's prompt is deliberately ordered
so that everything stable — the plan snapshot, the repository layout, the
completed history — leads, and everything situational follows. That ordering is
worth nothing without a breakpoint between the two, which is what these tests
pin.

The economic premise of the whole design is that the paid models are called at
checkpoints against a mostly-cached prefix. If that silently stops being true,
it shows up here rather than on an invoice.
"""

from orchestrator.plandoc import PlanDocument, PlanTree
from orchestrator.prompts import build_planner_messages


def a_plan(text="do the thing"):
    return PlanTree(
        root=PlanDocument(path="p.md", content=text),
        children=[],
        problems=[],
        skipped=[],
    )


def leading_text(messages):
    block = messages[0]["content"]
    return block[0]["text"] if isinstance(block, list) else block


class TestCacheLifetime:
    """The prefix has to still be cached when the next call arrives.

    Anthropic's ephemeral cache defaults to five minutes. Between two planner
    calls sits a stage: an executor attempt, a scoped suite, a review, and a
    full suite. On a large Rails suite that is comfortably more than five
    minutes, so a correctly-marked prefix would expire before it was ever
    reused and every call would pay full price anyway.
    """

    def test_the_configured_ttl_reaches_the_cache_control_marker(self):
        from orchestrator.planner import _system_blocks

        blocks = _system_blocks(cache_ttl="1h")
        assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_no_ttl_leaves_the_provider_default(self):
        from orchestrator.planner import _system_blocks

        assert _system_blocks()[0]["cache_control"] == {"type": "ephemeral"}


class TestPlannerCacheBreakpoint:
    def test_the_stable_prefix_is_marked_cacheable(self):
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="- `src/` (1)"
        )
        blocks = messages[0]["content"]
        assert isinstance(blocks, list), "a string cannot carry cache_control"
        assert blocks[-1]["cache_control"] == {"type": "ephemeral"}

    def test_the_plan_and_layout_are_inside_the_cached_block(self):
        messages = build_planner_messages(
            cfg=None,
            plan=a_plan("PLAN_MARKER"),
            completed=[],
            layout="LAYOUT_MARKER",
        )
        text = leading_text(messages)
        assert "PLAN_MARKER" in text
        assert "LAYOUT_MARKER" in text

    def test_situational_material_is_outside_the_cached_block(self):
        # A breakpoint after content that changes every call would invalidate
        # the cache on every call, which is worse than not caching at all.
        messages = build_planner_messages(
            cfg=None,
            plan=a_plan(),
            completed=[],
            layout="LAYOUT_MARKER",
            status_tail="TAIL_MARKER",
            interventions_used=3,
            interventions_max=12,
        )
        assert "TAIL_MARKER" not in leading_text(messages)
        assert "TAIL_MARKER" in messages[-1]["content"]

    def test_the_budget_countdown_is_not_cached(self):
        # It decrements on interventions, so caching it would defeat the point.
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[],
            interventions_used=3, interventions_max=12,
        )
        assert "intervention(s) left" not in leading_text(messages)

    def test_exactly_one_breakpoint(self):
        # Anthropic allows a small number of breakpoints; spending them on
        # anything but the one boundary that matters wastes them.
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="x"
        )
        marked = [
            b
            for m in messages
            if isinstance(m["content"], list)
            for b in m["content"]
            if "cache_control" in b
        ]
        assert len(marked) == 1

    def test_the_prefix_is_identical_across_calls_within_a_stage(self):
        # Caching depends on a byte-identical prefix. Anything varying here —
        # a timestamp, a counter — silently costs full price every call.
        first = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="L", status_tail="a"
        )
        second = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="L", status_tail="b"
        )
        assert first[0] == second[0]
