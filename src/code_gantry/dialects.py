"""Which wire a call goes out on, decided by the model rather than the role.

Each of the three roles used to hardcode an API surface, and none of them
chose it: the planner speaks Anthropic Messages because it talks to Anthropic,
and the executor and reviewer speak OpenAI Responses because of what they
replaced. That made *role* the axis along which the protocol varies, which is
an accident of arrival order rather than a decision — nothing about judging a
diff implies one wire and nothing about planning implies the other.

The axis is the model family. There are two wires and three families, because
Gemini is served best by the same one Anthropic uses.

Every row is a measurement rather than a preference:

- **`openai/*` -> Responses.** `gpt-5.6-sol` refuses function tools together
  with reasoning on chat/completions outright. On Responses a request writes
  four breakpoints but matching considers up to the latest eighty, so marks
  *accumulate* rather than move and every tool turn extends the cached prefix
  — 98.1%, 99.996% and 97.2% measured across three replayed stages.
- **`anthropic/*` -> Messages.** Structured output, `cache_control` with an
  explicit TTL, and the longest-matching-prefix extension the plan block
  depends on.
- **`google/*` -> Messages.** Measured 2026-08-21. On Responses our
  OpenAI-dialect marks mean nothing to it, leaving only free implicit prefix
  caching that pins at one figure however the conversation grows. Through
  Messages with a moving `cache_control` breakpoint, the same model over four
  tool turns read 13,427 -> 16,325 -> 28,579 -> 36,751, writing only on the
  first.

This module is pure request shaping — no client, no network — for the reason
`prompts.py` is kept apart from the clients: the decisions here are worth
testing without a model in the loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Dialect:
    """One provider's spelling of the same handful of request decisions."""

    name: str
    _structured_key: str
    _effort_key: str
    _effort_shape: Callable[[str], dict]
    _text_type: str
    _cache_key: str
    _cache_marker: Callable[[str | None], dict]
    _request_cache_options: dict
    _usage: Callable[[object], object]

    def structured(self, schema) -> dict:
        """The schema argument, when the caller wants a parsed answer."""
        return {self._structured_key: schema} if schema is not None else {}

    def effort(self, level) -> dict:
        """Reasoning effort, only when the operator chose one.

        Absent by default so a model that does not take the parameter is not
        sent it, and so no default of ours overrides a provider's.
        """
        return {self._effort_key: self._effort_shape(level)} if level else {}

    def text_block(self, text: str, cache: bool = False, ttl: str | None = None) -> dict:
        """One block of prompt text, optionally marked as a cache breakpoint.

        The TTL reaches only the wire that has one. Anthropic's default
        ephemeral window is about five minutes and a stage outlasts it — the
        plan block shipped with a bare marker once and the reports showed 3%
        cached, that 3% being the one block that carried a lifetime.
        """
        block = {"type": self._text_type, "text": text}
        if cache:
            block[self._cache_key] = self._cache_marker(ttl)
        return block

    def cache_options(self) -> dict:
        """Request-level cache switches, where the wire has any.

        Responses needs an explicit opt-in because GPT-5.6 caches at
        breakpoints and does *not* fall back to the longest matching prefix,
        so the marks below only count if this is set.
        """
        return dict(self._request_cache_options)

    def usage(self, raw):
        """Provider counts, normalised.

        The two disagree about what the words mean. Anthropic reports three
        orthogonal numbers — `input_tokens` is only what was neither read from
        nor written to cache — and read as OpenAI's shape that produced a 251%
        hit rate in a real report. Both come out here as total input and the
        part of it that was a cache read.
        """
        return self._usage(raw)


def _responses_usage(raw):
    from code_gantry.openaiclient import extract_usage

    return extract_usage(raw)


def _messages_usage(raw):
    from code_gantry.planner import _extract_usage

    return _extract_usage(raw)


RESPONSES = Dialect(
    name="responses",
    _structured_key="text_format",
    _effort_key="reasoning",
    _effort_shape=lambda level: {"effort": level},
    _text_type="input_text",
    _cache_key="prompt_cache_breakpoint",
    # The lifetime is fixed on this wire; a TTL here would be a field the API
    # does not take.
    _cache_marker=lambda _ttl: {"mode": "explicit"},
    _request_cache_options={"prompt_cache_options": {"mode": "explicit"}},
    _usage=_responses_usage,
)

MESSAGES = Dialect(
    name="messages",
    _structured_key="output_format",
    _effort_key="output_config",
    _effort_shape=lambda level: {"effort": level},
    _text_type="text",
    _cache_key="cache_control",
    _cache_marker=lambda ttl: {"type": "ephemeral", **({"ttl": ttl} if ttl else {})},
    _request_cache_options={},
    _usage=_messages_usage,
)

# Substrings, matched against a normalised model id. Deliberately not exact
# names: the same model arrives as `claude-opus-5`, `anthropic/claude-opus-5`
# and `us.anthropic.claude-opus-5` depending on the route, and both an
# operator and a router can extend the set. A fixed list of exact strings
# meeting an extensible set is a bet rather than a specification.
_FAMILIES: tuple[tuple[tuple[str, ...], Dialect], ...] = (
    (("anthropic", "claude"), MESSAGES),
    (("google", "gemini"), MESSAGES),
    (("openai", "gpt-", "o1", "o3", "o4"), RESPONSES),
)

# Named policies rather than models. Resolving one is the caller's job, at run
# start, on a surface where the choice is honoured — a dialect picked for a
# policy would be a guess about what it resolves to, and `openrouter/pareto-code`
# resolved to three different families inside one day.
_POLICIES = ("openrouter/pareto", "openrouter/auto", "openrouter/free",
             "openrouter/fusion", "openrouter/bodybuilder")

# What an unclassified model gets. A run must not end because a router picked
# something new, and every provider OpenRouter lists as caching automatically
# does so without the marks this wire sends — so an unknown model loses
# fine-grained control and nothing else.
_DEFAULT = RESPONSES

_BY_NAME = {"responses": RESPONSES, "messages": MESSAGES}


def dialect_for(model: str, override: str | None = None) -> Dialect:
    """The wire this model should be called on.

    `override` is the operator's escape hatch: the map is provider knowledge,
    but a deployment may know about a model this file has never heard of.
    """
    if override:
        try:
            return _BY_NAME[override]
        except KeyError:
            raise ValueError(
                f"unknown dialect {override!r}; expected one of {sorted(_BY_NAME)}"
            ) from None
    ident = (model or "").lower()
    if any(p in ident for p in _POLICIES):
        raise ValueError(
            f"{model!r} names a routing policy rather than a model. Resolve it "
            "to a concrete model first — which dialect it wants depends on what "
            "it resolves to, and that changes between runs."
        )
    for needles, dialect in _FAMILIES:
        if any(n in ident for n in needles):
            return dialect
    return _DEFAULT
