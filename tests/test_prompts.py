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

from code_gantry.promptfiles import raw, render, text
from code_gantry.prompts import build_planner_messages, build_review_messages
from test_config import as_test_tools


def line_of(name):
    """Enough of a prompt file to say it is present: its longest line that
    carries no placeholder. The words are the file's to change."""
    lines = [l.strip() for l in raw(name).splitlines() if "$" not in l and l.strip()]
    return max(lines, key=len)


def a_plan(text="do the thing"):
    """The plan as the planner receives it: rendered text with keys."""
    return f"# Plan {{#p.001}}\n\n- [ ] {{#p.002}} **{text}**\n"


def _cfg(addendum=None, cache_ttl="1h"):
    return SimpleNamespace(
        planner=SimpleNamespace(cache_ttl=cache_ttl),
        ledger=SimpleNamespace(key_prefix="p", note_chars=600, fold_ratio=0.25),
    )


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
        from code_gantry.planner import _system_blocks

        blocks = _system_blocks(cache_ttl="1h")
        assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_no_ttl_leaves_the_provider_default(self):
        from code_gantry.planner import _system_blocks

        assert _system_blocks()[0]["cache_control"] == {"type": "ephemeral"}

    def test_the_configured_ttl_reaches_the_plan_and_layout_marker(self):
        # The block above is the small one. This is the ninety-thousand-token
        # one, and it shipped without a TTL — so it expired between every pair
        # of planner calls while the system block, the only marker that carried
        # the configured lifetime, survived. Live over two runs: 3% cached,
        # where the 3% was the system block and nothing else.
        cfg = SimpleNamespace(planner=SimpleNamespace(cache_ttl="1h"))
        messages = build_planner_messages(
            cfg=cfg, plan_text=a_plan(), completed=[], layout="- `src/` (1)"
        )
        assert messages[0]["content"][0]["cache_control"] == {
            "type": "ephemeral",
            "ttl": "1h",
        }

    def test_a_project_without_a_ttl_still_builds(self):
        # cache_ttl is optional, and a missing one must not raise on a path
        # every planner call takes.
        messages = build_planner_messages(
            cfg=SimpleNamespace(planner=SimpleNamespace(cache_ttl=None)), plan_text=a_plan(), completed=[]
        )
        assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


class TestThePlanBlockCarriesTheConfiguredLifetime:
    """The regression this class exists for.

    `cache_ttl` lives on `PlannerConfig` and this builder is handed the
    `ProjectConfig`. The site read `getattr(cfg, "cache_ttl", None)`, which is
    not on that type, so the default answered `None` and the largest block in
    the request shipped a bare five-minute marker — while every test here
    passed, because the fixtures were `SimpleNamespace(cache_ttl=...)`, a shape
    production never sees. The fixture supplied what production could not
    reach.

    So this drives a real `ProjectConfig` rather than a stand-in.
    """

    def _real_cfg(self, ttl):
        from code_gantry.config import PlannerConfig, ProjectConfig
        import dataclasses
        cfg = ProjectConfig.model_construct(
            planner=PlannerConfig.model_construct(
                model="anthropic/claude-opus-5", cache_ttl=ttl
            )
        )
        return cfg

    def _plan_block(self, ttl):
        messages = build_planner_messages(
            cfg=self._real_cfg(ttl), plan_text=a_plan(), completed=[]
        )
        blocks = messages[0]["content"]
        marked = [b for b in blocks if b.get("cache_control")]
        assert len(marked) == 1, "the plan block is the only marked one here"
        return marked[0]

    def test_the_configured_lifetime_reaches_the_plan_block(self):
        assert self._plan_block("1h")["cache_control"] == {
            "type": "ephemeral",
            "ttl": "1h",
        }

    def test_an_unset_lifetime_leaves_a_bare_marker(self):
        assert self._plan_block(None)["cache_control"] == {"type": "ephemeral"}

    def test_a_config_without_a_planner_is_a_shape_error_not_a_default(self):
        # The failure mode that hid this: a missing attribute answered by a
        # default is indistinguishable from an operator leaving a field unset.
        from types import SimpleNamespace
        import pytest as _pytest

        with _pytest.raises(AttributeError) as e:
            build_planner_messages(
                cfg=SimpleNamespace(), plan_text=a_plan(), completed=[]
            )
        assert "cache_ttl" in str(e.value)


class TestPlannerCacheBreakpoint:
    def test_the_stable_prefix_is_marked_cacheable(self):
        messages = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=[], layout="- `src/` (1)"
        )
        blocks = messages[0]["content"]
        assert isinstance(blocks, list), "a string cannot carry cache_control"
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}

    def test_the_plan_and_layout_are_inside_the_cached_block(self):
        messages = build_planner_messages(
            cfg=None,
            plan_text=a_plan("PLAN_MARKER"),
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
            plan_text=a_plan(),
            completed=[],
            layout="LAYOUT_MARKER",
            stage_costs=[{"merge_sha": "abc123def456", "stage_id": "COST_MARKER",
                          "context_tokens": 1, "files": 2}],
            interventions_used=3,
            interventions_max=12,
        )
        assert "COST_MARKER" not in leading_text(messages)
        assert "COST_MARKER" in messages[0]["content"][-1]["text"]

    def test_the_budget_countdown_is_not_cached(self):
        # It decrements on interventions, so caching it would defeat the point.
        messages = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=[],
            interventions_used=3, interventions_max=12,
        )
        budget = render("planner/budget", remaining=9, max=12)
        assert budget not in leading_text(messages)
        assert budget in all_text(messages)

    def test_one_breakpoint_in_the_message_and_no_more(self):
        """After the plan, and nowhere else in the message.

        There were two. The second closed the completed history, which is
        byte-identical across the turns of a derivation and was worth marking
        while it sat next to nothing that churned. The progress log now leads
        that block — it came out of the plan block so a landing stops
        discarding the plan with it — and a mark after content that changes
        every landing writes an entry nobody reads, at cache-write rates. That
        is worse than no mark, which is the same reason the cost table has
        never carried one.

        So the message spends one and the budget has a spare. Marking the log
        would cost money; marking the history behind it would never hit; and
        putting the history first to save its mark would reorder what the
        planner reads to protect ~4KB.
        """
        messages = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=[], layout="x"
        )
        marked = [
            b for m in messages
            if isinstance(m["content"], list)
            for b in m["content"]
            if "cache_control" in b
        ]
        assert len(marked) == 1

    def test_the_prefix_is_identical_across_calls_within_a_stage(self):
        # Caching depends on a byte-identical prefix. Anything varying here —
        # a timestamp, a counter — silently costs full price every call.
        first = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=[], layout="L",
            stage_costs=[{"merge_sha": "a" * 12, "stage_id": "a",
                          "context_tokens": 1, "files": 1}],
        )
        second = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=[], layout="L",
            stage_costs=[{"merge_sha": "b" * 12, "stage_id": "b",
                          "context_tokens": 2, "files": 1}],
        )
        # The cached blocks, not the whole message: the volatile tail is
        # expected to differ, which is why it is outside the breakpoints.
        assert first[0]["content"][:2] == second[0]["content"][:2]


