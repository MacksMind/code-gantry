"""What the planner may spend on one answer, and what that silently depends on.

A derivation died mid-JSON at `max_tokens: 32_000` after 591 seconds and 27
reads, discarding the whole call. Same failure the 16,000 → 32,000 raise was
made for, reached again because a batch of five stages is five instructions plus
reasoning at `xhigh`.

Measured against the live API rather than recalled:

    max_tokens= 64,000  accepted
    max_tokens=128,000  accepted
    max_tokens=200,000  rejected — "200000 > 128000, which is the maximum"

**And there is a coupling that is invisible at the call site.** The installed
SDK refuses a *non-streaming* request whose budget implies a long generation:

    expected_time = 3600 * max_tokens / 128_000
    if expected_time > 600: raise "Streaming is required…"

That caps non-streaming at 21,333 tokens — well under the 32,000 already in
use. It does not fire only because the check runs when no explicit timeout is
given, and the planner always passes `request_timeout_seconds`. So the output
budget depends on a timeout being set, in a different config field, enforced in
a third-party base client. Remove the timeout and every planner call raises
before it is sent.

That dependency is why this file exists: it was found by probing the API without
a timeout, getting "Streaming is required", and nearly concluding the ceiling
was 21,333 — a wrong answer about our own configuration produced by measuring a
call we do not make.
"""

import pytest


class TestTheBudgetIsBigEnoughForABatch:
    def test_the_default_leaves_room_beyond_one_stage(self):
        from orchestrator.config import PlannerConfig

        cfg = PlannerConfig(model="claude-opus-5")
        assert cfg.max_tokens >= 64_000

    def test_it_stays_within_what_the_model_accepts(self):
        # 128,000 measured as the ceiling for claude-opus-5; above it the API
        # rejects the request outright rather than truncating.
        from orchestrator.config import PlannerConfig

        assert PlannerConfig(model="claude-opus-5").max_tokens <= 128_000


class TestTheTimeoutIsWhatMakesTheBudgetLegal:
    def test_a_timeout_is_always_configured(self):
        # Without it the SDK's non-streaming guard caps max_tokens at 21,333
        # and every call raises before it is sent.
        from orchestrator.config import PlannerConfig

        assert PlannerConfig(model="claude-opus-5").request_timeout_seconds > 0

    def test_the_sdk_threshold_is_what_this_assumes(self):
        """Pinned against the installed SDK, so a change there fails here.

        If Anthropic alters the formula or the constant, this test fails and
        someone reads it, rather than a run dying at 3am on a guard nobody
        knew was load-bearing.
        """
        from anthropic._base_client import BaseClient

        import inspect

        src = inspect.getsource(BaseClient._calculate_nonstreaming_timeout)
        assert "128_000" in src or "128000" in src
        assert "60 * 10" in src or "600" in src

        from orchestrator.config import PlannerConfig

        cfg = PlannerConfig(model="claude-opus-5")
        non_streaming_cap = 600 * 128_000 / 3600
        assert cfg.max_tokens > non_streaming_cap, (
            "if the budget ever drops below the SDK's non-streaming cap this "
            "coupling stops mattering and the comment should go"
        )
