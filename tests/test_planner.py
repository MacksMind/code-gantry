"""The planner client.

Two things carry the weight. The declarative-only schema — the planner cannot
return an executable field because there is nowhere in the response for one.
And the defensive handling: a refusal, a truncation, or a transport failure must
become `blocked`, not a crash and not a guess, because this runs unattended.
"""

from types import SimpleNamespace

import pytest

from test_config import as_test_tools

from code_gantry.config import PLANNER_WRITABLE_FIELDS, parse_config
from code_gantry.planner import (
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
        as_test_tools({
            "target_repo": "/tmp/x",
            "project_branch": "work",
            "plan_root": "PLAN.md",
            "full_test_command": "pytest",
            "executor": {"model": "m"},
            "planner": planner,
            "reviewer": {"model": "gpt-5.5"},
        })
    ).planner


def a_stage(**over):
    fields = {
        "id": "extract-service",
        "instruction": "Extract the service object.",
        "edit_files": ["app/services/**"],
        # Required on the schema, so every fixture answers it. The value is
        # arbitrary here — what the field is for is measured against real
        # stages, not asserted in a builder.
        "difficulty": "medium",
        "plan_keys": ["p.002"],
        "resolves": [],
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
            "full_test_command",
        ):
            assert field not in PlannedStage.model_fields, field

    def test_no_gate_removing_policy_fields(self):
        # `require_new_tests` is representable — it can only add a gate, and
        # merges with OR so it cannot waive the operator's. These two switch
        # gates off, so the schema keeps them out of reach.
        for field in ("review", "full_suite_on_approval"):
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
            id="s",
            instruction="x",
            edit_files=["a"],
            difficulty="low",
            plan_keys=["p.002"],
            resolves=[],
            command="rm -rf /",
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
        # cached read as a separate, orthogonal count.
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


class TestAStageTheCallerCannotUseIsRetried:
    """Validation the schema cannot express, fed back instead of escalated.

    Observed on stage 130 of a 129-stage run: the planner authored a
    `forbidden_patterns` entry containing `$?`. `$` is an anchor, so there is
    nothing for `?` to quantify and `re.compile` rejects it. The rest of the
    stage was correct. It cost a human round trip, and discarded twelve minutes
    and twenty-one tool reads, to fix one character.

    The schema cannot catch it, because `forbidden_patterns` is a list of
    strings and every invalid regex is a valid string. The check lives in
    `config.py`, which is why it arrives as an injected callable rather than an
    import — this module stays free of project knowledge, and the same retry
    then covers any future rule the caller adds.

    One retry, matching the malformed-answer path above. Two identical
    rejections mean the planner cannot fix it and a human should look.
    """

    BAD = r"\.html_safe\s*$?.*funnel_request"

    def _plan(self, stages, problems_for):
        planner = AnthropicPlanner(
            cfg(),
            client=SequenceClient(
                [
                    response(
                        parsed=PlannerResponse(
                            verdict="next_stage",
                            reasoning="r",
                            status_entry="e",
                            stage=s,
                        )
                    )
                    for s in stages
                ]
            ),
        )
        planner.validate_stage_fields = problems_for
        return planner.plan(MESSAGES), planner._client

    def _rejects_bad_pattern(self, fields):
        return [
            f"forbidden_patterns entry {p!r} is not a valid regex: "
            "nothing to repeat at position 12"
            for p in fields.get("forbidden_patterns") or []
            if p == self.BAD
        ]

    def test_a_rejected_stage_is_retried(self):
        outcome, client = self._plan(
            [a_stage(forbidden_patterns=[self.BAD]), a_stage(forbidden_patterns=["ok"])],
            self._rejects_bad_pattern,
        )
        assert outcome.verdict == "next_stage"
        assert outcome.stage_fields["forbidden_patterns"] == ["ok"]
        assert len(client.calls) == 2

    def test_the_retry_says_which_field_and_why(self):
        # "Answer again" without the reason is a coin flip on the same typo.
        _, client = self._plan(
            [a_stage(forbidden_patterns=[self.BAD]), a_stage(forbidden_patterns=["ok"])],
            self._rejects_bad_pattern,
        )
        correction = client.calls[1]["messages"][-1]["content"]
        assert "forbidden_patterns" in correction
        assert "not a valid regex" in correction

    def test_the_correction_is_appended_so_the_prefix_stays_cacheable(self):
        _, client = self._plan(
            [a_stage(forbidden_patterns=[self.BAD]), a_stage(forbidden_patterns=["ok"])],
            self._rejects_bad_pattern,
        )
        first, retry = client.calls[0]["messages"], client.calls[1]["messages"]
        assert retry[: len(first)] == first

    def test_a_second_rejection_blocks_rather_than_looping(self):
        outcome, client = self._plan(
            [a_stage(forbidden_patterns=[self.BAD])] * 2, self._rejects_bad_pattern
        )
        assert outcome.verdict == "blocked"
        assert "not a valid regex" in outcome.reasoning
        assert len(client.calls) == 2, "exactly one retry, not a loop"

    def test_every_problem_is_reported_not_just_the_first(self):
        # Fixing one and escalating on the next costs a whole round trip each.
        outcome, _ = self._plan(
            [a_stage(forbidden_patterns=[self.BAD])] * 2,
            lambda fields: ["first problem", "second problem"],
        )
        assert "first problem" in outcome.reasoning
        assert "second problem" in outcome.reasoning

    def test_an_accepted_stage_costs_only_one_call(self):
        _, client = self._plan([a_stage()], lambda fields: [])
        assert len(client.calls) == 1

    def test_no_validator_leaves_the_planner_unchanged(self):
        # The backstop in `nodes.py` still rejects the stage; this only decides
        # whether the planner got a chance to fix it first.
        good = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=good)])
        outcome = AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        assert outcome.verdict == "next_stage"
        assert len(client.calls) == 1

    def test_a_verdict_carrying_no_stage_is_not_validated(self):
        # `project_complete` has no stage to check, and calling a validator
        # with nothing would invent a problem out of an empty dict.
        seen = []

        def record(fields):
            seen.append(fields)
            return ["invented"]

        outcome, _ = self._plan_verdict("project_complete", record)
        assert outcome.verdict == "project_complete"
        assert seen == []

    def _plan_verdict(self, verdict, problems_for):
        planner = AnthropicPlanner(
            cfg(),
            client=SequenceClient(
                [
                    response(
                        parsed=PlannerResponse(
                            verdict=verdict, reasoning="r", status_entry="e", stage=None
                        )
                    )
                ]
            ),
        )
        planner.validate_stage_fields = problems_for
        return planner.plan(MESSAGES), planner._client


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
        # `KeyError` rather than `RuntimeError` since all three roles resolve a
        # key through one function: naming a variable and not setting it is the
        # same mistake wherever it happens, and it says the same thing.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(KeyError) as e:
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


