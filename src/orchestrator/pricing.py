"""Dollars for the roles that spend them, from a table nobody here maintains.

The run reported tokens for the planner and reviewer and dollars for the
executor alone — so the only column carrying money described the role that
spends 1-3% of it, while a review at ~$1-2 and a planner call at ~$3.40 went
unpriced. Effort could be argued about and not settled.

The prices are not ours to write down, and the first design here was going to
put a hand-written rate table in project config. That would have been the
`Config should hold the path, not the copy` mistake with money in it: a second
copy that drifts, and the copy is the one the report reads. Aider prices every
attempt from `model_prices_and_context_window.json`, which
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
from collections.abc import Callable
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


def load_price_map(
    cache_path: Path | str,
    url: str | None = None,
    fetch: Callable[[str], str] | None = None,
) -> dict:
    """The public price table, refreshed if reachable and cached if not.

    Deliberately unable to fail. A price list is a report, not a gate: a run
    that stopped because a JSON file was unreachable would be trading the work
    for the accounting. Every failure path lands on the cached copy and then on
    `{}`, which prices nothing and says so.
    """
    cache_path = Path(cache_path)
    fetch = fetch or _fetch
    try:
        text = fetch(url or PRICE_MAP_URL)
        data = json.loads(text)
        if isinstance(data, dict) and data:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text)
            return data
    except Exception:
        pass
    try:
        data = json.loads(cache_path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


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

    `None` rather than `0.0`, and that distinction is the reason to write this
    rather than read Aider's number: its accounting reports zero for "not
    priced" as often as for "free", so a local endpoint and a missing rate look
    identical in the record.

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
