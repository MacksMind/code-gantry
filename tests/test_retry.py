"""Surviving an outage that outlasts the SDK's own retries.

Both SDKs back off exponentially and then clamp every wait at
`MAX_RETRY_DELAY = 8.0`, so past four retries the schedule is linear at eight
seconds apiece. Covering a fifteen-minute outage that way needs 116 retries at
best and 154 at worst, and a `max_retries` set that high would grind through
the same 154 attempts on a genuine API error — trading a fast, legible
escalation for a twenty-minute silent hang.

So the waiting belongs here instead, where it can be measured in minutes rather
than eight-second increments, log each attempt so a watching human sees it
working, and be bounded by wall clock rather than by a count. Observed twice:
a laptop's Wi-Fi dropped, the planner and the reviewer both failed inside two
seconds of each other, and a fourteen-hour run ended needing a human to notice
and type `resume`.

Only transport failures. A refusal, a truncation and a malformed answer are all
decisions the model made, and asking again does not change them.
"""

import pytest

from code_gantry.retry import (
    Backoff,
    delays,
    is_spurious_request_status,
    is_transient_status,
    with_transport_retry,
)


class Boom(Exception):
    """Stands in for the SDK's connection and timeout errors."""


class Other(Exception):
    """Anything the model itself decided."""


def recorder():
    slept: list[float] = []
    return slept, slept.append


def logged():
    lines: list[str] = []
    return lines, lines.append


class TestTheSchedule:
    def test_it_doubles(self):
        assert delays(Backoff(budget_seconds=100, initial_seconds=1)) [:4] == [
            1.0, 2.0, 4.0, 8.0
        ]

    def test_it_stops_at_the_budget(self):
        # 1+2+4+8+16 = 31, so a 20-second budget cannot afford the fifth.
        assert sum(delays(Backoff(budget_seconds=20, initial_seconds=1))) <= 20

    def test_the_last_wait_is_trimmed_rather_than_overshooting(self):
        # Sleeping past the budget would make a 15-minute cap mean 20 minutes.
        got = delays(Backoff(budget_seconds=10, initial_seconds=1))
        assert sum(got) == pytest.approx(10.0)

    def test_fifteen_minutes_costs_about_ten_attempts(self):
        # The property the cap was chosen for. The SDK needs 116-154 retries
        # for the same coverage because its per-wait cap makes it linear.
        got = delays(Backoff(budget_seconds=900, initial_seconds=1))
        assert sum(got) == pytest.approx(900.0)
        assert 9 <= len(got) <= 11, got

    def test_a_configurable_per_wait_cap_flattens_it(self):
        # For an operator who would rather retry often than wait long.
        got = delays(Backoff(budget_seconds=60, initial_seconds=1, max_delay_seconds=5))
        assert max(got) == 5.0
        assert sum(got) == pytest.approx(60.0)

    def test_a_zero_budget_yields_no_waits(self):
        assert delays(Backoff(budget_seconds=0)) == []