class TestUsageNormalisation:
    """Anthropic and OpenAI use the same words for different quantities.

    OpenAI's `prompt_tokens` is the total and its cached count is a subset of
    it. Anthropic's `input_tokens` is the *uncached* remainder, with cache reads
    and cache writes reported as two further orthogonal counts. Treating them the
    same way printed `Uncached prompt tokens: -2,438` and `251%` in a real
    report — on the one metric the whole economic argument rests on.

    So `prompt_tokens` is normalised here to mean total input, for both.
    """

    def test_total_input_includes_the_cached_read(self):
        from code_gantry.planner import _extract_usage

        class U:
            input_tokens = 1613
            cache_read_input_tokens = 4051
            cache_creation_input_tokens = 0
            output_tokens = 300

        usage = _extract_usage(U())
        assert usage.prompt_tokens == 5664
        assert usage.cached_tokens == 4051

    def test_uncached_is_never_negative(self):
        from code_gantry.planner import _extract_usage

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
        from code_gantry.planner import _extract_usage

        class U:
            input_tokens = 1632
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 4051
            output_tokens = 300

        usage = _extract_usage(U())
        assert usage.cache_write_tokens == 4051
        assert usage.prompt_tokens == 5683
        assert usage.cached_tokens == 0

    def test_the_long_window_share_of_a_write_is_carried(self):
        # A 1h write is 2x base against a 5m write's 1.25x, and Anthropic sums
        # them into one field. The planner is the role it was measured on: its
        # plan block is the largest in the request and the one carrying `1h`.
        from code_gantry.planner import _extract_usage

        class U:
            input_tokens = 1632
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 4051
            cache_creation = type(
                "C", (), {
                    "ephemeral_5m_input_tokens": 51,
                    "ephemeral_1h_input_tokens": 4000,
                },
            )()
            output_tokens = 300

        usage = _extract_usage(U())
        assert usage.cache_write_tokens == 4051
        assert usage.cache_write_1h_tokens == 4000

    def test_both_roles_read_the_wire_the_same_way(self):
        # `PlannerUsage` and `TokenUsage` stay separate types because they
        # transpose, but a *reader* has no field order to get wrong — so the
        # two roles share one, and this is what says so. A rule fixed in one
        # role's type has already failed to reach the role using the other.
        from code_gantry.dialects import MESSAGES
        from code_gantry.planner import _extract_usage

        class U:
            input_tokens = 40
            output_tokens = 10
            cache_read_input_tokens = 60
            cache_creation_input_tokens = 100
            cache_creation = type(
                "C", (), {"ephemeral_1h_input_tokens": 70},
            )()

        assert _extract_usage(U()).cache_write_1h_tokens == 70
        assert MESSAGES.usage(U()).cache_write_1h_tokens == 70

    def test_a_missing_field_is_zero_not_an_error(self):
        from code_gantry.planner import _extract_usage

        class U:
            input_tokens = 100
            output_tokens = 10

        assert _extract_usage(U()).prompt_tokens == 100


class ToolBlock:
    """A tool_use block as the SDK presents one."""

    type = "tool_use"

    def __init__(self, name, args, id="tu_1"):
        self.name, self.input, self.id = name, args, id


class TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class TestRepositoryToolLoop:
    """The planner asks, gets an answer, and then decides.

    Every expensive failure of the first long run came from it deciding
    without asking, because it had nothing to ask with.
    """

    def _reader(self, tmp_path):
        import subprocess

        from code_gantry.gitops import Git
        from code_gantry.repotools import ReadBudget, RepoReader

        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "a.rb").write_text("render text: 'x'\n")
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
            ["add", "-A"],
            ["commit", "-q", "-m", "x"],
        ):
            subprocess.run(["git", *args], cwd=tmp_path, check=True)
        return RepoReader(Git(tmp_path), tmp_path, ReadBudget(max_calls=4))

    def test_a_tool_request_is_answered_and_the_conversation_continues(self, tmp_path):
        asked = SimpleNamespace(
            content=[ToolBlock("search", {"pattern": "render text:"})],
            stop_reason="tool_use",
            usage=None,
            parsed_output=None,
        )
        answered = SimpleNamespace(
            content=[TextBlock("done")],
            stop_reason="end_turn",
            usage=None,
            parsed_output=PlannerResponse(
                verdict="project_complete", reasoning="nothing left", status_entry="e"
            ),
        )
        client = SequenceClient([asked, answered])
        planner = AnthropicPlanner(
            cfg(), client=client, reader=self._reader(tmp_path)
        )
        out = planner.plan(MESSAGES)

        assert out.verdict == "project_complete"
        assert len(client.calls) == 2, "the loop must call again after a tool result"
        # The second call carries the assistant turn and the tool result, in
        # that order — the API rejects a result that answers nothing.
        second = client.calls[1]["messages"]
        assert second[-2]["role"] == "assistant"
        assert second[-1]["content"][0]["type"] == "tool_result"
        assert "a.rb" in second[-1]["content"][0]["content"]

    def test_tools_are_offered_only_when_a_reader_exists(self, tmp_path):
        done = SimpleNamespace(
            content=[TextBlock("x")],
            stop_reason="end_turn",
            usage=None,
            parsed_output=PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e"),
        )
        without = StubClient(done)
        AnthropicPlanner(cfg(), client=without).plan(MESSAGES)
        assert "tools" not in without.calls[0]

        with_reader = StubClient(done)
        AnthropicPlanner(
            cfg(), client=with_reader, reader=self._reader(tmp_path)
        ).plan(MESSAGES)
        assert [t["name"] for t in with_reader.calls[0]["tools"]] == [
            "read_file",
            "list_files",
            "search",
            "git_show",
            "git_diff",
        ]

    def test_a_refused_tool_call_is_returned_rather_than_raised(self, tmp_path):
        # The planner must be able to recover from a bad path by answering with
        # what it has. Raising would discard the reasoning already done.
        asked = SimpleNamespace(
            content=[ToolBlock("read_file", {"path": "../../etc/passwd"})],
            stop_reason="tool_use",
            usage=None,
            parsed_output=None,
        )
        answered = SimpleNamespace(
            content=[TextBlock("ok")],
            stop_reason="end_turn",
            usage=None,
            parsed_output=PlannerResponse(verdict="project_complete", reasoning="r", status_entry="e"),
        )
        client = SequenceClient([asked, answered])
        out = AnthropicPlanner(
            cfg(), client=client, reader=self._reader(tmp_path)
        ).plan(MESSAGES)
        assert out.verdict == "project_complete"
        result = client.calls[1]["messages"][-1]["content"][0]["content"]
        assert "outside the repository" in result

    def test_the_loop_is_bounded(self, tmp_path):
        # A model that ignores an exhausted budget and keeps asking must not
        # run until the request timeout.
        forever = SimpleNamespace(
            content=[ToolBlock("list_files", {})],
            stop_reason="tool_use",
            usage=None,
            parsed_output=None,
        )
        client = SequenceClient([forever] * 50)
        AnthropicPlanner(cfg(), client=client, reader=self._reader(tmp_path)).plan(
            MESSAGES
        )
        assert len(client.calls) <= 8, "budget 4 + 2 turns, plus one malformed retry"


class TestTheReadLogIsChronological:
    """Order is most of how a conclusion was reached.

    A read that confirms a semantic hit is a different act from one that
    preceded it. Two lists concatenated said what was looked at and lied about
    when — the first live run reported four reads before two searches, having
    done the searches first.
    """

    def test_reads_and_searches_interleave_in_real_order(self, tmp_path):
        import subprocess

        from code_gantry.gitops import Git
        from code_gantry.repotools import ReadBudget, RepoReader
        from code_gantry.semantic import SemanticSearch, SemanticSearchConfig

        (tmp_path / "a.rb").write_text("x\n")
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
            ["add", "-A"],
            ["commit", "-q", "-m", "x"],
        ):
            subprocess.run(["git", *args], cwd=tmp_path, check=True)

        reader = RepoReader(Git(tmp_path), tmp_path, ReadBudget())
        semantic = SemanticSearch(
            SemanticSearchConfig(
                api_base="http://x/v1", qdrant_url="http://y",
                embedding_model="m", collection="c",
            ),
            http=lambda url, payload, timeout: (_ for _ in ()).throw(OSError("down")),
            reader=reader,
        )
        planner = AnthropicPlanner(cfg(), client=StubClient(None), reader=reader, semantic=semantic)

        semantic.query("where is postage decided")
        reader.read_file("a.rb")
        semantic.query("and the rate table")

        assert [c.tool for c in reader.calls] == [
            "semantic_search",
            "read_file",
            "semantic_search",
        ]
        assert [line.split("(")[0] for line in planner._tool_log()] == [
            "semantic_search",
            "read_file",
            "semantic_search",
        ]


