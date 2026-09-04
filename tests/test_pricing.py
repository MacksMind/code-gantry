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
from types import SimpleNamespace

import pytest

from code_gantry.pricing import (
    PRICE_MAP_FILENAME,
    PRICE_MAP_URL,
    cached_price_map,
    clear_price_cache,
    configured_models,
    load_price_map,
    price_map_path,
    price_usage,
    project_entries,
)


def a_config(work_dir=None, planner="claude-opus-5", executor="openai/gpt-5.6-luna",
             reviewer="openai/gpt-5.6-sol"):
    """Enough of a config to be billed: three roles and somewhere to cache."""
    return SimpleNamespace(
        work_dir=work_dir,
        planner=SimpleNamespace(model=planner),
        executor=SimpleNamespace(model=executor),
        reviewer=SimpleNamespace(model=reviewer),
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
            cache_writes=1_000_000, completion=1_000_000, writes_1h=0,
        )
        assert got == pytest.approx(5.00 + 0.50 + 6.25 + 25.00)

    def test_cache_writes_are_not_billed_as_uncached_input(self):
        # The reason this needs its own bucket: a write costs 1.25x base on
        # Anthropic, so folding writes into the uncached remainder understates
        # the planner by 25% of whatever it just wrote — largest on exactly the
        # calls that grow the cacheable prefix.
        writes = price_usage(ENTRY, prompt=1_000_000, cached=0,
                             cache_writes=1_000_000, completion=0, writes_1h=0)
        uncached = price_usage(ENTRY, prompt=1_000_000, cached=0,
                               cache_writes=0, completion=0, writes_1h=0)
        assert writes > uncached

    def test_prompt_tokens_is_the_total_not_the_remainder(self):
        # `prompt_tokens` means total input for either provider — reads and
        # writes are parts of it, not additions to it. Read the other way it
        # produced "Uncached prompt tokens: -2,438" in a real report.
        got = price_usage(ENTRY, prompt=1_000_000, cached=1_000_000,
                          cache_writes=0, completion=0, writes_1h=0)
        assert got == pytest.approx(0.50)

    def test_a_missing_rate_falls_back_to_the_base_input_rate(self):
        # A model with no cache pricing is not a model whose cache is free.
        bare = {"input_cost_per_token": 5e-06, "output_cost_per_token": 2.5e-05}
        got = price_usage(bare, prompt=1_000_000, cached=1_000_000,
                          cache_writes=0, completion=0, writes_1h=0)
        assert got == pytest.approx(5.00)

    def test_the_one_hour_write_bucket_costs_more_than_the_five_minute_one(self):
        # The whole reason the split is carried. Anthropic sums the two buckets
        # into `cache_creation_input_tokens`; pricing that sum at the cheaper
        # rate understated the planner line by 25% a derivation once the plan
        # block started shipping a `1h` marker.
        entry = dict(ENTRY, cache_creation_input_token_cost_above_1hr=1e-05)
        short = price_usage(entry, prompt=1_000_000, cached=0,
                            cache_writes=1_000_000, completion=0, writes_1h=0)
        long = price_usage(entry, prompt=1_000_000, cached=0,
                           cache_writes=1_000_000, completion=0,
                           writes_1h=1_000_000)
        assert short == pytest.approx(6.25)
        assert long == pytest.approx(10.00)
        # And a mixed call is billed at both rates, not at either one.
        half = price_usage(entry, prompt=1_000_000, cached=0,
                           cache_writes=1_000_000, completion=0,
                           writes_1h=400_000)
        assert half == pytest.approx(600_000 * 6.25e-06 + 400_000 * 1e-05)

    def test_a_table_with_no_long_window_rate_prices_as_it_always_did(self):
        # An unpriced distinction costs the old arithmetic exactly, rather than
        # a guess in the expensive direction: no `above_1hr` key means the
        # table cannot separate them, not that the long window is free.
        assert "cache_creation_input_token_cost_above_1hr" not in ENTRY
        got = price_usage(ENTRY, prompt=1_000_000, cached=0,
                          cache_writes=1_000_000, completion=0,
                          writes_1h=1_000_000)
        assert got == pytest.approx(6.25)

    def test_a_breakdown_larger_than_its_own_total_is_clamped(self):
        # Two fields of one provider report. A breakdown exceeding the total it
        # is part of is a reading about the wire, and pricing the remainder
        # negative would answer it by handing money back.
        entry = dict(ENTRY, cache_creation_input_token_cost_above_1hr=1e-05)
        got = price_usage(entry, prompt=1_000_000, cached=0,
                          cache_writes=1_000_000, completion=0,
                          writes_1h=9_000_000)
        assert got == pytest.approx(10.00)

    def test_an_unpriced_model_is_none_not_zero(self):
        # An accounting layer that reports 0.0 for "not priced" as often as for
        # "free", which is why a local model and a billing error looked alike.
        assert price_usage(None, prompt=1, cached=0, cache_writes=0, completion=1, writes_1h=0) is None
        assert price_usage({}, prompt=1, cached=0, cache_writes=0, completion=1, writes_1h=0) is None