class TestRetrying:
    def test_a_call_that_works_is_not_retried(self):
        slept, sleep = recorder()
        assert with_transport_retry(
            lambda: "ok", retry_on=(Boom,), backoff=Backoff(60), sleep=sleep
        ) == "ok"
        assert slept == []

    def test_it_returns_the_first_success_after_a_failure(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise Boom("Connection error.")
            return "ok"

        slept, sleep = recorder()
        out = with_transport_retry(
            flaky, retry_on=(Boom,), backoff=Backoff(60, initial_seconds=1), sleep=sleep
        )
        assert out == "ok"
        assert slept == [1.0, 2.0], "it waited before each retry and then stopped"

    def test_a_non_transport_error_is_raised_immediately(self):
        # A refusal is a decision. Retrying it burns the budget to be told the
        # same thing, and hides the answer behind a fifteen-minute wait.
        slept, sleep = recorder()
        with pytest.raises(Other):
            with_transport_retry(
                _raising(Other), retry_on=(Boom,), backoff=Backoff(60), sleep=sleep
            )
        assert slept == []

    def test_the_last_failure_is_raised_when_the_budget_runs_out(self):
        # And it is the real exception, not a wrapper: the caller's message
        # says what actually went wrong.
        with pytest.raises(Boom, match="Connection error"):
            with_transport_retry(
                _raising(Boom, "Connection error."),
                retry_on=(Boom,),
                backoff=Backoff(10, initial_seconds=1),
                sleep=lambda _: None,
            )

    def test_it_gives_up_after_the_scheduled_number_of_attempts(self):
        slept, sleep = recorder()
        with pytest.raises(Boom):
            with_transport_retry(
                _raising(Boom),
                retry_on=(Boom,),
                backoff=Backoff(10, initial_seconds=1),
                sleep=sleep,
            )
        assert len(slept) == len(delays(Backoff(10, initial_seconds=1)))

    def test_no_budget_means_no_retry_at_all(self):
        # Setting the cap to zero turns the behaviour off, rather than being
        # an unsupported value someone has to discover.
        slept, sleep = recorder()
        with pytest.raises(Boom):
            with_transport_retry(
                _raising(Boom), retry_on=(Boom,), backoff=Backoff(0), sleep=sleep
            )
        assert slept == []


class TestItSaysWhatItIsDoing:
    """A silent wait and a hang are the same thing to whoever is watching."""

    def test_each_retry_is_logged_with_the_wait_and_the_cause(self):
        lines, log = logged()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise Boom("Connection error.")
            return "ok"

        with_transport_retry(
            flaky,
            retry_on=(Boom,),
            backoff=Backoff(60, initial_seconds=1),
            sleep=lambda _: None,
            log=log,
        )
        assert len(lines) == 1
        assert "Connection error." in lines[0]
        assert "1" in lines[0], "the wait is stated"

    def test_giving_up_is_logged_too(self):
        lines, log = logged()
        with pytest.raises(Boom):
            with_transport_retry(
                _raising(Boom),
                retry_on=(Boom,),
                backoff=Backoff(3, initial_seconds=1),
                sleep=lambda _: None,
                log=log,
            )
        assert any("giving up" in line for line in lines)

    def test_a_successful_call_logs_nothing(self):
        lines, log = logged()
        with_transport_retry(
            lambda: "ok", retry_on=(Boom,), backoff=Backoff(60), sleep=lambda _: None,
            log=log,
        )
        assert lines == []


class TestTheDefaultBudgetCoversAnHour:
    """Raised from fifteen minutes after a second provider incident.

    Fifteen minutes was chosen against a dropped Wi-Fi, which comes back when
    the laptop does. A provider incident is not that shape: two 529s arrived
    inside an hour of each other, and an outage that outlasts the budget costs
    a human noticing rather than a longer wait.

    The per-wait cap matters more than the total. Doubling to an hour with no
    cap makes the last sleep 26 minutes, so a provider that recovers a minute
    into it is not noticed for 25 more — the budget would be an hour and the
    *latency* would be half an hour. Capping each wait costs nothing, because
    a failed request is free, and buys twenty attempts instead of twelve.
    """

    def test_the_default_covers_an_hour(self):
        from code_gantry.config import PlannerConfig, ReviewerConfig

        for role in (PlannerConfig, ReviewerConfig):
            field = role.model_fields["transport_retry_seconds"]
            assert field.default == 3600.0, role.__name__

    def test_no_single_wait_exceeds_five_minutes(self):
        from code_gantry.config import PlannerConfig, ReviewerConfig

        for role in (PlannerConfig, ReviewerConfig):
            cap = role.model_fields["transport_retry_max_delay_seconds"].default
            assert cap == 300.0, role.__name__

    def test_the_schedule_that_produces(self):
        # The property, not the setting: an hour of coverage where recovery is
        # noticed within five minutes.
        got = delays(Backoff(budget_seconds=3600, initial_seconds=1, max_delay_seconds=300))
        assert sum(got) == pytest.approx(3600.0)
        assert max(got) == 300.0
        assert len(got) >= 18, "an hour should buy more than a dozen attempts"


class TestASpuriousRejectionOfAValidRequest:
    """A 400 that clears when the identical request is sent again.

    Normally a 400 is a statement about the request we sent and retrying it is
    the worst thing to do: it hides a legible error behind a wait. This is the
    exception, and it was established rather than assumed — three arrived in
    62 minutes, and the exact request, rebuilt from the run's own state and
    replayed, returned 200. Roughly 3-4% of planner calls, which is a run that
    stops every half hour and cannot be left alone.

    So the budget is small and separate: two waits, two minutes then three,
    five minutes from the first failure to escalation. A genuinely malformed
    request still reaches a human in five minutes with its own message, which
    is the property that makes this safe — the hour-long transient budget
    would not have been.
    """

    def test_the_schedule_is_two_then_three_minutes(self):
        got = delays(Backoff(budget_seconds=300, initial_seconds=120, factor=1.5))
        assert got == [120.0, 180.0]

    def test_that_is_three_attempts_and_five_minutes(self):
        got = delays(Backoff(budget_seconds=300, initial_seconds=120, factor=1.5))
        assert len(got) + 1 == 3, "two waits means three calls"
        assert sum(got) == 300.0

    def test_only_a_400_qualifies(self):
        assert is_spurious_request_status(400) is True
        for other in (401, 403, 404, 422, 429, 500, 503, 529, None):
            assert is_spurious_request_status(other) is False, other

    def test_a_rejection_that_clears_is_not_an_escalation(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:
                raise Boom("Invalid request data")
            return "ok"

        slept, sleep = recorder()
        out = with_transport_retry(
            flaky,
            retry_on=(Boom,),
            backoff=Backoff(budget_seconds=300, initial_seconds=120, factor=1.5),
            sleep=sleep,
        )
        assert out == "ok"
        assert slept == [120.0]

    def test_a_persistent_rejection_still_reaches_a_human_with_its_message(self):
        # The property that keeps this from being the mistake it resembles.
        slept, sleep = recorder()
        with pytest.raises(Boom, match="Invalid request data"):
            with_transport_retry(
                _raising(Boom, "Invalid request data"),
                retry_on=(Boom,),
                backoff=Backoff(budget_seconds=300, initial_seconds=120, factor=1.5),
                sleep=sleep,
            )
        assert sum(slept) == 300.0, "five minutes, then the real error"


class TestWhichFailuresAreWorthWaitingOut:
    """The status code decides, not the exception class.

    Extended after a 529 ended a 29-stage run: the original set was the
    exceptions meaning "the request never arrived", and an overloaded provider
    is a request that arrived and was told to come back later. The same
    outage, one layer up.

    Filtered by code rather than by class because the class is not portable —
    the installed SDKs disagree about 529, calling it `OverloadedError` on one
    and `InternalServerError` on the other, and the enumeration that looked
    obvious would have been half right.
    """

    def test_a_failure_that_never_arrived_is_transient(self):
        # No status at all: nothing answered, so there is nothing to read.
        assert is_transient_status(None) is True

    def test_an_overloaded_provider_is_transient(self):
        assert is_transient_status(529) is True

    def test_a_server_error_is_transient(self):
        assert all(is_transient_status(s) for s in (500, 502, 503))

    def test_a_rate_limit_is_transient(self):
        # Bounded by our own wall clock, which is what makes this safe here
        # and unsafe as an SDK `max_retries`.
        assert is_transient_status(429) is True

    def test_a_request_we_got_wrong_is_not(self):
        # These say the same thing in fifteen minutes.
        assert not any(is_transient_status(s) for s in (400, 401, 403, 404, 422))

    def test_it_matches_what_the_installed_sdks_retry(self):
        # The SDKs' own `_should_retry` is the authority on this, and it is on
        # disk. Pinning against it means a provider that changes its mind is a
        # test failure here rather than a run that stops at 3am.
        import httpx

        from anthropic import _base_client as anthropic_base
        from openai import _base_client as openai_base

        for base in (anthropic_base, openai_base):
            client = base.BaseClient
            for status in (400, 401, 403, 404, 422, 429, 500, 503, 529):
                response = httpx.Response(
                    status, request=httpx.Request("POST", "https://x/y")
                )
                assert client._should_retry(client, response) is is_transient_status(
                    status
                ), f"{base.__name__} disagrees about {status}"


def _raising(kind, message="boom"):
    def call():
        raise kind(message)

    return call


class TestADeterministic400IsNotWaitedOut:
    """A 400 that says what is wrong with the request is not spurious.

    `is_spurious_request_status` treats every 400 as worth one replay, on
    measured evidence: three `invalid_request_error` 400s stopped a run in 62
    minutes and the exact requests replayed 200. That rule is right about the
    provider having a bad minute and wrong about one case, because the category
    is drawn around the status code rather than around what the status *means*.

    Measured: a planner prompt that exceeded the model's context was rejected,
    retried twice, and escalated 300 seconds later than it could have. Every
    attempt was certain to fail — the prompt is the same prompt. A long
    unattended run is exactly where this fires, and five minutes of waiting is
    five minutes before the operator learns the thing they must act on.
    """

    def test_a_context_overflow_is_not_replayed(self):
        from code_gantry.retry import is_spurious_request_status

        assert not is_spurious_request_status(
            400,
            "prompt is too long: 1138774 tokens > 1000000 maximum",
        )

    def test_an_unexplained_400_is_still_replayed(self):
        # The case the replay exists for. Nothing in the message identifies a
        # property of the request, so the provider may simply have been wrong.
        from code_gantry.retry import is_spurious_request_status

        assert is_spurious_request_status(400, "invalid_request_error")
        assert is_spurious_request_status(400, "")
        assert is_spurious_request_status(400, None)

    def test_neighbouring_codes_are_unaffected(self):
        from code_gantry.retry import is_spurious_request_status

        for status in (401, 404, 422, 500, None):
            assert not is_spurious_request_status(status, "prompt is too long")


class TestTheTwoBudgetsAreNamedApart:
    """A 400 the provider answered is not a transport failure.

    One loop serves both budgets and its log line was a constant, so a
    malformed request announced itself in the words of a dropped connection.
    Measured on a live failure: a `prompt_cache_key` rejected for length was
    logged as "transport failure, retrying in 120s", and the first minutes of
    diagnosis went to the retry logic rather than to the request — which was
    working exactly as designed, replaying an unrecognised 400 on the narrow
    budget kept for the spurious ones.
    """

    def _log_of(self, status, message):
        from code_gantry.retry import Backoff, with_provider_retry

        seen: list[str] = []
        calls = {"n": 0}

        class Boom(Exception):
            status_code = status

            def __str__(self):
                return message

        def call():
            calls["n"] += 1
            raise Boom()

        try:
            with_provider_retry(
                call,
                retry_on=(Boom,),
                transient=Backoff(budget_seconds=1),
                spurious=Backoff(budget_seconds=1),
                sleep=lambda _s: None,
                log=seen.append,
            )
        except Boom:
            pass
        return "\n".join(seen)

    def test_a_500_is_a_transport_failure(self):
        assert "transport failure" in self._log_of(500, "upstream is unwell")

    def test_a_spurious_400_says_the_provider_rejected_it(self):
        text = self._log_of(400, "invalid_request_error: something odd")
        assert "provider rejected the request" in text
        assert "transport failure" not in text.split("giving up")[0]
