"""Deferrals are gone, and what they carried has a durable home.

`deferred` was a structured channel CodeGantry carried between planner
calls so that a step taken out of order could not be quietly forgotten. Its
framing was ordering — `safe_because` read "why nothing already done or still to
come depends on it. If you cannot say this, the order is required and you must
keep it."

Measured across every planner prompt on disk that carried the block: 11 distinct
deferrals, and not one was an ordering decision. Every reason was a capability
boundary — a scanner that needs a human driving a signed-in browser session, a
document our own scope gate reverts, a base image that has to change. The same
five items were re-asserted between 9 and 23 times, which is what a fact with no
durable home looks like.

That fact belongs in the plan: this item is not executable by this pipeline, and
here is who could do it. A plan note says it, the fold makes the plan say it, and
the next run's planner reads the plan and does not draw it. Nothing needs to be
carried, and `cli.reconcile`'s docstring had already drawn the line — "reconciling
is a judgement about what the work has become, and doing it mid-run would let a
run rewrite its own premises."

The exit code goes with it. `EXIT_DEFERRED` had exactly one producer.
"""

import pytest


class TestTheChannelIsGone:
    def test_the_planner_cannot_return_a_deferral(self):
        from code_gantry.planner import PlannerResponse

        assert "deferred" not in PlannerResponse.model_fields

    def test_there_is_no_deferral_model(self):
        import code_gantry.planner as planner

        assert not hasattr(planner, "Deferral")

    def test_the_outcome_carries_none(self):
        from code_gantry.planner import PlannerOutcome

        assert "deferred" not in {f.name for f in PlannerOutcome.__dataclass_fields__.values()}

    def test_run_state_declares_none(self):
        from code_gantry.state import RunState

        assert "deferred" not in RunState.__annotations__

    def test_the_state_helpers_are_gone(self):
        import code_gantry.state as state

        assert not hasattr(state, "merge_deferrals")
        assert not hasattr(state, "outstanding_deferrals")

    def test_no_prompt_block_renders_them(self):
        import code_gantry.prompts as prompts

        assert not hasattr(prompts, "_deferred_block")

    def test_build_planner_messages_takes_no_deferred_argument(self):
        import inspect

        from code_gantry.prompts import build_planner_messages

        assert "deferred" not in inspect.signature(build_planner_messages).parameters


class TestNothingStillReferencesIt:
    def test_the_exit_code_has_no_producer_left(self):
        import code_gantry.cli as cli

        assert not hasattr(cli, "EXIT_DEFERRED")

    def test_the_report_has_no_deferred_section(self):
        import code_gantry.report as report

        assert not hasattr(report, "_deferred_section")

    def test_the_prompt_tells_the_planner_what_to_do_instead(self):
        """Deleting a mechanism must not delete the capability it served.

        A planner that meets a step this pipeline cannot execute still needs an
        answer, and now it is a note: say so, name who can, and move on.
        """
        from code_gantry.planner import PLANNER_SYSTEM_PROMPT

        text = PLANNER_SYSTEM_PROMPT.lower()
        assert "cannot" in text
        assert "plan note" in text or "plan_notes" in text


class TestStatusTail:
    """The planner is no longer shown its own prior prose.

    `status.md` records `status_entry` plus `**Why:** <reasoning>`, and the last
    4,000 characters were fed back on every call. It was 0.75% of one measured
    prompt, so this is not a cost change: `reasoning` is free text with no scope
    discipline on it, and it was the remaining route by which a finding excluded
    from the log could still reach the next derivation.
    """

    def test_build_planner_messages_takes_no_status_tail(self):
        import inspect

        from code_gantry.prompts import build_planner_messages

        assert "status_tail" not in inspect.signature(build_planner_messages).parameters

    def test_nodes_no_longer_reads_the_tail(self):
        import code_gantry.nodes as nodes

        assert not hasattr(nodes, "_status_tail")
        assert not hasattr(nodes, "STATUS_TAIL_CHARS")

    def test_status_is_still_written_for_the_operator(self, tmp_path):
        from code_gantry.planner import append_status

        path = append_status(
            tmp_path,
            stage_index=0,
            stage_id="s1",
            revision=0,
            verdict="next_stage",
            entry="goal, expected, actual",
            reasoning="why",
        )
        assert path.is_file()
        assert "goal, expected, actual" in path.read_text()
