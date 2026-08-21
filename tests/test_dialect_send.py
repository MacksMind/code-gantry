"""Building the client, splitting the system prompt, and making the call.

The last wire-shaped things the executor still did inline. The system prompt is
the awkward one: Responses carries it as the first input item with
`role: "system"`, Messages as a separate `system=` argument, and a
`{"role": "system"}` item is simply invalid there. A role that builds one
conversation should not have to know which.
"""

import pytest

from code_gantry.dialects import MESSAGES, RESPONSES


class _Cfg:
    def __init__(self, base=None, key="OPENROUTER_API_KEY"):
        self.model = "m"
        self.api_base = base
        self.api_key_env = key
        self.request_timeout_seconds = 30
        self.max_retries = 0
    def resolve_api_base(self):
        return self.api_base


CONV = [
    {"role": "system", "content": [{"type": "input_text", "text": "SYS"}]},
    {"role": "user", "content": [{"type": "input_text", "text": "GO"}]},
]


class TestSplittingTheSystemPrompt:
    def test_responses_keeps_it_in_the_conversation(self):
        system, rest = RESPONSES.split_system(CONV)
        assert system is None
        assert len(rest) == 2 and rest[0]["role"] == "system"

    def test_messages_lifts_it_out(self):
        system, rest = MESSAGES.split_system(CONV)
        assert system == [{"type": "text", "text": "SYS"}]
        assert len(rest) == 1 and rest[0]["role"] == "user"

    def test_messages_rewrites_block_types_it_lifts(self):
        """`input_text` is not a block type on this wire."""
        system, _ = MESSAGES.split_system(CONV)
        assert system[0]["type"] == "text"

    def test_a_conversation_with_no_system_item_is_untouched(self):
        conv = [{"role": "user", "content": [{"type": "input_text", "text": "GO"}]}]
        for wire in (RESPONSES, MESSAGES):
            system, rest = wire.split_system(conv)
            assert system is None and len(rest) == 1

    def test_the_original_is_not_mutated(self):
        """The caller holds this list across turns and mirrors it to disk."""
        before = len(CONV)
        MESSAGES.split_system(CONV)
        assert len(CONV) == before and CONV[0]["role"] == "system"


class TestClients:
    def test_each_wire_builds_its_own_sdk(self):
        import anthropic
        from openai import OpenAI

        import os
        os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
        assert isinstance(RESPONSES.client(_Cfg()), OpenAI)
        assert isinstance(MESSAGES.client(_Cfg()), anthropic.Anthropic)

    def test_a_missing_key_says_which_variable(self):
        """Named in config and read from the environment, so no key is ever
        written to a file that gets committed."""
        cfg = _Cfg(key="DEFINITELY_NOT_SET_ANYWHERE")
        for wire in (RESPONSES, MESSAGES):
            with pytest.raises(KeyError, match="DEFINITELY_NOT_SET_ANYWHERE"):
                wire.client(cfg)
