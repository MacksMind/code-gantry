"""The parts of talking to OpenAI that are not about what we are asking.

Two roles now call the Responses API — the reviewer, which judges a diff, and
the executor, which writes one. Everything about *what* they ask differs;
everything about how a turn is read back is identical: where the usage figures
live, how a `function_call` item carries its arguments, which exception a
transient failure arrives as.

Kept apart because the alternative was measured elsewhere in this codebase and
found expensive. `_clip` was written twice, in `nodes.py` and `verify.py`, and
the decision about what to keep when output is too long got made twice and
differently; `verify._resolve_declared` and `executor._auto_test_command` both
select test paths and disagree about a path that does not exist yet. Two copies
of a reading do not stay one reading. This module exists so the second OpenAI
client cannot fork the first.

The names carry no leading underscore any more, because a helper imported
across a module boundary is that module's interface whatever it is called.
`reviewer.py` aliases the old private names so its tests keep naming what they
have always named.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from orchestrator.plannertools import call_detail


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    # Last, so positional construction keeps working. Written to cache but not
    # read back: billed above base rate, so a run that writes on every call and
    # reads on none is paying a premium for nothing — which is exactly what
    # gpt-5.6-sol was measured doing, six calls, ~55k written each, zero read.
    cache_write_tokens: int = 0

    @property
    def uncached_prompt_tokens(self) -> int:
        return max(self.prompt_tokens - self.cached_tokens, 0)


def transport_errors() -> tuple[type[BaseException], ...]:
    """Exception types a transient failure can arrive as.

    `APITimeoutError` subclasses `APIConnectionError`, so one entry covers
    both. Resolved lazily and degrading to no retrying, matching how the
    SDK is imported everywhere else here.

    `APIStatusError` covers everything the server did answer, and needs
    `is_transient_status` behind it to separate "come back later" from "your
    request was wrong".
    """
    try:
        from openai import APIConnectionError, APIStatusError
    except ImportError:  # pragma: no cover - the SDK is a hard dependency
        return ()
    return (APIConnectionError, APIStatusError)


def tool_request(item) -> tuple[str, dict]:
    """Name and arguments from one `function_call` output item.

    Flat on the Responses API — `name` and `arguments` sit on the item itself
    rather than under a nested `function` object as they do on chat
    completions.

    Arguments arrive as a JSON *string* rather than an object, and a model can
    emit one that does not parse. That is a bad request, not a dead turn — an
    empty dict reaches `dispatch`, which answers with a readable refusal the
    caller can act on.
    """
    name = getattr(item, "name", "") or ""
    raw = getattr(item, "arguments", "") or "{}"
    try:
        args = json.loads(raw)
    except (TypeError, ValueError):
        return name, {}
    return name, args if isinstance(args, dict) else {}


def refusal(response) -> str:
    """The refusal text, if the model declined.

    A refusal is a content part inside an output message rather than a field on
    the response, so it has to be looked for. Missing it would let `None` reach
    the parsed check and be reported as an unparsable answer — true, but not
    the diagnosis.
    """
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            if getattr(part, "type", "") == "refusal":
                return getattr(part, "refusal", "") or "no reason given"
    return ""


def describe_call(name: str, args: dict) -> str:
    """One tool call, rendered for the log and the artifact."""
    detail = call_detail(args)
    return f"{name}({detail})" if detail else name


def merge_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    """Totals across the turns of one call.

    A tool loop bills once per turn, so the single-call reading understates what
    the turn cost by however many times it looked at something. Summing here is
    what keeps `report.md` honest — the economic argument for splitting the
    models depends on that number staying true.
    """
    return TokenUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        cached_tokens=left.cached_tokens + right.cached_tokens,
        cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
    )


def extract_usage(usage) -> TokenUsage:
    """Read what the provider reported, tolerating absent fields.

    The Responses API names these `input_tokens` and `output_tokens`, with the
    cache figures under `input_tokens_details`. The chat-completions names are
    still read as a fallback so a stub or an older shape does not silently
    report zero — a usage of zero is indistinguishable from a free call, and
    the economic argument for splitting the models depends on this number
    staying true.
    """
    if usage is None:
        return TokenUsage()

    details = getattr(usage, "input_tokens_details", None) or getattr(
        usage, "prompt_tokens_details", None
    )
    prompt = getattr(usage, "input_tokens", None)
    if prompt is None:
        prompt = getattr(usage, "prompt_tokens", 0)
    completion = getattr(usage, "output_tokens", None)
    if completion is None:
        completion = getattr(usage, "completion_tokens", 0)

    return TokenUsage(
        prompt_tokens=prompt or 0,
        completion_tokens=completion or 0,
        cached_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
        cache_write_tokens=(
            (getattr(details, "cache_write_tokens", 0) or 0) if details else 0
        ),
    )