class TestStageCostsSurviveTheRun:
    """Calibration data has to outlive the run that measured it.

    Executor context is recorded on `StageResult`, which lives in the run's
    state database. A fresh run starts with an empty completed list, so every
    figure vanishes and the planner sizes its first batch — the decision that
    matters most — with nothing to go on. The same defect as telling a restart
    it is the first stage of the project, in a different field.

    Written to `status.md`, which is project-level and append-only, and keyed by
    the **merge sha**: the stage branch and every executor commit are squashed
    away, so that is the only identifier still resolving afterwards. A cost you
    cannot tie back to a diff is a number, not evidence.
    """

    def test_the_line_is_keyed_by_the_merge_sha(self, tmp_path):
        from code_gantry.planner import append_stage_cost

        append_stage_cost(tmp_path, "batch-1", "0285803b159a", 10, 13_000)
        text = (tmp_path / "stage-costs.md").read_text()
        assert "0285803b159a" in text
        assert "10" in text and "13,000" in text

    def test_a_priced_executor_adds_its_cost_to_the_line(self, tmp_path):
        from code_gantry.planner import append_stage_cost

        append_stage_cost(
            tmp_path, "s", "0285803b159a", 3, 13_000,
            spend=[{"role": "executor", "prompt": 40_000, "cached": 30_000,
                    "completion": 900, "cost_usd": 0.42}],
        )
        text = (tmp_path / "stage-costs.md").read_text()
        assert "$0.42" in text
        # Tokens beside it, because the peak and the billed total are
        # different quantities and the line used to show only one of each.
        assert "executor 40,000 in (30,000 cached) / 900 out" in text

    def test_a_free_executor_adds_nothing(self, tmp_path):
        # A local endpoint costs nothing, and "$0.00" on every line of a file
        # the planner reads on every call says the same thing as its absence
        # while taking tokens to do it.
        from code_gantry.planner import append_stage_cost

        append_stage_cost(tmp_path, "s", "0285803b159a", 3, 13_000)
        assert "$" not in (tmp_path / "stage-costs.md").read_text()

    def test_sub_cent_costs_are_not_rounded_to_nothing(self, tmp_path):
        # A cheap model on a small stage lands well under a cent, and a
        # two-place format would record a run's whole executor spend as zero.
        from code_gantry.planner import append_stage_cost

        append_stage_cost(
            tmp_path, "s", "0285803b159a", 1, 900,
            spend=[{"role": "executor", "prompt": 900, "cached": 0,
                    "completion": 10, "cost_usd": 0.0004}],
        )
        assert "$0.0004" in (tmp_path / "stage-costs.md").read_text()

    def test_the_line_records_which_models_and_efforts_produced_it(self, tmp_path):
        # Without this a line is a cost with no configuration attached, and the
        # configuration is the thing being tuned: three effort changes landed
        # in one session and nothing in the record says which stages ran under
        # which. Correlating them afterwards is the only reason to keep a
        # per-stage cost at all.
        from code_gantry.planner import append_stage_cost

        append_stage_cost(
            tmp_path,
            "s",
            "0285803b159a",
            3,
            13_000,
            spend=[{"role": "executor", "prompt": 40_000, "cached": 0,
                    "completion": 900, "cost_usd": 0.42}],
            roles=(
                ("exec", "gpt-5.6-luna", "max"),
                ("plan", "claude-opus-5", "xhigh"),
                ("review", "gpt-5.6-sol", "high"),
            ),
        )
        line = (tmp_path / "stage-costs.md").read_text()
        assert "exec gpt-5.6-luna@max" in line
        assert "plan claude-opus-5@xhigh" in line
        assert "review gpt-5.6-sol@high" in line

    def test_the_models_are_still_parsed_back_as_a_cost_line(self, tmp_path):
        # The suffix must not break the reader: `recent_stage_costs` is what
        # feeds the planner's batch sizing, and a line it cannot match is a
        # stage that silently stops counting.
        from code_gantry.planner import append_stage_cost, recent_stage_costs

        append_stage_cost(
            tmp_path, "s", "0285803b159a", 3, 13_000,
            spend=[{"role": "executor", "prompt": 40_000, "cached": 0,
                    "completion": 900, "cost_usd": 0.42}],
            roles=(("exec", "gpt-5.6-luna", "max"),),
        )
        got = recent_stage_costs(tmp_path)
        assert len(got) == 1
        assert got[0]["context_tokens"] == 13_000
        assert got[0]["merge_sha"] == "0285803b159a"

    def test_lines_written_before_this_existed_still_parse(self, tmp_path):
        # The file is append-only and spans runs, so every line already in it
        # predates the suffix. A reader that needed it would drop the entire
        # history the first time it ran.
        path = tmp_path / "stage-costs.md"
        path.write_text(
            "- cost `aaaaaaaaaaaa` `old` — 2 file(s), 9,000 executor tokens\n"
        )
        from code_gantry.planner import recent_stage_costs

        got = recent_stage_costs(tmp_path)
        assert len(got) == 1 and got[0]["context_tokens"] == 9_000

    def test_no_roles_adds_nothing(self, tmp_path):
        # Same reasoning as the "$0.00" rule above: this file is read by the
        # planner on every call, so a field with nothing to say stays absent.
        from code_gantry.planner import append_stage_cost

        append_stage_cost(tmp_path, "s", "0285803b159a", 3, 13_000)
        assert "[" not in (tmp_path / "stage-costs.md").read_text()

    def test_a_role_with_no_effort_records_just_the_model(self, tmp_path):
        # Not every provider takes an effort, and "model/" with nothing after
        # it reads as a missing value rather than an inapplicable one.
        from code_gantry.planner import append_stage_cost

        append_stage_cost(
            tmp_path, "s", "0285803b159a", 3, 13_000,
            roles=(("exec", "some-local-model", ""),),
        )
        text = (tmp_path / "stage-costs.md").read_text()
        assert "exec some-local-model]" in text
        assert "some-local-model@" not in text

    def test_costs_are_read_back_in_order(self, tmp_path):
        # Its own file rather than a section of status.md, which is a
        # hundreds-of-kilobytes narrative the planner sees only the tail of.
        # Here every line is a cost line, so the whole file stays readable
        # however long the project runs.
        from code_gantry.planner import append_stage_cost, recent_stage_costs

        append_stage_cost(tmp_path, "old", "aaaaaaaaaaaa", 1, 14_000)
        append_stage_cost(tmp_path, "new", "bbbbbbbbbbbb", 10, 13_000)
        assert not (tmp_path / "status.md").exists(), "must not touch status.md"

        costs = recent_stage_costs(tmp_path)
        assert [c["merge_sha"] for c in costs] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
        assert costs[-1]["files"] == 10
        assert costs[-1]["context_tokens"] == 13_000

    def test_only_the_most_recent_are_kept(self, tmp_path):
        from code_gantry.planner import append_stage_cost, recent_stage_costs

        for i in range(30):
            append_stage_cost(tmp_path, f"s{i}", f"{i:012d}", 1, 1_000 + i)
        costs = recent_stage_costs(tmp_path, limit=5)
        assert len(costs) == 5
        assert costs[-1]["context_tokens"] == 1_029

    def test_a_project_with_no_status_file_reads_empty(self, tmp_path):
        from code_gantry.planner import recent_stage_costs

        assert recent_stage_costs(tmp_path) == []


class TestInstructionsQuoteCodeInFencedBlocks:
    """An indented code block makes markup indistinguishable from source.

    The executor reads the instruction as raw text, not rendered Markdown, so
    the four spaces that open an indented block arrive as four spaces of
    content. Told to match a line exactly, it matches what it was shown.

    Observed: a stage quoted two lines of a model file as an indented block,
    presenting them at six spaces where the file has two. Four attempts
    produced no edit at all — `SearchReplaceNoExactMatch` every time — the
    stage exhausted its budget without one diff reaching review, and the
    instruction itself had said "keep the run of spaces exactly as shown".
    """

    def test_the_field_says_fenced_not_indented(self):
        from code_gantry.planner import PlannedStage

        described = PlannedStage.model_fields["instruction"].description.lower()
        assert "fenced" in described
        assert "indented" in described

    def test_it_says_why_rather_than_only_what(self):
        # A rule with no reason is one the model discards under pressure to be
        # helpful. This one has to survive an instruction that is otherwise
        # begging to be indented for readability.
        described = __import__(
            "code_gantry.planner", fromlist=["PlannedStage"]
        ).PlannedStage.model_fields["instruction"].description.lower()
        assert "raw text" in described


