"""A cost the provider reports is a fact; a cost we derive is a claim.

`CLAUDE.md` already says to prefer the fact to the label, and `pricing.py`
exists because nobody here wants to maintain a rate table — the fix at the time
was to stop hand-writing one and fetch someone else's instead, which is a
verbatim copy of an upstream schema and has already cost a run when it landed
in the wrong directory.

A gateway that returns `cost` on the usage block removes the copy entirely. It
also becomes the *only* possible answer under a router: `openrouter/pareto-code`
resolves the model per request, lists its price as `-1`, and was measured
answering the same call as `openai/gpt-5.6-sol` and `x-ai/grok-4.6` at
different scores. `project_entries` projects the downloaded map by
`configured_models`, so the table would be asked about a model that has no
entry and cannot have one.

`None` rather than `0.0` throughout, for the reason `_price` already documents:
a zero has meant "no rate for this model" as often as it has meant "free".
"""

import pytest

from code_gantry.openaiclient import TokenUsage, extract_usage, merge_usage


class Reported:
    """One turn's usage as a gateway hands it back."""

    def __init__(self, prompt=100, completion=10, cached=0, cost=None):
        self.input_tokens = prompt
        self.output_tokens = completion
        self.input_tokens_details = type(
            "D", (), {"cached_tokens": cached, "cache_write_tokens": None}
        )()
        if cost is not None:
            self.cost = cost


class TestExtracted:
    def test_a_reported_cost_is_carried(self):
        assert extract_usage(Reported(cost=0.0038326)).provider_cost_usd == 0.0038326

    def test_no_reported_cost_is_none_not_zero(self):
        """The distinction the whole field exists to preserve."""
        assert extract_usage(Reported()).provider_cost_usd is None

    def test_a_genuinely_free_call_reports_zero(self):
        """Zero is a real answer when the provider is the one saying it."""
        assert extract_usage(Reported(cost=0.0)).provider_cost_usd == 0.0

    def test_a_null_cache_write_does_not_become_a_crash(self):
        """OpenRouter sends `cache_write_tokens: null` on a cache *read*."""
        assert extract_usage(Reported(cached=15060)).cache_write_tokens == 0


class TestMerged:
    def test_costs_sum_across_the_turns_of_a_loop(self):
        """Unlike the peak. A loop bills once per turn and pays for each."""
        merged = merge_usage(
            TokenUsage(provider_cost_usd=0.01), TokenUsage(provider_cost_usd=0.02)
        )
        assert merged.provider_cost_usd == pytest.approx(0.03)

    def test_two_unreported_costs_stay_unreported(self):
        merged = merge_usage(TokenUsage(), TokenUsage())
        assert merged.provider_cost_usd is None

    def test_one_reported_side_survives_the_merge(self):
        """A provider that reports on some turns and not others still bills."""
        merged = merge_usage(TokenUsage(), TokenUsage(provider_cost_usd=0.02))
        assert merged.provider_cost_usd == pytest.approx(0.02)
        merged = merge_usage(TokenUsage(provider_cost_usd=0.02), TokenUsage())
        assert merged.provider_cost_usd == pytest.approx(0.02)


class TestPositional:
    def test_the_new_field_went_last(self):
        """This type is constructed positionally in the tests and the loop.

        `cache_write_tokens` and `peak_prompt_tokens` both carry a comment
        saying they went last for this reason. A field inserted in the middle
        silently reassigns every positional caller.
        """
        u = TokenUsage(1, 2, 3, 4, 5)
        assert (u.prompt_tokens, u.completion_tokens, u.cached_tokens) == (1, 2, 3)
        assert (u.cache_write_tokens, u.peak_prompt_tokens) == (4, 5)
        assert u.provider_cost_usd is None


class TestPricePrefersTheReport:
    """`_price` derives from a rate table; a gateway hands us the answer.

    Two arithmetics over the same tokens is the shape that produced a peak
    computed twice earlier this week. There is one computation here, chosen by
    whether the provider reported, rather than two that can disagree.
    """

    def test_a_reported_cost_is_used_and_the_table_is_not_consulted(self, monkeypatch):
        from code_gantry import executorloop, pricing

        def explode(*a, **k):  # pragma: no cover - the point is it never runs
            raise AssertionError("the rate table was consulted")

        monkeypatch.setattr(pricing, "cached_price_map", explode)
        usage = TokenUsage(prompt_tokens=15080, provider_cost_usd=0.0038326)
        assert executorloop._price(None, usage, "openrouter/pareto-code") == 0.0038326

    def test_without_a_report_the_table_still_answers(self, monkeypatch):
        from code_gantry import executorloop, pricing

        monkeypatch.setattr(pricing, "cached_price_map", lambda cfg: {})
        monkeypatch.setattr(pricing, "entry_for", lambda prices, model: {"x": 1})
        monkeypatch.setattr(pricing, "price_usage", lambda *a: 1.25)
        usage = TokenUsage(prompt_tokens=15080)
        assert executorloop._price(None, usage, "gpt-5.6-luna") == 1.25

    def test_a_reported_zero_is_not_mistaken_for_no_report(self, monkeypatch):
        from code_gantry import executorloop, pricing

        def explode(*a, **k):  # pragma: no cover
            raise AssertionError("the rate table was consulted")

        monkeypatch.setattr(pricing, "cached_price_map", explode)
        assert executorloop._price(None, TokenUsage(provider_cost_usd=0.0), "m") == 0.0


class TestServedModels:
    """Under a router, the configured model is not the model that answered.

    `stage-costs.md` and every measurement in `CLAUDE.md` assume one executor.
    A run whose turns resolved to different models produces numbers no later
    reading can decompose, and the only way to notice is to record what served
    each turn.
    """

    def test_the_turn_records_what_answered_it(self):
        from code_gantry.executorclient import ExecutorTurn

        turn = ExecutorTurn()
        turn.note_served("openai/gpt-5.6-sol")
        turn.note_served("openai/gpt-5.6-sol")
        turn.note_served("x-ai/grok-4.6")
        assert turn.served_models == {"openai/gpt-5.6-sol": 2, "x-ai/grok-4.6": 1}

    def test_an_unreported_model_is_not_counted_as_empty_string(self):
        from code_gantry.executorclient import ExecutorTurn

        turn = ExecutorTurn()
        turn.note_served("")
        turn.note_served(None)
        assert turn.served_models == {}
