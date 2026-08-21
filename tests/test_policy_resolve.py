"""A routing policy is resolved to a model once, at run start.

`openrouter/pareto-code` names a policy, not a model. It re-decides per
request — nine byte-identical calls interleaved across three scores had one
score return two different models inside ninety seconds — and a live attempt
opened on one model for a single turn and served nineteen from another.

Resolving once per run buys three things a per-request router cannot: a warm
cross-stage prefix, an outcome attributable to a model, and a *dialect*, since
which wire caches best depends on what the policy resolved to and
`dialect_for` refuses to guess.

It does not stop us following the frontier. The frontier moves over weeks and
a router moves over seconds; a fresh run re-resolves, which samples it far
more often than it changes.

Resolved on Responses deliberately: measured 2026-08-21, `min_coding_score`
separates cleanly there and has no effect at all through the Messages
endpoint, where both 0.3 and 0.9 returned the High tier.
"""

import pytest

from code_gantry.config import ExecutorConfig


class TestWhenItRuns:
    def test_a_policy_is_resolved(self):
        from code_gantry.gateway import resolve_policy

        cfg = ExecutorConfig(model="openrouter/pareto-code",
                             api_base="https://openrouter.ai/api/v1")
        out = resolve_policy(cfg, ask=lambda _c: "google/gemini-3.7-flash")
        assert out.model == "google/gemini-3.7-flash"

    def test_a_concrete_model_is_left_alone(self):
        """It must not pay for a call that could only confirm itself."""
        from code_gantry.gateway import resolve_policy

        def explode(_c):  # pragma: no cover - the point is it never runs
            raise AssertionError("a concrete model was probed")

        cfg = ExecutorConfig(model="openai/gpt-5.6-luna",
                             api_base="https://openrouter.ai/api/v1")
        assert resolve_policy(cfg, ask=explode).model == "openai/gpt-5.6-luna"

    def test_a_failed_probe_leaves_the_policy_in_place(self):
        """The router still answers; what is lost is the warm prefix and the
        dialect choice. Refusing to start would turn an optimisation into a new
        way for a run to fail."""
        from code_gantry.gateway import resolve_policy

        def fails(_c):
            raise RuntimeError("no answer")

        cfg = ExecutorConfig(model="openrouter/pareto-code",
                             api_base="https://openrouter.ai/api/v1")
        assert resolve_policy(cfg, ask=fails).model == "openrouter/pareto-code"

    def test_an_empty_answer_is_not_a_model(self):
        from code_gantry.gateway import resolve_policy

        cfg = ExecutorConfig(model="openrouter/pareto-code",
                             api_base="https://openrouter.ai/api/v1")
        assert resolve_policy(cfg, ask=lambda _c: "").model == "openrouter/pareto-code"

    def test_nothing_else_about_the_endpoint_changes(self):
        from code_gantry.gateway import resolve_policy

        cfg = ExecutorConfig(
            model="openrouter/pareto-code",
            api_base="https://openrouter.ai/api/v1",
            api_key_env="OPENROUTER_API_KEY",
            request_extra={"plugins": [{"id": "pareto-router", "min_coding_score": 0.3}]},
        )
        out = resolve_policy(cfg, ask=lambda _c: "google/gemini-3.7-flash")
        assert out.api_base == cfg.api_base
        assert out.api_key_env == cfg.api_key_env
        assert out.request_extra == cfg.request_extra


class TestWhatItUnlocks:
    def test_the_dialect_can_answer_afterwards(self):
        """Before: `dialect_for` refuses a policy and the caller falls back to
        whatever the client already speaks. After: the model decides."""
        from code_gantry.dialects import MESSAGES, dialect_for
        from code_gantry.gateway import resolve_policy

        cfg = ExecutorConfig(model="openrouter/pareto-code",
                             api_base="https://openrouter.ai/api/v1")
        with pytest.raises(ValueError):
            dialect_for(cfg.model)
        out = resolve_policy(cfg, ask=lambda _c: "google/gemini-3.7-flash")
        assert dialect_for(out.model) is MESSAGES
