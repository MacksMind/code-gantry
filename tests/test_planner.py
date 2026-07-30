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
        assert outcome.usage.prompt_tokens == 9000


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
