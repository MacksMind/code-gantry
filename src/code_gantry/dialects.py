"""Which wire a call goes out on, decided by the model rather than the role.

Each of the three roles used to hardcode an API surface, and none of them
chose it: the planner speaks Anthropic Messages because it talks to Anthropic,
and the executor and reviewer speak OpenAI Responses because of what they
replaced. That made *role* the axis along which the protocol varies, which is
an accident of arrival order rather than a decision — nothing about judging a
diff implies one wire and nothing about planning implies the other.

The axis is the model family, and where the family is routed. There are three
wires: Responses, Messages, and chat completions — the last for an Anthropic
model reached through OpenRouter, where the Messages wire drops the schema and
refuses tools but chat completions carries schema, tools, effort and a 1h cache
marker (measured 2026-09-11).

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


# The text-block discriminators any builder here writes. Named once so the
# translation and the builders cannot drift apart silently.
_TEXT_TYPES = ("input_text", "output_text", "text")


def describe_end(role: str, end: "TurnEnd") -> str:
    """Why a role has no answer, in one sentence, named the same way everywhere.

    One function rather than a sentence per role, because three roles writing
    their own produced three vocabularies for one event: the planner called it
    "no parsable verdict", the reviewer "no parsable verdict." with a full
    stop, and the executor counted it as an empty finish without saying so.
    The raw reason is always quoted — a label nobody has seen before is worth
    more to whoever reads it than the bucket it fell into.
    """
    what = {
        "refusal": "refused to answer",
        "max_tokens": "ran out of room, so its answer is truncated and cannot be trusted",
        "empty": "ended its turn without answering",
        "tool_use": "was still asking for tools",
        "unknown": "ended for a reason this code does not recognise",
    }.get(end.label, "returned nothing usable")
    reason = f" (stop reason {end.reason!r})" if end.reason else ""
    return f"the {role} {what}{reason}"


# The stop reasons that mean the model was interrupted rather than done. Named
# once, per wire's vocabulary, because two of them are spelled differently on
# the two endpoints and a role comparing string literals is how the planner
# came to check for exactly two of them.
_CUT_SHORT = ("max_tokens", "max_output_tokens", "length", "content_filter")
_REFUSED = ("refusal",)
_FINISHED = ("end_turn", "stop", "completed", "stop_sequence")
_ASKED = ("tool_use", "function_call", "tool_calls")


@dataclass(frozen=True)
class TurnEnd:
    """How one turn ended, as facts plus a label derived from them.

    The facts are what the wire said and what came back with it; `label` and
    `abnormal` are computed. That order matters. `empty` is not a provider's
    word — it is a reason plus no content — and a record holding only the
    label cannot answer a question nobody has thought of yet. Recording the
    derivation's *input* is what lets a later reader disagree with the
    derivation.

    An unrecognised reason keeps its own name and counts as abnormal. That is
    the half that makes a new failure legible: a planner call once blocked on
    a message covering three different bugs, and nothing in the artifact could
    narrow it because the code checked two known values and bucketed the rest.
    """

    reason: str
    has_content: bool
    has_tool_calls: bool

    @property
    def label(self) -> str:
        if self.has_tool_calls or self.reason in _ASKED:
            return "tool_use"
        if self.reason in _REFUSED:
            return "refusal"
        if self.reason in _CUT_SHORT:
            return "max_tokens"
        if self.reason in _FINISHED:
            return "finished" if self.has_content else "empty"
        return "unknown"

    @property
    def abnormal(self) -> bool:
        """Whether this ending needs explaining to somebody.

        `empty` is here because finishing and giving up render identically on
        the wire — a model that returns a terminal reason carrying nothing is,
        to a loop, a model that has finished. Only the absence of content
        separates them, and until the executor grew `empty_finishes` nothing
        looked.
        """
        return self.label in ("refusal", "max_tokens", "empty", "unknown")

    def as_record(self) -> dict:
        """What an artifact stores: the facts first, the label beside them."""
        return {
            "reason": self.reason,
            "has_content": self.has_content,
            "has_tool_calls": self.has_tool_calls,
            "label": self.label,
            "abnormal": self.abnormal,
        }


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
    # The request-level cache-key parameter, where the wire has one at all.
    # Empty on Messages: Anthropic keys its cache off the `cache_control`
    # markers, so there is no request field to name. Last on the dataclass,
    # like `cache_write_tokens` on `TokenUsage` and for the same reason.
    _cache_key_param: str = ""
    # Whether this wire needs the conversation translated out of the
    # Responses vocabulary it is built in. False where it *is* that
    # vocabulary, so the common path stays an identity.
    _translates_blocks: bool = False
    # Whether this wire's native effort parameter is one a gateway can
    # route. Responses spells it `reasoning` at the top level, which both
    # OpenAI and OpenRouter take; Messages spells it `output_config`,
    # which only Anthropic itself accepts.
    _effort_in_gateway_body: bool = False
    # The SDK exception types a failure on this wire arrives as. Last on the
    # dataclass, like `_cache_key_param` above and for the same reason: a new
    # field in the middle silently reassigns every positional caller.
    _transport_errors: Callable[[], tuple] | None = None
    # How a turn ended, read per wire. Last, for the reason above.
    _turn_end: Callable[[object], "TurnEnd"] | None = None
    # The validated model a `parse` call attaches, read per wire.
    _parsed: Callable[[object], object] | None = None
    # Whether cache marks on this wire *move* (only the latest counts, four
    # at most) rather than accumulate. Decides whether `mark_latest` marks.
    _moving_marks: bool = False

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

    @property
    def effort_in_gateway_body(self) -> bool:
        """See `_effort_in_gateway_body`; read by `request_extras`."""
        return self._effort_in_gateway_body

    def cache_key_param(self, key: str | None) -> dict:
        """The request-level cache key, spelled for this wire.

        Beside `cache_options` because it is the same kind of decision, and it
        is the one that was left behind: hardcoded as `prompt_cache_key` at two
        call sites, it reached `messages.create()` on the first run whose
        executor resolved to a Messages-wire model and was refused client-side
        before a request left the process. Sixteen stages had landed; four
        attempts died in seconds without the coding model being invoked.

        The empty-key guard lives here rather than at the callers. Both had
        their own `if key:` and a third would have had to remember one.
        """
        if not key or not self._cache_key_param:
            return {}
        return {self._cache_key_param: key}

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

    def normalise(self, conversation: list) -> list:
        """The conversation in this wire's own vocabulary.

        Every builder in this codebase writes Responses shapes — `input_text`
        blocks carrying `prompt_cache_breakpoint` — because that is what the
        executor spoke when they were written. Once the wire became a property
        of the model, those builders were left behind: `messages.create()`
        answers 400 `invalid_request_error` on an unknown block discriminator,
        and the coding model never sees the instruction.

        Translated here rather than at the seven sites that construct blocks,
        for the reason `request_extras` is one function: the eighth site will
        not remember. `split_system` had been translating the system block on
        its own, which is why the rejection named `messages[0]` and
        `messages[1]` and looked like a prompt problem.

        Never mutates. The caller holds this list across turns and mirrors it
        to disk as it grows, so a rewrite in place would corrupt the record as
        well as the request.
        """
        if not self._translates_blocks:
            return conversation
        out = []
        for message in conversation:
            content = _get(message, "content")
            if not isinstance(content, list):
                out.append(message)
                continue
            out.append({**message, "content": [self._translate(b) for b in content]})
        return out

    def _translate(self, block):
        """One block, if it is one of ours to translate.

        Only the text vocabulary moves. Tool calls and tool results are built
        per wire by `append_model_turn` and `append_tool_results`, and reaching
        into those would break what the dialect already gets right.
        """
        if not isinstance(block, dict) or block.get("type") not in _TEXT_TYPES:
            return block
        out = {k: v for k, v in block.items() if k not in CACHE_MARKER_FIELDS}
        out["type"] = self._text_type
        marked = [k for k in CACHE_MARKER_FIELDS if k in block]
        if marked:
            # A mark has to survive translation, or the translated wire
            # silently loses caching the other one has. The TTL travels where
            # the wire has one and is dropped where it does not.
            ttl = None
            marker = block[marked[0]]
            if isinstance(marker, dict):
                ttl = marker.get("ttl")
            out[self._cache_key] = self._cache_marker(ttl)
        return out

    @property
    def marks_move(self) -> bool:
        """Whether only the newest cache mark counts on this wire."""
        return self._moving_marks

    def parsed(self, response):
        """The validated model a structured call attached, or None."""
        return self._parsed(response) if self._parsed else None

    def mark_latest(self, conversation: list, ttl: str | None = None) -> list:
        """A copy with the newest block carrying a cache mark, where marks move.

        On Messages and chat completions only the latest marks count and four
        is the ceiling, so a tool loop marks the end of the newest message on
        each send and lets the mark move. On Responses marks accumulate and
        the results carry their own, so the conversation is returned as is.

        Never mutates: the caller holds this list across turns.
        """
        if not self._moving_marks or not conversation:
            return conversation
        last = conversation[-1]
        content = _get(last, "content")
        if not isinstance(content, list) or not content or not isinstance(content[-1], dict):
            return conversation
        blocks = list(content)
        blocks[-1] = {**blocks[-1], self._cache_key: self._cache_marker(ttl)}
        return conversation[:-1] + [{**last, "content": blocks}]

    def turn_end(self, response) -> TurnEnd:
        """How this turn ended, read from the wire's own answer.

        Beside `stopped`, which is now the same reading narrowed to a bool
        rather than a second, independent look at the response. Each wire's
        existing rule is preserved exactly — this adds what was being thrown
        away, and changes no loop's behaviour.
        """
        return self._turn_end(response)

    def transport_errors(self) -> tuple[type[BaseException], ...]:
        """The exception types a retry on this wire must be told to catch.

        Beside `client` because it is the same fact read from the other end:
        the client decides which SDK makes the call, so the SDK's classes are
        what a failure arrives as. `executorclient` asked
        `openaiclient.transport_errors()` for them instead — correct while
        every executor call was a Responses call, and silently wrong from the
        day a role became wire-polymorphic.

        An `anthropic.APIStatusError` is not an `openai.APIStatusError`, so
        `retry_on` matched nothing on the Messages wire and every failure
        propagated on its first raise. Measured: four attempts in four
        seconds, no backoff logged, the stage's whole rework allowance spent
        by requests that never reached a model — and the same hole covered
        429s and dropped sockets, so nothing on that wire had ever been
        retried.

        Raising rather than defaulting to an empty tuple: a dialect added
        without one would reintroduce exactly this, and silently, because no
        retrying looks identical to nothing having failed.
        """
        if self._transport_errors is None:
            raise ValueError(
                f"dialect {self.name!r} names no transport errors; a retry on "
                "this wire would catch nothing"
            )
        return self._transport_errors()

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
    """Anthropic's three orthogonal counts, as one `TokenUsage`.

    Built here rather than borrowed from the planner's `_extract_usage`, which
    returns a `PlannerUsage`: the two carry the same field *names* in a
    different *order*, and both are constructed positionally elsewhere. A type
    that merges correctly today and silently transposes on the next positional
    caller is not worth the shared line.

    `input_tokens` excludes both cache figures on this wire, so total input is
    the sum — read as OpenAI's shape it produced a 251% hit rate in a real
    report.
    """
    from code_gantry.openaiclient import TokenUsage

    if raw is None:
        return TokenUsage()
    read = _num(raw, "cache_read_input_tokens")
    written = _num(raw, "cache_creation_input_tokens")
    prompt = _num(raw, "input_tokens") + read + written
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=_num(raw, "output_tokens"),
        cached_tokens=read,
        cache_write_tokens=written,
        # One reading is its own peak; `merge_usage` maxes this while
        # everything else sums.
        peak_prompt_tokens=prompt,
        provider_cost_usd=getattr(raw, "cost", None),
        cache_write_1h_tokens=cache_write_1h(raw),
    )


def cache_write_1h(raw) -> int:
    """How much of this call's cache write went out under a one-hour marker.

    Anthropic reports the buckets under a nested `cache_creation` object and
    the *total* at the top level, so a reader that only takes the total cannot
    tell a 2x write from a 1.25x one — which is how `report.md` came to
    understate the planner by a quarter while the marker it was reporting on
    was the improvement being measured.

    Shared by both extractors rather than copied into each, unlike the usage
    types themselves: those stay apart because they transpose, and a reader has
    no field order to get wrong.

    Falls back to zero rather than to the total. An endpoint that reports no
    breakdown is one whose writes we cannot separate, and charging all of them
    at the higher rate would be a guess in the expensive direction — the total
    is still priced, at base, exactly as before.
    """
    detail = getattr(raw, "cache_creation", None)
    if detail is None and isinstance(raw, dict):
        detail = raw.get("cache_creation")
    if detail is None:
        return 0
    return _num(detail, "ephemeral_1h_input_tokens")


def _num(raw, name) -> int:
    value = getattr(raw, name, None)
    if value is None and isinstance(raw, dict):
        value = raw.get(name)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0



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


def _responses_text_present(response) -> bool:
    for item in getattr(response, "output", None) or []:
        for part in _get(item, "content") or []:
            if _get(part, "type") in _TEXT_TYPES and (_get(part, "text") or "").strip():
                return True
    return False


def _responses_turn_end(response) -> TurnEnd:
    """This wire's answer, which nothing used to read.

    `status` and `incomplete_details` are on every Responses object and
    `_responses_stopped` consulted neither — it inferred stopping from the
    absence of tool calls, so an interrupted turn and a finished one were the
    same observation.
    """
    status = getattr(response, "status", "") or ""
    detail = getattr(response, "incomplete_details", None)
    # The details explain an incomplete status and nothing else; a stub or a
    # provider that fills them on a completed response is not reporting a cut.
    reason = _get(detail, "reason") if (detail and status == "incomplete") else None
    return TurnEnd(
        reason=str(reason or status),
        has_content=_responses_text_present(response),
        has_tool_calls=bool(_responses_tool_calls(response)),
    )


def _responses_stopped(response) -> bool:
    # The rule this wire always used, over the one captured reading.
    return not _responses_turn_end(response).has_tool_calls


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


def _messages_turn_end(response) -> TurnEnd:
    text = False
    calls = False
    for block in getattr(response, "content", None) or []:
        kind = _get(block, "type")
        if kind == "text" and (_get(block, "text") or "").strip():
            text = True
        elif kind == "tool_use":
            calls = True
    return TurnEnd(
        reason=str(getattr(response, "stop_reason", "") or ""),
        has_content=text,
        has_tool_calls=calls,
    )


def _messages_stopped(response) -> bool:
    # The rule this wire always used, over the one captured reading.
    return _messages_turn_end(response).reason != "tool_use"


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
    content = _get(head, "content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    # Through the wire's own translation, which is idempotent: a block already
    # in this vocabulary is unchanged, one in the other's is rewritten, and a
    # cache mark survives either way.
    blocks = [
        wire._translate(b) if isinstance(b, dict) else {"type": "text", "text": _get(b, "text") or ""}
        for b in (content or [])
    ]
    return blocks, conv[1:]


# What a role sends when its config names no environment variable. Not empty:
# one SDK builds without a key and the other refuses to, so an absence is two
# different behaviours and a placeholder is one.
NO_KEY_REQUIRED = "no-key-required"


def _api_key(cfg) -> str:
    import os

    name = getattr(cfg, "api_key_env", None)
    if not name:
        # A declared choice, not an omission: the operator has said this
        # endpoint serves without one. A placeholder rather than nothing
        # because `OpenAI(...)` refuses to build without a key while
        # `anthropic.Anthropic(...)` does not, so omitting it works on one
        # wire and raises on the other. Whatever the endpoint makes of it is
        # the endpoint's answer to give.
        return NO_KEY_REQUIRED
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
    return f"{trimmed}/v1" if wire in ("responses", "chat") else trimmed


def _responses_transport_errors() -> tuple[type[BaseException], ...]:
    from openai import APIConnectionError, APIStatusError

    return (APIConnectionError, APIStatusError)


def _messages_transport_errors() -> tuple[type[BaseException], ...]:
    from anthropic import APIConnectionError, APIStatusError

    return (APIConnectionError, APIStatusError)


# Both wires' spellings, named once. A conversation is built in Responses
# vocabulary and translated at `send`, so either can be on a block by the time
# anything looks — and a stripper that knew only its own dialect's spelling
# would leave the other one in place.
CACHE_MARKER_FIELDS = ("cache_control", "prompt_cache_breakpoint")


def without_cache_markers(payload):
    """The same payload with every cache marker removed, however deep.

    Recursive rather than a pass over the places that add markers, because
    there are four of those in this module alone and the fifth is one wire
    away. The marks go on text blocks, on tool results, and on blocks nested
    inside a `function_call_output` — a caller that had to know which is a
    caller that will miss one.

    Only the markers. The conversation itself is untouched: what a stale-cache
    rejection refuses is a pointer to a cache, and dropping any of the content
    would answer a different problem.
    """
    if isinstance(payload, dict):
        return {
            key: without_cache_markers(value)
            for key, value in payload.items()
            if key not in CACHE_MARKER_FIELDS
        }
    if isinstance(payload, list):
        return [without_cache_markers(item) for item in payload]
    return payload


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
    # `parse` when the caller asked for a validated answer, `create` otherwise;
    # the structured key is the dialect's own, so its presence is the signal.
    method = client.responses.parse if wire._structured_key in extra else client.responses.create
    kwargs = {
        "model": cfg.model,
        # A plain list on the wire. `conversation` is a `Transcript` — a list
        # subclass that mirrors itself to disk — and what the SDK does with a
        # subclass is its business rather than a fact to rely on.
        "input": list(conversation),
        **extra,
    }
    if tools:
        kwargs["tools"] = tools
    return method(**kwargs)


def _messages_send(wire, client, cfg, conversation, tools, extra):
    # Translate before splitting: `split_system` only ever handled the
    # system block, and everything behind it went out in the other wire's
    # vocabulary and was refused.
    system, messages = wire.split_system(wire.normalise(conversation))
    kwargs = {"model": cfg.model, "messages": messages, **extra}
    if tools:
        kwargs["tools"] = tools
    if system:
        kwargs["system"] = system
    # Mandatory on this wire, unlike the other. Generous rather than tight:
    # thinking counts against it along with the answer, so a small budget
    # truncates the response rather than the reasoning.
    kwargs["max_tokens"] = getattr(cfg, "max_tokens", None) or 32_000
    method = client.messages.parse if wire._structured_key in extra else client.messages.create
    return method(**kwargs)


# --- chat completions -------------------------------------------------------

def _chat_message(response):
    choices = getattr(response, "choices", None) or []
    return _get(choices[0], "message") if choices else None


def _chat_text(message) -> str:
    content = _get(message, "content") if message is not None else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _get(part, "text") or "" for part in content if _get(part, "type") in _TEXT_TYPES
        )
    return ""


def _chat_tool_calls(response) -> list[dict]:
    import json as _json

    out = []
    for call in _get(_chat_message(response), "tool_calls") or []:
        function = _get(call, "function")
        raw = _get(function, "arguments") or "{}"
        try:
            args = _json.loads(raw)
        except (TypeError, ValueError):
            args = {}
        out.append({"id": _get(call, "id") or "", "name": _get(function, "name") or "",
                    "args": args if isinstance(args, dict) else {}})
    return out


def _chat_turn_end(response) -> TurnEnd:
    choices = getattr(response, "choices", None) or []
    reason = _get(choices[0], "finish_reason") if choices else ""
    # A refusal arrives as `content_filter` with the text in `refusal`; the
    # message, not the finish reason, is what says which.
    if _get(_chat_message(response), "refusal"):
        reason = "refusal"
    return TurnEnd(
        reason=str(reason or ""),
        has_content=bool(_chat_text(_chat_message(response)).strip()),
        has_tool_calls=bool(_chat_tool_calls(response)),
    )


def _chat_stopped(response) -> bool:
    return not _chat_turn_end(response).has_tool_calls


def _chat_append_model_turn(conversation: list, response) -> None:
    message = _chat_message(response)
    if message is None:
        return
    turn: dict = {"role": "assistant", "content": _chat_text(message) or None}
    calls = []
    for call in _get(message, "tool_calls") or []:
        function = _get(call, "function")
        calls.append({
            "id": _get(call, "id") or "",
            "type": "function",
            "function": {"name": _get(function, "name") or "",
                         "arguments": _get(function, "arguments") or "{}"},
        })
    if calls:
        turn["tool_calls"] = calls
    # OpenRouter hands an Anthropic model's thinking back as `reasoning_details`
    # and needs it echoed to keep the reasoning across turns.
    details = _get(message, "reasoning_details")
    if details:
        turn["reasoning_details"] = details
    conversation.append(turn)


def _chat_append_tool_results(wire, conversation, results, cache, ttl):
    # One `tool` message per call, content as parts so the last can carry the
    # mark; marks move on this wire, so only the newest is marked.
    items = list(results)
    for i, (call_id, text) in enumerate(items):
        block = {"type": "text", "text": text}
        if cache and i == len(items) - 1:
            block[wire._cache_key] = wire._cache_marker(ttl)
        conversation.append({"role": "tool", "tool_call_id": call_id, "content": [block]})


def _chat_tool_schemas(specs) -> list[dict]:
    from code_gantry.plannertools import as_strict_tool

    out = []
    for spec in specs:
        flat = as_strict_tool(spec)
        out.append({"type": "function", "function": {
            k: flat[k] for k in ("name", "description", "strict", "parameters")
        }})
    return out


def _chat_usage(raw):
    from code_gantry.openaiclient import TokenUsage

    if raw is None:
        return TokenUsage()
    details = getattr(raw, "prompt_tokens_details", None)
    if details is None and isinstance(raw, dict):
        details = raw.get("prompt_tokens_details")
    prompt = _num(raw, "prompt_tokens")
    return TokenUsage(
        prompt_tokens=prompt,
        completion_tokens=_num(raw, "completion_tokens"),
        cached_tokens=_num(details, "cached_tokens") if details else 0,
        cache_write_tokens=_num(details, "cache_write_tokens") if details else 0,
        peak_prompt_tokens=prompt,
        provider_cost_usd=_get(raw, "cost"),
        # No TTL breakdown on this wire; zero, not the total.
        cache_write_1h_tokens=0,
    )


def _chat_refusal(response) -> str:
    return _get(_chat_message(response), "refusal") or ""


def _chat_final_text(response) -> str:
    return _chat_text(_chat_message(response))


def _chat_parsed(response):
    return _get(_chat_message(response), "parsed")


def _chat_client(cfg):
    from openai import OpenAI

    kwargs = {"api_key": _api_key(cfg), "max_retries": 0}
    base = cfg.resolve_api_base() if hasattr(cfg, "resolve_api_base") else None
    base = _gateway_base(base, "chat")
    if base:
        kwargs["base_url"] = base
    timeout = getattr(cfg, "request_timeout_seconds", None)
    if timeout:
        kwargs["timeout"] = timeout
    return OpenAI(**kwargs)


def _chat_send(wire, client, cfg, conversation, tools, extra):
    kwargs = {"model": cfg.model, "messages": wire.normalise(conversation), **extra}
    if tools:
        kwargs["tools"] = tools
    ceiling = getattr(cfg, "max_tokens", None)
    if ceiling:
        kwargs["max_tokens"] = ceiling
    if wire._structured_key not in extra:
        return client.chat.completions.create(**kwargs)
    try:
        return client.chat.completions.parse(**kwargs)
    except Exception as e:  # noqa: BLE001 - narrowed below
        # The SDK's `parse` raises on a refusal or a truncation instead of
        # returning the completion that says so. Re-read the same answer raw,
        # so the loop reports the refusal rather than a failed call.
        if type(e).__name__ not in ("ContentFilterFinishReasonError", "LengthFinishReasonError"):
            raise
        from openai.lib._parsing import type_to_response_format_param

        kwargs["response_format"] = type_to_response_format_param(kwargs["response_format"])
        return client.chat.completions.create(**kwargs)


def _messages_parsed(response):
    return getattr(response, "parsed_output", None)


def _responses_parsed(response):
    return getattr(response, "output_parsed", None)


CHAT = Dialect(
    name="chat",
    _structured_key="response_format",
    _effort_key="reasoning_effort",
    _effort_shape=lambda level: level,
    _text_type="text",
    _cache_key="cache_control",
    _cache_marker=lambda ttl: {"type": "ephemeral", **({"ttl": ttl} if ttl else {})},
    _request_cache_options={},
    _translates_blocks=True,
    # Through the gateway effort rides in the body as `reasoning`; first party
    # takes `reasoning_effort` at the top level.
    _effort_in_gateway_body=True,
    _transport_errors=_responses_transport_errors,
    _turn_end=_chat_turn_end,
    _parsed=_chat_parsed,
    _moving_marks=True,
    _usage=_chat_usage,
    _tool_calls=_chat_tool_calls,
    _stopped=_chat_stopped,
    _append_model_turn=_chat_append_model_turn,
    _append_tool_results=_chat_append_tool_results,
    _tool_schemas=_chat_tool_schemas,
    _split_system=_no_split,
    _client=_chat_client,
    _refusal=_chat_refusal,
    _final_text=_chat_final_text,
    _send=_chat_send,
)


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
    _cache_key_param="prompt_cache_key",
    _transport_errors=_responses_transport_errors,
    _turn_end=_responses_turn_end,
    _parsed=_responses_parsed,
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
    _transport_errors=_messages_transport_errors,
    _turn_end=_messages_turn_end,
    _parsed=_messages_parsed,
    _moving_marks=True,
    _cache_marker=lambda ttl: {"type": "ephemeral", **({"ttl": ttl} if ttl else {})},
    _request_cache_options={},
    _translates_blocks=True,
    _effort_in_gateway_body=True,
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
_ANTHROPIC = ("anthropic", "claude")
_FAMILIES: tuple[tuple[tuple[str, ...], Dialect], ...] = (
    (_ANTHROPIC, MESSAGES),
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

_BY_NAME = {"responses": RESPONSES, "messages": MESSAGES, "chat": CHAT}


def dialect_for(model: str, override: str | None = None, api_base=None) -> Dialect:
    """The wire this model should be called on, given where it is reached.

    `override` is the operator's escape hatch: the map is provider knowledge,
    but a deployment may know about a model this file has never heard of.

    `api_base` is the route. An Anthropic model through OpenRouter goes on
    chat completions: on Messages the gateway drops `output_format` in
    silence and answers 404 to any `tools` block, while chat completions
    carries schema, strict tools, effort and a 1h `cache_control`.
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
            if dialect is MESSAGES and needles is _ANTHROPIC and _via_openrouter(api_base):
                return CHAT
            return dialect
    return _DEFAULT


