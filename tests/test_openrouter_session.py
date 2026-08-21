"""`session_id` goes on every OpenRouter call, and is scoped to the run.

It is not a Pareto control. OpenRouter uses it as the sticky-routing key
generally: "sticky routing keeps sessions working across turns by sending a
session back to the same provider that holds the warm cache." One model can be
served by several upstream providers with separate caches, so a *pinned* model
benefits too — without a key, consecutive calls can land on different providers
and the prefix is cold through no fault of the model. Reusing the resolved
model is an extra effect on router models, not the whole of it.

Triggered on the endpoint rather than declared. Which API accepts this is a
property of the provider, the same category as `prompt_cache_options` being
OpenAI's and already hardcoded — and a first-party endpoint rejects an argument
it does not recognise, so the trigger has to be right rather than optional.

Scoped to the **run**, not the project. The first cut keyed it on
`project_branch`, which is stable across runs — so two runs inside the idle
window could inherit each other's routing, and a fresh run could be held on the
model the last one resolved. A fresh run should re-ask; that is the whole
argument for letting the executor follow the frontier.
"""

import pytest

from code_gantry.config import ExecutorConfig


class TestTheTrigger:
    @pytest.mark.parametrize(
        "base",
        [
            "https://openrouter.ai/api/v1",
            "https://openrouter.ai/api/v1/",
            "https://OpenRouter.ai/api/v1",
        ],
    )
    def test_openrouter_endpoints_are_recognised(self, base):
        from code_gantry.executorclient import is_openrouter

        assert is_openrouter(base)

    @pytest.mark.parametrize(
        "base",
        [
            None,
            "",
            "https://api.openai.com/v1",
            "http://localhost:8080/v1",
            "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1",
            # The name appearing somewhere in a path is not the host.
            "https://example.com/openrouter.ai/v1",
        ],
    )
    def test_everything_else_is_not(self, base):
        from code_gantry.executorclient import is_openrouter

        assert not is_openrouter(base)


class TestItIsSentWithoutBeingAskedFor:
    def test_an_openrouter_endpoint_gets_it(self):
        from code_gantry.executorclient import session_param

        cfg = ExecutorConfig(model="m", api_base="https://openrouter.ai/api/v1")
        assert session_param(cfg, "run-1") == {"extra_body": {"session_id": "run-1"}}

    def test_a_first_party_endpoint_does_not(self):
        """It would 400 on an argument it does not recognise."""
        from code_gantry.executorclient import session_param

        assert session_param(ExecutorConfig(model="m"), "run-1") == {}

    def test_no_identity_means_nothing_is_sent(self):
        from code_gantry.executorclient import session_param

        cfg = ExecutorConfig(model="m", api_base="https://openrouter.ai/api/v1")
        assert session_param(cfg, "") == {}


class TestTheIdentityIsPerRun:
    def test_two_runs_do_not_share_a_session(self):
        from code_gantry.cachekey import cache_key

        assert cache_key("session", "run-a") != cache_key("session", "run-b")

    def test_it_fits_the_provider_limit(self):
        from code_gantry.cachekey import MAX_CACHE_KEY, cache_key

        long_id = "20260821-163451-" + "upgrade/rails-5" * 8
        assert len(cache_key("session", long_id)) <= MAX_CACHE_KEY

    def test_the_executor_derives_it_from_the_run_it_was_given(self):
        from code_gantry.cachekey import cache_key
        from code_gantry.executor import Executor

        ex = Executor.__new__(Executor)
        ex.run_id = "20260821-163451-upgrade-rails-5"
        assert ex.session_identity() == cache_key("session", ex.run_id)

    def test_no_run_id_yields_no_session(self):
        """Rather than a constant every run would share."""
        from code_gantry.executor import Executor

        ex = Executor.__new__(Executor)
        ex.run_id = ""
        assert ex.session_identity() == ""


class TestOperatorsCannotSetIt:
    """Derived from the run, like `prompt_cache_key` beside it. A constant in
    config would put two runs, or two projects, on one session."""

    def test_session_id_is_reserved(self):
        from code_gantry.config import RESERVED_REQUEST_KEYS

        assert "session_id" in RESERVED_REQUEST_KEYS

    def test_config_refuses_it(self):
        from code_gantry.config import _request_extra_problems

        cfg = ExecutorConfig(model="m", request_extra={"session_id": "mine"})
        problems = _request_extra_problems("executor", cfg)
        assert problems and "session_id" in problems[0]


class TestTheKwargsAreAcceptable:
    """The seam neither earlier test drove, and it stopped a run.

    `session_param` returned `{"session_id": ...}` and the loop splatted it
    into `responses.create(**extra)`. That is not a parameter of the Responses
    API, so every call raised `TypeError: got an unexpected keyword argument
    'session_id'` — four attempts in seconds, before the executor read a file.
    The unit test passed because it asserted the dict; the probe passed because
    it happened to pass the value as `extra_body`. Neither drove what the loop
    actually builds against what the SDK actually accepts.

    `session_id` is a *body* field, like the operator's `plugins`, so both have
    to end up inside one `extra_body` — two `extra_body` keys in one splat and
    the later wins silently.
    """

    def test_the_session_goes_inside_the_body(self):
        from code_gantry.executorclient import session_param

        cfg = ExecutorConfig(model="m", api_base="https://openrouter.ai/api/v1")
        assert session_param(cfg, "run-1") == {"extra_body": {"session_id": "run-1"}}

    def test_it_merges_with_an_operator_declared_body(self):
        """Rather than one replacing the other."""
        from code_gantry.executorclient import merged_body

        cfg = ExecutorConfig(
            model="m",
            api_base="https://openrouter.ai/api/v1",
            request_extra={"plugins": [{"id": "pareto-router"}]},
        )
        body = merged_body(cfg, "run-1")["extra_body"]
        assert body["session_id"] == "run-1"
        assert body["plugins"] == [{"id": "pareto-router"}]

    def test_every_top_level_kwarg_is_one_the_sdk_accepts(self):
        """Checked against the installed SDK's own signature.

        Provider shapes come from the installed package, not from recall — and
        the failure this pins was a keyword the package does not declare.
        """
        import inspect

        from openai import OpenAI
        from code_gantry.executorclient import merged_body, _reasoning_param

        cfg = ExecutorConfig(
            model="m",
            api_base="https://openrouter.ai/api/v1",
            reasoning_effort="max",
            request_extra={"plugins": [{"id": "pareto-router"}]},
        )
        built = {
            "prompt_cache_options": {"mode": "explicit"},
            **_reasoning_param(cfg),
            **merged_body(cfg, "run-1"),
            "prompt_cache_key": "k",
        }
        accepted = set(
            inspect.signature(OpenAI(api_key="x").responses.create).parameters
        )
        unknown = sorted(set(built) - accepted)
        assert not unknown, f"not parameters of responses.create: {unknown}"
