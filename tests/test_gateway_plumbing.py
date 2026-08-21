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

    def test_both_usage_types_carry_it_last(self):
        """Both are constructed positionally, so a field in the middle would
        silently reassign every caller."""
        for kind in (TokenUsage, PlannerUsage):
            names = list(kind.__dataclass_fields__)
            assert names[-1] == "provider_cost_usd", kind.__name__

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
