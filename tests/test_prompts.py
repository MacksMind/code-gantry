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

from types import SimpleNamespace

from orchestrator.plandoc import PlanDocument, PlanTree
from orchestrator.prompts import build_planner_messages, build_review_messages


def a_plan(text="do the thing"):
    return PlanTree(
        root=PlanDocument(path="p.md", content=text),
        children=[],
        problems=[],
        skipped=[],
    )


def _cfg(addendum="docs/proj/progress_log.md", cache_ttl="1h"):
    return SimpleNamespace(cache_ttl=cache_ttl, plan_addendum_path=addendum)


def all_text(messages):
    """Every text block, in order — the prompt as the model receives it.

    `leading_text` reads only the cached prefix, which is the right lens for a
    breakpoint test and the wrong one for anything after it. The completed
    history sits deliberately *after* the breakpoint, so asserting on it
    through `leading_text` passes whatever it says.
    """
    out = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            out.extend(block.get("text", "") for block in content)
        else:
            out.append(content)
    return "\n\n".join(out)


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

    def test_the_configured_ttl_reaches_the_plan_and_layout_marker(self):
        # The block above is the small one. This is the ninety-thousand-token
        # one, and it shipped without a TTL — so it expired between every pair
        # of planner calls while the system block, the only marker that carried
        # the configured lifetime, survived. Live over two runs: 3% cached,
        # where the 3% was the system block and nothing else.
        cfg = SimpleNamespace(cache_ttl="1h")
        messages = build_planner_messages(
            cfg=cfg, plan=a_plan(), completed=[], layout="- `src/` (1)"
        )
        assert messages[0]["content"][0]["cache_control"] == {
            "type": "ephemeral",
            "ttl": "1h",
        }

    def test_a_project_without_a_ttl_still_builds(self):
        # cache_ttl is optional, and a missing one must not raise on a path
        # every planner call takes.
        messages = build_planner_messages(
            cfg=SimpleNamespace(cache_ttl=None), plan=a_plan(), completed=[]
        )
        assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


class TestPlannerCacheBreakpoint:
    def test_the_stable_prefix_is_marked_cacheable(self):
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="- `src/` (1)"
        )
        blocks = messages[0]["content"]
        assert isinstance(blocks, list), "a string cannot carry cache_control"
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

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
        assert "TAIL_MARKER" in messages[0]["content"][-1]["text"]

    def test_the_budget_countdown_is_not_cached(self):
        # It decrements on interventions, so caching it would defeat the point.
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[],
            interventions_used=3, interventions_max=12,
        )
        assert "intervention(s) left" not in leading_text(messages)

    def test_two_breakpoints_and_no_more(self):
        """One after the plan, one after the completed history.

        The second is what makes the agentic loop affordable: a derivation
        runs ten to twenty-five turns, every turn re-sends the whole prompt,
        and the history is byte-identical across all of them. It sits after
        the plan rather than with it because it grows — and it grows only at
        the end, so the provider extends the cached prefix instead of
        rebuilding it.

        The cost table and the deferral list stay outside both. The table is a
        sliding window of the last twelve and the list mutates in place, so a
        breakpoint after them would miss on every stage and cost more than not
        caching at all.
        """
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="x"
        )
        marked = [
            b for m in messages
            if isinstance(m["content"], list)
            for b in m["content"]
            if "cache_control" in b
        ]
        assert len(marked) == 2

    def test_the_prefix_is_identical_across_calls_within_a_stage(self):
        # Caching depends on a byte-identical prefix. Anything varying here —
        # a timestamp, a counter — silently costs full price every call.
        first = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="L", status_tail="a"
        )
        second = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="L", status_tail="b"
        )
        # The cached blocks, not the whole message: the volatile tail is
        # expected to differ, which is why it is outside the breakpoints.
        assert first[0]["content"][:2] == second[0]["content"][:2]


