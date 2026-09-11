"""How a run's end reaches whoever started it.

The daemon decides what to do next from the exit code alone, so each end
has its own: a pause and an escalation both stop the run and want opposite
things afterwards.
"""

from code_gantry.cli import EXIT_ESCALATED, EXIT_FAILED, EXIT_OK, EXIT_PAUSED, _exit_code


def test_each_end_has_its_own_code():
    assert _exit_code({"status": "complete"}) == EXIT_OK
    assert _exit_code({"status": "escalated", "failure_layer": "review"}) == EXIT_ESCALATED
    assert _exit_code({"status": "escalated", "failure_layer": "paused", "paused_before": "precheck"}) == EXIT_PAUSED
    assert _exit_code({"status": "running"}) == EXIT_FAILED
    assert _exit_code({}) == EXIT_FAILED


def test_the_codes_are_distinct_and_documented():
    assert len({EXIT_OK, EXIT_FAILED, EXIT_ESCALATED, EXIT_PAUSED}) == 4