class TestTheToolLoopIsCached:
    """The loop re-sent everything it had accumulated, every turn.

    A derivation runs ten to twenty-five turns, and each one resends the whole
    conversation. With breakpoints only on the system prompt, the plan snapshot
    and the completed history, everything after them — the volatile tail, every
    assistant turn, every tool result — was uncached on every turn. Cost grew
    with the square of the turn count.

    Measured over 125 decisions of one run: a decision making no tool calls
    spent 48k uncached input tokens, one making sixteen or more spent 1.4M. The
    run's 70.6M uncached tokens are almost entirely this.

    Anthropic allows four breakpoints and three were in use. The fourth moves:
    each turn marks the end of the newest message, so the turn writes only its
    own increment and reads everything before it. Marking every turn's message
    instead would exceed the limit by turn five, which is why it moves rather
    than accumulates.

    Its lifetime is deliberately the 5-minute default rather than the run's
    configured 1h. Turns within a decision are seconds apart — 19 tool calls in
    five minutes, measured — so the shorter window is enough, and its writes
    cost 1.25x against 2x. The static prefix still carries the long TTL,
    because that is what has to survive a whole stage between decisions.
    """

    def _blocks(self, text="derive the next stage"):
        return [{"role": "user", "content": [{"type": "text", "text": text}]}]

    def _done(self):
        return SimpleNamespace(
            content=[TextBlock("done")],
            stop_reason="end_turn",
            usage=None,
            parsed_output=PlannerResponse(
                verdict="project_complete", reasoning="r", status_entry="e"
            ),
        )

    def test_the_last_block_of_the_last_message_is_marked(self):
        client = StubClient(self._done())
        AnthropicPlanner(cfg(), client=client).plan(self._blocks())
        sent = client.calls[0]["messages"]
        assert sent[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_the_mark_uses_the_default_lifetime_not_the_configured_one(self):
        # 1h writes cost 2x; 5m cost 1.25x. Turns are seconds apart, so the
        # loop breakpoint has no use for the long window the prefix needs.
        client = StubClient(self._done())
        AnthropicPlanner(cfg(cache_ttl="1h"), client=client).plan(self._blocks())
        marker = client.calls[0]["messages"][-1]["content"][-1]["cache_control"]
        assert "ttl" not in marker
        assert client.calls[0]["system"][0]["cache_control"]["ttl"] == "1h"

    def test_string_content_is_left_alone(self):
        # Nothing to attach a marker to without rewriting the message shape,
        # and only the tests send bare strings.
        client = StubClient(self._done())
        AnthropicPlanner(cfg(), client=client).plan(MESSAGES)
        assert client.calls[0]["messages"] == MESSAGES

    def test_the_mark_moves_and_never_accumulates(self, tmp_path):
        # Four breakpoints is the API limit and three are already spoken for.
        # A mark left behind on every turn would break the request by turn five.
        asked = SimpleNamespace(
            content=[ToolBlock("search", {"pattern": "render text:"})],
            stop_reason="tool_use",
            usage=None,
            parsed_output=None,
        )
        client = SequenceClient([asked, asked, self._done()])
        planner = AnthropicPlanner(
            cfg(), client=client, reader=TestRepositoryToolLoop()._reader(tmp_path)
        )
        planner.plan(self._blocks())

        assert len(client.calls) == 3
        for call in client.calls:
            marked = [
                block
                for message in call["messages"]
                if isinstance(message["content"], list)
                for block in message["content"]
                if isinstance(block, dict) and "cache_control" in block
            ]
            assert len(marked) == 1, "exactly one moving breakpoint at a time"
        # And it is on the newest message each time, which is what makes the
        # turn's write an increment rather than the whole conversation.
        for call in client.calls:
            assert "cache_control" in call["messages"][-1]["content"][-1]

    def test_the_stored_conversation_stays_clean(self, tmp_path):
        # The marker is applied to the outgoing copy. If it were written into
        # the conversation the loop accumulates, the marks would pile up and
        # the corrective retry would append to a mutated prefix.
        asked = SimpleNamespace(
            content=[ToolBlock("search", {"pattern": "render text:"})],
            stop_reason="tool_use",
            usage=None,
            parsed_output=None,
        )
        client = SequenceClient([asked, self._done()])
        planner = AnthropicPlanner(
            cfg(), client=client, reader=TestRepositoryToolLoop()._reader(tmp_path)
        )
        planner.plan(self._blocks())
        # The first message is resent on the second turn without its old mark.
        first_message_second_turn = client.calls[1]["messages"][0]
        assert "cache_control" not in first_message_second_turn["content"][-1]


class TestOutagesAreWaitedOutNotEscalated:
    """A dropped network must not end a fourteen-hour run.

    It has twice. The laptop's Wi-Fi went down, the planner and the reviewer
    both failed inside two seconds of each other, and the run escalated with
    `the planner call failed: Connection error.` — leaving the work intact but
    needing a human to notice and type `resume`.

    Not `max_retries`, which stays small and is the SDK's. Both SDKs clamp each
    wait at 8s, so covering fifteen minutes there costs 116 retries at best and
    154 at worst, their retries log at DEBUG where `run.log` never sees them,
    and a count is not a clock — the same setting honours `retry-after` on a
    429 and could wait for hours.
    """

    def _connection_error(self):
        import httpx
        from anthropic import APIConnectionError

        return APIConnectionError(request=httpx.Request("POST", "https://x/y"))

    def test_a_connection_error_is_retried_rather_than_blocking(self):
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="r", status_entry="e"
        )
        client = SequenceClient([self._connection_error(), response(parsed=parsed)])
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=0.01), client=client
        ).plan(MESSAGES)
        assert out.verdict == "project_complete", out.reasoning
        assert len(client.calls) == 2

    def test_it_still_blocks_once_the_budget_is_spent(self):
        # Bounded by wall clock, so an outage that outlasts the cap escalates
        # with the real error rather than retrying forever.
        client = StubClient(self._connection_error())
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=0.01), client=client
        ).plan(MESSAGES)
        assert out.verdict == "blocked"
        assert out.failed is True
        assert "Connection error" in out.reasoning

    def test_a_zero_budget_blocks_on_the_first_failure(self):
        client = StubClient(self._connection_error())
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=0), client=client
        ).plan(MESSAGES)
        assert out.verdict == "blocked"
        assert len(client.calls) == 1

    def _status_error(self, status, kind="overloaded_error"):
        """The exception the installed SDK actually raises for a status code.

        Constructed through `_make_status_error` rather than by naming a class,
        because the class is not the same on both providers: 529 is
        `OverloadedError` on Anthropic and `InternalServerError` on OpenAI.
        Naming one would have passed here and been wrong in the reviewer.
        """
        import httpx
        from anthropic import Anthropic

        message = "Overloaded" if kind == "overloaded_error" else "Invalid request data"
        return Anthropic(api_key="x")._make_status_error(
            message,
            body={"type": "error", "error": {"type": kind, "message": message}},
            response=httpx.Response(
                status, request=httpx.Request("POST", "https://x/y")
            ),
        )

    def test_an_overloaded_provider_is_waited_out(self):
        # The failure this was extended for. A 529 ended a 29-stage run at
        # 09:43 with the work intact and nothing wrong with it — the provider
        # had simply said "come back later", which is what an outage is.
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="r", status_entry="e"
        )
        client = SequenceClient([self._status_error(529), response(parsed=parsed)])
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=0.01), client=client
        ).plan(MESSAGES)
        assert out.verdict == "project_complete", out.reasoning
        assert len(client.calls) == 2

    def test_a_server_error_is_waited_out(self):
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="r", status_entry="e"
        )
        client = SequenceClient([self._status_error(500), response(parsed=parsed)])
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=0.01), client=client
        ).plan(MESSAGES)
        assert out.verdict == "project_complete"

    def test_a_rate_limit_is_waited_out(self):
        # Safe here in a way it is not inside the SDK: the wait is bounded by
        # our wall clock, so honouring a long `retry-after` cannot run for
        # hours — it escalates when the budget is spent, like any other outage.
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="r", status_entry="e"
        )
        client = SequenceClient([self._status_error(429), response(parsed=parsed)])
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=0.01), client=client
        ).plan(MESSAGES)
        assert out.verdict == "project_complete"

    def test_a_bad_request_is_retried_a_few_times_then_blocks(self):
        # Was "not retried at all", and the run disproved it: three 400s in 62
        # minutes on requests that returned 200 when replayed unchanged. Three
        # attempts over five minutes, then the real error — the short budget
        # is what keeps a genuinely malformed request legible.
        client = StubClient(self._status_error(400, kind="invalid_request_error"))
        out = AnthropicPlanner(
            cfg(
                transport_retry_seconds=900,
                invalid_request_retry_seconds=0.01,
                invalid_request_initial_seconds=0.004,
            ),
            client=client,
        ).plan(MESSAGES)
        assert out.verdict == "blocked"
        assert "Invalid request data" in out.reasoning or "400" in out.reasoning
        assert len(client.calls) == 3, "two waits means three calls"

    def test_a_rejection_that_clears_on_the_second_try_is_not_an_escalation(self):
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="r", status_entry="e"
        )
        client = SequenceClient(
            [self._status_error(400, kind="invalid_request_error"),
             response(parsed=parsed)]
        )
        out = AnthropicPlanner(
            cfg(
                invalid_request_retry_seconds=0.01,
                invalid_request_initial_seconds=0.004,
            ),
            client=client,
        ).plan(MESSAGES)
        assert out.verdict == "project_complete", out.reasoning
        assert len(client.calls) == 2

    def test_an_authentication_error_is_not_retried(self):
        # Waiting out a wrong key is fifteen minutes spent to be told it twice.
        client = StubClient(self._status_error(401, kind="authentication_error"))
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=900), client=client
        ).plan(MESSAGES)
        assert out.verdict == "blocked"
        assert len(client.calls) == 1

    def test_a_model_decision_is_not_retried(self):
        # A refusal is an answer. Waiting fifteen minutes to be told it again
        # would hide the answer behind the whole budget.
        client = StubClient(response(parsed=None, stop_reason="refusal"))
        out = AnthropicPlanner(
            cfg(transport_retry_seconds=900), client=client
        ).plan(MESSAGES)
        assert out.verdict == "blocked"
        assert len(client.calls) == 1

    def test_the_wait_is_written_to_the_run_log(self):
        # The whole reason this is not `max_retries`. A silent wait and a hung
        # process are indistinguishable from outside, and the last outage was
        # diagnosed by a human noticing the run had stopped.
        lines = []
        parsed = PlannerResponse(
            verdict="project_complete", reasoning="r", status_entry="e"
        )
        client = SequenceClient([self._connection_error(), response(parsed=parsed)])
        AnthropicPlanner(
            cfg(transport_retry_seconds=0.01), client=client, log=lines.append
        ).plan(MESSAGES)
        assert any("retrying in" in line for line in lines), lines


