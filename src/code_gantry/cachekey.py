"""Identifying a run's prompt cache to the provider, within its limits.

OpenAI's `prompt_cache_key` is capped at 64 characters and rejects anything
longer with a 400. That was invisible while a project's identity was a slug:
under twenty characters, and nothing was going to grow it.

Then the config moved into the repository it describes, `work_dir` became the
project's identity, and the identity became a path — the plan directory plus a
work directory beneath it, 85 characters on the project that found this and 98
with the role prefix. The value did not change meaning; it changed *length*, and a
limit nobody had thought about since it was set began to bind. It is the same
shape as a value that was private while its file was private: what a field can
hold is a property of where it comes from, and moving the source re-opens
every question about it.

Hashing rather than truncating. `executor.py` had already met this once and
answered it with `[:64]`, which is right until two projects share a long
prefix — `/Users/someone/very/long/path/{a,b}` truncate to the same key and
silently share a cache. The prefix is kept because a legible key is worth
something where it fits, and because keeping it means an identity that already
fits produces exactly the key it produced before: no working cache is
invalidated by this existing.
"""

from __future__ import annotations

import hashlib

# The provider's limit, not ours.
MAX_CACHE_KEY = 64

# Enough that a collision is not a thing to reason about, short enough that
# every role prefix fits alongside it.
_DIGEST = 24


def cache_key(role: str, identity: str) -> str:
    """A stable per-project key for `role`, never longer than the limit."""
    whole = f"{role}:{identity}"
    if len(whole) <= MAX_CACHE_KEY:
        return whole
    return f"{role}:{hashlib.sha256(identity.encode()).hexdigest()[:_DIGEST]}"
