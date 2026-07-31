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


class TestPerProjectGuidance:
    """Operator prose appended to the planner's system prompt.

    It lives inline in config.yaml rather than in a file the config points at,
    because approval hashes config.yaml's bytes. Guidance in a separate file
    could be rewritten after approval and change how the planner behaves
    without invalidating anything.

    It cannot weaken the safety partition whatever it says: the planner's
    schema has no field for a command and the allowlist filters the response
    regardless. This is advice, not permission.
    """

    def test_guidance_reaches_the_system_prompt(self):
        from orchestrator.planner import _system_blocks

        blocks = _system_blocks(guidance="Prefer stages of one file each.")
        assert "Prefer stages of one file each." in blocks[0]["text"]

    def test_guidance_is_inside_the_cached_block(self):
        # It is fixed for the run, so it belongs in the prefix rather than
        # being re-sent uncached on every call.
        from orchestrator.planner import _system_blocks

        blocks = _system_blocks(guidance="G", cache_ttl="1h")
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_no_guidance_changes_nothing(self):
        from orchestrator.planner import _system_blocks

        assert _system_blocks()[0]["text"] == _system_blocks(guidance="")[0]["text"]

    def test_guidance_is_attributed_to_the_operator(self):
        # The planner should be able to tell project policy from the standing
        # contract, and weigh a conflict knowingly rather than silently.
        from orchestrator.planner import _system_blocks

        text = _system_blocks(guidance="G")[0]["text"]
        assert "project" in text.lower().split("## ")[-1]

    def test_it_is_not_a_planner_writable_field(self):
        from orchestrator.config import PLANNER_WRITABLE_FIELDS
        from orchestrator.planner import PlannedStage

        assert "guidance" not in PlannedStage.model_fields
        assert "guidance" not in PLANNER_WRITABLE_FIELDS


class TestDeployableIncrements:
    def test_the_prompt_asks_for_independently_shippable_stages(self):
        from orchestrator.planner import PLANNER_SYSTEM_PROMPT

        lowered = PLANNER_SYSTEM_PROMPT.lower()
        assert "deploy" in lowered

    def test_the_prompt_permits_reordering_the_plan(self):
        # A step needing access the run does not have should be deferred, not
        # escalated — but only if the planner knows it is allowed to.
        from orchestrator.planner import PLANNER_SYSTEM_PROMPT

        lowered = PLANNER_SYSTEM_PROMPT.lower()
        assert "reorder" in lowered or "out of order" in lowered
        assert "defer" in lowered


class TestScopedTestGuidance:
    def test_the_prompt_says_what_omitting_test_paths_costs(self):
        # A behaviour-preserving refactor changes no specs by design, so on a
        # migration the scoped path depends almost entirely on the planner
        # naming the specs that cover the code it touches. Left as a neutral
        # optional field, it will be skipped, and every stage pays for a full
        # suite on every retry.
        from orchestrator.planner import PLANNER_SYSTEM_PROMPT

        lowered = PLANNER_SYSTEM_PROMPT.lower()
        assert "test_paths" in lowered
        assert "whole suite" in lowered or "full suite" in lowered