class TestARejectedAnswerIsStillRecorded:
    """What the planner said, when we decided we could not use it.

    `planner.json` records the outcome, and on a rejection the outcome is ours:
    a synthesized `blocked` carrying our reason. The model's own answer — the
    verdict it chose, the stage it drew, the reasoning it gave — is discarded at
    the moment it becomes most worth reading.

    Live: the planner returned `revise` without a stage spec twice and the run
    escalated. The tool calls survived in the artifact and the answer did not,
    so there was no way afterwards to tell a model that had reasoned well and
    fumbled a field from one that had produced nonsense. It resolved on a third
    attempt, which is the other reason to keep it — a stochastic failure is
    only diagnosable across occurrences.
    """

    def _rejected(self, bad, good=None):
        responses = [response(parsed=bad)]
        responses.append(response(parsed=good if good is not None else bad))
        client = SequenceClient(responses)
        return AnthropicPlanner(cfg(), client=client).plan(MESSAGES)

    def test_a_semantic_rejection_keeps_the_answer(self):
        bad = PlannerResponse(
            verdict="revise", reasoning="narrow it", status_entry="e", stage=a_stage()
        )
        outcome = self._rejected(bad)
        assert outcome.verdict == "blocked"
        assert outcome.raw is not None
        assert outcome.raw["verdict"] == "revise"
        assert outcome.raw["reasoning"] == "narrow it"

    def test_the_stage_it_drew_is_kept_too(self):
        # The field that was missing is the point; the fields that were there
        # say whether the rest of the answer was sound.
        bad = PlannerResponse(
            verdict="revise", reasoning="r", status_entry="e", stage=a_stage()
        )
        outcome = self._rejected(bad)
        assert outcome.raw["stage"]["id"] == "extract-service"

    def test_an_accepted_answer_records_nothing_extra(self):
        # `raw` is for the rejected case. On success the outcome already is the
        # answer, and duplicating it would double the artifact for nothing.
        good = PlannerResponse(
            verdict="next_stage", reasoning="r", status_entry="e", stage=a_stage()
        )
        client = SequenceClient([response(parsed=good)])
        assert AnthropicPlanner(cfg(), client=client).plan(MESSAGES).raw is None

    def test_a_refusal_has_no_answer_to_keep(self):
        # Nothing was parsed, so there is nothing to record. It must not invent
        # an empty one that reads like a malformed answer.
        client = SequenceClient([response(parsed=None, stop_reason="refusal")])
        assert AnthropicPlanner(cfg(), client=client).plan(MESSAGES).raw is None

    def test_a_transport_failure_has_no_answer_to_keep(self):
        outcome, _ = plan_with(RuntimeError("connection reset"))
        assert outcome.failed is True
        assert outcome.raw is None