class TestARunsHistoryIsNotTheProjectsHistory:
    """An empty completed list is this run's history, not the project's, and
    the file that says so is sent only then."""

    def test_an_empty_history_sends_the_empty_case_file(self):
        text_ = all_text(build_planner_messages(_cfg(), a_plan(), []))
        assert text("planner/history_empty") in text_

    def test_a_populated_history_does_not_get_the_empty_case(self):
        text_ = all_text(
            build_planner_messages(
                _cfg(), a_plan(), [{"index": 0, "id": "s1", "instruction": "did it"}]
            )
        )
        assert "s1" in text_
        assert text("planner/history_empty") not in text_
        assert text("planner/history_head") in text_


class TestTheProjectionIsNamedAsTheRecordOfWhatIsDone:
    """The intro that explains keys leads the plan text, and the projection
    section appears only when there is a projection."""

    def test_the_intro_leads_the_plan_text(self):
        leading = leading_text(build_planner_messages(_cfg(), a_plan(), []))
        intro = render("planner/plan_intro", prefix="p")
        assert intro in leading
        assert leading.index(intro) < leading.index("do the thing")

    def test_the_projection_reaches_the_planner_after_the_mark(self):
        messages = build_planner_messages(
            _cfg(), a_plan(), [], projection="### Landed\n\n- {#p.002} — `abc`"
        )
        blocks = messages[0]["content"]
        assert "{#p.002} — `abc`" not in blocks[0]["text"]
        assert "{#p.002} — `abc`" in blocks[1]["text"]

    def test_an_empty_projection_adds_no_section(self):
        text_ = all_text(build_planner_messages(_cfg(), a_plan(), [], projection=""))
        assert line_of("planner/projection") not in text_


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
        from code_gantry.planner import _system_blocks

        blocks = _system_blocks(guidance="Prefer stages of one file each.")
        assert "Prefer stages of one file each." in blocks[0]["text"]

    def test_guidance_is_inside_the_cached_block(self):
        # It is fixed for the run, so it belongs in the prefix rather than
        # being re-sent uncached on every call.
        from code_gantry.planner import _system_blocks

        blocks = _system_blocks(guidance="G", cache_ttl="1h")
        assert len(blocks) == 1
        assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_no_guidance_changes_nothing(self):
        from code_gantry.planner import _system_blocks

        assert _system_blocks()[0]["text"] == _system_blocks(guidance="")[0]["text"]

    def test_guidance_is_framed_by_its_file_at_the_end(self):
        from code_gantry.planner import _system_blocks

        text_ = _system_blocks(guidance="G")[0]["text"]
        assert text_.endswith("\n\n" + render("planner/guidance", guidance="G"))

    def test_it_is_not_a_planner_writable_field(self):
        from code_gantry.config import PLANNER_WRITABLE_FIELDS
        from code_gantry.planner import PlannedStage

        assert "guidance" not in PlannedStage.model_fields
        assert "guidance" not in PLANNER_WRITABLE_FIELDS


class TestTheZeroDiffHandoff:
    """The prompt names a heading `nodes` builds, so the two have to agree.

    `_consume_executor_note` folds the executor's closing words into the
    failure detail under a heading of its own, and the "produced no changes"
    bullet tells the planner to read it by that name. Two maintained
    statements of one string: reword the note and the prompt is pointing at a
    heading nothing produces.
    """

    def test_the_heading_the_prompt_names_is_the_one_nodes_builds(self):
        from code_gantry.nodes import _consume_executor_note
        from code_gantry.planner import PLANNER_SYSTEM_PROMPT

        detail, _ = _consume_executor_note({"executor_note": "NOTE"}, "DETAIL")
        heading = "what the executor said when it stopped"
        assert heading in detail.lower()
        assert heading in " ".join(PLANNER_SYSTEM_PROMPT.lower().split())


