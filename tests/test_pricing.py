"""Dollars, from the public rate table.

CodeGantry reported tokens for the planner and reviewer and dollars for
the executor only — so the file carrying money covered the role that spends
1-3% of it, and the two roles worth ~$1-2 a review and ~$3.40 a planner call
were unpriced. Effort could therefore be argued about but not settled.

The prices are not ours to write down. `litellm/__init__.py:433` fetches
`model_prices_and_context_window.json` at import and every attempt is priced
from it; the copy bundled in the package is a stale fallback that lacks all
three of our models. Reading the same URL keeps our planner and reviewer
figures consistent with the executor's by construction rather than by
coincidence, and means nobody maintains a rate by hand.

Nothing in that table is keyed by reasoning effort — the effort keys in it are
capability booleans, not rates. Effort changes how many reasoning tokens are
produced, billed at the ordinary output rate, so its whole cost is already in
`completion_tokens`. What was missing was not a price but a label: which model
and which effort produced the count.
"""

import json

import pytest

from code_gantry.pricing import (
    PRICE_MAP_URL,
    load_price_map,
    price_usage,
)


ENTRY = {
    "input_cost_per_token": 5e-06,
    "output_cost_per_token": 2.5e-05,
    "cache_read_input_token_cost": 5e-07,
    "cache_creation_input_token_cost": 6.25e-06,
}


class TestPricingUsage:
    def test_each_bucket_is_charged_at_its_own_rate(self):
        # 1M uncached in, 1M cache read, 1M cache write, 1M out.
        got = price_usage(
            ENTRY, prompt=3_000_000, cached=1_000_000,
            cache_writes=1_000_000, completion=1_000_000,
        )
        assert got == pytest.approx(5.00 + 0.50 + 6.25 + 25.00)

    def test_cache_writes_are_not_billed_as_uncached_input(self):
        # The reason this needs its own bucket: a write costs 1.25x base on
        # Anthropic, so folding writes into the uncached remainder understates
        # the planner by 25% of whatever it just wrote — largest on exactly the
        # calls that grow the cacheable prefix.
        writes = price_usage(ENTRY, prompt=1_000_000, cached=0,
                             cache_writes=1_000_000, completion=0)
        uncached = price_usage(ENTRY, prompt=1_000_000, cached=0,
                               cache_writes=0, completion=0)
        assert writes > uncached

    def test_prompt_tokens_is_the_total_not_the_remainder(self):
        # `prompt_tokens` means total input for either provider — reads and
        # writes are parts of it, not additions to it. Read the other way it
        # produced "Uncached prompt tokens: -2,438" in a real report.
        got = price_usage(ENTRY, prompt=1_000_000, cached=1_000_000,
                          cache_writes=0, completion=0)
        assert got == pytest.approx(0.50)

    def test_a_missing_rate_falls_back_to_the_base_input_rate(self):
        # A model with no cache pricing is not a model whose cache is free.
        bare = {"input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05}
        got = price_usage(bare, prompt=1_000_000, cached=1_000_000,
                          cache_writes=0, completion=0)
        assert got == pytest.approx(5.00)

    def test_an_unpriced_model_is_none_not_zero(self):
        # An accounting layer that reports 0.0 for "not priced" as often as for
        # "free", which is why a local model and a billing error looked alike.
        assert price_usage(None, prompt=1, cached=0, cache_writes=0, completion=1) is None
        assert price_usage({}, prompt=1, cached=0, cache_writes=0, completion=1) is None


class TestLoadingTheMap:
    def _fetch(self, payload):
        def fetch(url):
            assert url == PRICE_MAP_URL
            return json.dumps(payload)
        return fetch

    def test_it_fetches_and_caches(self, tmp_path):
        cache = tmp_path / "model-prices.json"
        got = load_price_map(cache, fetch=self._fetch({"claude-opus-5": ENTRY}))
        assert got["claude-opus-5"] == ENTRY
        assert json.loads(cache.read_text())["claude-opus-5"] == ENTRY

    def test_a_failed_fetch_falls_back_to_the_cache(self, tmp_path):
        # A run does not stop because a price list was unreachable. The figure
        # is a report, not a gate.
        cache = tmp_path / "model-prices.json"
        cache.write_text(json.dumps({"claude-opus-5": ENTRY}))

        def boom(url):
            raise OSError("no network")

        assert load_price_map(cache, fetch=boom)["claude-opus-5"] == ENTRY

    def test_no_cache_and_no_network_is_empty_not_an_error(self, tmp_path):
        def boom(url):
            raise OSError("no network")

        assert load_price_map(tmp_path / "absent.json", fetch=boom) == {}

    def test_a_corrupt_cache_does_not_raise(self, tmp_path):
        cache = tmp_path / "model-prices.json"
        cache.write_text("{ not json")

        def boom(url):
            raise OSError("no network")

        assert load_price_map(cache, fetch=boom) == {}

    def test_a_provider_prefixed_model_resolves(self, tmp_path):
        # Config names the executor `openai/gpt-5.6-luna`; the table keys it as
        # `gpt-5.6-luna`. Looking up only the literal string reports the one
        # model we already had a price for as unpriced.
        cache = tmp_path / "model-prices.json"
        m = load_price_map(cache, fetch=self._fetch({"gpt-5.6-luna": ENTRY}))
        from code_gantry.pricing import entry_for

        assert entry_for(m, "openai/gpt-5.6-luna") == ENTRY
        assert entry_for(m, "gpt-5.6-luna") == ENTRY
        assert entry_for(m, "openai/responses/gpt-5.6-luna") == ENTRY
        assert entry_for(m, "no-such-model") is None
