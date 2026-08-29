"""Building the client, splitting the system prompt, and making the call.

The last wire-shaped things the executor still did inline. The system prompt is
the awkward one: Responses carries it as the first input item with
`role: "system"`, Messages as a separate `system=` argument, and a
`{"role": "system"}` item is simply invalid there. A role that builds one
conversation should not have to know which.
"""

import pytest

from code_gantry.dialects import MESSAGES, RESPONSES


# A variable this file owns, for the same reason the missing-key test names
# `DEFINITELY_NOT_SET_ANYWHERE`: a test that borrows a real credential's name
# is asking about the shell it was run from. `CLAUDE.md` records that rule in
# the other direction — a test asserting a variable was *absent* failed for
# anyone who had configured the tool — and this is the same defect pointed the
# other way, failing for anyone who has not. A fresh clone is the first thing a
# new contributor runs.
KEY_VAR = "CG_TEST_API_KEY"


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    """A key for every client this file builds, and only for this file.

    `monkeypatch` rather than `os.environ.setdefault`, which is what this
    replaced: an unscoped write with no cleanup made the base-URL tests pass or
    fail on whether they shared an xdist worker with the test that did it. Two
    machines running the same commit reported four failures and two.
    """
    monkeypatch.setenv(KEY_VAR, "test-key")


class _Cfg:
    def __init__(self, base=None, key=KEY_VAR):
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

        assert isinstance(RESPONSES.client(_Cfg()), OpenAI)
        assert isinstance(MESSAGES.client(_Cfg()), anthropic.Anthropic)

    def test_a_missing_key_says_which_variable(self):
        """Named in config and read from the environment, so no key is ever
        written to a file that gets committed."""
        cfg = _Cfg(key="DEFINITELY_NOT_SET_ANYWHERE")
        for wire in (RESPONSES, MESSAGES):
            with pytest.raises(KeyError, match="DEFINITELY_NOT_SET_ANYWHERE"):
                wire.client(cfg)


class TestTheBaseUrlSuitsTheWire:
    """One host in config; each wire adds the suffix it needs.

    The two SDKs disagree about what a base URL is. The OpenAI client wants
    `.../api/v1` and appends `responses`; the Anthropic client wants
    `.../api` and appends `v1/messages`. The planner's config carries no `/v1`
    for exactly that reason and the executor's carries one.

    That was survivable while a role's wire was fixed. It stops being
    survivable when the wire is chosen from the model: the same `api_base`
    then has to serve both, and the executor pointed at a Gemini model built
    an Anthropic client on `.../api/v1`, which resolves to
    `/api/v1/v1/messages`.

    So config names the endpoint and the dialect adjusts, which is the same
    division as everything else here — an operator should not have to know
    that two SDKs count path segments differently.
    """

    def test_messages_drops_a_trailing_v1_on_openrouter(self):
        from code_gantry.dialects import MESSAGES

        c = MESSAGES.client(_Cfg("https://openrouter.ai/api/v1"))
        assert str(c.base_url).rstrip("/") == "https://openrouter.ai/api"

    def test_responses_adds_v1_on_openrouter(self):
        from code_gantry.dialects import RESPONSES

        c = RESPONSES.client(_Cfg("https://openrouter.ai/api"))
        assert str(c.base_url).rstrip("/") == "https://openrouter.ai/api/v1"

    def test_each_wire_leaves_a_correct_base_alone(self):
        from code_gantry.dialects import MESSAGES, RESPONSES

        m = MESSAGES.client(_Cfg("https://openrouter.ai/api"))
        r = RESPONSES.client(_Cfg("https://openrouter.ai/api/v1"))
        assert str(m.base_url).rstrip("/") == "https://openrouter.ai/api"
        assert str(r.base_url).rstrip("/") == "https://openrouter.ai/api/v1"

    def test_a_non_gateway_base_is_untouched(self):
        """Only OpenRouter's layout is ours to know. A local server or a
        first-party endpoint is spelled by whoever runs it."""
        from code_gantry.dialects import MESSAGES, RESPONSES

        for wire in (MESSAGES, RESPONSES):
            c = wire.client(_Cfg("http://localhost:8080/v1"))
            assert str(c.base_url).rstrip("/") == "http://localhost:8080/v1"