class TestARunsHistoryIsNotTheProjectsHistory:
    """An empty completed list means this run is new, not the project.

    The block used to render "None yet — this is the first stage of the
    project", which is true exactly once and false every restart after. A month
    of landed work looks identical to a greenfield start, and the planner was
    being told the false one as a statement of fact.

    What it must not do is invent a substitute claim in the other direction.
    The run genuinely does not know what the project has done; it knows where
    that is written down. So it says that, and points at the plan directory and
    the progress log inside it.
    """

    def test_it_does_not_claim_the_project_is_starting(self):
        text = all_text(build_planner_messages(_cfg(), a_plan(), []))
        assert "first stage of the project" not in text

    def test_it_says_the_runs_history_is_what_is_empty(self):
        text = all_text(build_planner_messages(_cfg(), a_plan(), []))
        assert "this run" in text.lower()

    def test_it_points_at_the_written_record(self):
        # The planner has read tools; what it needs is to be told where the
        # record is, not to be handed a summary. Asserted inside the history
        # block itself — the plan block names the log too, and this is about
        # the empty history not leaving the planner to infer anything.
        text = all_text(build_planner_messages(_cfg(), a_plan(), []))
        history = text.split("## Completed stages", 1)[1]
        assert "docs/proj/progress_log.md" in history

    def test_a_populated_history_is_unchanged(self):
        text = all_text(
            build_planner_messages(
                _cfg(), a_plan(), [{"index": 0, "id": "s1", "instruction": "did it"}]
            )
        )
        assert "did it" in text


class TestTheProgressLogIsNamedAsTheRecordOfWhatIsDone:
    """The planner must know which plan document reports progress.

    The log is a plan child like any other once PLAN.md links it, so it arrives
    in the payload as one more document among eight. Which of them is the
    record of what has landed is not inferable from the content, and it is the
    one document whose role changes how the others should be read.

    Driven by `plan_addendum_path`, so it stays a property of the project's
    configuration rather than prose a human has to remember to write.
    """

    def test_the_configured_log_is_identified(self):
        text = leading_text(
            build_planner_messages(_cfg(addendum="docs/proj/progress_log.md"), a_plan(), [])
        )
        assert "docs/proj/progress_log.md" in text

    def test_it_says_the_plan_alone_does_not_know_what_is_done(self):
        text = leading_text(
            build_planner_messages(_cfg(addendum="docs/proj/progress_log.md"), a_plan(), [])
        )
        lowered = text.lower()
        assert "what has been done" in lowered or "what is done" in lowered

    def test_a_project_without_one_says_nothing_about_it(self):
        text = all_text(build_planner_messages(_cfg(addendum=None), a_plan(), []))
        assert "progress_log" not in text


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
        marked = messages[0]["content"][0]
        assert "PLAN_TEXT" in marked["text"]
        assert "LAYOUT_TEXT" in marked["text"]
        assert "cache_control" in marked
        # And the volatile tail is deliberately not marked.
        assert "cache_control" not in messages[0]["content"][-1]

    def test_the_history_is_outside_the_marked_block(self):
        from orchestrator.prompts import build_planner_messages

        landed = [{"id": "earlier", "index": 0, "merge_sha": "abc123"}]
        messages = build_planner_messages(
            cfg=None, plan=a_plan(), completed=landed, layout="L"
        )
        assert "earlier" not in leading_text(messages)

    def test_the_prefix_is_identical_before_and_after_a_landing(self):
        """The plan block must survive a landing untouched.

        The history block is *expected* to change — it grew — but it grows only
        at the end, which is what lets the provider extend the cached prefix
        rather than rebuild it.
        """
        from orchestrator.prompts import build_planner_messages

        one = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[{"id": "x", "index": 0}], layout="L"
        )
        two = build_planner_messages(
            cfg=None, plan=a_plan(),
            completed=[{"id": "x", "index": 0}, {"id": "y", "index": 1}],
            layout="L",
        )
        assert one[0]["content"][0] == two[0]["content"][0], "the plan is untouched"
        # Steady state, which is what the cache sees for all but the first
        # landing: the empty-history block says "no stages yet" and so is not a
        # prefix of the populated one, but every landing after that appends.
        assert two[0]["content"][1]["text"].startswith(
            one[0]["content"][1]["text"]
        ), "the history must grow at the end, never be rewritten"

    def test_a_deferral_does_not_evict_the_plan_either(self):
        from orchestrator.prompts import build_planner_messages

        before = build_planner_messages(cfg=None, plan=a_plan(), completed=[], layout="L")
        after = build_planner_messages(
            cfg=None, plan=a_plan(), completed=[], layout="L",
            deferred=[{"plan_step": "aws", "reason": "no access"}],
        )
        assert before[0]["content"][:2] == after[0]["content"][:2]

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