class TestReviewerCacheBreakpoint:
    """GPT-5.6 caches at an explicit breakpoint, not at the longest prefix.

    Its default `implicit` mode puts the breakpoint on the *latest* message.
    The reviewer's latest message is the diff, which differs every call, so the
    prefix at the breakpoint was never the same twice. Measured against the live
    API: two calls sharing 55,489 identical prefix tokens, both reporting
    `read=0, write=55,498`. Every review paid a cache write — billed at 1.25x
    uncached on GPT-5.6 — and read nothing back. That is worse than not caching.

    With the breakpoint moved before the diff: `read=55,489, write=0`.

    The stable payload is already ordered first for exactly this reason; the
    ordering was necessary and, on this model family, not sufficient.
    """

    def a_review(self, **over):
        from orchestrator.config import Stage
        from orchestrator.prompts import build_review_messages

        args = dict(
            stage=Stage(id="s", instruction="do it", edit_files=["a.py"]),
            cfg=None,
            diff="--- a\n+++ b\n",
            plan=a_plan("PLAN"),
            completed=[],
        )
        args.update(over)
        return build_review_messages(**args)

    def test_the_stable_message_carries_a_breakpoint(self):
        messages = self.a_review()
        stable = messages[1]["content"]
        assert isinstance(stable, list), "a string cannot carry a breakpoint"
        assert stable[-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}

    def test_the_breakpoint_is_before_the_diff(self):
        # The whole point: the diff must be free to change without moving it.
        messages = self.a_review(diff="DIFF_MARKER")
        marked = [
            i for i, m in enumerate(messages)
            if isinstance(m["content"], list)
            and any("prompt_cache_breakpoint" in b for b in m["content"])
        ]
        last = messages[-1]["content"]
        text = last if isinstance(last, str) else last[0]["text"]
        assert "DIFF_MARKER" in text
        assert marked and max(marked) < len(messages) - 1

    def test_exactly_one_breakpoint(self):
        messages = self.a_review()
        marked = [
            b for m in messages if isinstance(m["content"], list)
            for b in m["content"] if "prompt_cache_breakpoint" in b
        ]
        assert len(marked) == 1

    def test_the_prefix_is_byte_identical_across_diffs(self):
        first = self.a_review(diff="one")
        second = self.a_review(diff="two")
        assert first[:2] == second[:2]


class TestTheExecutorCannotRunCommands:
    """A stage that asks for the impossible gets an infinite argument.

    Observed live. The planner wrote, into `instruction`:

        Find the sites by content instead:
            grep -n 'nothing:' app/controllers/fckeditor_controller.rb
        ...
        When done, re-run the grep above and confirm

    Aider's executor cannot run commands — its own prompt only lets it
    *suggest* them. So the model hallucinated grep output and argued with
    itself about the file's contents twenty times over, decoding 24,120 tokens
    before the client cancelled it at ten minutes. Three times in one evening.

    Capping output bounds what that costs. It does not stop it. The fix is not
    to ask: mechanical verification belongs in `forbidden_patterns`, which the
    orchestrator checks against the diff deterministically and for free, and
    which this very stage already used for unrelated patterns while omitting
    the one that was its actual goal.
    """

    def test_the_prompt_says_the_executor_cannot_run_commands(self):
        from orchestrator.planner import PLANNER_SYSTEM_PROMPT

        lowered = PLANNER_SYSTEM_PROMPT.lower()
        assert "cannot run" in lowered or "cannot execute" in lowered
        assert "grep" in lowered

    def test_it_points_at_forbidden_patterns_as_the_alternative(self):
        from orchestrator.planner import PLANNER_SYSTEM_PROMPT

        section = PLANNER_SYSTEM_PROMPT.lower()
        assert "forbidden_patterns" in section
        # The guidance has to connect the two: do not ask the executor to
        # check; declare the check instead.
        idx = section.find("cannot run")
        assert idx != -1
        assert "forbidden_patterns" in section[idx : idx + 900]

    def test_the_field_description_says_it_too(self):
        # The planner sees field descriptions even when it skims the prose.
        from orchestrator.planner import PlannedStage

        description = PlannedStage.model_fields["forbidden_patterns"].description
        assert "verif" in description.lower() or "check" in description.lower()