class TestLoadingTheMap:
    """What lands on disk, and what a run gets back.

    The cache is a projection of the public table, not a copy of it. Measured
    on the real one: 3,055 entries and 1.76MB, of which a run consumes three
    — and the copy went wherever the process happened to be started, which put
    it inside a tracked directory of the target repo, where a stage's `checks`
    commit swept it onto a branch and the 1.8M-character diff took the
    planner's next call past the provider's million-token ceiling.

    Entries are kept whole rather than trimmed to the four rate keys
    `price_usage` reads. A projection by *key* would be a hand-written subset
    of somebody else's schema — the shape this project has already been bitten
    by three times — and the saving over projecting by model is a few
    kilobytes on a file that is now a few kilobytes.
    """

    def _fetch(self, payload, counter=None):
        def fetch(url):
            assert url == PRICE_MAP_URL
            if counter is not None:
                counter.append(url)
            return json.dumps(payload)
        return fetch

    def test_it_fetches_and_caches(self, tmp_path):
        cache = tmp_path / "model-prices.json"
        got = load_price_map(
            cache, ("claude-opus-5",), fetch=self._fetch({"claude-opus-5": ENTRY})
        )
        assert got["claude-opus-5"] == ENTRY
        assert json.loads(cache.read_text())["claude-opus-5"] == ENTRY

    def test_only_the_configured_models_reach_the_cache(self, tmp_path):
        # The whole reason this is a projection: the public table is three
        # thousand entries and a run prices three of them.
        cache = tmp_path / "model-prices.json"
        table = {f"model-{n}": ENTRY for n in range(50)}
        table["claude-opus-5"] = ENTRY
        got = load_price_map(cache, ("claude-opus-5",), fetch=self._fetch(table))
        assert set(json.loads(cache.read_text())) == {"claude-opus-5"}
        assert set(got) == {"claude-opus-5"}

    def test_the_entry_is_kept_whole(self, tmp_path):
        # Not trimmed to the keys `price_usage` happens to read today. A
        # hand-written subset of an upstream schema is the defect this project
        # keeps paying for, and here it would buy nothing.
        cache = tmp_path / "model-prices.json"
        entry = dict(ENTRY, max_input_tokens=1_000_000, litellm_provider="anthropic")
        load_price_map(cache, ("claude-opus-5",), fetch=self._fetch({"claude-opus-5": entry}))
        assert json.loads(cache.read_text())["claude-opus-5"] == entry

    def test_a_provider_prefixed_model_is_cached_under_the_key_that_resolved_it(
        self, tmp_path
    ):
        # Config names the executor `openai/responses/gpt-5.6-luna`; the table
        # keys it bare. The projection has to store what `entry_for` will look
        # for, or the fallback path prices nothing.
        from code_gantry.pricing import entry_for

        cache = tmp_path / "model-prices.json"
        got = load_price_map(
            cache,
            ("openai/responses/gpt-5.6-luna",),
            fetch=self._fetch({"gpt-5.6-luna": ENTRY, "other": ENTRY}),
        )
        assert set(json.loads(cache.read_text())) == {"gpt-5.6-luna"}
        assert entry_for(got, "openai/responses/gpt-5.6-luna") == ENTRY

    def test_the_fallback_returns_what_the_live_path_returns(self, tmp_path):
        # A fallback that behaves differently from the path it stands in for is
        # the trap, not the fallback.
        cache = tmp_path / "model-prices.json"
        models = ("claude-opus-5",)
        live = load_price_map(cache, models, fetch=self._fetch({"claude-opus-5": ENTRY}))

        def boom(url):
            raise OSError("no network")

        assert load_price_map(cache, models, fetch=boom) == live

    def test_a_failed_fetch_falls_back_to_the_cache(self, tmp_path):
        # A run does not stop because a price list was unreachable. The figure
        # is a report, not a gate.
        cache = tmp_path / "model-prices.json"
        cache.write_text(json.dumps({"claude-opus-5": ENTRY}))

        def boom(url):
            raise OSError("no network")

        assert load_price_map(cache, ("claude-opus-5",), fetch=boom)["claude-opus-5"] == ENTRY

    def test_a_cache_written_before_the_projection_still_reads(self, tmp_path):
        # The file on disk when this shipped was the whole upstream table.
        cache = tmp_path / "model-prices.json"
        cache.write_text(json.dumps({f"m{n}": ENTRY for n in range(50)} | {"gpt-5.6-luna": ENTRY}))

        def boom(url):
            raise OSError("no network")

        from code_gantry.pricing import entry_for

        got = load_price_map(cache, ("openai/gpt-5.6-luna",), fetch=boom)
        assert entry_for(got, "openai/gpt-5.6-luna") == ENTRY

    def test_no_cache_and_no_network_is_empty_not_an_error(self, tmp_path):
        def boom(url):
            raise OSError("no network")

        assert load_price_map(tmp_path / "absent.json", ("claude-opus-5",), fetch=boom) == {}

    def test_a_corrupt_cache_does_not_raise(self, tmp_path):
        cache = tmp_path / "model-prices.json"
        cache.write_text("{ not json")

        def boom(url):
            raise OSError("no network")

        assert load_price_map(cache, ("claude-opus-5",), fetch=boom) == {}

    def test_a_configured_model_absent_from_the_table_is_not_priced(self, tmp_path):
        from code_gantry.pricing import entry_for

        cache = tmp_path / "model-prices.json"
        got = load_price_map(cache, ("local/llama",), fetch=self._fetch({"claude-opus-5": ENTRY}))
        assert entry_for(got, "local/llama") is None
        assert price_usage(entry_for(got, "local/llama"), 1, 0, 0, 1, writes_1h=0) is None

    def test_with_nowhere_to_cache_it_still_prices(self, tmp_path):
        # No work dir means no project directory, so there is nowhere safe to
        # write. Fetching still works; only the offline fallback is given up.
        got = load_price_map(None, ("claude-opus-5",), fetch=self._fetch({"claude-opus-5": ENTRY}))
        assert got["claude-opus-5"] == ENTRY

    def test_a_provider_prefixed_model_resolves(self, tmp_path):
        # Config names the executor `openai/gpt-5.6-luna`; the table keys it as
        # `gpt-5.6-luna`. Looking up only the literal string reports the one
        # model we already had a price for as unpriced.
        cache = tmp_path / "model-prices.json"
        m = load_price_map(
            cache,
            ("openai/gpt-5.6-luna",),
            fetch=self._fetch({"gpt-5.6-luna": ENTRY}),
        )
        from code_gantry.pricing import entry_for

        assert entry_for(m, "openai/gpt-5.6-luna") == ENTRY
        assert entry_for(m, "gpt-5.6-luna") == ENTRY
        assert entry_for(m, "openai/responses/gpt-5.6-luna") == ENTRY
        assert entry_for(m, "no-such-model") is None


