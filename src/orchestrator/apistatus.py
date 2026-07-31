"""Classifying an API failure by whether waiting could help.

Every model call in this system is over HTTP, and every one of them can fail in
two very different ways. A key expired, a model name is wrong, an account has
no access: waiting changes nothing, and an unattended run should stop and say
so. A timeout, a rate limit, a gateway error: waiting is exactly the remedy,
and stopping the run would be an overreaction to a bad minute.

Collapsing the two costs real time. A key with a short expiry died mid-session
and every planner call returned 401. The failure surfaced as a generic blocked
verdict with an empty tool log, which read like a planner declining to use its
tools — and a retry mechanism and a confident explanation were built on it
before anyone checked the status code. Thirty seconds of looking would have
saved an hour of theory.

Deliberately not a taxonomy of every provider's error hierarchy. Three
questions matter — is it authentication, is it worth retrying, and what did the
service actually say — and they are answerable from a status code and a
message without importing anyone's SDK.
"""

from __future__ import annotations

from dataclasses import dataclass

# Waiting cannot fix these. The credentials, the model name, or the account is
# wrong, and every subsequent call will fail the same way.
_PERMANENT = {400, 401, 403, 404, 422}

# Worth another attempt: the service is up and saying "not now".
_TRANSIENT = {408, 409, 425, 429, 500, 502, 503, 504}


@dataclass
class ApiFailure:
    status: int | None
    detail: str

    @property
    def is_auth(self) -> bool:
        """Credentials rather than request shape.

        Checked by message as well as code because a gateway in front of a
        model server may return 200 with an error body, or 403 where the
        provider would have said 401.
        """
        if self.status in (401, 403):
            return True
        low = self.detail.lower()
        return "authentication" in low or "api key" in low or "unauthorized" in low

    @property
    def retry_worthwhile(self) -> bool:
        if self.status in _TRANSIENT:
            return True
        if self.status in _PERMANENT:
            return False
        # No status at all is a connection that never completed — a refused
        # socket, a DNS failure, a read timeout. Those are the transient kind.
        return self.status is None

    def describe(self) -> str:
        prefix = f"HTTP {self.status}: " if self.status else ""
        if self.is_auth:
            return (
                f"{prefix}{self.detail}\n\nThis is a credentials problem, not a "
                "transient one. Check the key named in the config, and that it "
                "has not expired."
            )
        return f"{prefix}{self.detail}"


def classify(exc: BaseException) -> ApiFailure:
    """Read a status code out of whatever the caller raised.

    Providers disagree on where it lives: `urllib` puts it on `.code`, `httpx`
    and most SDKs on `.status_code`, some wrap a `.response`. Rather than
    special-case each, look in all the usual places and fall back to the
    message, which in practice contains "Error code: 401" or similar.
    """
    status = None
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            status = value
            break
    if status is None:
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            status = value

    detail = str(exc).strip() or exc.__class__.__name__
    if status is None:
        # "Error code: 401 - {...}" is what several SDKs stringify to.
        for code in (*_PERMANENT, *_TRANSIENT):
            if f"code: {code}" in detail or f"code {code}" in detail:
                status = code
                break

    return ApiFailure(status=status, detail=detail)