class TestATruncatedAnswerIsLegible:
    """A response that ran out of room must say so.

    The `stop_reason == "max_tokens"` guard cannot catch this shape: the SDK
    parses structured output before returning, so a response truncated
    mid-JSON raises inside the call and the guard never runs. What reached one
    run's log was a pydantic dump for what is simply an answer too long for its
    budget — and the two have different fixes, so the distinction has to
    survive to the operator.
    """

    def test_a_truncated_json_payload_is_named_as_truncation(self):
        from code_gantry.planner import _call_failure

        message = (
            "1 validation error for PlannerResponse\n  Invalid JSON: EOF while "
            "parsing a string at line 1 column 11710"
        )
        text = _call_failure(ValueError(message))
        assert "truncated" in text
        assert "max_tokens" in text

    def test_an_ordinary_failure_is_left_alone(self):
        from code_gantry.planner import _call_failure

        text = _call_failure(RuntimeError("connection reset by peer"))
        assert "connection reset by peer" in text
        assert "truncated" not in text

    def test_the_diagnosis_survives_and_not_only_the_count(self):
        """The line that says how far it got is the second one.

        pydantic renders a validation error as a count, then the finding, then
        a documentation link. Keeping `splitlines()[0]` kept the count —
        `1 validation error for PlannerResponse` — and discarded the column
        number, which is the only figure that says how large the answer had
        become before it ran out. A gate that reads its evidence and then
        throws it away is indistinguishable afterwards from one that never
        read it.
        """
        from code_gantry.planner import _call_failure

        message = (
            "1 validation error for PlannerResponse\n"
            "  Invalid JSON: EOF while parsing a string at line 1 column 11710 "
            "[type=json_invalid, input_value='{\"verdict\": \"deri', input_type=str]\n"
            "    For further information visit https://errors.pydantic.dev/2.13/v/json_invalid"
        )
        text = _call_failure(ValueError(message))

        assert "column 11710" in text
        assert "json_invalid" in text

    def test_the_message_stays_one_line(self):
        """The run log is one line per event, and this is a rendering of it.

        The same reasoning as `run_argv`'s joined label: a multi-line failure
        pasted into a timeline attaches its tail to whatever came next. Collapse
        rather than truncate — the offending detail is at the end.
        """
        from code_gantry.planner import _call_failure

        truncation = _call_failure(
            ValueError("1 validation error\n  Invalid JSON: EOF while parsing\n    see docs")
        )
        ordinary = _call_failure(RuntimeError("upstream said no\nand then said it again"))

        assert "\n" not in truncation
        assert "\n" not in ordinary
        assert "and then said it again" in ordinary


class TestReasoningEffortIsOperatorControlled:
    """Three roles, three providers, one knob each — and none of it was config.

    The planner's was hard-coded `high`; the reviewer had none at all, so it ran
    at whatever the provider defaults to; the executor never passed one. That is
    the same defect as a stage-size default baked into the system prompt: a
    tuning decision about one deployment, unreachable by the operator who owns
    the deployment.

    Literals are the SDKs' own. `OutputConfigParam.effort` is
    `Literal["low","medium","high","xhigh","max"]` in the installed anthropic
    package, and `ReasoningEffort` in the installed openai package carries
    `max`. Both are generated from the providers' specs and sit on disk, which
    beats recalling them.
    """

    def test_the_planner_default_is_unchanged_behaviour(self):
        from code_gantry.config import PlannerConfig

        assert PlannerConfig(model="m").effort == "high"

    def test_the_planner_effort_reaches_the_call(self):
        from code_gantry.config import PlannerConfig
        from code_gantry.planner import _output_config

        assert _output_config(PlannerConfig(model="m", effort="xhigh")) == {
            "effort": "xhigh"
        }

    def test_the_reviewer_sends_nothing_unless_asked(self):
        # It has always run at the provider default. Inventing one here would
        # change the gate's behaviour on every project that never chose.
        from code_gantry.config import ReviewerConfig
        from code_gantry.reviewer import _reasoning_param

        assert _reasoning_param(ReviewerConfig(model="m")) == {}

    def test_the_reviewer_effort_reaches_the_call(self):
        from code_gantry.config import ReviewerConfig
        from code_gantry.reviewer import _reasoning_param

        assert _reasoning_param(ReviewerConfig(model="m", effort="max")) == {
            "reasoning": {"effort": "max"}
        }


