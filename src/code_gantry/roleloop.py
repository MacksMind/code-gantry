"""The structured tool loop the planner and the reviewer share.

A role builds a conversation and names a schema; the model reads, asks for
tools, is answered, and eventually returns a validated object. Every wire
spells those steps differently and the dialect owns the spellings; this owns
the loop, once, so the two roles cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from code_gantry.dialects import Dialect, TurnEnd
from code_gantry.openaiclient import TokenUsage, merge_usage
from code_gantry.retry import Backoff, with_provider_retry


@dataclass
class LoopResult:
    """How the loop ended: what came back, or why nothing did."""

    response: object | None = None
    parsed: object | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    # One reading per turn, in order; a sum cannot say where a cache stopped
    # matching.
    turns: list[dict] = field(default_factory=list)
    end: TurnEnd | None = None
    failure: Exception | None = None

    @property
    def refusal(self) -> str:
        return self._refusal

    _refusal: str = ""


def run_structured_loop(
    *,
    wire: Dialect,
    client,
    cfg,
    conversation: list,
    tools: list,
    schema,
    extra: dict,
    dispatch: Callable[[str, dict], str],
    max_turns: int,
    log=None,
    after_batch: Callable[[], None] | None = None,
) -> LoopResult:
    """Call until the model stops asking for tools, or the turn ceiling.

    `dispatch(name, args)` answers one tool call with text. `after_batch`
    runs once per tool round, after the results are in and before the next
    request goes out — the one point in a long decision where anything is
    known about what it is doing.
    """
    result = LoopResult()
    conversation = list(conversation)
    request = {**wire.structured(schema), **extra}
    retry_on = wire.transport_errors()

    for _ in range(max_turns + 1):
        try:
            response = with_provider_retry(
                lambda: wire.send(
                    client, cfg, wire.mark_latest(conversation), tools, request
                ),
                retry_on=retry_on,
                transient=Backoff(
                    budget_seconds=cfg.transport_retry_seconds,
                    max_delay_seconds=cfg.transport_retry_max_delay_seconds,
                ),
                spurious=Backoff(
                    budget_seconds=cfg.invalid_request_retry_seconds,
                    initial_seconds=cfg.invalid_request_initial_seconds,
                    factor=cfg.invalid_request_factor,
                ),
                log=log,
            )
        except Exception as e:  # noqa: BLE001 - any failure means "no answer"
            result.failure = e
            return result

        result.response = response
        reading = wire.usage(getattr(response, "usage", None))
        result.turns.append(
            {
                "prompt_tokens": reading.prompt_tokens,
                "cached_tokens": reading.cached_tokens,
                "cache_write_tokens": reading.cache_write_tokens,
            }
        )
        result.usage = merge_usage(result.usage, reading)

        requests = wire.tool_calls(response)
        if not requests:
            break
        wire.append_model_turn(conversation, response)
        # Where marks accumulate every result carries one; where they move,
        # the stored conversation stays unmarked and `mark_latest` marks the
        # outgoing copy on each send.
        wire.append_tool_results(
            conversation,
            [(req["id"], dispatch(req["name"], req["args"])) for req in requests],
            cache=not wire.marks_move,
        )
        if after_batch:
            after_batch()

    if result.response is not None:
        result.end = wire.turn_end(result.response)
        result._refusal = wire.refusal(result.response)
        result.parsed = wire.parsed(result.response)
    return result