class TestProjecting:
    def test_it_keeps_one_key_per_model_however_config_spells_it(self):
        table = {"gpt-5.6-luna": ENTRY, "claude-opus-5": ENTRY, "unused": ENTRY}
        got = project_entries(table, ("openai/responses/gpt-5.6-luna", "claude-opus-5"))
        assert set(got) == {"gpt-5.6-luna", "claude-opus-5"}

    def test_a_model_the_table_does_not_carry_is_simply_absent(self):
        assert project_entries({"a": ENTRY}, ("b",)) == {}

    def test_no_models_projects_nothing(self):
        assert project_entries({"a": ENTRY}, ()) == {}


class TestWhereTheCacheGoes:
    """Under the work dir, never the process's cwd.

    The cwd default is the defect this fix exists for: it decided, invisibly
    from the launch command, that a 1.76MB file would be written into a
    tracked directory of somebody's repository.
    """

    def test_it_lands_under_the_work_dir(self, tmp_path):
        assert price_map_path(a_config(work_dir=tmp_path)) == tmp_path / PRICE_MAP_FILENAME

    def test_it_is_never_relative_to_the_cwd(self, tmp_path):
        got = price_map_path(a_config(work_dir=tmp_path))
        assert got.is_absolute()

    def test_without_a_work_dir_there_is_nowhere_to_cache(self):
        assert price_map_path(a_config(work_dir=None)) is None

    def test_the_environment_variable_wins(self, tmp_path, monkeypatch):
        # An air-gapped operator supplies a table; a test pins one.
        monkeypatch.setenv("CODE_GANTRY_PRICE_MAP", str(tmp_path / "pinned.json"))
        assert price_map_path(a_config(work_dir=tmp_path)) == tmp_path / "pinned.json"

    def test_only_pricing_builds_the_path(self):
        # Three call sites each wrote `os.environ.get(...) or PRICE_MAP_FILENAME`
        # and all three therefore carried the cwd default. One selector, so a
        # caller cannot spell it a fourth way.
        import pathlib

        # The name and the value both, and the first draft of this test looked
        # for `PRICE_MAP_FILENAME`'s *value* while every other module names the
        # *identifier* — so it passed against three call sites that each built
        # the path by hand.
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "code_gantry"
        offenders = sorted(
            f.name
            for f in src.glob("*.py")
            if f.name != "pricing.py"
            and ("PRICE_MAP_FILENAME" in (t := f.read_text()) or PRICE_MAP_FILENAME in t)
        )
        assert offenders == []


