"""Waiting out a network outage, above the SDK rather than inside it.

Both provider SDKs back off exponentially and then clamp every wait at
`MAX_RETRY_DELAY = 8.0` seconds. That makes the schedule linear after four
retries, so spanning a fifteen-minute outage costs 116 retries at best and 154
at worst — and `max_retries` set that high would spend the same 154 attempts on
a genuine API error, turning a fast legible escalation into a twenty-minute
silent hang.

So the waiting happens here, where three things are possible that are not
possible inside the SDK. It can be bounded by wall clock rather than by a
count, which is what an operator actually means by "survive a fifteen-minute
outage". It can wait in minutes rather than eight-second increments, so the
same coverage costs ten attempts instead of a hundred and fifty. And it can
say what it is doing, which matters more than it sounds: a silent wait and a
hung process look identical from outside, and the failure this exists for was
first diagnosed by a human noticing the run had stopped.

Transport failures only. A refusal, a truncation, and a malformed answer are
decisions the model made; asking again does not change them, and retrying them
hides the answer behind the whole budget. The caller passes the exception types
that mean "the request never arrived", because those belong to the SDK it
imports rather than to this module.

Observed twice on one run: a laptop's Wi-Fi dropped, the planner and reviewer
both failed within two seconds of each other, and a fourteen-hour run ended
needing a human to notice and type `resume`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Backoff:
    """An exponential schedule bounded by total wall clock.

    `budget_seconds` is the cap, and it is the number an operator sets: how
    long an outage should be survivable. Zero disables retrying, rather than
    being an unsupported value someone finds out about during an outage.

    `max_delay_seconds` is optional and off by default. Capping each wait is
    what makes the SDK's own schedule need 154 retries; it is exposed only for
    an operator who would rather retry often than wait long.
    """

    budget_seconds: float
    initial_seconds: float = 1.0
    factor: float = 2.0
    max_delay_seconds: float | None = None


def delays(backoff: Backoff) -> list[float]:
    """Every wait in the schedule, in order.

    Computed up front rather than as it goes, because it is the part worth
    testing: the question "how long does this survive" should be answerable
    without a clock.

    The final wait is trimmed to what is left of the budget instead of
    overshooting it. A fifteen-minute cap that waits twenty is not a cap.
    """
    out: list[float] = []
    spent = 0.0
    wait = max(backoff.initial_seconds, 0.0)
    while spent < backoff.budget_seconds and wait > 0:
        if backoff.max_delay_seconds is not None:
            wait = min(wait, backoff.max_delay_seconds)
        out.append(min(wait, backoff.budget_seconds - spent))
        spent += out[-1]
        wait *= backoff.factor
    return out


def with_transport_retry(
    call,
    *,
    retry_on: tuple[type[BaseException], ...],
    backoff: Backoff,
    sleep=time.sleep,
    log=None,
):
    """Run `call`, waiting out failures of the kinds in `retry_on`.

    Returns whatever `call` returns. Re-raises the last transport failure when
    the budget is spent, and any other exception immediately — the original
    exception either way, so the caller's message says what actually happened
    rather than naming this wrapper.
    """
    schedule = delays(backoff)
    for attempt, wait in enumerate(schedule + [None]):
        try:
            return call()
        except retry_on as failure:
            if wait is None:
                if log:
                    log(
                        f"giving up after {attempt} retr"
                        f"{'y' if attempt == 1 else 'ies'} over "
                        f"{sum(schedule):.0f}s: {failure}"
                    )
                raise
            if log:
                log(
                    f"transport failure, retrying in {wait:.0f}s "
                    f"({attempt + 1} of {len(schedule)}): {failure}"
                )
            sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
