"""Classifying an API failure by whether waiting could help.

Written after a key with a short expiry died mid-session. Every planner call
returned 401, which surfaced as a generic blocked verdict with an empty tool
log — indistinguishable from a planner that had chosen not to use its tools. A
retry mechanism and a confident explanation were built on that before anyone
read the status code.

The three questions that matter: is it authentication, is another attempt
worth making, and what did the service actually say.
"""

import urllib.error

from code_gantry.apistatus import classify


class FakeSdkError(Exception):
    """Most SDKs put the code on `.status_code`."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class WrappedResponseError(Exception):
    """Some wrap a response object instead."""

    def __init__(self, status, message):
        super().__init__(message)
        self.response = type("R", (), {"status_code": status})()


class TestFindingTheStatus:
    def test_reads_status_code_from_an_sdk_error(self):
        assert classify(FakeSdkError(401, "API key is invalid.")).status == 401

    def test_reads_code_from_a_urllib_error(self):
        exc = urllib.error.HTTPError("http://x", 503, "busy", {}, None)
        assert classify(exc).status == 503

    def test_reads_it_through_a_wrapped_response(self):
        assert classify(WrappedResponseError(429, "slow down")).status == 429

    def test_falls_back_to_the_message(self):
        # What the Anthropic and OpenAI SDKs stringify to.
        exc = Exception("Error code: 401 - {'type': 'authentication_error'}")
        assert classify(exc).status == 401

    def test_a_connection_failure_has_no_status(self):
        assert classify(OSError("Connection refused")).status is None


class TestIsItAuthentication:
    def test_401_is(self):
        assert classify(FakeSdkError(401, "nope")).is_auth

    def test_403_is(self):
        assert classify(FakeSdkError(403, "forbidden")).is_auth

    def test_a_message_can_say_so_without_a_code(self):
        # A gateway in front of a model server may answer 200 with an error
        # body, or 403 where the provider would have said 401.
        assert classify(Exception("authentication_error: API key is invalid")).is_auth

    def test_a_rate_limit_is_not(self):
        assert not classify(FakeSdkError(429, "rate limited")).is_auth

    def test_a_timeout_is_not(self):
        assert not classify(OSError("timed out")).is_auth


class TestIsAnotherAttemptWorthMaking:
    def test_rate_limits_and_gateway_errors_are(self):
        for status in (429, 500, 502, 503, 504):
            assert classify(FakeSdkError(status, "later")).retry_worthwhile, status

    def test_credentials_and_bad_requests_are_not(self):
        # Retrying a 401 fourteen times is how an expired key becomes an hour.
        for status in (400, 401, 403, 404, 422):
            assert not classify(FakeSdkError(status, "no")).retry_worthwhile, status

    def test_a_connection_that_never_completed_is(self):
        # A refused socket or a DNS failure is the transient kind.
        assert classify(OSError("Connection refused")).retry_worthwhile


class TestWhatItTellsTheOperator:
    def test_an_auth_failure_says_it_is_not_transient(self):
        described = classify(FakeSdkError(401, "API key is invalid.")).describe()
        assert "credentials problem" in described
        assert "expired" in described
        assert "401" in described

    def test_an_ordinary_failure_is_quoted_plainly(self):
        described = classify(FakeSdkError(503, "upstream busy")).describe()
        assert "503" in described
        assert "upstream busy" in described
        assert "credentials" not in described

    def test_a_failure_with_no_status_still_says_something(self):
        assert "refused" in classify(OSError("Connection refused")).describe()