class TestFetchingOncePerRun:
    """`advance` priced every landing, and priced it over the network.

    `executorloop` had a memo and a comment saying why; `nodes._stage_spend`
    called the loader directly and ran once per landed stage, so a long run
    re-downloaded the table — and rewrote the cache file — on every landing.
    The guard existed at one of the two call sites, which is why it lives in
    the loader now.
    """

    def setup_method(self):
        clear_price_cache()

    def teardown_method(self):
        clear_price_cache()

    def _fetch(self, payload, calls):
        def fetch(url):
            calls.append(url)
            return json.dumps(payload)
        return fetch

    def test_the_table_is_fetched_once_however_often_it_is_asked_for(self, tmp_path):
        calls: list[str] = []
        cfg = a_config(work_dir=tmp_path)
        fetch = self._fetch({"claude-opus-5": ENTRY}, calls)
        first = cached_price_map(cfg, fetch=fetch)
        for _ in range(20):
            assert cached_price_map(cfg, fetch=fetch) == first
        assert len(calls) == 1

    def test_two_configs_do_not_share_an_answer(self, tmp_path):
        calls: list[str] = []
        fetch = self._fetch({"claude-opus-5": ENTRY}, calls)
        cached_price_map(a_config(work_dir=tmp_path / "a"), fetch=fetch)
        cached_price_map(a_config(work_dir=tmp_path / "b"), fetch=fetch)
        assert len(calls) == 2


class TestWhichModelsGetPriced:
    def test_it_is_the_three_roles(self, tmp_path):
        assert configured_models(a_config()) == (
            "claude-opus-5",
            "openai/gpt-5.6-luna",
            "openai/gpt-5.6-sol",
        )

    def test_a_model_named_twice_is_listed_once(self):
        cfg = a_config(planner="m", executor="m", reviewer="m")
        assert configured_models(cfg) == ("m",)

    def test_a_role_with_no_model_is_skipped(self):
        assert configured_models(a_config(reviewer=None)) == (
            "claude-opus-5",
            "openai/gpt-5.6-luna",
        )
