"""The planner client.

Two things carry the weight. The declarative-only schema — the planner cannot
return an executable field because there is nowhere in the response for one.
And the defensive handling: a refusal, a truncation, or a transport failure must
become `blocked`, not a crash and not a guess, because this runs unattended.
"""

from types import SimpleNamespace

import pytest

from orchestrator.config import PLANNER_WRITABLE_FIELDS, parse_config
from orchestrator.planner import (
    AnthropicPlanner,
    Deferral,
    PlannedStage,
    PlannerResponse,
    append_status,
    make_planner,
)


def cfg(**over):
    planner = {"model": "claude-opus-5"}
    planner.update(over)
    return parse_config(
        {
            "target_repo": "/tmp/x",
            "project_branch": "work",
            "plan_root": "PLAN.md",
            "test_command": "pytest",
            "executor": {"model": "m"},
            "planner": planner,
            "reviewer": {"model": "gpt-5.5"},
        }
    ).planner


def a_stage(**over):
    fields = {
        "id": "extract-service",
        "instruction": "Extract the service object.",
        "edit_files": ["app/services/**"],
    }
    fields.update(over)
    return PlannedStage(**fields)


def response(
    parsed=None,
    stop_reason="end_turn",
    usage=SimpleNamespace(
        input_tokens=9000, output_tokens=400, cache_read_input_tokens=8500
    ),
):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, usage=usage)


class StubClient:
    def __init__(self, result):
        self._result = result
        self.calls = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


MESSAGES = [{"role": "user", "content": "derive the next stage"}]


def plan_with(result):
    client = StubClient(result)
    return AnthropicPlanner(cfg(), client=client).plan(MESSAGES), client


class TestSchemaExcludesExecutableFields:
    """The core safety property, checked at the schema level."""

    def test_no_command_field(self):
        assert "command" not in PlannedStage.model_fields

    def test_no_command_like_fields_at_all(self):
        for field in (
            "command",
            "checks",
            "preconditions",
            "context_commands",
            "setup_command",
            "test_command",
        ):
            assert field not in PlannedStage.model_fields, field

    def test_no_policy_fields(self):
        for field in ("require_new_tests", "review", "full_suite_on_approval"):
            assert field not in PlannedStage.model_fields, field

    def test_no_kind_field(self):
        # Every planner-derived stage is an agent stage.
        assert "kind" not in PlannedStage.model_fields

    def test_every_field_is_in_the_allowlist(self):
        # The schema and the filter must agree, or one of them is dead code.
        assert set(PlannedStage.model_fields) <= PLANNER_WRITABLE_FIELDS

    def test_passing_an_executable_field_is_silently_dropped(self):
        # Pydantic ignores unknown keys here; the allowlist filter in config is
        # the second line of defence.
        stage = PlannedStage(
            id="s", instruction="x", edit_files=["a"], command="rm -rf /"
        )
        assert not hasattr(stage, "command")

    def test_test_paths_is_paths_not_a_command(self):
        stage = a_stage(test_paths=["spec/models/order_spec.rb"])
        assert stage.test_paths == ["spec/models/order_spec.rb"]


