"""Two naming conventions, and the check knew only one.

A litellm-style route puts the provider in the model string and strips it
before the request leaves — `openai/qwen3-coder-next` reaches the server as
`qwen3-coder-next`, which is what `_served_model_name` was written for. A
gateway does the opposite: OpenRouter's model *is* the whole slug, so
`openrouter/pareto-code` must match `openrouter/pareto-code`.

Measured: the first real OpenRouter config failed preflight against a list
that contained the model, because the check looked for `pareto-code` in a
catalogue whose entry is `openrouter/pareto-code`. The gate was right to be
loud and wrong about the fact — which is the worse half, because a check that
refuses a correct config teaches people to disable it.

The second defect is the message. It printed all 437 names, into a log that is
appended across every resume.
"""

from code_gantry.preflight import model_is_offered, unavailable_detail


class TestEitherConvention:
    def test_a_gateway_slug_matches_whole(self):
        assert model_is_offered("openrouter/pareto-code", {"openrouter/pareto-code"})

    def test_a_litellm_prefix_matches_stripped(self):
        assert model_is_offered("openai/qwen3-coder-next", {"qwen3-coder-next"})

    def test_a_bare_name_matches_itself(self):
        assert model_is_offered("gpt-5.6-luna", {"gpt-5.6-luna"})

    def test_a_genuine_typo_still_fails(self):
        assert not model_is_offered("openai/gpt-5.6-lunar", {"openai/gpt-5.6-luna"})

    def test_the_stripped_form_does_not_match_a_different_provider(self):
        """`openai/pareto-code` is not `openrouter/pareto-code`.

        Stripping is a fallback, not a licence to ignore the prefix when the
        catalogue is qualified — otherwise two providers' identically named
        models become one.
        """
        assert not model_is_offered("openai/pareto-code", {"openrouter/pareto-code"})


class TestTheMessageStaysReadable:
    def test_near_misses_lead_and_the_rest_is_a_count(self):
        names = {f"vendor/model-{i}" for i in range(400)} | {"openai/gpt-5.6-luna"}
        detail = unavailable_detail("openai/gpt-5.6-lunar", names)
        assert "openai/gpt-5.6-luna" in detail
        assert "401" in detail
        assert len(detail) < 1000, "the whole catalogue reached the log again"

    def test_a_short_catalogue_is_shown_whole(self):
        detail = unavailable_detail("nope", {"alpha", "beta"})
        assert "alpha" in detail and "beta" in detail
