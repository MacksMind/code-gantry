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

"Never arrived" turned out to be too narrow by one case. A third run ended on
`Error code: 529 - overloaded_error` after 29 landed stages, with the work
intact and nothing wrong with it: the request arrived, and the provider said
come back later. That is the same outage one layer up, and the only reason it
escalated is that the original set was drawn around the transport rather than
around what the failure means. `is_transient_status` widens it to the codes the
SDKs themselves retry — which is also why the filter is a status code and not a
list of classes. The installed SDKs disagree about 529: Anthropic raises
`OverloadedError`, OpenAI raises `InternalServerError`. Enumerating classes
reads as the obvious implementation and would have been right on one provider.
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


def is_transient_status(status: int | None) -> bool:
    """Whether an HTTP status is worth waiting out.

    `None` means nothing answered — a dropped socket, a DNS failure, a read
    timeout — and there is no status to read, so it is transient by definition.

    Otherwise this is the SDKs' own `_should_retry` rule, deliberately: 429 and
    every 5xx. A 400 or a 401 is a statement about the request we sent and will
    say the same thing in fifteen minutes, so waiting hides a legible error
    behind the budget. 429 is safe to include *here* and would not be safe as
    an SDK `max_retries`, because the wait is bounded by our wall clock rather
    than by a `retry-after` header that can name hours.
    """
    if status is None:
        return True
    return status == 429 or status >= 500


def is_spurious_request_status(status: int | None, message: str | None = None) -> bool:
    """A 400 the provider returns for a request that is not malformed.

    Everything about this is uncomfortable, so the evidence matters. Three
    `invalid_request_error` 400s stopped one run in 62 minutes; the exact
    request was rebuilt from the run's own state, replayed unchanged, and
    returned 200. At roughly 3-4% of planner calls that is a stop every half
    hour, which defeats unattended operation entirely.

    Only 400. The neighbouring codes are genuine statements about the request
    — 401 is a wrong key, 404 a wrong path, 422 an unprocessable body — and
    every one of them says the same thing five minutes later.

    And not every 400, which is the correction. The rule above is drawn around
    the *status code*, and one 400 states a property of the request as plainly
    as a 422 does: a prompt over the model's context window is the same prompt
    on the next attempt. Measured — a planner call rejected at 1,138,774 tokens
    against a 1,000,000 ceiling was retried twice and escalated 300 seconds
    later than it needed to, on two attempts that could not have succeeded.
    That is the failure a long unattended run reaches first, and waiting on it
    only delays the message the operator has to act on.

    Matched on the message rather than on a provider error code because the
    codes disagree — Anthropic sends `invalid_request_error` for this and for
    the genuinely spurious ones alike — while the sentence is stable and says
    what the classification needs to know. Narrow deliberately: anything not
    recognised keeps its replay, so a new deterministic 400 costs five minutes
    rather than a wrongly-suppressed retry.
    """
    if status != 400:
        return False
    text = (message or "").lower()
    return not any(
        phrase in text
        for phrase in ("prompt is too long", "context length", "too many tokens")
    )


def with_provider_retry(
    call,
    *,
    retry_on: tuple[type[BaseException], ...],
    transient: Backoff,
    spurious: Backoff,
    status_of=lambda e: getattr(e, "status_code", None),
    sleep=time.sleep,
    log=None,
):
    """Two budgets, because the two failures deserve different patience.

    An outage is waited out for an hour; a spurious rejection for five
    minutes. Nested rather than merged so each keeps its own schedule, and in
    this order so that a retried 400 gets a fresh transient budget underneath
    it — a provider having a bad enough minute to reject a valid request is
    exactly one that may also be overloaded on the next attempt.

    The exception *types* still come from the caller, because they belong to
    the SDK it imports. Only the status is read here, and both SDKs put it in
    the same place.
    """

    def wait_out_outages():
        return with_transport_retry(
            call,
            retry_on=retry_on,
            retry_if=lambda e: is_transient_status(status_of(e)),
            backoff=transient,
            sleep=sleep,
            log=log,
        )

    return with_transport_retry(
        wait_out_outages,
        retry_on=retry_on,
        retry_if=lambda e: is_spurious_request_status(status_of(e), str(e)),
        backoff=spurious,
        sleep=sleep,
        log=log,
    )


def with_transport_retry(
    call,
    *,
    retry_on: tuple[type[BaseException], ...],
    backoff: Backoff,
    sleep=time.sleep,
    log=None,
    retry_if=None,
):
    """Run `call`, waiting out failures of the kinds in `retry_on`.

    Returns whatever `call` returns. Re-raises the last transport failure when
    the budget is spent, and any other exception immediately — the original
    exception either way, so the caller's message says what actually happened
    rather than naming this wrapper.

    `retry_if` narrows `retry_on` for types that cover both the transient and
    the permanent: the SDKs' status errors share one base class, so the type is
    not enough to tell an overloaded provider from a malformed request. A
    failure it rejects is raised immediately, exactly like an unlisted type.
    """
    schedule = delays(backoff)
    for attempt, wait in enumerate(schedule + [None]):
        try:
            return call()
        except retry_on as failure:
            if retry_if is not None and not retry_if(failure):
                raise
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
