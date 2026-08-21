"""Ask the router to stay on one model, instead of inferring that it won't.

Measured before this existed: a live attempt served turn 1 from
`google/gemini-3.7-flash` and turns 2-20 from `x-ai/grok-4.6`. The switch is at
turn 1->2 exactly, and OpenRouter's Pareto documentation says why — without an
explicit `session_id` the router falls back to fingerprinting the first system
and user message, and that fallback "activates only after the provider reports
cache usage", which has not happened on the opening turn.

So the per-request re-ranking reported from this project was partly a
parameter we never sent. Nine byte-identical probe calls that also returned
different models were all trivial prompts under the 1024-token cache minimum,
so stickiness never engaged there either.

Run-scoped rather than stage-scoped, and the arithmetic decides it: sessions
expire after five minutes idle, and our inter-stage gaps are 500-900 seconds
because the full suite alone takes 274-353. A stage-scoped id would expire
between every pair of stages by construction; a run-scoped one is identical
inside an attempt and at least asks to hold across them.

Opt-in, because `session_id` is this gateway's field and a first-party
endpoint 400s on an argument it does not recognise. Which endpoints accept it
is a property of a deployment, so it is declared rather than guessed from the
model id.
"""

import pytest

from code_gantry.config import ExecutorConfig, RESERVED_REQUEST_KEYS


class TestItIsSentOnlyWhenAsked:
    def test_absent_by_default(self):
        """A first-party endpoint must not be handed an unknown argument."""
        from code_gantry.executorclient import session_param

        assert session_param(ExecutorConfig(model="m"), "abc") == {}

    def test_present_when_declared(self):
        from code_gantry.executorclient import session_param

        cfg = ExecutorConfig(model="m", session_stickiness=True)
        assert session_param(cfg, "abc") == {"session_id": "abc"}

    def test_nothing_is_sent_without_an_identity(self):
        """An empty session id is not stickiness, it is a malformed request."""
        from code_gantry.executorclient import session_param

        cfg = ExecutorConfig(model="m", session_stickiness=True)
        assert session_param(cfg, "") == {}
        assert session_param(cfg, None) == {}


class TestOperatorsCannotSetIt:
    def test_session_id_is_reserved(self):
        """Derived from the project identity, like `prompt_cache_key` beside
        it. A hand-written constant in config would put two projects, or two
        roles, on one session."""
        assert "session_id" in RESERVED_REQUEST_KEYS

    def test_config_refuses_it(self):
        from code_gantry.config import _request_extra_problems

        cfg = ExecutorConfig(model="m", request_extra={"session_id": "mine"})
        problems = _request_extra_problems("executor", cfg)
        assert problems and "session_id" in problems[0]


class TestTheIdentity:
    def test_it_is_stable_for_a_project_and_within_the_limit(self):
        """Same shape as the cache key: stable across a run's stages, and
        distinct between projects."""
        from code_gantry.cachekey import MAX_CACHE_KEY, cache_key

        a = cache_key("session", "upgrade/rails-5")
        b = cache_key("session", "upgrade/rails-5")
        c = cache_key("session", "other-branch")
        assert a == b and a != c
        assert len(a) <= MAX_CACHE_KEY

    def test_it_is_not_the_cache_key(self):
        """Two different controls. Sharing one string would mean a change to
        either forcing the other, and they answer different questions."""
        from code_gantry.cachekey import cache_key

        assert cache_key("session", "b") != cache_key("exec", "b")