class TestTheHistoryShowsWhatStagesCostTheExecutor:
    """So the planner can size the next stage from evidence, not a file count.

    Batching guidance had to invent a number — "up to roughly ten files" —
    because nothing told the planner what a stage actually costs. It is the
    wrong unit: two stages that each edited one file differed 3.4x in context,
    14k against 47k, and ten of the first is a different proposition from ten
    of the second.

    Aider reports the figure every attempt. Once it reaches the history, the
    planner is calibrating against this executor on these files rather than
    against a guess someone wrote down once.
    """

    def _history(self, completed):
        text = all_text(build_planner_messages(_cfg(), a_plan(), completed))
        return text.split("## Completed stages", 1)[1]

    def test_the_context_cost_is_shown(self):
        history = self._history(
            [{"index": 0, "id": "s1", "instruction": "did it",
              "executor_context_tokens": 47_000}]
        )
        assert "47" in history

    def test_a_stage_without_a_figure_says_nothing_about_it(self):
        # Script stages and older runs have none; an absent number must not
        # render as a zero the planner could read as "free".
        history = self._history([{"index": 0, "id": "s1", "instruction": "did it"}])
        assert "context" not in history.lower()


class TestTheReviewerIsToldWhatTheEditorDoes:
    """One exemption, stated by the tool rather than by each project.

    Aider normalises the final newline of every file it writes. On a file
    committed without one that produces a diff hunk no model chose and no
    instruction can suppress — so a reviewer enforcing scope to the letter
    rejects correct work, the executor reproduces it, and the stage burns its
    whole rework budget before reaching the planner. Observed exactly once,
    costing three attempts at fifteen correct edits.

    A per-project override was considered and rejected: it would not stop the
    change, only guarantee the rejection, permanently. And the real guard is
    already in place and made of evidence rather than prose — if a final
    newline broke something, the full suite is red at the merge gate and the
    stage does not land.
    """

    def test_the_exemption_is_stated(self):
        from orchestrator.prompts import REVIEW_SYSTEM_PROMPT

        text = REVIEW_SYSTEM_PROMPT.lower()
        assert "final newline" in text
        assert "no newline at end of file" in text

    def test_trailing_whitespace_on_added_lines_is_declared(self):
        # The second thing the machinery does without asking. `advance` strips
        # it before committing, because a pre-commit hook rejecting it killed a
        # stage four times — so the diff the reviewer reads and the commit that
        # lands genuinely differ, and only the tool can say so.
        from orchestrator.prompts import REVIEW_SYSTEM_PROMPT

        text = REVIEW_SYSTEM_PROMPT.lower()
        assert "trailing whitespace" in text
        assert "before the stage is committed" in text

    def test_it_is_narrow(self):
        # Not a licence on whitespace generally. Everything else about it stays
        # the reviewer's to judge, which is the difference between an exemption
        # and a hole.
        from orchestrator.prompts import REVIEW_SYSTEM_PROMPT

        assert "anything else about whitespace" in REVIEW_SYSTEM_PROMPT.lower()


