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
    _tool_calls: Callable[[object], list]
    _stopped: Callable[[object], bool]
    _append_model_turn: Callable[[list, object], None]
    _append_tool_results: Callable[..., None]
    _tool_schemas: Callable[[object], list]
    _split_system: Callable[..., tuple]
    _client: Callable[[object], object]
    _refusal: Callable[[object], str]
    _final_text: Callable[[object], str]
    _send: Callable[..., object]

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


    def refusal(self, response) -> str:
        """The refusal text, if the model declined.

        Looked for rather than read off a field: on Responses it is a content
        part inside an output message; on Messages it is a stop reason. Missing
        it lets the absence of an answer be reported as an unparsable one —
        true, but not the diagnosis.
        """
        return self._refusal(response)

    def final_text(self, response) -> str:
        """The model's closing message, for the attempt log."""
        return self._final_text(response)

    def send(self, client, cfg, conversation: list, tools, extra: dict):
        """One call, on this wire.

        Everything shape-dependent is settled here: where the system prompt
        goes, whether a token ceiling is mandatory, and which method carries a
        tool loop.
        """
        return self._send(self, client, cfg, conversation, tools, extra)


    def split_system(self, conversation: list) -> tuple[list | None, list]:
        """The system prompt, lifted out where the wire wants it separate.

        Responses carries it as the first input item; Messages takes it as its
        own argument and rejects a `{"role": "system"}` item outright. A role
        builds one conversation and should not have to know which.

        Never mutates: the caller holds this list across turns and mirrors it
        to disk as it grows.
        """
        return self._split_system(self, conversation)

    def client(self, cfg):
        """The SDK client this wire talks through.

        The key comes from the environment by name, never from config: config
        is committed and hashed, and a key in either place is a key in the
        repository.
        """
        return self._client(cfg)


    # --- the tool loop ----------------------------------------------------
    #
    # Both clients grew their own: the planner's on Messages, the executor's
    # on Responses, neither aware the other existed. They do the same four
    # things in two shapes, and the shape belongs to the endpoint rather than
    # the vendor — the same Google model returned `function_call` items on one
    # and `tool_use` blocks on the other.

    def tool_calls(self, response) -> list[dict]:
        """What the model asked for, as `{id, name, args}`.

        Tolerant of shape: this reads an SDK object in one place and a stub in
        another, and a fourteen-hour run should not end on an attribute error.
        """
        return self._tool_calls(response)

    def stopped(self, response) -> bool:
        """Whether the model has finished asking for things.

        Deliberately not a claim of success — "I am done" and "this needs a
        file outside my scope so I have stopped" are the same signal here, and
        the gates decide which happened.
        """
        return self._stopped(response)

    def append_model_turn(self, conversation: list, response) -> None:
        """Put the model's turn back into the conversation, in this wire's shape."""
        self._append_model_turn(conversation, response)

    def append_tool_results(
        self, conversation: list, results, cache: bool = False, ttl: str | None = None
    ) -> None:
        """Answer every call from the last turn, marking only the last result.

        The two wires want opposite things and this is the seam that says so.
        On Responses marks *accumulate*: a request writes its latest four but
        matching considers up to eighty, so every marked result extends the
        cached prefix. On Messages they *move*: only the last one counts for
        Gemini, and four is a hard ceiling that marking every result would
        pass by the fifth turn.
        """
        self._append_tool_results(self, conversation, list(results), cache, ttl)

    def tool_schemas(self, specs) -> list[dict]:
        """Operator- and role-declared tools, in this wire's shape."""
        return self._tool_schemas(specs)


def _responses_usage(raw):
    from code_gantry.openaiclient import extract_usage

    return extract_usage(raw)


def _messages_usage(raw):
    from code_gantry.planner import _extract_usage

    return _extract_usage(raw)



# --- Responses ------------------------------------------------------------

def _responses_tool_calls(response) -> list[dict]:
    import json as _json

    out = []
    for item in getattr(response, "output", None) or []:
        if _get(item, "type") != "function_call":
            continue
        raw = _get(item, "arguments") or "{}"
        try:
            args = _json.loads(raw)
        except (TypeError, ValueError):
            # A bad request, not a dead turn: an empty dict reaches dispatch,
            # which answers with a refusal the model can act on.
            args = {}
        out.append({"id": _get(item, "call_id") or "", "name": _get(item, "name") or "",
                    "args": args if isinstance(args, dict) else {}})
    return out


def _responses_stopped(response) -> bool:
    return not _responses_tool_calls(response)