def _via_openrouter(api_base) -> bool:
    from code_gantry.gateway import is_openrouter

    return is_openrouter(api_base)


def wire_for(cfg, default: Dialect) -> Dialect:
    """The wire a role's configured endpoint wants, or `default` for a policy.

    A routing policy resolves per run and cannot be classified, and a run
    must not fail here over it. The route is read without raising: a config
    naming an unset `api_base_env` is preflight's to report, not this
    function's to crash on.
    """
    try:
        base = cfg.resolve_api_base() if hasattr(cfg, "resolve_api_base") else getattr(cfg, "api_base", None)
    except KeyError:
        base = None
    try:
        return dialect_for(getattr(cfg, "model", ""), getattr(cfg, "wire", None), api_base=base)
    except ValueError:
        return default


def request_extra(cfg) -> dict:
    """An operator's own request fields, as one `extra_body`."""
    extra = getattr(cfg, "request_extra", None) or {}
    return {"extra_body": dict(extra)} if extra else {}


def request_extras(cfg, session_id: str = "", cache_key: str | None = None) -> dict:
    """Every top-level keyword a role's call carries beyond the basics.

    One function for every role, because it is one decision: the two outages
    it exists to prevent were both a keyword an endpoint does not take, and
    each was pinned afterwards by a test that rebuilt this dict by hand. The
    seam test reads what production reads.
    """
    from code_gantry.gateway import gateway_body, gateway_effort_body

    wire = wire_for(cfg, _DEFAULT)
    level = getattr(cfg, "reasoning_effort", None) or getattr(cfg, "effort", None)
    # Effort is spelled for the route where the wire's native spelling is one
    # a gateway cannot carry — see `gateway_effort_body`.
    via_body = gateway_effort_body(cfg, level) if wire.effort_in_gateway_body else {}
    declared = dict(request_extra(cfg).get("extra_body") or {})
    return {
        **wire.cache_options(),
        **wire.cache_key_param(cache_key),
        **({} if via_body else wire.effort(level)),
        # An operator's own declared fields win outright, so ours go under.
        **gateway_body(cfg, session_id, {**via_body, **declared}),
    }
