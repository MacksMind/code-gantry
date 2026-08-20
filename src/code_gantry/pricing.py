"""Dollars for the roles that spend them, from a table nobody here maintains.

The run reported tokens for the planner and reviewer and dollars for the
executor alone — so the only column carrying money described the role that
spends 1-3% of it, while a review at ~$1-2 and a planner call at ~$3.40 went
unpriced. Effort could be argued about and not settled.

The prices are not ours to write down, and the first design here was going to
put a hand-written rate table in project config. That would have been the
`Config should hold the path, not the copy` mistake with money in it: a second
copy that drifts, and the copy is the one the report reads. The rates live in
`model_prices_and_context_window.json`, which
`litellm/__init__.py:433` fetches from this URL at import — the copy bundled in
the package is a stale fallback that lacks all three of the models this project
uses. Reading the same URL makes our planner and reviewer figures consistent
with the executor's by construction rather than by coincidence.

Nothing in that table is keyed by reasoning effort. The effort-shaped keys in
it — `supports_max_reasoning_effort` and friends — are capability booleans, not
rates, and they describe the default endpoint: they say the gpt-5.6 models do
not support `max`, which is true of chat/completions and false of
/v1/responses. Effort changes how many reasoning tokens are produced, billed at
the ordinary output rate, so its whole cost is already in `completion_tokens`.
What was missing was never a price. It was a label: which model, at which
effort, produced the count.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

PRICE_MAP_URL = os.getenv(
    "LITELLM_MODEL_COST_MAP_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json",
)

PRICE_MAP_FILENAME = "model-prices.json"


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=15) as fh:  # noqa: S310
        return fh.read().decode("utf-8")


def price_map_path(cfg) -> Path | None:
    """Where the cache lives, or `None` when there is nowhere to put it.

    This used to be the bare relative string `PRICE_MAP_FILENAME`, spelled out
    at all three call sites, which meant the file landed wherever the process
    had been started from. Launched beside the plan documents — the obvious
    cwd, because it is where the config is — that put 1.76MB of somebody
    else's rate table into a *tracked* directory of the target repository. It
    sat untracked until a stage's `checks` commit swept it onto the stage
    branch, the scope gate correctly flagged it, and the revision prompt then
    embedded its whole 1,807,718-character diff and was refused by the
    provider at 1,020,584 tokens against a 1,000,000 ceiling. The run ended
    there, on a stage that had nothing wrong with it.

    That the cwd decided this was invisible: it appears in no config, no log
    header and no artifact. So the path is derived from the work directory,
    which is gitignored by construction, and there is no cwd-relative fallback
    to reach for — a `None` return says "do not cache" rather than "cache
    somewhere arbitrary".

    `CODE_GANTRY_PRICE_MAP` still names the file outright, so a test can pin
    one and an air-gapped operator can supply one.
    """
    override = os.getenv("CODE_GANTRY_PRICE_MAP")
    if override:
        return Path(override)
    work_dir = getattr(cfg, "work_dir", None)
    return Path(work_dir) / PRICE_MAP_FILENAME if work_dir else None


def configured_models(cfg) -> tuple[str, ...]:
    """Every model this run can be billed for, in role order.

    The one place the roles are enumerated for pricing, so the cache, the
    report and the loop cannot disagree about which models matter. Named
    explicitly rather than discovered, for the same reason `_stage_spend`
    names them: a fourth role should be a line here, not a rule to work out.
    """
    seen: list[str] = []
    for role in ("planner", "executor", "reviewer"):
        model = getattr(getattr(cfg, role, None), "model", None)
        if model and model not in seen:
            seen.append(model)
    return tuple(seen)


def project_entries(price_map: dict, models: Iterable[str | None]) -> dict:
    """Just the models this run prices, each entry kept whole.

    Measured on the real table: 3,055 entries and 1.76MB, of which a run reads
    three — 5,165 characters between them. Keeping the whole table was the
    `config should hold the path, not the copy` instinct honoured at the config
    layer and abandoned one layer out: we avoided a hand-maintained rate table
    by making a verbatim copy of somebody else's.

    Projected by *model* and not by key. Trimming each entry to the four rates
    `price_usage` reads would save a few kilobytes off a file that is now a few
    kilobytes, and it would be a hand-written subset of an upstream schema —
    which is the shape that has already cost this project an artifact missing
    the field that answered the question it existed for.

    Keyed by whatever key actually resolved, so `entry_for` finds it again on
    the fallback path exactly as it does on the live one.
    """
    out: dict = {}
    for model in models:
        if not model:
            continue
        for key in (model, model.rsplit("/", 1)[-1]):
            entry = price_map.get(key)
            if isinstance(entry, dict):
                out[key] = entry
                break
    return out


def load_price_map(
    cache_path: Path | str | None,
    models: Iterable[str | None],
    url: str | None = None,
    fetch: Callable[[str], str] | None = None,
) -> dict:
    """The rates for `models`, refreshed if reachable and cached if not.

    Deliberately unable to fail. A price list is a report, not a gate: a run
    that stopped because a JSON file was unreachable would be trading the work
    for the accounting. Every failure path lands on the cached copy and then on
    `{}`, which prices nothing and says so.

    `models` is required rather than defaulted, because a caller that could
    omit it would be a caller that could write the whole table again — and the
    three that existed each wrote it, none of them deliberately.

    What is returned is the projection on both paths. A fallback that hands
    back a different shape from the path it stands in for is the trap rather
    than the protection; a cache written before this shipped is the whole
    table and still answers, because `entry_for` looks up the same keys.
    """
    fetch = fetch or _fetch
    cache_path = Path(cache_path) if cache_path is not None else None
    try:
        data = json.loads(fetch(url or PRICE_MAP_URL))
        if isinstance(data, dict) and data:
            wanted = project_entries(data, models)
            if cache_path is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(wanted, indent=2, sort_keys=True))
            return wanted
    except Exception:
        pass
    if cache_path is None:
        return {}
    try:
        data = json.loads(cache_path.read_text())
    except Exception:
        return {}
    return project_entries(data, models) if isinstance(data, dict) else {}


# One table per (cache path, model set) for the life of the process.
#
# `executorloop` carried this memo, with a comment explaining that the loader
# reaches the network on every call and that pricing an attempt without one
# would make hundreds of HTTP calls a run. `nodes._stage_spend` called the
# loader directly and is reached from `advance`, so every landed stage
# refetched the table and rewrote the cache — the guard was written for one of
# the two call sites. It lives here now, where a third caller inherits it.
#
# Rebuilt on the next start, which is when a changed rate would matter anyway.
_MEMO: dict[tuple[str, tuple[str, ...]], dict] = {}


def clear_price_cache() -> None:
    """Forget the memo. For tests, and for anything that reloads config."""
    _MEMO.clear()


def cached_price_map(cfg, fetch: Callable[[str], str] | None = None) -> dict:
    """The rates for this config's roles, fetched at most once per process."""
    path = price_map_path(cfg)
    models = configured_models(cfg)
    key = (str(path), models)
    if key not in _MEMO:
        _MEMO[key] = load_price_map(path, models, fetch=fetch)
    return _MEMO[key]


