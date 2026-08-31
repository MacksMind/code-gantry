"""Running out of turns is a diagnosis, and it was silent.

`ExecutorTurn.stopped` is set False when the model is still calling tools at
the turn ceiling, and its comment says "the loop must not treat this as
'finished'". The loop never read it. Written in three places, consulted in
none.

What that cost, live: `cart-explicit-routes` made 85, 103, 76 and 138 tool
calls across four attempts, every one of them a read, and never edited
anything. Each attempt hit `model_turns: 20` — exactly `max_model_turns` — and
each was reported by the scope gate as "the attempt produced no changes",
which is true and useless. Eleven minutes and $0.68 with the cause invisible,
and the planner was handed a diagnosis that pointed at the stage rather than
at the ceiling.

The two are different problems with different fixes. A model that stops having
changed nothing has decided it has nothing to do — that is a stage drawn
wrongly. A model still asking for things when the turns run out was working:
the stage is too large to survey in the budget it was given, and redrawing it
smaller or raising the ceiling is the answer. Reporting them identically sends
the planner to fix the wrong thing.
"""

import pytest


class TestTheLoopNoticesTheCeiling:
    def _run(self, stopped, edits):
        from code_gantry.config import parse_config, Stage
        from code_gantry.executorclient import ExecutorTurn
        from code_gantry.executorloop import run_loop

        class Model:
            def run(self, conversation, reader, editor, semantic=None, cache_key=None):
                turn = ExecutorTurn()
                turn.turns = 20
                turn.stopped = stopped
                if edits:
                    editor.touched.add("app/a.rb")
                return turn

        cfg = parse_config({
            "target_repo": ".", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "full_test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        })

        class Editor:
            def __init__(self):
                self.touched = set()
                self.calls = []

        class Git:
            def is_clean(self):
                return True

            def head_sha(self):
                return "abc"

        return run_loop(
            Stage(id="s", instruction="do", edit_files=["app/**"]),
            cfg, Git(), None, Model(), object(), Editor(), since_sha="abc",
        )

    def test_exhausting_the_turns_is_recorded(self):
        out = self._run(stopped=False, edits=False)
        assert out.turns_exhausted is True

    def test_a_model_that_finished_is_not(self):
        out = self._run(stopped=True, edits=False)
        assert out.turns_exhausted is False

    def test_it_is_not_reported_as_a_broken_executor(self):
        # `ok=False` means the executor itself broke — transport, auth, an
        # unhandled exception. Running out of turns is none of those, and the
        # work it committed still stands.
        out = self._run(stopped=False, edits=False)
        assert out.ok is True


class TestThePlannerIsToldWhichProblemItIs:
    def test_the_ceiling_produces_its_own_advice(self):
        from code_gantry.nodes import _no_change_reason

        text = _no_change_reason(turns_exhausted=True, turns=20)
        assert "20" in text
        assert "turn" in text.lower()
        # The actionable half: what to do about it.
        assert "smaller" in text.lower() or "narrower" in text.lower()

    def test_a_model_that_simply_stopped_says_so_instead(self):
        from code_gantry.nodes import _no_change_reason

        text = _no_change_reason(turns_exhausted=False, turns=3)
        assert "turn" not in text.lower()

    def test_the_two_are_not_the_same_sentence(self):
        from code_gantry.nodes import _no_change_reason

        assert _no_change_reason(True, 20) != _no_change_reason(False, 20)