def _responses_append_model_turn(conversation: list, response) -> None:
    # The whole output list, not just the calls: a reasoning model emits a
    # reasoning item that each function_call declares as required, and sending
    # the call alone is rejected outright.
    conversation.extend(getattr(response, "output", None) or [])


def _responses_append_tool_results(wire, conversation, results, cache, ttl):
    # *Every* result, not just the last. Marks accumulate on this wire — a
    # request writes its latest four but matching considers up to the latest
    # eighty in the conversation — so each mark extends the cached prefix
    # instead of restarting it. That is the arrangement measured at 98.1%,
    # 99.996% and 97.2% across three replayed stages, and marking only the
    # newest would quietly undo it.
    for _i, (call_id, text) in enumerate(results):
        block = {"type": "input_text", "text": text}
        if cache:
            block["prompt_cache_breakpoint"] = {"mode": "explicit"}
        conversation.append(
            {"type": "function_call_output", "call_id": call_id, "output": [block]}
        )


def _responses_tool_schemas(specs) -> list[dict]:
    # Strict is not a preference: the SDK will not auto-parse a structured
    # response beside a non-strict tool, and strict in turn requires every
    # property listed as required with `additionalProperties: false` — so
    # optional ones are made nullable, which is the shape strict mode provides
    # for "may be omitted".
    #
    # Delegated rather than reimplemented. `as_strict_tool` already rewrites
    # nested object properties, which `edit` needs and which the planner's
    # version never had to do.
    from code_gantry.plannertools import as_strict_tool

    return [as_strict_tool(spec) for spec in specs]


# --- Messages -------------------------------------------------------------

def _messages_tool_calls(response) -> list[dict]:
    out = []
    for block in getattr(response, "content", None) or []:
        if _get(block, "type") != "tool_use":
            continue
        out.append({"id": _get(block, "id") or "", "name": _get(block, "name") or "",
                    "args": _get(block, "input") or {}})
    return out


def _messages_stopped(response) -> bool:
    return getattr(response, "stop_reason", None) != "tool_use"


def _messages_append_model_turn(conversation: list, response) -> None:
    blocks = []
    for block in getattr(response, "content", None) or []:
        kind = _get(block, "type")
        if kind == "text":
            blocks.append({"type": "text", "text": _get(block, "text") or ""})
        elif kind == "tool_use":
            blocks.append({"type": "tool_use", "id": _get(block, "id") or "",
                           "name": _get(block, "name") or "",
                           "input": _get(block, "input") or {}})
        elif kind == "thinking":
            # Carried so the model keeps its own reasoning across turns.
            blocks.append({"type": "thinking", "thinking": _get(block, "thinking") or "",
                           "signature": _get(block, "signature") or ""})
        elif kind == "redacted_thinking":
            # Gemini emits these on this wire. A thinking block dropped from
            # the echo breaks the turn it belongs to.
            blocks.append({"type": "redacted_thinking", "data": _get(block, "data") or ""})
    if blocks:
        conversation.append({"role": "assistant", "content": blocks})


def _messages_append_tool_results(wire, conversation, results, cache, ttl):
    # One message holding all of them: the API requires every tool_use to be
    # answered in the next message.
    blocks = [{"type": "tool_result", "tool_use_id": cid, "content": text}
              for cid, text in results]
    if blocks and cache:
        blocks[-1]["cache_control"] = wire._cache_marker(ttl)
    if blocks:
        conversation.append({"role": "user", "content": blocks})


def _messages_tool_schemas(specs) -> list[dict]:
    # The neutral spec this codebase already builds is this wire's shape:
    # `{name, description, input_schema}`. Copied rather than passed through,
    # so a caller cannot mutate what it was handed.
    return [dict(spec) for spec in specs]