class TestVerdicts:
    def test_next_stage(self):
        parsed = PlannerResponse(
            verdict="next_stage", reasoning="first unit", status_entry="entry",
            stage=a_stage(),
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.verdict == "next_stage"
        assert outcome.stage_fields["id"] == "extract-service"

    def test_revise_carries_the_revision_mode(self):
        # extend vs restart decides whether existing work survives.
        parsed = PlannerResponse(
            verdict="revise", reasoning="too narrow", status_entry="e",
            stage=a_stage(edit_files=["app/**", "spec/**"]), revision_mode="extend",
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.verdict == "revise"
        assert outcome.revision_mode == "extend"

    def test_project_complete(self):
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="plan executed", status_entry="e"
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.verdict == "project_complete"
        assert outcome.stage_fields is None

    def test_blocked_from_the_planner_is_not_marked_as_our_failure(self):
        parsed = PlannerResponse(
            verdict="blocked", reasoning="plan contradicts itself", status_entry="e"
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.verdict == "blocked"
        assert outcome.failed is False


class TestSemanticValidation:
    def test_next_stage_without_a_spec_is_blocked(self):
        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=None
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.verdict == "blocked"
        assert "without a stage spec" in outcome.reasoning

    def test_revise_without_a_revision_mode_is_blocked(self):
        # Without it there is no way to know whether to keep the branch.
        parsed = PlannerResponse(
            verdict="revise", reasoning="r", status_entry="e", stage=a_stage()
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.verdict == "blocked"
        assert "revision_mode" in outcome.reasoning


class SequenceClient:
    """Answers each call with the next scripted result."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        result = self._results.pop(0) if self._results else self._results
        if isinstance(result, Exception):
            raise result
        return result


class TestMalformedResponseIsRetried:
    """A stochastic model omitting an optional field must not end the run.

    Found live: the planner answered `revise` with no stage spec, and the run
    escalated on the spot. Over a fourteen-hour unattended session that is a
    coin flip on whether the whole thing survives.
    """

    def test_a_missing_stage_spec_is_retried(self):
        bad = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=None
        )
        good = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=bad), response(parsed=good)])
        outcome = AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        assert outcome.verdict == "next_stage"
        assert len(client.calls) == 2

    def test_the_retry_tells_the_planner_what_was_wrong(self):
        bad = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=None
        )
        good = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=bad), response(parsed=good)])
        AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        retry_messages = client.calls[1]["messages"]
        assert "stage spec" in retry_messages[-1]["content"]

    def test_the_correction_is_appended_so_the_prefix_stays_cacheable(self):
        bad = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=None
        )
        good = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=bad), response(parsed=good)])
        AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        first, retry = client.calls[0]["messages"], client.calls[1]["messages"]
        assert retry[: len(first)] == first

    def test_a_second_malformed_response_blocks(self):
        bad = PlannerResponse(
            verdict="revise", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=bad), response(parsed=bad)])
        outcome = AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        assert outcome.verdict == "blocked"
        assert "revision_mode" in outcome.reasoning
        assert len(client.calls) == 2, "exactly one retry, not a loop"

    def test_both_attempts_are_billed(self):
        # The discarded attempt cost real tokens; hiding them would understate
        # the run's cost.
        bad = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=None
        )
        good = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=bad), response(parsed=good)])
        outcome = AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        # 2 x (9,000 uncached + 8,500 cache read). prompt_tokens is total
        # input, normalised to the reviewer's shape — Anthropic reports the
        # cached read as a separate, disjoint count.
        assert outcome.usage.prompt_tokens == 35_000
        assert outcome.usage.cached_tokens == 17_000
        assert outcome.usage.completion_tokens == 800

    def test_a_good_response_costs_only_one_call(self):
        good = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=good)])
        AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        assert len(client.calls) == 1

    def test_a_refusal_is_not_retried(self):
        # A refusal is a decision, not a malformed answer.
        client = SequenceClient([response(parsed=None, stop_reason="refusal")])
        outcome = AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        assert outcome.verdict == "blocked"
        assert len(client.calls) == 1


class TestDefensiveHandling:
    def test_a_transport_failure_becomes_blocked(self):
        outcome, _ = plan_with(RuntimeError("connection reset"))
        assert outcome.verdict == "blocked"
        assert outcome.failed is True
        assert "connection reset" in outcome.reasoning

    def test_a_refusal_becomes_blocked(self):
        outcome, _ = plan_with(response(parsed=None, stop_reason="refusal"))
        assert outcome.verdict == "blocked"
        assert "refused" in outcome.reasoning

    def test_a_truncated_response_becomes_blocked(self):
        # A verdict cut off mid-JSON is not a verdict, even if it parsed.
        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        outcome, _ = plan_with(response(parsed=parsed, stop_reason="max_tokens"))
        assert outcome.verdict == "blocked"
        assert "truncated" in outcome.reasoning

    def test_no_parsed_output_becomes_blocked(self):
        outcome, _ = plan_with(response(parsed=None))
        assert outcome.verdict == "blocked"

    def test_usage_survives_a_failure_path(self):
        # The report should still account for what the call cost.
        outcome, _ = plan_with(response(parsed=None, stop_reason="refusal"))
        assert outcome.usage.prompt_tokens == 17_500  # 9,000 uncached + 8,500 read


class TestRequestShape:
    def test_requests_the_planner_schema(self):
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="r", status_entry="e"
        )
        _, client = plan_with(response(parsed=parsed))
        assert client.calls[0]["output_format"] is PlannerResponse

    def test_uses_the_configured_model(self):
        parsed = PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e")
        client = StubClient(response(parsed=parsed))
        AnthropicPlanner(cfg(model="claude-opus-5"), client=client).plan(MESSAGES)
        assert client.calls[0]["model"] == "claude-opus-5"

    def test_system_prompt_is_cacheable(self):
        # It never changes across a run, so it belongs in the cached prefix.
        parsed = PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e")
        _, client = plan_with(response(parsed=parsed))
        system = client.calls[0]["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_leaves_headroom_for_thinking(self):
        # Thinking is on by default and counts against max_tokens along with the
        # response, so a tight budget truncates the verdict.
        parsed = PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e")
        _, client = plan_with(response(parsed=parsed))
        assert client.calls[0]["max_tokens"] >= 8000

    def test_sets_no_sampling_parameters(self):
        # temperature/top_p/top_k are rejected on current models.
        parsed = PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e")
        _, client = plan_with(response(parsed=parsed))
        for banned in ("temperature", "top_p", "top_k"):
            assert banned not in client.calls[0]

    def test_messages_pass_through_unchanged(self):
        parsed = PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e")
        _, client = plan_with(response(parsed=parsed))
        assert client.calls[0]["messages"] == MESSAGES


class TestUsageAccounting:
    def test_records_cached_tokens(self):
        parsed = PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e")
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.usage.cached_tokens == 8500

    def test_absent_usage_does_not_crash(self):
        parsed = PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e")
        outcome, _ = plan_with(response(parsed=parsed, usage=None))
        assert outcome.usage.prompt_tokens == 0


class TestFactory:
    def test_missing_api_key_is_reported_clearly(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as e:
            make_planner(cfg())
        assert "ANTHROPIC_API_KEY" in str(e.value)


class TestStatusLog:
    def test_creates_the_log_with_a_header(self, tmp_path):
        path = append_status(
            tmp_path, 0, "extract", 0, "next_stage", "entry text", "because",
            now="2026-07-30 01:00:00Z",
        )
        body = path.read_text()
        assert body.startswith("# Expected vs actual")
        assert "entry text" in body

    def test_appends_rather_than_replacing(self):
        # The divergence over time is the whole value; a status page that always
        # shows current state throws it away.
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            append_status(d, 0, "one", 0, "next_stage", "first entry", "r1", now="t1")
            path = append_status(d, 1, "two", 0, "next_stage", "second entry", "r2", now="t2")
            body = path.read_text()
            assert "first entry" in body
            assert "second entry" in body
            assert body.index("first entry") < body.index("second entry")

    def test_records_stage_and_revision(self, tmp_path):
        path = append_status(
            tmp_path, 14, "bump", 2, "revise", "e", "r", now="t"
        )
        body = path.read_text()
        assert "stage 14" in body
        assert "revision 2" in body

    def test_records_the_verdict_and_reasoning(self, tmp_path):
        path = append_status(tmp_path, 0, "s", 0, "blocked", "e", "the plan is wrong", now="t")
        body = path.read_text()
        assert "blocked" in body
        assert "the plan is wrong" in body

    def test_creates_the_project_directory(self, tmp_path):
        append_status(tmp_path / "new", 0, "s", 0, "next_stage", "e", "r", now="t")
        assert (tmp_path / "new" / "status.md").is_file()


class TestDeferrals:
    """Skipped plan steps, as data rather than prose.

    The planner may take the plan out of order when the order is incidental.
    The risk is that a deferral is mentioned once and then forgotten across
    fifty more calls, and the run reports success having quietly dropped work.
    Structuring it moves that memory from the model to the orchestrator.
    """

    def test_a_deferral_survives_the_round_trip(self):
        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage(),
            deferred=[
                Deferral(
                    plan_step="Audit CloudWatch logs",
                    reason="needs AWS credentials this run does not have",
                    blocked_on="AWS access",
                    safe_because="nothing later reads the audit output",
                )
            ],
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.deferred[0]["plan_step"] == "Audit CloudWatch logs"

    def test_no_deferrals_is_an_empty_list_not_none(self):
        parsed = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        outcome, _ = plan_with(response(parsed=parsed))
        assert outcome.deferred == []

    def test_deferred_is_not_a_stage_field(self):
        # It describes the plan, not a unit of work, and it must not leak into
        # the allowlist that guards what the planner may author on a stage.
        from orchestrator.config import PLANNER_WRITABLE_FIELDS

        assert "deferred" not in PlannedStage.model_fields
        assert "deferred" not in PLANNER_WRITABLE_FIELDS


class TestUsageNormalisation:
    """Anthropic and OpenAI use the same words for different quantities.

    OpenAI's `prompt_tokens` is the total and its cached count is a subset of
    it. Anthropic's `input_tokens` is the *uncached* remainder, with cache reads
    and cache writes reported as two further disjoint counts. Treating them the
    same way printed `Uncached prompt tokens: -2,438` and `251%` in a real
    report — on the one metric the whole economic argument rests on.

    So `prompt_tokens` is normalised here to mean total input, for both.
    """

    def test_total_input_includes_the_cached_read(self):
        from orchestrator.planner import _extract_usage

        class U:
            input_tokens = 1613
            cache_read_input_tokens = 4051
            cache_creation_input_tokens = 0
            output_tokens = 300

        usage = _extract_usage(U())
        assert usage.prompt_tokens == 5664
        assert usage.cached_tokens == 4051

    def test_uncached_is_never_negative(self):
        from orchestrator.planner import _extract_usage

        class U:
            input_tokens = 1613
            cache_read_input_tokens = 4051
            cache_creation_input_tokens = 0
            output_tokens = 300

        usage = _extract_usage(U())
        assert usage.prompt_tokens - usage.cached_tokens == 1613

    def test_cache_writes_are_counted_and_kept_separate(self):
        # Anthropic bills a cache write above base rate, so a run that writes
        # the prefix and never reads it is worse than not caching at all. That
        # has to be visible rather than absent.
        from orchestrator.planner import _extract_usage

        class U:
            input_tokens = 1632
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 4051
            output_tokens = 300

        usage = _extract_usage(U())
        assert usage.cache_write_tokens == 4051
        assert usage.prompt_tokens == 5683
        assert usage.cached_tokens == 0

    def test_a_missing_field_is_zero_not_an_error(self):
        from orchestrator.planner import _extract_usage

        class U:
            input_tokens = 100
            output_tokens = 10

        assert _extract_usage(U()).prompt_tokens == 100
