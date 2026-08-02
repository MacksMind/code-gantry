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

from orchestrator.retry import Backoff, delays, with_transport_retry


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


def _raising(kind, message="boom"):
    def call():
        raise kind(message)

    return call
