"""What the executor is told about ending its turn.

The paragraph promised another pass unconditionally — "finishing is not a
claim that the work is correct, only that you have no more edits to make."
True between cycles and false at the end of the last one, and false outright
for a command that fails outside the gates: a setup failure routes to a human
and ends the run.

Watched once, an executor left `bundle install` failing and closed with
"Added temporary bundle-path constraints to allow **the next** dependency
resolution to install the missing locked gem". There was no next one. The
sentence is not proof it caused that, but it says the thing the model acted
on, and it costs nothing to say what is actually true.
"""

from __future__ import annotations

from orchestrator.prompts import _executor_system_prompt


def _stop_section(cfg=None) -> str:
    text = _executor_system_prompt(cfg)
    start = text.index("## What happens when you stop")
    return text[start : text.index("\n## ", start + 1)]


def test_it_no_longer_promises_a_pass_unconditionally():
    assert "finishing is not a claim that the work is correct" not in _stop_section()


def test_it_says_a_failing_command_is_a_finished_failed_attempt():
    """The distinction the old wording collapsed: feedback that returns to
    you, versus a state you are leaving behind."""
    section = _stop_section().lower()
    assert "finished" in section
    assert "may be no" in section or "not always" in section


def test_it_still_says_the_tests_are_not_yours_to_run(self=None):
    """The half that was right and must survive the edit."""
    assert "no tool" in _stop_section()