class TestTheToolLogSaysWhatWasDenied:
    """A ceiling is only legible if the artifact records hitting it.

    Measured over one run of 65 planning steps: 24 stopped at exactly the
    25-call cap, and no refusal appeared in `run.log` or any `planner.json`,
    because the ledger was written by `_spend` and `_spend` only runs when a
    tool succeeds. Truncation and satisfaction produced identical records.
    """

    def _planner(self, calls):
        from code_gantry.planner import AnthropicPlanner

        reader = SimpleNamespace(calls=calls)
        return AnthropicPlanner(cfg(), client=object(), reader=reader)

    def test_an_answered_call_reads_as_before(self):
        from code_gantry.repotools import ToolCall

        p = self._planner([ToolCall("read_file", "app/order.rb", 12)])
        assert p._tool_log() == ["read_file(app/order.rb) -> 12 line(s)"]

    def test_a_refusal_says_so_instead_of_reporting_zero_lines(self):
        # "-> 0 line(s)" already means "the search found nothing", which is a
        # different fact and one the planner acts on differently.
        from code_gantry.repotools import ToolCall

        p = self._planner(
            [ToolCall("read_file", "app/ghost.rb", 0, refusal="does not exist")]
        )
        assert p._tool_log() == ["read_file(app/ghost.rb) -> refused: does not exist"]

    def test_a_fruitless_search_is_not_a_refusal(self):
        from code_gantry.repotools import ToolCall

        p = self._planner([ToolCall("search", "widget in app", 0)])
        assert p._tool_log() == ["search(widget in app) -> 0 line(s)"]

    def test_the_answered_count_travels_with_the_outcome(self):
        """The count, not the length of the log.

        `tool_calls` used to be a proxy for "the planner read something", and
        `reconcile` refuses a verdict reached without reading. Recording
        refusals broke that proxy — two refused calls make a non-empty log
        describing nothing seen — so the count crosses the boundary as its own
        value rather than being re-derived from rendered strings at the far end.
        """
        from code_gantry.repotools import ToolCall

        p = self._planner(
            [
                ToolCall("read_file", "app/order.rb", 12),
                ToolCall("read_file", "app/ghost.rb", 0, refusal="does not exist"),
            ]
        )
        assert p._reads_answered() == 1

    def test_a_log_of_nothing_but_refusals_answers_zero(self):
        from code_gantry.repotools import ToolCall

        p = self._planner(
            [ToolCall("read_file", "app/ghost.rb", 0, refusal="does not exist")]
        )
        assert p._reads_answered() == 0


class TestTheStatOnTheCostLine:
    """Measured off the landing commit, and old lines still parse.

    `stage-costs.md` is append-only and spans every run of a project, so the
    entries that inform a derivation are mostly ones written before any given
    field existed. A parser that required the new group would drop the whole
    history the first time it ran — which is the shape of defect this file has
    already had once, when the models suffix was added.
    """

    def test_the_stat_is_written_when_it_was_measured(self, tmp_path):
        from code_gantry.planner import append_stage_cost

        append_stage_cost(
            tmp_path, "s", "0285803b159a", 4, 13_000, changed=(2, 131, 0),
        )
        assert "2 changed +131 -0" in (tmp_path / "stage-costs.md").read_text()

    def test_no_measurement_writes_no_stat(self, tmp_path):
        # `shortstat` returns None when git cannot answer. A stage that
        # changed nothing and a stage nobody could measure are different, and
        # "0 changed" would claim the first.
        from code_gantry.planner import append_stage_cost

        append_stage_cost(tmp_path, "s", "0285803b159a", 4, 13_000)
        assert "changed" not in (tmp_path / "stage-costs.md").read_text()

    def test_it_parses_back(self, tmp_path):
        from code_gantry.planner import append_stage_cost, recent_stage_costs

        append_stage_cost(
            tmp_path, "s", "0285803b159a", 4, 13_000, changed=(2, 131, 7),
        )
        got = recent_stage_costs(tmp_path)[0]
        assert (got["changed"], got["insertions"], got["deletions"]) == (2, 131, 7)
        assert got["files"] == 4, "the declared scope survives beside it"

    def test_a_line_written_before_the_stat_existed_still_parses(self, tmp_path):
        from code_gantry.planner import recent_stage_costs

        (tmp_path / "stage-costs.md").write_text(
            "- cost `aaaaaaaaaaaa` `old` — 2 file(s), 9,000 executor tokens\n"
            "- cost `bbbbbbbbbbbb` `mid` — 1 file(s), 8,000 peak\n"
        )
        got = recent_stage_costs(tmp_path)
        assert [c["stage_id"] for c in got] == ["old", "mid"]
        assert "changed" not in got[0] and "changed" not in got[1]

    def test_the_stat_survives_a_spend_suffix(self, tmp_path):
        # Order on the line matters: the stat sits before the per-role spend,
        # and the regex must not stop at the first semicolon.
        from code_gantry.planner import append_stage_cost, recent_stage_costs

        append_stage_cost(
            tmp_path, "s", "0285803b159a", 4, 13_000, changed=(2, 131, 0),
            spend=[{"role": "planner", "prompt": 500, "cached": 0,
                    "completion": 10, "cost_usd": 1.5}],
            roles=(("plan", "claude-opus-5", "xhigh"),),
        )
        got = recent_stage_costs(tmp_path)[0]
        assert got["changed"] == 2
