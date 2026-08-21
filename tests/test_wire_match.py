"""A role's client and its model's dialect must agree, and say so if they don't.

`dialects.py` decides which wire a model should be called on. Until every role
can speak both, a configured model can want one wire while the role that calls
it speaks the other — and the symptom is silent: on Responses our
OpenAI-dialect cache marks mean nothing to Gemini, so the cached region pins
and the bill grows, with nothing in any log naming the cause. It took an
afternoon of probes to find it once.

So the mismatch is a preflight check. It is a *warning*, not a blocker: the
call still works and the only loss is fine-grained cache control, and a gate
that refuses a working configuration is one people learn to switch off.
"""

import pytest

from code_gantry.dialects import MESSAGES, RESPONSES
from code_gantry.wirecheck import ROLE_WIRE, wire_mismatches


class _Cfg:
    def __init__(self, planner, executor, reviewer):
        self.planner = type("P", (), {"model": planner})()
        self.executor = type("E", (), {"model": executor})()
        self.reviewer = type("R", (), {"model": reviewer})()


class TestWhatEachRoleSpeaks:
    def test_the_table_matches_the_clients_as_built(self):
        """Pinned, because this is the fact the check depends on and it stops
        being true the moment a role learns a second wire."""
        assert ROLE_WIRE == {"planner": MESSAGES, "reviewer": RESPONSES}


class TestAgreement:
    def test_the_shipped_configuration_is_quiet(self):
        cfg = _Cfg("anthropic/claude-opus-5", "openai/gpt-5.6-luna", "openai/gpt-5.6-sol")
        assert wire_mismatches(cfg) == []

    def test_gemini_on_the_executor_is_no_longer_a_mismatch(self):
        """The case this was built for, now fixed at the source: the executor
        speaks whichever wire its model wants, so there is nothing to report."""
        cfg = _Cfg("anthropic/claude-opus-5", "google/gemini-3.7-flash", "openai/gpt-5.6-sol")
        assert wire_mismatches(cfg) == []

    def test_an_anthropic_reviewer_is_reported(self):
        cfg = _Cfg("anthropic/claude-opus-5", "openai/gpt-5.6-luna", "anthropic/claude-opus-5")
        assert len(wire_mismatches(cfg)) == 1

    def test_a_policy_is_not_a_mismatch(self):
        """`openrouter/pareto-code` resolves per run and cannot be classified
        here. Refusing to answer is right; reporting it as wrong is not."""
        cfg = _Cfg("anthropic/claude-opus-5", "openrouter/pareto-code", "openai/gpt-5.6-sol")
        assert wire_mismatches(cfg) == []

    def test_an_unknown_model_is_not_reported(self):
        """It gets the default dialect, which is what the role already speaks."""
        cfg = _Cfg("anthropic/claude-opus-5", "acme/new-thing", "openai/gpt-5.6-sol")
        assert wire_mismatches(cfg) == []

    def test_the_message_says_what_is_lost(self):
        """Not just that they differ — an operator needs to know whether to act."""
        cfg = _Cfg("anthropic/claude-opus-5", "openai/gpt-5.6-luna", "anthropic/claude-opus-5")
        assert "cache" in wire_mismatches(cfg)[0].lower()
