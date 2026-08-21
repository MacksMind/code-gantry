"""The per-attempt record carries every field of the result it summarises.

`executor-loop.json` enumerated ten keys of a twenty-field `ExecutionResult`.
Missing were `ok`, `timed_out`, `turns_exhausted` and `log` — which together
are the entire answer to "why did this attempt end" — plus `tool_counts`,
`refusal_counts`, `gate_records`, `dropped_reads` and, as of this morning,
`commit_refused`.

Found by making the mistake it causes. Asked why an attempt stopped after three
reads and no edits, I read this file, got `None` for four keys, and was one step
from reporting that the loop had not recorded why. It had not recorded it *here*;
the model's closing sentence was in `executor.log` next door, saying it could
not do the work because the fix lay outside `edit_files`.

That is the standing rule twice over: a summary artifact must carry the number
it is about, and an absent key cannot be told apart from a false one — so an
artifact that drops fields does not fail to answer, it answers wrongly with the
confidence of a record. The same defect in `planner.json` had me report zero
deferrals across 542 calls when the real figure was 50 of 70.

So the test is on the *set*, not on the fields I happened to notice. A field
added to `ExecutionResult` and not to the writer fails here, which is the only
version of this that survives the next change.
"""

import dataclasses
import json

import pytest

from code_gantry.executor import ExecutionResult

# Written under a different name because the artifact says what the number is
# for rather than what the attribute is called.
RENAMED = {"context_tokens": "peak_prompt_tokens"}
# Folded into a nested object rather than dropped.
NESTED = {"first_prompt_tokens", "first_cached_tokens"}


def _written(tmp_path, result):
    from code_gantry.executor import _write_loop_record

    _write_loop_record(tmp_path, result)
    return json.loads((tmp_path / "executor-loop.json").read_text())


def a_result(**over):
    fields = dict(ok=True, cycles=2, model_turns=5, edits_applied=3)
    fields.update(over)
    return ExecutionResult(**fields)


class TestEveryFieldIsCarried:
    def test_no_field_of_the_result_is_dropped(self, tmp_path):
        written = _written(tmp_path, a_result())
        keys = set(written) | set(written.get("opening_turn") or {})
        missing = []
        for f in dataclasses.fields(ExecutionResult):
            name = RENAMED.get(f.name, f.name)
            if name in keys or f.name in NESTED and written.get("opening_turn"):
                continue
            if name not in keys:
                missing.append(f.name)
        assert not missing, (
            f"executor-loop.json drops {missing}. A field the writer does not "
            "know about is one nobody can measure, and an absent key reads as "
            "a false value."
        )

    def test_the_three_that_say_how_it_ended(self, tmp_path):
        # The ones whose absence sent me to the wrong conclusion.
        written = _written(tmp_path, a_result(ok=False, timed_out=True))
        assert written["ok"] is False
        assert written["timed_out"] is True
        assert "turns_exhausted" in written

    def test_the_closing_message_is_carried(self, tmp_path):
        written = _written(tmp_path, a_result(log="I cannot do this: role.rb is out of scope."))
        assert "role.rb" in written["log"]

    def test_a_refused_commit_leaves_a_trace(self, tmp_path):
        # Added this morning; the escalation would otherwise fire with nothing
        # in the per-attempt record to show it had.
        written = _written(tmp_path, a_result(commit_refused="hook said no"))
        assert written["commit_refused"] == "hook said no"

    def test_the_counts_are_structured_not_only_rendered(self, tmp_path):
        # They reach run.log as a summary line. Every count quoted from this
        # project so far came from parsing that line back, which is the thing
        # the ledger exists to make unnecessary.
        written = _written(tmp_path, a_result(
            tool_counts={"read_file": 9}, refusal_counts={"edit not found": 1}
        ))
        assert written["tool_counts"] == {"read_file": 9}
        assert written["refusal_counts"] == {"edit not found": 1}


class TestItStaysBestEffort:
    def test_an_unwritable_directory_does_not_raise(self, tmp_path):
        from code_gantry.executor import _write_loop_record

        # The attempt has already happened; failing to describe it must not
        # take the run down.
        _write_loop_record(tmp_path / "does" / "not" / "exist", a_result())

    def test_a_missing_usage_object_still_writes(self, tmp_path):
        """Empty rather than zeros.

        This asserted `prompt_tokens == 0`, which invents a reading: an
        attempt whose provider reported nothing did not use nothing. It is the
        same zero-versus-absent confusion `_price` exists to guard — a zero
        has meant "no figure" as often as it has meant "none" — and reading a
        written zero as a measurement is how a whole channel of executor cost
        was once reported as free.

        The record still exists and the run still survives, which is what this
        class is about.
        """
        written = _written(tmp_path, a_result(usage=None))
        assert written["usage"] == {}
        assert "ok" in written
