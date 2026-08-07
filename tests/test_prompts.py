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

    def test_a_populated_history_does_not_get_the_empty_case(self):
        text = all_text(
            build_planner_messages(
                _cfg(), a_plan(), [{"index": 0, "id": "s1", "instruction": "did it"}]
            )
        )
        history = text.split("## Completed stages", 1)[1]
        assert "s1" in history
        assert "None **in this run**" not in history


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

    def test_a_breakpoint_precedes_the_diff(self):
        # The plan block must stay cached however the diff changes.
        messages = self.a_review(diff="DIFF_MARKER")
        marked = [
            i for i, m in enumerate(messages)
            if isinstance(m["content"], list)
            and any("prompt_cache_breakpoint" in b for b in m["content"])
        ]
        last = messages[-1]["content"]
        text = last if isinstance(last, str) else last[0]["text"]
        assert "DIFF_MARKER" in text
        assert marked and min(marked) < len(messages) - 1

    def test_the_diff_message_is_also_marked(self):
        # The reviewer has tools, so a review is several turns. Without a mark
        # at the end of the per-stage payload every turn re-sends the progress
        # log, the history, the stage and the diff at full price.
        messages = self.a_review(diff="DIFF_MARKER")
        last = messages[-1]["content"]
        assert isinstance(last, list), "a string cannot carry a breakpoint"
        assert last[-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert "DIFF_MARKER" in last[-1]["text"]

    def test_two_breakpoints(self):
        # Two of the four the provider allows: the static plan, and the end of
        # the per-stage payload. Anything more would have to earn its place.
        messages = self.a_review()
        marked = [
            b for m in messages if isinstance(m["content"], list)
            for b in m["content"] if "prompt_cache_breakpoint" in b
        ]
        assert len(marked) == 2

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


class TestTheHistoryCarriesOnlyWhatHasNoOtherHome:
    """Four channels were carrying overlapping versions of the same stage.

    The planner is fed its completed-stage history, the live progress log,
    `stage-costs.md` and the status tail — and this block was reproducing what
    three of them already said. Measured at 45 stages: 74,000 tokens, ~1,650 an
    entry, resent on each of ~15 tool iterations, growing ~6,600 characters per
    landing. Over half of what a planner call paid for was reading itself.

    So the instruction goes (git has the commit subject, and what the stage
    *did* is the reviewer's record in the progress log, fed live); the reviewer
    summary goes (superseded by that record); the context cost goes
    (`stage-costs.md` renders it via `_costs_block`, across every run rather
    than only this one). What survives is what nothing else records.
    """

    def _history(self, completed):
        text = all_text(build_planner_messages(_cfg(), a_plan(), completed))
        return text.split("## Completed stages", 1)[1]

    def test_the_instruction_is_not_echoed_back(self):
        history = self._history(
            [{"index": 0, "id": "s1", "instruction": "PLANNER OWN WORDS"}]
        )
        assert "PLANNER OWN WORDS" not in history

    def test_the_reviewer_summary_is_not_repeated_here(self):
        # It is in the progress log now, and the log is fed on every call.
        history = self._history(
            [{"index": 0, "id": "s1", "review_summary": "VERDICT RATIONALE"}]
        )
        assert "VERDICT RATIONALE" not in history

    def test_the_context_cost_is_not_repeated_here(self):
        history = self._history(
            [{"index": 0, "id": "s1", "executor_context_tokens": 47_000}]
        )
        assert "47" not in history

    def test_what_landed_is_still_identifiable(self):
        history = self._history(
            [{"index": 3, "id": "convert-thing", "merge_sha": "abc123def456789"}]
        )
        assert "convert-thing" in history
        assert "abc123def456" in history

    def test_revisions_survive_because_nothing_else_records_them(self):
        history = self._history([{"index": 0, "id": "s1", "revisions": 2}])
        assert "3 revisions" in history

    def test_withheld_reads_survive_for_the_same_reason(self):
        history = self._history(
            [{"index": 0, "id": "s1", "withheld_reads": ["big/file.rb"]}]
        )
        assert "big/file.rb" in history


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


class TestThePlannerSeesWhatTheStageHasAlreadyDone:
    """A revision is written against a baseline; it must be the reviewer's.

    Observed live, and diagnosed by the reviewer itself. On an `extend`
    revision the work of the failed attempt stays on the branch, so every file
    the planner reads through its tools shows that work as ordinary existing
    code. It wrote the revision in those terms — the header and two examples
    "are already present and must remain byte-identical, add only the missing
    one" — which is a true statement about the tree.

    The reviewer is shown the cumulative diff from `stage_start_sha`, where
    those same lines are additions attributed to this stage. So the instruction
    asserted as pre-existing what the diff attributed to the stage, and its
    "one example only" scope constraint contradicted the diff it was judged
    against. The verdict was `blocked`, correctly, and the next revision had
    the same information and made the same mistake.

    The executor has had this diff since rework stopped resetting the tree. The
    reviewer has always had it. The planner — the one participant that *writes*
    the instruction the other two are held to — was the only one that had to go
    find it, and what it found by reading files looked like baseline.
    """

    DIFF = "--- a/spec/x_spec.rb\n+++ b/spec/x_spec.rb\n+  let(:seo_header) { 'h' }"

    def _messages(self, diff=DIFF, stage=True):
        return build_planner_messages(
            cfg=_cfg(),
            plan=a_plan(),
            completed=[],
            current_stage=(
                SimpleNamespace(
                    id="s", instruction="do it", edit_files=["spec/**"],
                    constraints=None,
                )
                if stage
                else None
            ),
            failure={"layer": "review", "summary": "rework", "detail": "d"},
            stage_diff=diff,
        )

    def test_the_branch_work_is_rendered_on_a_revision(self):
        assert "let(:seo_header)" in all_text(self._messages())

    def test_it_is_named_as_this_stage_s_own_doing(self):
        # The whole failure was the planner treating it as pre-existing. The
        # section has to say whose work it is, not merely show it.
        text = all_text(self._messages())
        assert "this stage" in text.lower()

    def test_it_says_the_reviewer_judges_the_same_diff(self):
        # Without this the planner has the diff and no reason to write in its
        # vocabulary, which is the half of the bug that showing it does not fix.
        assert "reviewer" in all_text(self._messages()).lower()

    def test_nothing_is_rendered_when_the_branch_is_empty(self):
        # Revision 0 of a restart, and every first attempt: an empty section
        # in a paid prompt invites the planner to explain the absence.
        text = all_text(self._messages(diff=""))
        assert "let(:seo_header)" not in text

    def test_nothing_is_rendered_when_deriving_a_new_stage(self):
        # No stage under revision means no branch and no baseline to reconcile.
        assert "let(:seo_header)" not in all_text(self._messages(stage=False))

    def test_it_sits_outside_the_cached_prefix(self):
        # It changes on every attempt of every stage. Inside the breakpoint it
        # would invalidate the plan and the layout along with it.
        assert "let(:seo_header)" not in leading_text(self._messages())

    def test_the_diagnosis_still_leads_it(self):
        # The failure is what the planner acts on; the diff is the evidence for
        # `extend` versus `restart`. Reversed, a large diff pushes the
        # diagnosis down the prompt behind material the planner reads second.
        text = all_text(self._messages())
        assert text.index("rework") < text.index("let(:seo_header)")


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


class TestTheAgentContextRidesInTheCachedPrefix:
    """Repository conventions, where the planner will actually see them.

    Static for the run — read once at the plan sha — so it belongs before the
    breakpoint with the plan and the layout, not after it with the situation.
    """

    def _messages(self, agent_context="### `AGENTS.md`\n\nthe bundle installs itself"):
        return build_planner_messages(
            cfg=_cfg(), plan=a_plan(), completed=[],
            layout="- `app/` (1)", agent_context=agent_context,
        )

    def test_it_reaches_the_planner(self):
        assert "the bundle installs itself" in all_text(self._messages())

    def test_it_is_inside_the_cached_prefix(self):
        cached = self._messages()[0]["content"][0]
        assert "cache_control" in cached
        assert "the bundle installs itself" in cached["text"]

    def test_it_says_these_are_conventions_not_instructions(self):
        # The plan says what the work is. This says how the repository
        # behaves — a planner that conflates them will draw stages from it.
        text = all_text(self._messages())
        assert "conventions" in text.lower()

    def test_a_project_without_one_builds_normally(self):
        assert "do the thing" in all_text(self._messages(agent_context=""))


class TestReviewerToolGuidance:
    """The tool section is conditional on there being tools.

    Told it can read when it cannot, the reviewer either invents a lookup or
    hedges a verdict it should have given outright.
    """

    def _cfg(self, repo_access):
        from orchestrator.config import parse_config

        return parse_config(
            {
                "target_repo": "/tmp/x",
                "project_branch": "work",
                "plan_root": "PLAN.md",
                "test_command": "pytest",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol", "repo_access": repo_access},
            }
        )

    def _system(self, repo_access):
        from orchestrator.config import Stage

        return build_review_messages(
            stage=Stage(id="s", instruction="do it", edit_files=["a.py"]),
            cfg=self._cfg(repo_access),
            diff="--- a\n+++ b\n",
            plan=a_plan("PLAN"),
            completed=[],
        )[0]["content"][0]["text"]

    def test_absent_without_repo_access(self):
        assert "Looking at the repository" not in self._system(False)

    def test_present_with_repo_access(self):
        assert "Looking at the repository" in self._system(True)

    def test_pre_existing_problems_are_not_grounds_for_rework(self):
        # A reviewer that can look will find things the stage did not cause.
        # Rejecting for them burns attempts on work that can never be in scope.
        text = self._system(True)
        assert "not grounds for rework" in text
        assert "problem worse, approve" in text

    def test_it_is_told_where_a_finding_should_go(self):
        # Without this the tool access is half-wired: a reviewer that can look
        # will find things, and a finding left in the summary is read once and
        # lost.
        text = self._system(True)
        assert "`observations`" in text
        assert "progress log" in text

    def test_observations_are_distinguished_from_issues(self):
        # Conflating them would route a pre-existing problem back to an
        # executor that cannot fix it.
        text = self._system(True)
        assert "those are `issues`" in text

    def test_a_difference_with_no_consequence_is_an_observation(self):
        # The routing that matters. Rejecting over a difference nobody can
        # observe costs a rework cycle and returns the same diff; recording it
        # reaches a human who can decide.
        text = self._system(True)
        assert "cannot trace to a consequence" in text
        assert "approve it and write an observation instead" in text

    def test_the_guidance_carries_no_project_vocabulary(self):
        # This string ships to every project's reviewer. An example drawn from
        # one stack is a hint about a repository it may not be looking at.
        # Only tokens that cannot be ordinary English. "permit" and "form" are
        # excluded deliberately: the base contract already says "permitted to
        # edit", and rejecting that would be the test dictating prose rather
        # than catching a leak.
        text = self._system(True).lower()
        for word in (
            "rails", "ruby", "gemfile", "rspec", "attr_accessible",
            ".erb", "activerecord", "bundler", "app/", "spec/",
        ):
            assert word not in text, f"{word!r} is project knowledge in a prompt"

    def test_it_is_told_to_read_before_approving_on_an_unseen_file(self):
        assert "read the file" in self._system(True)


class TestEveryParticipantSeesTheRepositoryConventions:
    """The document that says how this repository is worked in.

    It reached the planner only. The reviewer — the gate that would catch a
    violation — never saw it, and a gate that cannot reach what decides its
    verdict restates the stage instruction in its own voice. That was live: an
    executor recased a SQL keyword against a convention documented in the
    repository, and the only reason the reviewer caught it was that the stage
    happened to pin the exact output string.

    The executor gets the same documents by a different route — `--read`, not
    the prompt — because Aider attaches every path named in its message. That
    is `TestConventionsReachAiderAsReadOnlyFiles` in the executor's tests.
    """

    CONVENTIONS = "## Shop scoping\n\nAlways scope by the current tenant."

    def test_the_reviewer_prompt_carries_it(self):
        messages = build_review_messages(
            stage=SimpleNamespace(
                id="s", instruction="i", constraints=None, acceptance=None
            ),
            cfg=_review_cfg(),
            diff="--- a\n+++ b",
            plan=_plan_with_log(),
            completed=[],
            agent_context=self.CONVENTIONS,
        )
        assert "Always scope by the current tenant" in all_text(messages)

    def test_the_reviewer_gets_it_inside_the_cached_prefix(self):
        # Fixed for the whole run, so it belongs with the plan ahead of the
        # breakpoint. GPT-5.6 caches at an explicit breakpoint and does not
        # fall back to the longest matching prefix, so anything static placed
        # after it is re-billed on every stage for no reason.
        messages = build_review_messages(
            stage=SimpleNamespace(
                id="s", instruction="i", constraints=None, acceptance=None
            ),
            cfg=_review_cfg(),
            diff="--- a\n+++ b",
            plan=_plan_with_log(),
            completed=[],
            agent_context=self.CONVENTIONS,
        )
        cached = next(
            block
            for message in messages
            for block in message["content"]
            if isinstance(block, dict) and "prompt_cache_breakpoint" in block
        )
        assert "Always scope by the current tenant" in cached["text"]

    def test_neither_prompt_invents_a_section_when_there_is_none(self):
        # A project without such a document is an ordinary case, and an empty
        # heading promising conventions is worse than no heading.
        from orchestrator.config import Stage, parse_config
        from orchestrator.prompts import build_executor_prompt

        cfg = parse_config(
            {
                "target_repo": ".", "base_ref": "main", "project_branch": "p",
                "plan_root": "PLAN.md", "test_command": "true",
                "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        stage = Stage(id="s", instruction="do it", edit_files=["a"])
        assert "conventions" not in build_executor_prompt(stage, cfg).lower()

        messages = build_review_messages(
            stage=SimpleNamespace(
                id="s", instruction="i", constraints=None, acceptance=None
            ),
            cfg=_review_cfg(),
            diff="--- a\n+++ b",
            plan=_plan_with_log(),
            completed=[],
        )
        assert "conventions its maintainers" not in all_text(messages).lower()


class TestThePlannerIsToldWhatTheChecksWillDo:
    """The machinery rewrites the diff after the instruction is written.

    `checks` run once the executor has committed, and `checks_commit_changes`
    puts what they rewrite onto the child branch. The planner was told nothing
    about any of it — the word never appeared in its prompt — so it drafted a
    stage forbidding any line it had not named, the formatter collapsed two
    blank lines the executor's own deletions had stranded, and the reviewer
    blocked a diff that was otherwise correct. A whole revision cycle to
    discover a property of our own tooling.

    Same call as the line-endings note one file over: anything the shipped
    machinery does is the tool's to declare, not the operator's to work around
    and not the planner's to rediscover per project. The commands come from
    config, so nothing here names a language, a linter or a file extension —
    `test_the_block_carries_no_project_vocabulary` is what keeps that true.
    """

    def _cfg(self, checks):
        from orchestrator.config import parse_config

        return parse_config(
            {
                "target_repo": "/tmp/x",
                "project_branch": "work",
                "plan_root": "PLAN.md",
                "test_command": "t",
                "planner": {"model": "m"},
                "executor": {"model": "m"},
                "reviewer": {"model": "m"},
                "stage_defaults": {"checks": checks},
            }
        )

    def _text(self, checks):
        return leading_text(
            build_planner_messages(
                cfg=self._cfg(checks), plan=a_plan(), completed=[], layout="-"
            )
        )

    def test_the_configured_commands_are_named(self):
        assert "some-linter --fix" in self._text(["some-linter --fix"])

    def test_it_says_they_run_after_the_executor_finishes(self):
        text = self._text(["some-linter --fix"]).lower()
        assert "after" in text and "commit" in text

    def test_it_states_the_blast_radius_rather_than_asking_for_care(self):
        # The general fact, and the one that was actually violated: an edit can
        # strand whitespace that is no longer legal, so a constraint naming an
        # exact set of changed lines is unsatisfiable. Phrased as a property of
        # the tooling, because a rule asking the planner to be careful would be
        # routed around rather than followed.
        assert "strand" in self._text(["some-linter --fix"]).lower()

    def test_a_project_with_no_checks_gets_no_block(self):
        # Nothing runs, so there is nothing to declare, and a paragraph about
        # a step that does not happen is one more thing to reason past.
        assert "some-linter" not in self._text([])
        assert "may rewrite" not in self._text([])

    def test_it_sits_inside_the_cached_prefix(self):
        # Fixed for the whole run, like the plan and the layout. Behind the
        # breakpoint it would be re-billed on every planner call.
        messages = build_planner_messages(
            cfg=self._cfg(["some-linter --fix"]),
            plan=a_plan(),
            completed=[],
            layout="-",
        )
        assert "some-linter --fix" in leading_text(messages)

    def test_the_block_carries_no_project_vocabulary(self):
        # The rule that keeps this generic: the commands arrive from config,
        # so the prose around them must not smuggle in the shape of whatever
        # project happened to be in front of whoever wrote it.
        text = self._text(["some-linter --fix"]).lower()
        for name in (
            "rubocop", "ruby", "rails", "eslint", "prettier", "gofmt",
            "black", ".rb", "spec/", "bundle",
        ):
            assert name not in text, f"project vocabulary leaked: {name}"


class TestThePlannerIsToldToStateTheEndState:
    """Instruction *form*, measured rather than argued.

    Over one run of 48 stages the reviewer returned 11 rejections. Four were
    the executor writing an empty file. Five conceded the behaviour and
    rejected the shape — "the coverage is present, but the ordering constraint
    was violated", "functionally aligned, but does not follow the exact-content
    requirement". Two more were instructions that could not be satisfied at
    all, one of them for contradicting its own scope.

    So seven of eleven were bought by the instruction, and the four that were
    genuine executor failures are a mode prescription cannot help with: an
    empty file satisfies an exact instruction exactly as poorly as a loose one.

    The reviewer is not at fault and is deliberately not changed. It already
    routes a difference it cannot trace to a consequence into `observations`.
    It rejected these because the instruction *made* placement a requirement,
    which turns a compliance check into a real one. The fix is upstream: stop
    writing the requirement that way.
    """

    def _system(self):
        from orchestrator.planner import _system_blocks

        return _system_blocks()[0]["text"]

    def test_it_asks_for_the_end_state_rather_than_the_edit(self):
        text = self._system().lower()
        assert "end state" in text

    def test_it_warns_that_the_instruction_is_a_reject_criterion(self):
        # The reason the rule bites. A placement mentioned in passing is
        # enforced as though it were the point of the stage.
        assert "reject criterion" in self._system()

    def test_it_says_an_edit_is_unsatisfiable_once_partly_true(self):
        # The revision case, and the one that deadlocked a stage: the earlier
        # attempt's work is on the branch, so an instruction phrased as the
        # edit describes a change that has already partly happened.
        assert "already true" in self._system()

    def test_the_line_is_authoring_code_not_quoting_it(self):
        # The rule is a bright line — the planner writes no code — and it fails
        # if read as "say less". The executor cannot see the plan or the
        # repository beyond what it is given, so quoting what exists is how it
        # gets its evidence. Only composing the replacement is forbidden.
        text = self._system().lower()
        assert "you do not write code" in text
        assert "quoting the repository is not writing code" in text

    def test_it_names_the_field_that_replaces_a_quoted_block(self):
        # The prohibition is enforced mechanically, so the guidance has to say
        # where the code goes instead — otherwise the cheapest way to satisfy
        # the validator is to drop the context rather than move it.
        text = self._system()
        assert "`read_excerpts`" in text
        assert "Quote by reference, not by transcription" in text

    def test_a_required_literal_is_not_treated_as_an_exception(self):
        # The loophole to close. A value that must match something elsewhere is
        # a property — name it and say what it agrees with — not a licence to
        # write the surrounding code.
        assert "A required literal is not an exception" in self._system()

    def test_stage_size_is_not_defaulted_from_one_deployment(self):
        """The framework states the mechanism; the run states the numbers.

        This bullet used to carry a default — prefer many small stages, seventy
        files is closer to seventy stages — and said outright that it was "a
        statement about the executor rather than about the work", "tuned to a
        local model with modest headroom". That is one deployment's tuning
        shipped in the framework's system prompt to every project, which is the
        rule about project knowledge belonging in config, one level up from
        where it usually breaks.

        The cost was visible: this project's guidance spent some fifty lines
        countermanding it. What replaces it is the mechanism — a stage lands
        completely or not at all, so a failure reverts the whole batch — plus a
        channel that exists everywhere and is measured rather than assumed.
        """
        text = self._system()
        assert "stage-costs.md" in text
        assert "modest headroom" not in text
        assert "one file per stage" not in text

    def test_the_guidance_carries_no_project_vocabulary(self):
        # Same rule as the reviewer's. This string ships to every project's
        # planner, and a paragraph of advice illustrated with one stack's
        # vocabulary is that stack's hint shipped everywhere.
        text = self._system().lower()
        for word in (
            "rails", "ruby", "gemfile", "rspec", "attr_accessible",
            ".erb", "activerecord", "bundler",
        ):
            assert word not in text, f"{word!r} is project knowledge in a prompt"


class TestExecutorPromptCarriesNoProjectVocabulary:
    """The same rule as the reviewer's, on the prompt that had no test.

    This one ships to every project's executor and was one edit away from
    carrying "a spec with no examples" — a framework's vocabulary, in a string
    the planner never sees and cannot correct.

    Only the static scaffolding is checked. `instruction`, `constraints` and
    `acceptance` are the planner's, about one repository, and are supposed to
    name its files.
    """

    def test_the_scaffolding_names_no_framework(self):
        from orchestrator.config import Stage, parse_config
        from orchestrator.prompts import build_executor_prompt

        cfg = parse_config(
            {
                "target_repo": ".",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "test_command": "true",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        stage = Stage(
            id="s",
            instruction="INSTRUCTION",
            edit_files=["EDIT"],
            read_files=["READ"],
            forbidden_patterns=["FORBIDDEN"],
            require_new_tests=True,
        )
        text = build_executor_prompt(
            stage, cfg, feedback=["FEEDBACK"], failure_layer="residue"
        ).lower()
        for word in (
            "rails", "ruby", "gemfile", "rspec", "attr_accessible",
            ".erb", "activerecord", "bundler", "app/", "spec/", "example",
        ):
            assert word not in text, f"{word!r} is project knowledge in a prompt"


class TestTheExecutorSystemPrompt:
    """Static, first, and replaceable.

    The ordering is the caching strategy — this block is byte-identical across
    every stage of a run, which is what lets it sit inside the breakpoint and
    be read rather than written on each call. But the reason it exists at all
    is quieter than caching: a failure in the edit tool announces itself as a
    refusal, and a failure in the prompt shows up as worse code with nothing
    to point at.
    """

    def test_it_states_the_tool_contract_and_what_runs_after(self, tmp_path):
        from orchestrator.prompts import _executor_system_prompt

        text = _executor_system_prompt(_exec_cfg(tmp_path))
        assert "exactly once" in text
        assert "none are applied" in text
        assert "refused by the tool" in text
        # What happens when it stops is the half a model cannot discover.
        assert "runs the project's checks" in text
        assert "no tool to do so" in text

    def test_it_names_no_projects_vocabulary(self, tmp_path):
        from orchestrator.prompts import _executor_system_prompt

        text = _executor_system_prompt(_exec_cfg(tmp_path)).lower()
        for word in ("rails", "rspec", "ruby", "python", "django", ".rb", ".py"):
            assert word not in text, word

    def test_an_operator_file_replaces_it(self, tmp_path):
        from orchestrator.prompts import _executor_system_prompt

        (tmp_path / "PROMPT.md").write_text("Follow the house style.\n")
        cfg = _exec_cfg(tmp_path, system_prompt_file="PROMPT.md")
        assert _executor_system_prompt(cfg) == "Follow the house style."

    def test_a_named_file_that_cannot_be_read_raises(self, tmp_path):
        import pytest

        from orchestrator.prompts import _executor_system_prompt

        cfg = _exec_cfg(tmp_path, system_prompt_file="missing.md")
        # Not a silent fallback to the default: an operator who named a file
        # meant that file, and a prompt quietly reverting is the failure that
        # shows up as worse code rather than as an error.
        with pytest.raises(FileNotFoundError, match="system_prompt_file"):
            _executor_system_prompt(cfg)

    def test_the_static_region_is_marked_and_the_stage_is_not(self, tmp_path):
        from orchestrator.prompts import build_executor_messages

        cfg = _exec_cfg(tmp_path)
        from orchestrator.config import Stage

        stage = Stage(id="s", instruction="do", edit_files=["a.py"])
        messages = build_executor_messages(
            stage, cfg, "THE STAGE", agent_context="CONVENTIONS", feedback=["FB"]
        )
        marked = [
            m for m in messages
            if any("prompt_cache_breakpoint" in part for part in m["content"])
        ]
        assert len(marked) == 1, "exactly one breakpoint closes the static region"
        assert "CONVENTIONS" in marked[0]["content"][0]["text"]
        # The stage and its feedback follow the mark, so they may vary without
        # invalidating what precedes them.
        tail = "".join(m["content"][0]["text"] for m in messages[2:])
        assert "THE STAGE" in tail and "FB" in tail


def _exec_cfg(tmp_path, **executor_over):
    from orchestrator.config import parse_config

    executor = {"model": "m", "provider": "openai"}
    executor.update(executor_over)
    return parse_config({
        "target_repo": str(tmp_path),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "executor": executor,
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    })


class TestTheConventionsAreFramedForWhoReadsThem:
    """One document, two jobs, and the framing is not interchangeable.

    Written for the reviewer and reused verbatim for the executor, this told
    something whose entire job is to write code that it was judging a diff.
    Nothing fails when a prompt is wrong in this way — the only symptom is
    worse work, which is why it is worth a test rather than a careful reading.
    """

    def test_the_executor_is_not_told_it_is_judging(self):
        from orchestrator.prompts import _conventions_block

        text = _conventions_block("SOME CONVENTIONS", role="executor").lower()
        assert "judging" not in text
        assert "the diff you are judging" not in text
        assert "what you write" in text

    def test_the_executor_is_told_it_cannot_run_commands(self):
        # The repository's own file makes this argument about why its
        # operations document is kept separate: an agent handed a coding task
        # follows a command it cannot run rather than ignoring it.
        from orchestrator.prompts import _conventions_block

        text = _conventions_block("SOME CONVENTIONS", role="executor")
        assert "cannot run commands" in text

    def test_the_reviewers_framing_is_unchanged(self):
        # It sits inside a cached prefix that is written once per run, so a
        # change here is a cache miss on every stage as well as a change of
        # meaning.
        from orchestrator.prompts import _conventions_block

        text = _conventions_block("SOME CONVENTIONS")
        assert "the diff you are judging" in text
        assert "a defect even where" in text

    def test_neither_invents_a_heading_when_there_is_no_document(self):
        from orchestrator.prompts import _conventions_block

        assert _conventions_block(None, role="executor") == ""
        assert _conventions_block("   ") == ""


class TestTheTwoBehaviouralRulesTheOldEditorHad:
    """Both were in the editor this replaces, and neither is about format.

    Its prompt was mostly SEARCH/REPLACE syntax, which a tool call makes
    unnecessary — dropping that is the point. But two of its rules were about
    conduct rather than encoding, and dropping those was an oversight:
    `lazy_prompt` ("NEVER leave comments describing code without implementing
    it") and `overeager_prompt` ("Do what they ask, but no more").

    Worded here as facts about this system rather than as exhortation. "A
    change outside what the stage asked for is rejected" is checkable against
    the reviewer's behaviour; "Do not improve... in any way!" is shouting.
    """

    def test_a_placeholder_is_named_as_not_being_a_change(self, tmp_path):
        from orchestrator.prompts import _executor_system_prompt

        text = _executor_system_prompt(_exec_cfg(tmp_path))
        assert "TODO" in text
        assert "is not a change" in text
        # And the honest alternative, so "stop" is a real option rather than
        # the model's only out being a stub.
        assert "say so " in text and "reaches a human" in text

    def test_scope_within_a_permitted_file_is_named_as_the_models_own(self, tmp_path):
        # The tool enforces file-level scope and cannot enforce this one, so
        # the prompt has to say which half is which — otherwise "scope is
        # refused at the tool" reads as covering everything.
        from orchestrator.prompts import _executor_system_prompt

        text = _executor_system_prompt(_exec_cfg(tmp_path))
        assert "Within a file it may legitimately edit" in text
        assert "rejected even when it is an improvement" in text

    def test_neither_rule_names_a_projects_vocabulary(self, tmp_path):
        from orchestrator.prompts import _executor_system_prompt

        text = _executor_system_prompt(_exec_cfg(tmp_path)).lower()
        for word in ("rails", "rspec", "rubocop", "ruby", "python", ".rb"):
            assert word not in text, word


class TestTheReadListIsDescribedAsAHintNotAFence:
    """It stopped being a permission list when the executor got a read tool.

    The subprocess editor had only the files it was handed, so "context you may
    read" was literally true. The in-process one has `read_file` pointed at the
    whole repository and `RepoReader` never consulted `stage.read_files` — so
    the wording promised a fence that does not exist.

    Which matters more than tidiness: a model that believes it is confined to a
    list will quote from memory rather than read, and quoting from memory is
    what produced a 38% edit-refusal rate, 54% of it on one 1,700-line file.
    """

    def test_it_does_not_claim_to_be_the_limit_of_what_may_be_read(self, tmp_path):
        from orchestrator.config import Stage
        from orchestrator.prompts import build_executor_prompt

        stage = Stage(
            id="s", instruction="do", edit_files=["a.py"], read_files=["b.py"]
        )
        text = build_executor_prompt(stage, _exec_cfg(tmp_path))
        assert "not a permission list" in text
        assert "may read anything in the repository" in text

    def test_it_still_says_the_write_list_is_enforced(self, tmp_path):
        # The asymmetry is the point: reading a file the planner did not
        # anticipate cannot damage the repository, writing one can.
        from orchestrator.config import Stage
        from orchestrator.prompts import build_executor_prompt

        stage = Stage(
            id="s", instruction="do", edit_files=["a.py"], read_files=["b.py"]
        )
        text = build_executor_prompt(stage, _exec_cfg(tmp_path))
        assert "that one is enforced" in text

    def test_a_stage_with_no_read_files_says_nothing(self, tmp_path):
        from orchestrator.config import Stage
        from orchestrator.prompts import build_executor_prompt

        stage = Stage(id="s", instruction="do", edit_files=["a.py"])
        assert "drawn against" not in build_executor_prompt(stage, _exec_cfg(tmp_path))
