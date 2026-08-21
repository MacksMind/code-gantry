"""An operator may add request parameters, but not silently replace ours.

A router's controls arrive as request body rather than as a model string:
OpenRouter's Pareto tier is `plugins: [{"id": "pareto-router",
"min_coding_score": 0.6}]`. Naming that in `ExecutorConfig` would be project
knowledge in code — `CLAUDE.md` is explicit that a gateway's vocabulary is a
property of a deployment, not of this tool — so the field is a passthrough.

A passthrough is a wide door. The keys the loop sets deliberately are the whole
of what makes an attempt what it is: `tools` is the capability partition,
`model` is who answers, `input` is the conversation. Merging over any of them
would be invisible at the call site and would look like the model
misbehaving. Refused at config load, which is the first moment the question
can be answered — the rule a startup check already earned by being asked from
inside `verify` and costing nine minutes and two model calls.
"""

import pytest

from code_gantry.config import ExecutorConfig, RESERVED_REQUEST_KEYS


def _problems(**kwargs):
    from code_gantry.config import _request_extra_problems

    return _request_extra_problems("executor", ExecutorConfig(model="m", **kwargs))


class TestReserved:
    @pytest.mark.parametrize("key", sorted(RESERVED_REQUEST_KEYS))
    def test_every_reserved_key_is_refused(self, key):
        problems = _problems(request_extra={key: "anything"})
        assert problems, f"{key} was allowed through"
        assert key in problems[0]

    def test_the_partition_is_named_among_them(self):
        """`tools` is the safety story, so it must be on the list by name."""
        assert "tools" in RESERVED_REQUEST_KEYS
        assert "model" in RESERVED_REQUEST_KEYS
        assert "input" in RESERVED_REQUEST_KEYS


class TestAllowed:
    def test_a_router_plugin_passes(self):
        extra = {"plugins": [{"id": "pareto-router", "min_coding_score": 0.6}]}
        assert _problems(request_extra=extra) == []

    def test_the_empty_default_is_not_a_problem(self):
        assert _problems() == []


class TestReachesTheRequest:
    def test_the_loop_merges_it_into_the_call(self):
        """The journey, not the endpoints: a value set in config has to arrive
        in the kwargs the SDK is called with."""
        from code_gantry.executorclient import request_extra

        cfg = ExecutorConfig(
            model="openrouter/pareto-code",
            request_extra={"plugins": [{"id": "pareto-router", "min_coding_score": 0.9}]},
        )
        assert request_extra(cfg) == {
            "extra_body": {"plugins": [{"id": "pareto-router", "min_coding_score": 0.9}]}
        }

    def test_nothing_is_sent_when_nothing_was_configured(self):
        """A model that does not take the parameter must not be handed one."""
        from code_gantry.executorclient import request_extra

        assert request_extra(ExecutorConfig(model="m")) == {}
