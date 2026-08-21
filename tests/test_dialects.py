"""Which wire a call goes out on is a property of the model, not of the role.

The three roles each hardcode an API surface, and none of them chose it: the
planner speaks Anthropic Messages because it talks to Anthropic, and the
executor and reviewer speak OpenAI Responses because of what they replaced.
Three weeks of that reads as a design and is an accident of arrival order.

The evidence for each row is a measurement, not a preference:

- `openai/*` -> Responses. gpt-5.6-sol refuses function tools together with
  reasoning on chat/completions outright, and on Responses marks accumulate
  rather than move, so each tool turn extends the cached prefix — 98.1%,
  99.996% and 97.2% across three replayed stages.
- `anthropic/*` -> Messages. Structured output, `cache_control` with a TTL,
  and the longest-matching-prefix extension the plan block depends on.
- `google/*` -> Messages. Measured 2026-08-21: on Responses our OpenAI-dialect
  marks are ignored and the cached region pins at one figure however the
  conversation grows; through Messages with a moving `cache_control`
  breakpoint the same model over four tool turns read 13,427 -> 16,325 ->
  28,579 -> 36,751 with no rewrite after the first.

Unknown families get a defined answer rather than an exception. A model
appearing that we have not classified must not end a run — the router can
resolve to anything on its shortlist, and the majority wire caches
automatically on every provider OpenRouter lists as automatic.
"""

import pytest

from code_gantry.dialects import MESSAGES, RESPONSES, dialect_for


class TestTheFamilyMap:
    @pytest.mark.parametrize(
        "model",
        ["claude-opus-5", "anthropic/claude-opus-5", "us.anthropic.claude-opus-5",
         "anthropic/claude-opus-5:batch", "~anthropic/claude-opus-latest"],
    )
    def test_anthropic_however_it_is_spelled(self, model):
        """Route prefixes, region prefixes and suffixes name one model.

        A fixed list of exact strings meeting a set an operator and a router
        can both extend is a bet, not a specification.
        """
        assert dialect_for(model) is MESSAGES

    @pytest.mark.parametrize(
        "model",
        ["gpt-5.6-luna", "openai/gpt-5.6-sol", "us.openai.gpt-5.6-luna",
         "openai/gpt-5.6-sol:batch"],
    )
    def test_openai_however_it_is_spelled(self, model):
        assert dialect_for(model) is RESPONSES

    @pytest.mark.parametrize(
        "model", ["google/gemini-3.7-flash", "gemini-3.7-flash", "~google/gemini-pro-latest"]
    )
    def test_gemini_goes_to_messages(self, model):
        assert dialect_for(model) is MESSAGES

    def test_an_unknown_family_still_answers(self):
        """A run must not end because a router picked something new."""
        assert dialect_for("acme/brand-new-model-9") is RESPONSES

    def test_an_operator_can_override(self):
        """The map is provider knowledge, but a deployment may know better."""
        assert dialect_for("acme/brand-new-9", override="messages") is MESSAGES
        assert dialect_for("anthropic/claude-opus-5", override="responses") is RESPONSES

    def test_the_router_itself_is_not_a_family(self):
        """`openrouter/pareto-code` names a policy. Resolving it is the caller's
        job — a dialect chosen for a policy would be a guess about what it
        resolves to, and it resolved to three different families in one day."""
        with pytest.raises(ValueError):
            dialect_for("openrouter/pareto-code")


class TestStructuredOutput:
    def test_each_wire_names_it_differently(self):
        class Schema: ...
        assert RESPONSES.structured(Schema) == {"text_format": Schema}
        assert MESSAGES.structured(Schema) == {"output_format": Schema}

    def test_no_schema_sends_nothing(self):
        assert RESPONSES.structured(None) == {}
        assert MESSAGES.structured(None) == {}


class TestEffort:
    def test_each_wire_names_it_differently(self):
        assert RESPONSES.effort("max") == {"reasoning": {"effort": "max"}}
        assert MESSAGES.effort("high") == {"output_config": {"effort": "high"}}

    def test_absent_when_the_operator_chose_none(self):
        """A model that does not take the parameter must not be sent one, and
        no default of ours should override a provider's."""
        assert RESPONSES.effort(None) == {}
        assert MESSAGES.effort("") == {}


class TestTextBlocks:
    def test_the_block_type_differs(self):
        assert RESPONSES.text_block("hi")["type"] == "input_text"
        assert MESSAGES.text_block("hi")["type"] == "text"

    def test_a_cache_mark_uses_each_wire_s_vocabulary(self):
        r = RESPONSES.text_block("hi", cache=True)
        m = MESSAGES.text_block("hi", cache=True)
        assert r["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert m["cache_control"] == {"type": "ephemeral"}

    def test_a_ttl_reaches_only_the_wire_that_has_one(self):
        """Anthropic's default ephemeral window is about five minutes, and a
        stage outlasts it — the plan block shipped with a bare marker once and
        the reports showed 3% cached. Responses fixes its own lifetime."""
        assert MESSAGES.text_block("hi", cache=True, ttl="1h")["cache_control"]["ttl"] == "1h"
        assert "ttl" not in RESPONSES.text_block("hi", cache=True)["prompt_cache_breakpoint"]

    def test_no_mark_leaves_the_block_clean(self):
        assert "cache_control" not in MESSAGES.text_block("hi")
        assert "prompt_cache_breakpoint" not in RESPONSES.text_block("hi")


class TestRequestLevelCacheOptions:
    def test_responses_must_opt_in(self):
        """GPT-5.6 caches at explicit breakpoints and does not fall back to the
        longest matching prefix, so this is required rather than helpful."""
        assert RESPONSES.cache_options() == {"prompt_cache_options": {"mode": "explicit"}}

    def test_messages_has_no_request_level_switch(self):
        assert MESSAGES.cache_options() == {}


class TestUsage:
    def test_each_wire_is_read_in_its_own_shape(self):
        class RUsage:
            input_tokens = 100
            output_tokens = 10
            input_tokens_details = type("D", (), {"cached_tokens": 60, "cache_write_tokens": 5})()
        class MUsage:
            input_tokens = 40
            output_tokens = 10
            cache_read_input_tokens = 60
            cache_creation_input_tokens = 5

        r = RESPONSES.usage(RUsage())
        m = MESSAGES.usage(MUsage())
        # Anthropic reports three orthogonal numbers; read as OpenAI's shape it
        # produced a 251% hit rate in a real report. Both normalise to "total
        # input" and "the part of it that was a cache read".
        assert (r.prompt_tokens, r.cached_tokens) == (100, 60)
        assert (m.prompt_tokens, m.cached_tokens) == (105, 60)