class TestTheBreakpointBudgetIsFullySpent:
    """Four is the API's limit, and all four are now in use.

    The system prompt, the plan-and-layout block and the completed history are
    static and carry the configured lifetime. The fourth moves with the tool
    loop and is added at request time by `_with_loop_breakpoint`.

    Pinned because exceeding the limit fails the request, not a test — every
    planner call in the run would break at once, and the cause would read as a
    transport error. A fifth breakpoint means removing one of these four
    deliberately, not adding to them.
    """

    def _marks(self, blocks):
        return [b for b in blocks if isinstance(b, dict) and "cache_control" in b]

    def test_the_built_message_spends_exactly_two(self):
        from orchestrator.prompts import build_planner_messages

        messages = build_planner_messages(
            cfg=SimpleNamespace(cache_ttl="1h"),
            plan=a_plan(),
            completed=[{"index": 0, "id": "s1", "instruction": "did it"}],
            layout="- `src/` (1)",
        )
        assert len(messages) == 1, "one user message; the loop appends after it"
        assert len(self._marks(messages[0]["content"])) == 2

    def test_system_plus_message_plus_the_moving_one_is_four(self):
        from orchestrator.planner import _system_blocks, _with_loop_breakpoint
        from orchestrator.prompts import build_planner_messages

        messages = build_planner_messages(
            cfg=SimpleNamespace(cache_ttl="1h"), plan=a_plan(), completed=[]
        )
        system = _system_blocks("1h")
        outgoing = _with_loop_breakpoint(messages)

        total = len(self._marks(system)) + sum(
            len(self._marks(m["content"])) for m in outgoing
        )
        assert total == 4, f"the API allows 4 cache breakpoints, found {total}"


class TestThePlannerSeesTheDiagnosisNotOnlyTheConsequence:
    """Both ends of a retry sequence, when they differ.

    A stage that fails its tests, is reworked twice, then trips the no-progress
    guard arrives at the planner describing only the guard. That is true and
    useless: "the attempt reproduced the previous diff exactly" says retrying
    is pointless and says nothing about what to draw instead. Observed live —
    the planner redrew the stage blind and the redraw failed on the same spec.

    Both blocks sit after the cache breakpoint, with the situational material
    they belong to. Nothing here touches the cached prefix.
    """

    def _stage(self):
        return SimpleNamespace(
            id="funnel-links",
            instruction="Convert the funnel request links.",
            edit_files=["app/views/**"],
            constraints=None,
        )

    def _messages(self, failure, opening):
        return build_planner_messages(
            cfg=_cfg(),
            plan=a_plan(),
            completed=[],
            current_stage=self._stage(),
            failure=failure,
            opening_failure=opening,
        )

    OPENING = {
        "layer": "tests",
        "summary": "the suite failed",
        "detail": "assert_select('a[href=...]', 'Add Personalization') failed",
        "failing_paths": ["spec/features/order_funnel_add_item_spec.rb"],
    }
    LATEST = {
        "layer": "progress",
        "summary": "the attempt reproduced the previous diff exactly",
        "detail": "retrying costs another review for the same result",
    }

    def test_the_opening_failure_is_rendered(self):
        text = all_text(self._messages(self.LATEST, self.OPENING))
        assert "Add Personalization" in text
        assert "order_funnel_add_item_spec.rb" in text

    def test_the_latest_failure_is_still_rendered(self):
        text = all_text(self._messages(self.LATEST, self.OPENING))
        assert "reproduced the previous diff" in text

    def test_the_diagnosis_leads(self):
        # The guard says retrying is pointless; the assertion says what to draw
        # instead. Read in the other order the planner acts on the consequence.
        text = all_text(self._messages(self.LATEST, self.OPENING))
        assert text.index("Add Personalization") < text.index(
            "reproduced the previous diff"
        )

    def test_an_unrepeated_failure_is_shown_once(self):
        # The common case: one failure, straight to the planner. A second
        # identical block would be noise in a paid prompt.
        text = all_text(self._messages(self.OPENING, self.OPENING))
        assert text.count("Add Personalization") == 1

    def test_no_opening_failure_renders_as_before(self):
        text = all_text(self._messages(self.LATEST, None))
        assert "reproduced the previous diff" in text
        assert "Add Personalization" not in text