class TestThePlannerPromptCarriesNoProjectVocabulary:
    """The rule the reviewer's and executor's prompts already have a guard for.

    Only the static string is checked. The capability paragraphs are
    substituted from `project_tools` and are supposed to name one project's
    vocabulary; this is about the contract they are substituted into.
    """

    def test_it_names_no_framework_or_layout(self):
        from code_gantry.planner import PLANNER_SYSTEM_PROMPT

        lowered = PLANNER_SYSTEM_PROMPT.lower()
        for word in (
            "rails", "ruby", "rspec", "gemfile", "django", "npm",
            "spec/", "app/", "src/", ".rb", ".py", ".erb", "activerecord",
        ):
            assert word not in lowered, f"{word!r} is one project's vocabulary"


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
        from code_gantry.config import Stage
        from code_gantry.prompts import build_review_messages

        args = dict(
            stage=Stage(id="s", instruction="do it", edit_files=["a.py"]),
            cfg=None,
            diff="--- a\n+++ b\n",
            plan_text=a_plan("PLAN"),
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
    """A project declaring no tools is told, through the capability
    paragraph, that the executor has none to run; the schema says so too."""

    def test_the_no_tools_paragraph_reaches_the_rendered_prompt(self):
        # On the rendered prompt, not the constant: the paragraph is generated
        # from the project's declared tools.
        from code_gantry.planner import _system_blocks

        assert text("planner/capability_no_declared_tools") in _system_blocks()[0]["text"]

    def test_the_field_description_says_it_too(self):
        # The planner sees field descriptions even when it skims the prose.
        from code_gantry.planner import PlannedStage

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
        from code_gantry.config import Stage
        from code_gantry.prompts import build_review_messages

        return build_review_messages(
            stage=Stage(id="s", instruction="i", edit_files=["a.py"]),
            cfg=None,
            diff=diff,
            plan_text=a_plan("PLAN_TEXT"),
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
        from code_gantry.prompts import build_planner_messages

        messages = build_planner_messages(
            cfg=None, plan_text=a_plan("PLAN_TEXT"), completed=[], layout="LAYOUT_TEXT"
        )
        marked = messages[0]["content"][0]
        assert "PLAN_TEXT" in marked["text"]
        assert "LAYOUT_TEXT" in marked["text"]
        assert "cache_control" in marked
        # And the volatile tail is deliberately not marked.
        assert "cache_control" not in messages[0]["content"][-1]

    def test_the_history_is_outside_the_marked_block(self):
        from code_gantry.prompts import build_planner_messages

        landed = [{"id": "earlier", "index": 0, "merge_sha": "abc123"}]
        messages = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=landed, layout="L"
        )
        assert "earlier" not in leading_text(messages)

    def test_the_prefix_is_identical_before_and_after_a_landing(self):
        """The plan block must survive a landing untouched.

        The history block is *expected* to change — it grew — but it grows only
        at the end, which is what lets the provider extend the cached prefix
        rather than rebuild it.
        """
        from code_gantry.prompts import build_planner_messages

        one = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=[{"id": "x", "index": 0}], layout="L"
        )
        two = build_planner_messages(
            cfg=None, plan_text=a_plan(),
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

    def test_the_history_still_reaches_the_planner(self):
        from code_gantry.prompts import build_planner_messages

        landed = [{"id": "earlier", "index": 0, "merge_sha": "abc123"}]
        messages = build_planner_messages(
            cfg=None, plan_text=a_plan(), completed=landed, layout="L"
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


class TestTheBreakpointBudgetIsFullySpent:
    """Four is the API's limit; three are in use and the fourth is spare.

    The system prompt and the plan-and-layout block are static and carry the
    configured lifetime. The third moves with the tool loop and is added at
    request time by `_with_loop_breakpoint`.

    The history used to hold the fourth. It lost it when the progress log
    moved in front of it: a mark after content that changes every landing is
    written and never read, which costs more than not marking. Leaving one
    unspent is the deliberate state, not an oversight — pinned here so that
    spending it is a decision somebody makes rather than a drift.

    Still pinned at the ceiling because exceeding the limit fails the request,
    not a test: every planner call in the run would break at once, and the
    cause would read as a transport error.
    """

    def _marks(self, blocks):
        return [b for b in blocks if isinstance(b, dict) and "cache_control" in b]

    def test_the_built_message_spends_exactly_one(self):
        from code_gantry.prompts import build_planner_messages

        messages = build_planner_messages(
            cfg=SimpleNamespace(planner=SimpleNamespace(cache_ttl="1h")),
            plan_text=a_plan(),
            completed=[{"index": 0, "id": "s1", "instruction": "did it"}],
            layout="- `src/` (1)",
        )
        assert len(messages) == 1, "one user message; the loop appends after it"
        assert len(self._marks(messages[0]["content"])) == 1

    def test_system_plus_message_plus_the_moving_one_stays_inside_four(self):
        from code_gantry.dialects import MESSAGES
        from code_gantry.planner import _system_blocks
        from code_gantry.prompts import build_planner_messages

        messages = build_planner_messages(
            cfg=SimpleNamespace(planner=SimpleNamespace(cache_ttl="1h")), plan_text=a_plan(), completed=[]
        )
        system = _system_blocks("1h")
        outgoing = MESSAGES.mark_latest(messages)

        total = len(self._marks(system)) + sum(
            len(self._marks(m["content"])) for m in outgoing
        )
        assert total == 3, f"three in use, one spare; found {total}"
        assert total <= 4, "the API allows 4 cache breakpoints"


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
            plan_text=a_plan(),
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
            plan_text=a_plan(),
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

    def test_it_is_rendered_through_the_stage_diff_file(self):
        assert render("planner/stage_diff", diff=self.DIFF) in all_text(self._messages())

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


class TestTheReviewerReadsTheProjection:
    """What has been done sits after the reviewer's breakpoint, and the
    history is capped.

    GPT-5.6 caches at an explicit breakpoint and does not fall back to the
    longest matching prefix, so a growing region placed before it misses on
    every landing. The plan text stays in the cached prefix; the projection
    and the tail go after it.
    """

    def _messages(self, projection="### Landed\n\n- {#p.002} — it was done", completed=None, proposed=None, **cfg_over):
        return build_review_messages(
            stage=SimpleNamespace(
                id="s", instruction="do it", constraints=None, acceptance=None,
                resolves=[],
            ),
            cfg=_review_cfg(**cfg_over),
            diff="--- a\n+++ b",
            plan_text="THE PLAN ITSELF",
            completed=completed if completed is not None else [],
            projection=projection,
            proposed=proposed,
        )

    def test_the_projection_is_in_the_prompt(self):
        assert "it was done" in all_text(self._messages())

    def test_the_projection_sits_after_the_breakpoint(self):
        messages = self._messages()
        cached = "".join(
            b["text"] for b in messages[1]["content"] if "prompt_cache_breakpoint" in b
        )
        assert "it was done" not in cached

    def test_the_plan_text_stays_cached(self):
        messages = self._messages()
        cached = "".join(
            b["text"] for b in messages[1]["content"] if "prompt_cache_breakpoint" in b
        )
        assert "THE PLAN ITSELF" in cached

    def test_proposed_resolutions_are_listed_after_the_mark(self):
        messages = self._messages(proposed=[("f-h-3", "two callers remain")])
        cached = "".join(
            b["text"] for b in messages[1]["content"] if "prompt_cache_breakpoint" in b
        )
        text_ = all_text(messages)
        assert render("reviewer/proposed", listed="- `f-h-3` — two callers remain") in text_
        assert "f-h-3" not in cached

    def test_the_history_is_capped(self):
        completed = [
            {"index": i, "id": f"stage-{i}", "instruction": f"work {i}"}
            for i in range(30)
        ]
        text = all_text(self._messages(completed=completed, history_stages=10))
        assert "stage-29" in text, "the most recent stages are the ones kept"
        assert "stage-19" not in text, "an older stage is dropped"

    def test_the_cap_says_what_it_dropped(self):
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

    def test_an_empty_projection_still_builds(self):
        assert "do it" in all_text(self._messages(projection=""))


def _review_cfg(history_stages=None, addendum=None):
    return SimpleNamespace(
        cache_ttl=None,
        ledger=SimpleNamespace(key_prefix="p", note_chars=600, fold_ratio=0.25),
        reviewer=SimpleNamespace(history_stages=history_stages),
    )


class TestTheAgentContextRidesInTheCachedPrefix:
    """Repository conventions, where the planner will actually see them.

    Static for the run — read once at the plan sha — so it belongs before the
    breakpoint with the plan and the layout, not after it with the situation.
    """

    def _messages(self, agent_context="### `AGENTS.md`\n\nthe bundle installs itself"):
        return build_planner_messages(
            cfg=_cfg(), plan_text=a_plan(), completed=[],
            layout="- `app/` (1)", agent_context=agent_context,
        )

    def test_it_reaches_the_planner(self):
        assert "the bundle installs itself" in all_text(self._messages())

    def test_it_is_inside_the_cached_prefix(self):
        cached = self._messages()[0]["content"][0]
        assert "cache_control" in cached
        assert "the bundle installs itself" in cached["text"]

    def test_it_is_framed_by_the_planner_conventions_file(self):
        expected = render(
            "planner/conventions", agent_context="### `AGENTS.md`\n\nthe bundle installs itself"
        )
        assert expected in all_text(self._messages())

    def test_a_project_without_one_builds_normally(self):
        assert "do the thing" in all_text(self._messages(agent_context=""))


class TestReviewerToolGuidance:
    """The tool section is conditional on there being tools.

    Told it can read when it cannot, the reviewer either invents a lookup or
    hedges a verdict it should have given outright.
    """

    def _cfg(self, repo_access):
        from code_gantry.config import parse_config

        return parse_config(
            as_test_tools({
                "target_repo": "/tmp/x",
                "project_branch": "work",
                "plan_root": "PLAN.md",
                "full_test_command": "pytest",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol", "repo_access": repo_access},
            })
        )

    def _system(self, repo_access):
        from code_gantry.config import Stage

        return build_review_messages(
            stage=Stage(id="s", instruction="do it", edit_files=["a.py"]),
            cfg=self._cfg(repo_access),
            diff="--- a\n+++ b\n",
            plan_text=a_plan("PLAN"),
            completed=[],
        )[0]["content"][0]["text"]

    def _tools(self):
        return render("reviewer/tools", state_not_change=text("shared/state_not_change"))

    def test_absent_without_repo_access(self):
        assert self._tools() not in self._system(False)

    def test_present_with_repo_access(self):
        assert self._tools() in self._system(True)

    def test_the_guidance_carries_no_project_vocabulary(self):
        # This string ships to every project's reviewer. An example drawn from
        # one stack is a hint about a repository it may not be looking at.
        # Only tokens that cannot be ordinary English. "permit" and "form" are
        # excluded deliberately: the base contract already says "permitted to
        # edit", and rejecting that would be the test dictating prose rather
        # than catching a leak.
        text_ = self._system(True).lower()
        for word in (
            "rails", "ruby", "gemfile", "rspec", "attr_accessible",
            ".erb", "activerecord", "bundler", "app/", "spec/",
        ):
            assert word not in text_, f"{word!r} is project knowledge in a prompt"


class TestEveryParticipantSeesTheRepositoryConventions:
    """The document that says how this repository is worked in.

    It reached the planner only. The reviewer — the gate that would catch a
    violation — never saw it, and a gate that cannot reach what decides its
    verdict restates the stage instruction in its own voice. That was live: an
    executor recased a SQL keyword against a convention documented in the
    repository, and the only reason the reviewer caught it was that the stage
    happened to pin the exact output string.

    The executor gets the same documents by a different route — `--read`, not
    the prompt — because a tool that scans its message attaches every path
    named in it. That
    is `TestConventionsReachTheExecutorAsReads` in the executor's tests.
    """

    CONVENTIONS = "## Shop scoping\n\nAlways scope by the current tenant."

    def test_the_reviewer_prompt_carries_it(self):
        messages = build_review_messages(
            stage=SimpleNamespace(
                id="s", instruction="i", constraints=None, acceptance=None
            ),
            cfg=_review_cfg(),
            diff="--- a\n+++ b",
            plan_text="THE PLAN ITSELF",
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
            plan_text="THE PLAN ITSELF",
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
        from code_gantry.config import Stage, parse_config
        from code_gantry.prompts import build_executor_prompt

        cfg = parse_config(
            as_test_tools({
                "target_repo": ".", "base_ref": "main", "project_branch": "p",
                "plan_root": "PLAN.md", "full_test_command": "true",
                "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )
        stage = Stage(id="s", instruction="do it", edit_files=["a"])
        assert line_of("shared/conventions") not in build_executor_prompt(stage, cfg)

        messages = build_review_messages(
            stage=SimpleNamespace(
                id="s", instruction="i", constraints=None, acceptance=None
            ),
            cfg=_review_cfg(),
            diff="--- a\n+++ b",
            plan_text="THE PLAN ITSELF",
            completed=[],
        )
        assert line_of("shared/conventions") not in all_text(messages)


class TestThePlannerIsToldWhatTheChecksWillDo:
    """`checks` run after the executor commits, so the planner is told what
    they are, from config, inside the cached prefix, and nothing when there
    are none."""

    def _cfg(self, checks):
        from code_gantry.config import parse_config

        return parse_config(
            as_test_tools({
                "target_repo": "/tmp/x",
                "project_branch": "work",
                "plan_root": "PLAN.md",
                "full_test_command": "t",
                "planner": {"model": "m"},
                "executor": {"model": "m"},
                "reviewer": {"model": "m"},
                "stage_defaults": {"checks": checks},
            })
        )

    def _text(self, checks):
        return leading_text(
            build_planner_messages(
                cfg=self._cfg(checks), plan_text=a_plan(), completed=[], layout="-"
            )
        )

    def test_the_configured_commands_are_named(self):
        assert "some-linter --fix" in self._text(["some-linter --fix"])

    def test_the_checks_file_is_rendered_around_them(self):
        assert render("shared/checks", listed="- `some-linter --fix`") in self._text(["some-linter --fix"])

    def test_a_project_with_no_checks_gets_no_block(self):
        # Nothing runs, so there is nothing to declare.
        assert "some-linter" not in self._text([])
        assert line_of("shared/checks") not in self._text([])

    def test_it_sits_inside_the_cached_prefix(self):
        # Fixed for the whole run, like the plan and the layout. Behind the
        # breakpoint it would be re-billed on every planner call.
        messages = build_planner_messages(
            cfg=self._cfg(["some-linter --fix"]),
            plan_text=a_plan(),
            completed=[],
            layout="-",
        )
        assert "some-linter --fix" in leading_text(messages)

    def test_the_block_carries_no_project_vocabulary(self):
        # The commands arrive from config, so the prose around them must not
        # smuggle in the shape of whatever project was in front of its author.
        text_ = self._text(["some-linter --fix"]).lower()
        for name in (
            "rubocop", "ruby", "rails", "eslint", "prettier", "gofmt",
            "black", ".rb", "spec/", "bundle",
        ):
            assert name not in text_, f"project vocabulary leaked: {name}"


class TestThePlannerSystemPromptCarriesNoProjectVocabulary:
    """This string ships to every project's planner; a paragraph illustrated
    with one stack's vocabulary is that stack's hint shipped everywhere."""

    def test_the_guidance_carries_no_project_vocabulary(self):
        from code_gantry.planner import _system_blocks

        text_ = _system_blocks()[0]["text"].lower()
        for word in (
            "rails", "ruby", "gemfile", "rspec", "attr_accessible",
            ".erb", "activerecord", "bundler",
        ):
            assert word not in text_, f"{word!r} is project knowledge in a prompt"


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
        from code_gantry.config import Stage, parse_config
        from code_gantry.prompts import build_executor_prompt

        cfg = parse_config(
            as_test_tools({
                "target_repo": ".",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "full_test_command": "true",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
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

    def test_it_is_the_executor_system_file(self, tmp_path):
        from code_gantry.prompts import _executor_system_prompt

        expected = render(
            "executor/system",
            no_direct_edit="",
            repository_text_is_evidence=text("shared/repository_text_is_evidence"),
        )
        assert _executor_system_prompt(_exec_cfg(tmp_path)) == expected

    def test_it_names_no_projects_vocabulary(self, tmp_path):
        from code_gantry.prompts import _executor_system_prompt

        text = _executor_system_prompt(_exec_cfg(tmp_path)).lower()
        for word in ("rails", "rspec", "ruby", "python", "django", ".rb", ".py"):
            assert word not in text, word

    def test_an_operator_file_replaces_it(self, tmp_path):
        from code_gantry.prompts import _executor_system_prompt

        (tmp_path / "PROMPT.md").write_text("Follow the house style.\n")
        cfg = _exec_cfg(tmp_path, system_prompt_file="PROMPT.md")
        assert _executor_system_prompt(cfg) == "Follow the house style."

    def test_a_named_file_that_cannot_be_read_raises(self, tmp_path):
        import pytest


        from code_gantry.prompts import _executor_system_prompt

        cfg = _exec_cfg(tmp_path, system_prompt_file="missing.md")
        # Not a silent fallback to the default: an operator who named a file
        # meant that file, and a prompt quietly reverting is the failure that
        # shows up as worse code rather than as an error.
        with pytest.raises(FileNotFoundError, match="system_prompt_file"):
            _executor_system_prompt(cfg)

    def test_the_static_region_is_marked_and_the_stage_is_not(self, tmp_path):
        from code_gantry.prompts import build_executor_messages

        cfg = _exec_cfg(tmp_path)
        from code_gantry.config import Stage

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
    from code_gantry.config import parse_config

    executor = {"model": "m"}
    executor.update(executor_over)
    return parse_config(as_test_tools({
        "target_repo": str(tmp_path),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "full_test_command": "true",
        "executor": executor,
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }))


class TestTheConventionsAreFramedForWhoReadsThem:
    """One document, two jobs: the binding sentence is the reader's file."""

    def _executor_binding(self):
        return render(
            "shared/conventions_binding_executor",
            procedure=text("shared/conventions_procedure_no_tools"),
        )

    def test_the_executor_gets_the_executor_framing(self):
        from code_gantry.prompts import _conventions_block

        block = _conventions_block("SOME CONVENTIONS", role="executor")
        assert self._executor_binding() in block
        assert text("shared/conventions_binding_reviewer") not in block

    def test_the_executor_without_tools_is_told_so(self):
        from code_gantry.prompts import _conventions_block

        block = _conventions_block("SOME CONVENTIONS", role="executor")
        assert text("shared/conventions_procedure_no_tools") in block
        assert text("shared/conventions_procedure_with_tools") not in block

    def test_the_reviewer_gets_the_reviewer_framing(self):
        from code_gantry.prompts import _conventions_block

        block = _conventions_block("SOME CONVENTIONS")
        assert text("shared/conventions_binding_reviewer") in block
        assert self._executor_binding() not in block

    def test_neither_invents_a_heading_when_there_is_no_document(self):
        from code_gantry.prompts import _conventions_block

        assert _conventions_block(None, role="executor") == ""
        assert _conventions_block("   ") == ""


class TestTheReadListBlockAppearsOnlyWithReadFiles:
    def test_a_stage_with_read_files_gets_the_block(self, tmp_path):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_prompt

        stage = Stage(
            id="s", instruction="do", edit_files=["a.py"], read_files=["b.py"]
        )
        text_ = build_executor_prompt(stage, _exec_cfg(tmp_path))
        assert render("executor/read_files", listed="- b.py") in text_

    def test_a_stage_with_no_read_files_says_nothing(self, tmp_path):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_prompt

        stage = Stage(id="s", instruction="do", edit_files=["a.py"])
        assert line_of("executor/read_files") not in build_executor_prompt(stage, _exec_cfg(tmp_path))


class TestAnExcerptIsOnlyCurrentWhileTheTreeHasNotMoved:
    """Excerpts are read at the stage's starting commit. Which currency note
    goes with them is decided by the cumulative diff, not by feedback: a
    `restart` revision arrives with feedback and a tree where nothing moved."""

    def _text(self, tmp_path, **kw):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_prompt

        stage = Stage(id="s", instruction="do", edit_files=["a.py"])
        return build_executor_prompt(
            stage,
            _exec_cfg(tmp_path),
            excerpts=[("b.py:1-2", "    1 | one\n    2 | two")],
            **kw,
        )

    def test_a_first_attempt_is_told_they_are_current(self, tmp_path):
        text_ = self._text(tmp_path)
        assert text("executor/excerpts_current") in text_
        assert text("executor/excerpts_moved") not in text_

    def test_a_tree_that_has_moved_is_told_they_may_be_stale(self, tmp_path):
        text_ = self._text(
            tmp_path, feedback=["missed two"], cumulative_diff="--- a\n+++ b"
        )
        assert text("executor/excerpts_current") not in text_
        assert text("executor/excerpts_moved") in text_

    def test_a_restart_revision_keeps_the_plain_wording(self, tmp_path):
        # Feedback, but the branch was reset to the stage baseline.
        text_ = self._text(tmp_path, feedback=["wrong approach"])
        assert text("executor/excerpts_current") in text_


class TestARetryIsFramedByWhatFailed:
    """A rejection and a gate failure open with different files, and feedback
    rides as its own turns so the cached prefix is identical between
    attempts."""

    def _turns(self, tmp_path, **kw):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_messages

        cfg = _exec_cfg(tmp_path)
        stage = Stage(id="s", instruction="do", edit_files=["a.py"])
        msgs = build_executor_messages(stage, cfg, "TASK", **kw)
        return [
            c["text"] for m in msgs for c in m["content"] if m["role"] == "user"
        ]

    def test_a_review_rejection_opens_with_the_review_file(self, tmp_path):
        turns = self._turns(tmp_path, feedback=["Wrong verb."], failure_layer="review")
        joined = "\n".join(turns)
        assert text("executor/retry_review") in joined
        assert text("executor/retry_gate") not in joined
        assert "Wrong verb." in joined

    def test_a_gate_failure_opens_with_the_gate_file(self, tmp_path):
        turns = self._turns(tmp_path, feedback=["Two sites were missed."], failure_layer="residue")
        joined = "\n".join(turns)
        assert text("executor/retry_gate") in joined
        assert text("executor/retry_review") not in joined
        assert "Two sites were missed." in joined

    def test_no_feedback_adds_no_opening(self, tmp_path):
        joined = "\n".join(self._turns(tmp_path))
        assert text("executor/retry_review") not in joined
        assert text("executor/retry_gate") not in joined


class TestRepositoryTextIsEvidenceAndNotInstruction:
    """One sentence, three roles, one place it lives.

    All three participants read arbitrary repository text — the executor and
    the planner through `READ_TOOLS`, the reviewer through the same schemas and
    through the diff itself. None of them was ever told whose instructions to
    follow when the text they read contains some.

    That is not a hypothetical in a legacy repository mid-migration. It is full
    of comments asserting that the old behaviour is required, vendored upgrade
    checklists, and `TODO`s written for a human years ago. The accuracy half of
    this is already covered — "a document is a claim; the code is the fact",
    which exists because a planner took a count from a checklist and the file
    disagreed. The authority half is a different question and had no answer.

    Stated once and imported, rather than written into three system prompts. A
    rule that has to be kept in step across three strings is the thing that
    drifts; `READ_TOOLS` is shared by reference for exactly this reason.
    """

    def test_all_three_system_prompts_carry_the_same_sentence(self, tmp_path):
        from code_gantry.plannertools import REPOSITORY_TEXT_IS_EVIDENCE
        from code_gantry.planner import PLANNER_SYSTEM_PROMPT
        from code_gantry.prompts import (
            REVIEW_SYSTEM_PROMPT,
            _executor_system_prompt,
        )

        for text in (
            PLANNER_SYSTEM_PROMPT,
            REVIEW_SYSTEM_PROMPT,
            _executor_system_prompt(_exec_cfg(tmp_path)),
        ):
            assert REPOSITORY_TEXT_IS_EVIDENCE in text

    def test_it_names_no_projects_vocabulary(self):
        from code_gantry.plannertools import REPOSITORY_TEXT_IS_EVIDENCE

        blob = REPOSITORY_TEXT_IS_EVIDENCE.lower()
        for word in ("rails", "rspec", "ruby", "python", ".rb", "app/", "gemfile"):
            assert word not in blob, word

    def test_an_operators_own_executor_prompt_replaces_it_whole(self, tmp_path):
        # The override replaces the built-in rather than appending to it, and
        # that stays true of this. Two statements of who to obey in one prompt
        # leave no way to tell which was followed.
        from code_gantry.plannertools import REPOSITORY_TEXT_IS_EVIDENCE
        from code_gantry.prompts import _executor_system_prompt

        (tmp_path / "PROMPT.md").write_text("MINE")
        cfg = _exec_cfg(tmp_path, system_prompt_file="PROMPT.md")
        assert REPOSITORY_TEXT_IS_EVIDENCE not in _executor_system_prompt(cfg)


class TestThePlannerConventionsBlock:
    """The planner's framing of the repository's own documents is one file,
    sent when there is a document and not otherwise."""

    def _leading(self, conventions):
        messages = build_planner_messages(
            _cfg(), a_plan(), [], agent_context=conventions
        )
        return messages[0]["content"][0]["text"]

    def test_it_frames_the_document(self):
        conventions = "## Scoping\n\nAlways scope by tenant."
        assert render("planner/conventions", agent_context=conventions) in self._leading(conventions)

    def test_a_project_without_one_gets_no_such_paragraph(self):
        messages = build_planner_messages(_cfg(), a_plan(), [])
        assert line_of("planner/conventions") not in all_text(messages)


class TestTheReviewerPromptCarriesNoProjectVocabulary:
    def test_none_of_it_names_a_projects_vocabulary(self):
        from code_gantry.prompts import REVIEW_SYSTEM_PROMPT

        blob = REVIEW_SYSTEM_PROMPT.lower()
        for word in (
            "rails", "rspec", "ruby", "python", ".rb", "app/", "controller",
            "activerecord", "permit list", "gemfile",
        ):
            assert word not in blob, word


class TestTheScopeListSaysWhichFilesDoNotExist:
    """The prompt claimed the editor had pre-created them. It had not.

    `**A file listed here that does not exist yet has already been created for
    you, empty.**` was true of the subprocess editor, whose own commit message
    says so — "the editor creates any file it is handed" — because a path
    handed to it via `--file` was created whether or not it existed. That
    editor is gone. `build_loop_parts` creates nothing, which is checkable in
    four lines and was.

    So the prompt asserted a file into existence. A model told an empty file is
    already there reaches for `edit`, and an `edit` against a file that is not
    on disk is refused — a wasted turn whose refusal contradicts the prompt
    that caused it. It was live on the first stage of the run started today,
    which declared a brand-new spec file.

    Replaced with the fact rather than a claim about machinery: CodeGantry
    knows which of these paths exist, so it says so per entry. That cannot go
    stale the way the sentence it replaces did, and it answers the question the
    model actually has — `edit` or `create_file` — at the point where it is
    looking at the list.

    Only literal paths are annotated. A glob names no particular file, and
    reporting that `app/**` "does not exist" would be false in a new way.
    """

    def _prompt(self, tmp_path, edit_files):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_prompt

        return build_executor_prompt(
            Stage(id="s", instruction="do", edit_files=edit_files),
            _exec_cfg(tmp_path),
        )

    def test_a_missing_file_is_marked(self, tmp_path):
        text = self._prompt(tmp_path, ["spec/new_spec.rb"])
        assert "spec/new_spec.rb (does not exist yet)" in text

    def test_an_existing_file_is_not_marked(self, tmp_path):
        (tmp_path / "here.rb").write_text("x\n")
        text = self._prompt(tmp_path, ["here.rb"])
        assert "- here.rb\n" in text
        assert "does not exist yet" not in text

    def test_a_glob_is_never_marked(self, tmp_path):
        # A glob names no particular file, so "does not exist" would be a new
        # falsehood rather than a correction of the old one.
        text = self._prompt(tmp_path, ["app/**", "spec/*_spec.rb"])
        assert "does not exist yet" not in text

    def test_the_create_instruction_appears_only_when_one_is_missing(self, tmp_path):
        (tmp_path / "here.rb").write_text("x\n")
        assert text("executor/edit_files_missing") not in self._prompt(tmp_path, ["here.rb"])
        assert text("executor/edit_files_missing") in self._prompt(tmp_path, ["spec/new_spec.rb"])

    def test_it_names_no_projects_vocabulary(self, tmp_path):
        text = self._prompt(tmp_path, ["EDIT_PATH"]).lower()
        for word in ("rails", "rspec", "ruby", ".rb", "app/", "spec/", "example"):
            assert word not in text, word


class TestNoSchemaDescribesTheExecutorThatWasDeleted:
    """Field descriptions and gate messages must not describe machinery that
    is gone or name one project's vocabulary."""

    def test_the_instruction_field_does_not_ask_for_what_validation_rejects(self):
        # `validate_stage` rejects a fenced block in `instruction` outright, and
        # this field told the planner to use them.
        from code_gantry.planner import PlannedStage

        d = PlannedStage.model_fields["instruction"].description
        assert "fenced blocks" not in d

    def test_read_files_is_not_described_as_a_permission_list(self):
        from code_gantry.planner import PlannedStage

        d = PlannedStage.model_fields["read_files"].description
        assert "may read" not in d

    def test_the_empty_test_feedback_does_not_claim_the_file_was_created(self):
        # Same retired claim as the scope list carried, in the gate that fires
        # when it bites — and its remedy named the wrong tool besides.
        import inspect

        from code_gantry import gates

        src = inspect.getsource(gates)
        assert "creates a file named in your scope" not in src

    def test_no_planner_field_ships_one_projects_vocabulary(self):
        """The invariant the executor and reviewer prompts already had.

        `must_not_remain` illustrated itself with `render text:` — a framework's
        method and its argument, in a string shipped to every project's planner
        and which no project can correct. Project knowledge belongs in config.
        """
        from code_gantry.planner import PlannedStage, PlannerResponse

        blob = " ".join(
            (f.description or "")
            for model in (PlannedStage, PlannerResponse)
            for f in model.model_fields.values()
        ).lower()
        for word in (
            # Whole words or distinctive fragments only: "erb" is a
            # substring of "verbatim", which is how a naive list turns a real
            # invariant into a nuisance nobody trusts.
            "rails", "django", "rspec", "pytest", " ruby", " python",
            ".erb", ".rb", ".py", "app/", "spec/", "controller",
            "activerecord", "gemfile", "render text",
        ):
            assert word not in blob, f"{word!r} is project knowledge in a prompt"


class TestAnExcerptHeadingDoesNotSwallowItsNote:
    """The label is a path and a range; the note is prose and contains backticks.

    Observed in a live prompt. `resolve_excerpts` builds one label carrying the
    range *and* the planner's note, and the heading wrapped the whole of it in
    a code span:

        ### `config/routes.rb:1473-1493 — ... leaving `display_title` unrouted`

    The note's own backticks close the span early, so the rest renders as prose
    inside a heading meant to be one identifier. Nothing breaks — the executor
    reads raw text — but the heading is what it scans to find the right
    excerpt, and the note is the sentence saying what the range is for. Both
    are worth keeping legible.

    Split rather than escaped: the path and range go in the span, the note
    follows it. That also stops the heading growing without bound, since a note
    is free-form and one has already reached a full line.
    """

    def _headings(self, tmp_path, excerpts):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_prompt

        text = build_executor_prompt(
            Stage(id="s", instruction="do", edit_files=["a"]),
            _exec_cfg(tmp_path),
            excerpts=excerpts,
        )
        return [ln for ln in text.splitlines() if ln.startswith("### ")]

    def test_the_span_holds_only_the_path_and_range(self, tmp_path):
        headings = self._headings(
            tmp_path, [("a.rb:1-9 — what `foo` must match", "    1 | x")]
        )
        assert headings == ["### `a.rb:1-9` — what `foo` must match"]

    def test_a_label_with_no_note_is_unchanged(self, tmp_path):
        headings = self._headings(tmp_path, [("a.rb:1-9", "    1 | x")])
        assert headings == ["### `a.rb:1-9`"]

    def test_a_clip_note_stays_with_the_range(self, tmp_path):
        # `resolve_excerpts` appends the clip warning to the range itself, and
        # it is about the range rather than about the code — it belongs inside.
        label = "a.rb:1-5 (clipped from 1-40 by max_read_lines) — the list"
        headings = self._headings(tmp_path, [(label, "    1 | x")])
        assert headings == [
            "### `a.rb:1-5 (clipped from 1-40 by max_read_lines)` — the list"
        ]


class TestTheBatchBlockIsSizedByTheSetting:
    """The cap is stated to the planner, from config, and a cap of one gets
    the one-stage file instead of the invitation."""

    def _leading(self, cap):
        cfg = SimpleNamespace(
            cache_ttl=None, planner=SimpleNamespace(max_batch_stages=cap)
        )
        messages = build_planner_messages(cfg=cfg, plan_text=a_plan(), completed=[])
        return messages[0]["content"][0]["text"]

    def test_one_stage_per_call_gets_the_one_stage_file(self):
        assert text("planner/batch_one") in self._leading(1)

    def test_the_actual_cap_reaches_the_planner(self):
        assert render("planner/batch", cap=5, rest=4) in self._leading(5)

    def test_the_invitation_is_absent_when_batching_is_off(self):
        assert line_of("planner/batch") not in self._leading(1)
        assert text("planner/batch_one") not in self._leading(5)

    def test_a_cfg_without_a_planner_section_still_builds(self):
        messages = build_planner_messages(
            cfg=SimpleNamespace(planner=SimpleNamespace(cache_ttl=None)), plan_text=a_plan(), completed=[]
        )
        assert messages[0]["content"][0]["text"]


class TestThePlannerPromptIsRecordedBeforeItIsSent:
    """The instrument that was missing when it was most wanted.

    A planner call was rejected as `prompt is too long: 1077433 tokens >
    1000000 maximum`, and nothing on disk said what had been in it. The
    executor has had `sent-prompt.md` since the transcript work, written before
    its first call because everything in it is known then. The planner had
    `planner.json`, written after — and a call that never returns writes
    nothing at all.

    Reconstructing it from `config.yaml`, the plan tree and the runtime
    accounted for 575,633 characters against a provider-measured ~1M tokens,
    and that gap is not closable by more careful reconstruction: the prompt is
    assembled from a dozen optional inputs and the one that matters is the one
    you did not think to pass.
    """

    def test_every_block_is_listed_with_its_size(self):
        from code_gantry.nodes import _render_sent_prompt

        out = _render_sent_prompt([
            {"role": "user", "content": [
                {"text": "a" * 100, "cache_control": {"type": "ephemeral"}},
                {"text": "b" * 5},
            ]},
            {"role": "assistant", "content": "c" * 7},
        ])
        assert "Total: 112 characters across 3 block(s)." in out
        assert "message 0 (user) block 0: 100 chars" in out
        assert "message 1 (assistant) block 0: 7 chars" in out

    def test_a_cache_breakpoint_is_marked(self):
        # Where the breakpoints fall is most of why a prompt costs what it
        # does, and it is invisible in the text itself.
        from code_gantry.nodes import _render_sent_prompt

        out = _render_sent_prompt([
            {"role": "user", "content": [
                {"text": "x", "cache_control": {"type": "ephemeral"}},
                {"text": "y"},
            ]},
        ])
        assert out.count("[cache breakpoint]") == 2  # summary line and section

    def test_the_text_itself_is_kept(self):
        from code_gantry.nodes import _render_sent_prompt

        out = _render_sent_prompt([{"role": "user", "content": "the actual bytes"}])
        assert "the actual bytes" in out


class TestHowLargeOneStageShouldBe:
    """One file, in the cached prefix, and it names no project."""

    def test_it_is_the_stage_size_file(self):
        from code_gantry.prompts import _stage_size_block

        assert _stage_size_block().strip() == text("planner/stage_size")

    def test_it_sits_in_the_cached_prefix(self):
        messages = build_planner_messages(cfg=_cfg(), plan_text=a_plan(), completed=[])
        assert text("planner/stage_size") in leading_text(messages)

    def test_it_carries_no_project_vocabulary(self):
        text_ = text("planner/stage_size").lower()
        for word in (
            "rails", "ruby", "rspec", "gemfile", "controller", "helper",
            ".rb", ".erb", "app/", "spec/", "bundle", "capybara",
        ):
            assert word not in text_, f"{word!r} is project knowledge in a prompt"



class TestTheCostsBlockRendersTheFileAroundTheLines:
    def _block(self):
        from code_gantry.prompts import _costs_block

        return _costs_block([
            {"merge_sha": "b52851a90c6398", "stage_id": "some-stage",
             "files": 1, "context_tokens": 88_762},
        ])

    def test_the_costs_file_wraps_the_lines(self):
        assert self._block() == "\n\n" + render(
            "planner/costs",
            lines="- `b52851a90c63` some-stage — 88,762 context tokens, 1 file(s) in scope",
        )

    def test_the_sha_is_rendered_long_enough_to_resolve(self):
        # Twelve characters. A shorter prefix is ambiguous on a large
        # repository, and an unresolvable key is the defect this block spent
        # months having in a different form.
        assert "`b52851a90c63`" in self._block()

    def test_no_costs_is_no_block(self):
        from code_gantry.prompts import _costs_block

        assert _costs_block([]) == ""


class TestTheCostLineCarriesWhatChanged:
    """A declared file count is a permission, not a record.

    `files` on a cost line is `len(stage.edit_files)` — what the stage was
    *allowed* to touch. Stages routinely touch less, and this project has
    already paid once for reading a permission as a record of what happened,
    when a batch check discarded usable work over edits that never occurred.

    The landing commit knows what actually changed, so `advance` measures it
    there. Both are kept: they answer different questions and each is a
    handful of characters.
    """

    def _block(self, entry):
        from code_gantry.prompts import _costs_block

        return _costs_block([{
            "merge_sha": "b52851a90c6398", "stage_id": "some-stage",
            "files": 4, "context_tokens": 88_762, **entry,
        }])

    def test_a_measured_stat_is_rendered(self):
        text = self._block({"changed": 2, "insertions": 131, "deletions": 0})
        assert "2 file(s) changed +131 -0" in text
        # Not the declared four: the measurement supersedes the permission.
        assert "4 file(s)" not in text

    def test_a_line_without_one_falls_back_to_the_declared_scope(self):
        """Most of the file predates the stat and must still render.

        `stage-costs.md` is append-only and spans every run of a project, so
        the entries that inform the first derivation after this ships are all
        old ones. A renderer that needed the new field would drop the history
        exactly when it is the only history there is.
        """
        text = self._block({})
        assert "4 file(s) in scope" in text
        assert "changed" not in text.split("- `b52851a90c63`")[1]


class TestARedrawIsAskedWhatItLearned:
    """The redraw-lesson file is sent on a revision and not on a plain
    derivation, where there is no redraw to learn from."""

    def _revision_prompt(self, **over):
        from code_gantry.config import Stage

        kwargs = dict(
            cfg=_cfg(),
            plan_text=a_plan(),
            completed=[],
            current_stage=Stage(id="s", instruction="do it", edit_files=["a.py"]),
            revision=1,
            failure={"layer": "review", "summary": "blocked"},
        )
        kwargs.update(over)
        return all_text(build_planner_messages(**kwargs))

    def test_the_redraw_is_asked_what_it_learned(self):
        assert text("planner/redraw_lesson") in self._revision_prompt()

    def test_a_plain_derivation_is_not_asked(self):
        text_ = all_text(
            build_planner_messages(cfg=_cfg(), plan_text=a_plan(), completed=[])
        )
        assert text("planner/redraw_lesson") not in text_
        assert text("planner/derive") in text_


class TestGateHistoryBlock:
    def test_it_groups_by_revision_and_names_the_two_verdicts_that_are_not_layers(self):
        """`passed` and `review` are not layer failures and must not read as any.

        A bare "residue, passed, review" invites reading the middle as a layer
        called passed. The two entries that carry the whole signal — the stage
        clearing every gate, and the reviewer turning it down afterwards — are
        the ones worth spelling out.
        """
        from code_gantry.prompts import format_gate_history

        text = format_gate_history(
            [
                {"revision": 3, "layer": "residue"},
                {"revision": 3, "layer": "passed"},
                {"revision": 3, "layer": "review"},
                {"revision": 3, "layer": "tests"},
                {"revision": 4, "layer": "residue"},
            ]
        )
        assert "revision 3: residue, all gates passed, review rejected, tests" in text
        assert "revision 4: residue" in text

    def test_nothing_renders_for_a_stage_with_no_history(self):
        """A first attempt has none, and an empty heading is noise in a prompt."""
        from code_gantry.prompts import format_gate_history

        assert format_gate_history([]) == ""

    def test_it_reaches_the_planner(self):
        """The journey, not the endpoints.

        Four defects here have been values computed correctly and lost in
        transit, so the formatter passing its own unit test proves nothing
        about whether a planner ever sees this.
        """
        from code_gantry.config import Stage
        from code_gantry.prompts import build_planner_messages

        text = all_text(
            build_planner_messages(
                cfg=_cfg(),
                plan_text=a_plan(),
                completed=[],
                current_stage=Stage(id="s", instruction="do it", edit_files=["a.py"]),
                gate_history=[
                    {"revision": 0, "layer": "passed"},
                    {"revision": 0, "layer": "review"},
                ],
            )
        )
        assert "all gates passed, review rejected" in text

    def test_it_renders_only_where_there_is_a_stage_to_have_a_history(self):
        """A history with no stage in flight is a heading about nothing."""
        from code_gantry.prompts import build_planner_messages

        text = all_text(
            build_planner_messages(
                cfg=_cfg(),
                plan_text=a_plan(),
                completed=[],
                gate_history=[{"revision": 0, "layer": "residue"}],
            )
        )
        assert line_of("planner/gate_history") not in text


class TestExcerptsAreRenderedFromTheirFile:
    def _prompt(self, tmp_path, **kw):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_prompt

        cfg = _exec_cfg(tmp_path)
        stage = Stage(id="s", instruction="do", edit_files=["app.py"])
        return build_executor_prompt(
            stage, cfg, excerpts=[("app.py:1-2", "1  def hello():")], **kw
        )

    def test_the_excerpt_file_carries_the_blocks(self, tmp_path):
        expected = render(
            "executor/excerpts",
            currency=text("executor/excerpts_current"),
            blocks="### `app.py:1-2`\n\n```\n1  def hello():\n```",
        )
        assert expected in self._prompt(tmp_path)


class TestTheExecutorPromptNamesNothingItCannotSee:
    """A prompt may only point at what the reader is looking at. `edit_files`
    is the planner's field name and appears nowhere the executor can see."""

    def test_the_field_name_never_reaches_the_executor(self, tmp_path):
        from code_gantry.config import Stage
        from code_gantry.prompts import build_executor_prompt

        stage = Stage(
            id="s",
            instruction="do the thing",
            edit_files=["app/a.rb"],
            read_files=["app/b.rb"],
        )
        text_ = build_executor_prompt(
            stage,
            _exec_cfg(tmp_path),
            excerpts=[("`app/a.rb:1-2`", "    1 | class A\n    2 | end")],
        )
        # The block has to be there, or this asserts the absence of a string
        # from a document that was never rendered.
        assert line_of("executor/excerpts") in text_
        assert "edit_files" not in text_


class TestALandingDoesNotDisturbThePlan:
    """The projection sits outside the marked block.

    Two prompts that differ only by a landing — a longer projection — must be
    byte-identical up to and including the marked block.
    """

    def _messages(self, projection):
        return build_planner_messages(
            cfg=_cfg(),
            plan_text="# ROOT {#p.001}\n",
            completed=[],
            projection=projection,
            layout="- `src/` (1)",
        )

    def test_the_marked_block_survives_a_new_landing(self):
        before = self._messages("### Landed\n\n- one")
        after = self._messages("### Landed\n\n- one\n- two")

        marked_before = [b for b in before[0]["content"] if "cache_control" in b]
        marked_after = [b for b in after[0]["content"] if "cache_control" in b]

        assert len(marked_before) == 1
        assert marked_before[0]["text"] == marked_after[0]["text"], (
            "a landing changed the cached block; the projection is inside it"
        )

    def test_the_projection_is_sent_just_after_the_mark(self):
        messages = self._messages("### Landed\n\n- entry one")
        blocks = messages[0]["content"]
        marked = next(i for i, b in enumerate(blocks) if "cache_control" in b)
        following = "".join(b.get("text", "") for b in blocks[marked + 1:])

        assert "entry one" in following, "the projection must still reach the planner"
        assert "entry one" not in blocks[marked]["text"]

    def test_the_plan_text_is_in_the_marked_block(self):
        blocks = self._messages("### Landed\n\n- entry one")[0]["content"]
        marked = next(b for b in blocks if "cache_control" in b)
        assert "ROOT" in marked["text"]


class TestTheTestWarningsReachThePlanner:
    """What a green suite still has to say.

    The runner tallies deprecations and unexpected output after every run, and
    that tally existed for as long as a scrollback buffer. Measured on one
    run: 203 first-party warnings at five sites, on a stream nothing read, and
    52 of 57 planner prompts mentioned any of it exactly once — a line in a
    plan document about a *gem*, not the application's own code.

    It sits after the cache breakpoint with the progress log, on the same
    reasoning: it changes whenever the suite runs, and content that churns
    ahead of the mark re-bills the plan behind it.
    """

    def _messages(self, warnings):
        return build_planner_messages(
            cfg=SimpleNamespace(planner=SimpleNamespace(cache_ttl="1h")),
            plan_text=a_plan(),
            completed=[],
            layout="- `src/` (1)",
            test_warnings=warnings,
        )

    def test_it_is_sent_when_there_is_one(self):
        text = leading_text(self._messages("  12 DEPRECATION WARNING: something\n"))
        assert "DEPRECATION WARNING: something" not in text

        whole = "".join(
            b.get("text", "")
            for m in self._messages("  12 DEPRECATION WARNING: something\n")
            for b in m["content"]
        )
        assert "DEPRECATION WARNING: something" in whole

    def test_it_follows_the_cache_mark(self):
        blocks = self._messages("  12 DEPRECATION WARNING: something\n")[0]["content"]
        marked = next(i for i, b in enumerate(blocks) if "cache_control" in b)
        after = "".join(b.get("text", "") for b in blocks[marked + 1:])
        assert "DEPRECATION WARNING: something" in after

    def test_a_project_without_one_says_nothing(self):
        whole = "".join(
            b.get("text", "")
            for m in self._messages(None)
            for b in m["content"]
        )
        assert line_of("planner/warnings") not in whole