class TestTheCachedPrefixSurvivesALandedStage:
    """History inside the breakpoint invalidates the plan with it.

    The reviewer's marked block held the plan documents *and* the completed-
    stage history. The plan never changes; the history changes every time a
    stage lands. So each landing invalidated ~56,000 tokens of prefix,
    including the ~50,000 that were identical.

    Measured across one run: three reviewer calls, all `cached=0`, all writing
    the full prefix at 1.25x. Two of those misses were retention expiry — the
    stages were 43 minutes apart — but the third came 13 minutes after its
    predecessor, well inside the window, and missed because a stage had landed
    between them.

    The plan block leads and is marked; the history follows the breakpoint,
    where it costs full price for a few hundred tokens instead of taking fifty
    thousand down with it.
    """

    def a_review(self, completed=None, diff="d"):
        from orchestrator.config import Stage
        from orchestrator.prompts import build_review_messages

        return build_review_messages(
            stage=Stage(id="s", instruction="i", edit_files=["a.py"]),
            cfg=None,
            diff=diff,
            plan=a_plan("PLAN_TEXT"),
            completed=completed or [],
        )

    def test_the_marked_block_holds_the_plan(self):
        blocks = self.a_review()[1]["content"]
        assert "PLAN_TEXT" in blocks[-1]["text"]
        assert blocks[-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}

    def test_the_marked_block_does_not_hold_the_history(self):
        landed = [{"id": "earlier", "index": 0, "merge_sha": "abc123"}]
        marked = self.a_review(completed=landed)[1]["content"][-1]["text"]
        assert "earlier" not in marked

    def test_the_prefix_is_identical_before_and_after_a_stage_lands(self):
        # The property that matters: landing a stage must not cost the plan.
        before = self.a_review(completed=[])
        after = self.a_review(completed=[{"id": "earlier", "index": 0}])
        assert before[:2] == after[:2]

    def test_the_history_still_reaches_the_reviewer(self):
        landed = [{"id": "earlier", "index": 0, "merge_sha": "abc123"}]
        text = "".join(
            m["content"] if isinstance(m["content"], str)
            else "".join(b["text"] for b in m["content"])
            for m in self.a_review(completed=landed)
        )
        assert "earlier" in text


class TestThePlannerPrefixAlsoSurvivesALanding:
    """Same defect as the reviewer's, one file over.

    `leading` held the plan, the repository layout, the completed history and
    the deferred list, all under one breakpoint. The first two are fixed for a
    run; the last two change every time a stage lands or a step is deferred —
    so each landing re-billed the plan and the layout with them.

    Measured: two consecutive planner calls a minute apart, each writing ~91,000
    tokens to cache and reading back 4,051 — the small system block, which is
    the only thing that had not changed.
    """

    def test_the_plan_and_layout_are_marked(self):
        from orchestrator.prompts import build_planner_messages

        messages = build_planner_messages(
            cfg=None, plan=a_plan("PLAN_TEXT"), completed=[], layout="LAYOUT_TEXT"
        )
        marked = messages[0]["content"][-1]
        assert "PLAN_TEXT" in marked["text"]
        assert "LAYOUT_TEXT" in marked["text"]
        assert "cache_control" in marked

    def test_the_history_is_outside_the_marked_block(self):
        from orchestrator.prompts import build_planner_messages

        landed = [{"id": "earlier", "index": 0, "merge_sha": "abc123"}]
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=landed, layout="L"
        )
        assert "earlier" not in leading_text(messages)

    def test_the_prefix_is_identical_before_and_after_a_landing(self):
        from orchestrator.prompts import build_planner_messages

        before = build_planner_messages(cfg=None, plan=a_plan(), completed=[], layout="L")
        after = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[{"id": "x", "index": 0}], layout="L"
        )
        assert before[0] == after[0]

    def test_a_deferral_does_not_evict_the_plan_either(self):
        from orchestrator.prompts import build_planner_messages

        before = build_planner_messages(cfg=None, plan=a_plan(), completed=[], layout="L")
        after = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="L",
            deferred=[{"plan_step": "aws", "reason": "no access"}],
        )
        assert before[0] == after[0]

    def test_the_history_still_reaches_the_planner(self):
        from orchestrator.prompts import build_planner_messages

        landed = [{"id": "earlier", "index": 0, "merge_sha": "abc123"}]
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=landed, layout="L"
        )
        text = "".join(
            m["content"] if isinstance(m["content"], str)
            else "".join(b["text"] for b in m["content"])
            for m in messages
        )
        assert "earlier" in text