class TestTheReviewerReadsTheLiveRecord:
    """What has been done, and how much of it is worth paying for every call.

    The reviewer was handed the frozen plan snapshot, whose copy of the
    progress log is whatever existed at run start — measured at 6,680 bytes
    against 480,867 on the branch. So its only account of this run's 133
    landed stages was the completed-stage history, which sits after the cache
    breakpoint and is re-billed in full on every review. At 322k prompt tokens
    against 56k cached, that history was most of what every review cost.

    Two changes, in opposite directions. The live log replaces the stale one,
    so the reviewer sees the actual record. The history is capped, because
    across 164 stored verdicts not one cites an earlier stage — it was paying
    roughly 220k tokens a call to prevent a failure that has not occurred.

    Ordering is the whole trick. GPT-5.6 caches at an explicit breakpoint and
    does not fall back to the longest matching prefix, so a growing region
    placed before it misses on every landing. The frozen documents stay in the
    cached prefix; the log and the tail go after it.
    """

    def _messages(self, log="## entry\n\nit was done", completed=None, **cfg_over):
        return build_review_messages(
            stage=SimpleNamespace(
                id="s", instruction="do it", constraints=None, acceptance=None
            ),
            cfg=_review_cfg(**cfg_over),
            diff="--- a\n+++ b",
            plan=_plan_with_log(),
            completed=completed if completed is not None else [],
            progress_log=log,
        )

    def test_the_live_log_is_in_the_prompt(self):
        assert "it was done" in all_text(self._messages())

    def test_the_frozen_copy_is_not(self):
        # Two copies of the same document, one of them wrong, is worse than
        # either alone.
        assert "STALE SNAPSHOT" not in all_text(self._messages())

    def test_the_log_sits_after_the_breakpoint(self):
        # Before it, every landing would invalidate the reviewer's only
        # working cache — this model has no longest-prefix fallback.
        messages = self._messages()
        cached = "".join(
            b["text"] for b in messages[1]["content"] if "prompt_cache_breakpoint" in b
        )
        assert "it was done" not in cached

    def test_the_plan_documents_stay_cached(self):
        messages = self._messages()
        cached = "".join(
            b["text"] for b in messages[1]["content"] if "prompt_cache_breakpoint" in b
        )
        assert "THE PLAN ITSELF" in cached

    def test_the_history_is_capped(self):
        completed = [
            {"index": i, "id": f"stage-{i}", "instruction": f"work {i}"}
            for i in range(30)
        ]
        text = all_text(self._messages(completed=completed, history_stages=10))
        assert "stage-29" in text, "the most recent stages are the ones kept"
        assert "stage-19" not in text, "an older stage is dropped"

    def test_the_cap_says_what_it_dropped(self):
        # A truncated list that does not say it is truncated reads as the whole
        # record, and the reviewer would judge completeness against it.
        completed = [
            {"index": i, "id": f"stage-{i}", "instruction": f"work {i}"}
            for i in range(30)
        ]
        text = all_text(self._messages(completed=completed, history_stages=10))
        assert "30" in text and "10" in text

    def test_no_cap_keeps_everything(self):
        completed = [
            {"index": i, "id": f"stage-{i}", "instruction": f"work {i}"}
            for i in range(30)
        ]
        text = all_text(self._messages(completed=completed, history_stages=None))
        assert "stage-0" in text

    def test_a_missing_log_still_builds(self):
        # A project with no addendum configured, or one not yet written. A
        # review is far too expensive to fail over a missing progress file.
        assert "do it" in all_text(self._messages(log=None))


def _review_cfg(history_stages=None, addendum="docs/progress_log.md"):
    return SimpleNamespace(
        cache_ttl=None,
        plan_addendum_path=addendum,
        reviewer=SimpleNamespace(history_stages=history_stages),
    )


def _plan_with_log():
    return PlanTree(
        root=PlanDocument(path="PLAN.md", content="THE PLAN ITSELF"),
        children=[
            PlanDocument(path="docs/progress_log.md", content="STALE SNAPSHOT"),
        ],
        problems=[],
        skipped=[],
    )