def _get(obj, name):
    """Attribute or key. An SDK object here, a plain dict in a transcript."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)



def _no_split(wire, conversation):
    """Responses keeps the system prompt where the caller put it."""
    return None, list(conversation)


def _lift_system(wire, conversation):
    conv = list(conversation)
    if not conv or _get(conv[0], "role") != "system":
        return None, conv
    head = conv[0]
    blocks = [
        {"type": "text", "text": _get(b, "text") or ""}
        for b in (_get(head, "content") or [])
    ]
    return blocks, conv[1:]


def _api_key(cfg) -> str:
    import os

    name = getattr(cfg, "api_key_env", None)
    if not name:
        raise KeyError("no api_key_env is configured for this endpoint")
    if name not in os.environ:
        raise KeyError(
            f"{name} is not set; the client cannot authenticate. It is named in "
            "the project config and read from the environment so that no key is "
            "ever written to a file that gets committed."
        )
    return os.environ[name]


def _gateway_base(base, wire: str):
    """OpenRouter's base URL for this wire.

    The two SDKs disagree about what a base is: the OpenAI client wants
    `.../api/v1` and appends `responses`, the Anthropic client wants `.../api`
    and appends `v1/messages`. That was survivable while each role's wire was
    fixed — the planner's config carries no `/v1` and the executor's does.
    Choosing the wire from the *model* makes one `api_base` serve both, and an
    executor pointed at a Gemini model built an Anthropic client on
    `.../api/v1`, resolving to `/api/v1/v1/messages`.

    Only OpenRouter's layout is ours to know. Anything else is spelled by
    whoever runs it and is left exactly as configured.
    """
    from code_gantry.gateway import is_openrouter

    if not base or not is_openrouter(base):
        return base
    trimmed = str(base).rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return f"{trimmed}/v1" if wire == "responses" else trimmed


def _responses_client(cfg):
    from openai import OpenAI

    kwargs = {"api_key": _api_key(cfg), "max_retries": 0}
    base = cfg.resolve_api_base() if hasattr(cfg, "resolve_api_base") else None
    base = _gateway_base(base, "responses")
    if base:
        kwargs["base_url"] = base
    timeout = getattr(cfg, "request_timeout_seconds", None)
    if timeout:
        kwargs["timeout"] = timeout
    return OpenAI(**kwargs)


def _messages_client(cfg):
    import anthropic

    kwargs = {"api_key": _api_key(cfg), "max_retries": 0}
    base = cfg.resolve_api_base() if hasattr(cfg, "resolve_api_base") else None
    base = _gateway_base(base, "messages")
    if base:
        kwargs["base_url"] = base
    timeout = getattr(cfg, "request_timeout_seconds", None)
    if timeout:
        kwargs["timeout"] = timeout
    return anthropic.Anthropic(**kwargs)



def _responses_refusal(response) -> str:
    for item in getattr(response, "output", None) or []:
        for part in _get(item, "content") or []:
            if _get(part, "type") == "refusal":
                return _get(part, "refusal") or "no reason given"
    return ""


def _messages_refusal(response) -> str:
    return ("the model declined to answer"
            if getattr(response, "stop_reason", None) == "refusal" else "")


def _responses_final_text(response) -> str:
    parts = []
    for item in getattr(response, "output", None) or []:
        for part in _get(item, "content") or []:
            text = _get(part, "text")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _messages_final_text(response) -> str:
    parts = [_get(b, "text") for b in (getattr(response, "content", None) or [])
             if _get(b, "type") == "text" and _get(b, "text")]
    return "\n".join(parts)


def _responses_send(wire, client, cfg, conversation, tools, extra):
    return client.responses.create(
        model=cfg.model,
        # A plain list on the wire. `conversation` is a `Transcript` — a list
        # subclass that mirrors itself to disk — and what the SDK does with a
        # subclass is its business rather than a fact to rely on.
        input=list(conversation),
        tools=tools,
        **extra,
    )


def _messages_send(wire, client, cfg, conversation, tools, extra):
    system, messages = wire.split_system(conversation)
    kwargs = {"model": cfg.model, "messages": messages, "tools": tools, **extra}
    if system:
        kwargs["system"] = system
    # Mandatory on this wire, unlike the other. Generous rather than tight:
    # thinking counts against it along with the answer, so a small budget
    # truncates the response rather than the reasoning.
    kwargs["max_tokens"] = getattr(cfg, "max_tokens", None) or 32_000
    return client.messages.create(**kwargs)


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
    _tool_calls=_responses_tool_calls,
    _stopped=_responses_stopped,
    _append_model_turn=_responses_append_model_turn,
    _append_tool_results=_responses_append_tool_results,
    _tool_schemas=_responses_tool_schemas,
    _split_system=_no_split,
    _client=_responses_client,
    _refusal=_responses_refusal,
    _final_text=_responses_final_text,
    _send=_responses_send,
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
    _tool_calls=_messages_tool_calls,
    _stopped=_messages_stopped,
    _append_model_turn=_messages_append_model_turn,
    _append_tool_results=_messages_append_tool_results,
    _tool_schemas=_messages_tool_schemas,
    _split_system=_lift_system,
    _client=_messages_client,
    _refusal=_messages_refusal,
    _final_text=_messages_final_text,
    _send=_messages_send,
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
