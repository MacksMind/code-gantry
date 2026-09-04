"""All three roles reach a gateway the same way, and carry its cost home.

The behaviour is decided from the endpoint, never declared: an operator
chooses OpenRouter by pointing `api_base` at it and should not have to know
that `session_id` or `provider.require_parameters` exist. Which API accepts
them is a property of the provider, and a first-party endpoint rejects an
argument it does not recognise — so this has to be a fact rather than a
setting, and one shared implementation rather than three that drift.
"""

import pytest

from code_gantry.config import ExecutorConfig, PlannerConfig, ReviewerConfig
from code_gantry.gateway import gateway_body, is_openrouter
from code_gantry.openaiclient import TokenUsage
from code_gantry.planner import PlannerUsage
from code_gantry.state import accumulate_usage, usage_deltas

ROLES = [
    ExecutorConfig(model="m", api_base="https://openrouter.ai/api/v1"),
    PlannerConfig(model="claude-opus-5", api_base="https://openrouter.ai/api/v1"),
    ReviewerConfig(model="gpt-5.6-sol", api_base="https://openrouter.ai/api/v1"),
]


class TestEveryRoleGetsTheSameTreatment:
    @pytest.mark.parametrize("cfg", ROLES, ids=lambda c: type(c).__name__)
    def test_the_gateway_fields_are_present(self, cfg):
        body = gateway_body(cfg, "sess-1")["extra_body"]
        assert body["provider"]["require_parameters"] is True
        assert body["session_id"] == "sess-1"

    @pytest.mark.parametrize("cfg", ROLES, ids=lambda c: type(c).__name__)
    def test_a_first_party_endpoint_gets_nothing(self, cfg):
        direct = cfg.model_copy(update={"api_base": None})
        assert gateway_body(direct, "sess-1") == {}

    def test_the_host_decides_not_the_string(self):
        assert not is_openrouter("https://example.com/openrouter.ai/v1")
        assert is_openrouter("https://openrouter.ai/api/v1")


class TestCostComesHome:
    """A gateway bills us and says what it billed. Deriving the same figure
    from a rate table is a second arithmetic over the same tokens, and the
    second is the one that drifts."""

    def test_both_usage_types_only_ever_grow_at_the_end(self):
        """Both are constructed positionally, so a field in the middle would
        silently reassign every caller. Pinning the prefix rather than the last
        name says the actual rule — new fields append — and keeps saying it
        after the next one lands. It also writes down the transposition: these
        two carry the same names in a different order at positions 1 and 2, and
        both are built positionally."""
        assert list(TokenUsage.__dataclass_fields__)[:6] == [
            "prompt_tokens", "completion_tokens", "cached_tokens",
            "cache_write_tokens", "peak_prompt_tokens", "provider_cost_usd",
        ]
        assert list(PlannerUsage.__dataclass_fields__)[:6] == [
            "prompt_tokens", "cached_tokens", "completion_tokens",
            "cache_write_tokens", "peak_prompt_tokens", "provider_cost_usd",
        ]

    def test_the_long_window_write_reaches_the_run_totals_and_the_bill(self):
        """Tested the way the values that were lost crossing these schemas were
        not: extractor to `run_usage` to a priced figure, with the arithmetic
        checked at the far end rather than the near one. No ordinal claimed —
        the count is spelled differently in two places already."""
        from code_gantry.pricing import price_usage
        from code_gantry.planner import _extract_usage

        class U:
            input_tokens = 0
            output_tokens = 0
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 1_000_000
            cache_creation = type(
                "C", (), {"ephemeral_1h_input_tokens": 400_000},
            )()

        totals = accumulate_usage(None, **usage_deltas("planner_", _extract_usage(U())))
        assert totals["planner_cache_write_tokens"] == 1_000_000
        assert totals["planner_cache_write_1h_tokens"] == 400_000
        # And it is a component, so two calls sum like any other total.
        twice = accumulate_usage(totals, **usage_deltas("planner_", _extract_usage(U())))
        assert twice["planner_cache_write_1h_tokens"] == 800_000

        entry = {
            "input_cost_per_token": 1e-05,
            "output_cost_per_token": 5e-05,
            "cache_read_input_token_cost": 2.5e-07,
            "cache_creation_input_token_cost": 1.25e-05,
            "cache_creation_input_token_cost_above_1hr": 2e-05,
        }
        billed = price_usage(
            entry,
            totals["planner_prompt_tokens"],
            totals["planner_cached_tokens"],
            totals["planner_cache_write_tokens"],
            totals["planner_completion_tokens"],
            writes_1h=totals["planner_cache_write_1h_tokens"],
        )
        assert billed == pytest.approx(600_000 * 1.25e-05 + 400_000 * 2e-05)
        # What it cost before the split, which is the size of the defect.
        blind = price_usage(
            entry,
            totals["planner_prompt_tokens"],
            totals["planner_cached_tokens"],
            totals["planner_cache_write_tokens"],
            totals["planner_completion_tokens"],
            writes_1h=0,
        )
        assert blind == pytest.approx(12.50)
        assert billed > blind

    def test_deltas_are_walked_not_enumerated(self):
        """Four call sites named these by hand, and this codebase has already
        lost three values that way — each computed correctly at both ends and
        dropped crossing a schema."""
        keys = set(usage_deltas("planner_", PlannerUsage(1, 2, 3, 4, 5, 0.25)))
        assert "planner_provider_cost_usd" in keys
        assert len(keys) == len(PlannerUsage.__dataclass_fields__)

    def test_costs_sum_through_none(self):
        a = accumulate_usage(None, **usage_deltas("", TokenUsage(1, 2, 3, 4, 5, 0.5)))
        b = accumulate_usage(a, **usage_deltas("", TokenUsage(1, 2, 3, 4, 5, 0.25)))
        assert b["provider_cost_usd"] == pytest.approx(0.75)

    def test_unreported_stays_unreported(self):
        """Not zero. A zero has meant 'no rate for this model' as often as it
        has meant free, which is why the field is optional at all."""
        out = accumulate_usage(None, **usage_deltas("", TokenUsage(1, 2, 3, 4, 5)))
        assert out["provider_cost_usd"] is None

    def test_stage_spend_prefers_the_reported_figure(self, monkeypatch):
        from code_gantry import nodes, pricing

        def explode(*a, **k):  # pragma: no cover - the point is it never runs
            raise AssertionError("the rate table was consulted")

        monkeypatch.setattr(pricing, "cached_price_map", explode)
        cfg = type("C", (), {
            "planner": PlannerConfig(model="p"),
            "executor": ExecutorConfig(model="e"),
            "reviewer": ReviewerConfig(model="r"),
        })()
        rows = nodes._stage_spend(
            cfg,
            {"planner_prompt_tokens": 100, "planner_provider_cost_usd": 0.42},
        )
        assert [r["cost_usd"] for r in rows if r["role"] == "planner"] == [0.42]
