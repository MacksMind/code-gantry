import os
import pytest

from test_config import as_test_tools

from code_gantry.config import parse_config


def _cfg(**over):
    data = {
        "target_repo": "/tmp/app",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "full_test_command": "true",
        "executor": {"model": "openai/local", "api_base": "http://gpubox:8080/v1"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(over)
    return parse_config(as_test_tools(data))


class TestAKeylessEndpointIsExpressibleInEveryRole:
    """One field, three behaviours, and the accommodating one was dead.

    `api_key_env` was optional on the executor alone, and even there it did not
    work: `build_openai_client` omitted the key when none was named and has no
    callers, while production goes through `dialects._api_key`, which raises.
    The planner and the reviewer could not express it at all — both compared
    `cfg.api_key_env not in os.environ`, which is a `TypeError` on `None`
    rather than a miss.

    Someone running three local models should be able to say so in all three
    places. And an operator who names no variable against a *cloud* endpoint
    has made a claim rather than an omission: the request goes out and the
    provider answers 401, which is a better error than one of ours guessing.

    Omitting the key is not enough, measured: `OpenAI(...)` raises without one
    and `anthropic.Anthropic(...)` does not, so a keyless endpoint needs a
    placeholder rather than an absence.
    """

    def test_all_three_accept_no_variable(self):
        cfg = _cfg(
            executor={"model": "openai/local", "api_base": "http://x/v1", "api_key_env": None},
            planner={"model": "claude-opus-5", "api_key_env": None},
            reviewer={"model": "gpt-5.5", "api_key_env": None},
        )
        assert cfg.executor.api_key_env is None
        assert cfg.planner.api_key_env is None
        assert cfg.reviewer.api_key_env is None

    def test_the_defaults_are_unchanged(self):
        cfg = _cfg()
        assert cfg.planner.api_key_env == "ANTHROPIC_API_KEY"
        assert cfg.reviewer.api_key_env == "OPENAI_API_KEY"

    def test_a_named_variable_that_is_unset_still_says_so(self, monkeypatch):
        # The distinction worth keeping: naming a variable and not setting it
        # is a mistake with a clear message; naming none is a decision.
        from code_gantry.dialects import _api_key

        monkeypatch.delenv("NOPE_KEY", raising=False)
        cfg = _cfg(executor={"model": "m", "api_key_env": "NOPE_KEY"}).executor
        with pytest.raises(KeyError, match="NOPE_KEY"):
            _api_key(cfg)

    def test_naming_none_yields_a_placeholder_rather_than_raising(self):
        from code_gantry.dialects import _api_key

        cfg = _cfg(executor={"model": "m", "api_key_env": None}).executor
        assert _api_key(cfg)  # something the SDK will accept

    def test_each_role_builds_a_client_without_a_key(self, monkeypatch):
        from code_gantry.dialects import MESSAGES, RESPONSES

        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        cfg = _cfg(
            executor={"model": "openai/local", "api_base": "http://x/v1", "api_key_env": None},
            planner={"model": "claude-opus-5", "api_key_env": None, "api_base": "http://x"},
            reviewer={"model": "gpt-5.5", "api_key_env": None, "api_base": "http://x/v1"},
        )
        assert MESSAGES.client(cfg.planner) is not None
        assert RESPONSES.client(cfg.reviewer) is not None
        assert RESPONSES.client(cfg.executor) is not None