def entry_for(price_map: dict, model: str | None) -> dict | None:
    """The table's entry for a model as *config* spells it.

    Config carries the provider — `openai/gpt-5.6-luna` — and the table keys on
    the bare id. Looking up only the literal string reports the one model we
    already had a price for as unpriced, which is the failure this whole module
    exists to remove. The `responses/` infix that routes to /v1/responses falls
    off the same way.
    """
    if not model:
        return None
    entry = price_map.get(model)
    if entry is None:
        entry = price_map.get(model.rsplit("/", 1)[-1])
    return entry if isinstance(entry, dict) else None


def price_usage(
    entry: dict | None,
    prompt: int,
    cached: int,
    cache_writes: int,
    completion: int,
) -> float | None:
    """What one role's token counts cost, or None when the model is unpriced.

    `None` rather than `0.0`, and the distinction is the whole point. A rate
    table that reports zero for "not priced" as readily as for "free" makes a
    local endpoint and a missing rate identical in the record, and the second
    is a bug while the first is a fact.

    `prompt` is total input for either provider — reads and writes are parts of
    it, not additions to it. Read the other way it produced "Uncached prompt
    tokens: -2,438" and a 251% hit rate in a real report.

    Cache writes get their own bucket because they are billed *above* base:
    6.25e-06 against 5e-06 on Opus. Folding them into the uncached remainder
    understates by 25% of whatever was just written, which is worst on exactly
    the calls that grow the cacheable prefix — the ones a prompt-ordering
    change is meant to be judged on.
    """
    if not entry:
        return None
    in_rate = entry.get("input_cost_per_token")
    out_rate = entry.get("output_cost_per_token")
    if in_rate is None and out_rate is None:
        return None
    in_rate = in_rate or 0.0
    out_rate = out_rate or 0.0
    read_rate = entry.get("cache_read_input_token_cost")
    write_rate = entry.get("cache_creation_input_token_cost")
    # A model with no cache pricing is not a model whose cache is free.
    read_rate = in_rate if read_rate is None else read_rate
    write_rate = in_rate if write_rate is None else write_rate

    uncached = max(prompt - cached - cache_writes, 0)
    return (
        uncached * in_rate
        + cached * read_rate
        + cache_writes * write_rate
        + completion * out_rate
    )
