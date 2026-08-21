"""The execute line should say who answered.

Under a router `cfg.model` names a policy, not a model. `served_models` is on
`executor-loop.json`, but the run log is the timeline an operator actually
reads while a run is going, and reading it meant knowing the tier and guessing
— which is the inference this codebase keeps paying for.

The interesting case is a *split*. Sticky routing on OpenRouter is a
five-minute window and an attempt routinely runs longer, so a model can change
between turns. That shows up as a cold prefix at full price, and without the
split in the line it would look like an unexplained cost.
"""

from code_gantry.nodes import served_summary


class TestRendering:
    def test_one_model_is_named_plainly(self):
        assert served_summary({"openai/gpt-5.6-sol": 12}) == " via openai/gpt-5.6-sol"

    def test_a_split_shows_the_turns_each_took(self):
        """Ordered by turns, because the majority model is the one that did it."""
        out = served_summary({"x-ai/grok-4.6": 4, "openai/gpt-5.6-sol": 8})
        assert out == " via openai/gpt-5.6-sol x8, x-ai/grok-4.6 x4"

    def test_nothing_reported_renders_nothing(self):
        """An endpoint that does not echo the model must not print `via `.

        Absence and a model named the empty string are different facts, and the
        line should not make them look the same.
        """
        assert served_summary({}) == ""
        assert served_summary(None) == ""
