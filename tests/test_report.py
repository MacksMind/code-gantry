"""The run report.

Its job is to make two things easy: undoing the whole run, and confirming the
paid model was invoked at checkpoints rather than continuously. Everything
else is detail.
"""

from orchestrator.config import parse_config
from orchestrator.report import build_report


def cfg_with(**over):
    data = {
        "target_repo": "/tmp/some-app",
        "branch": "refactor/thing",
        "base_ref": "main",
        "test_command": "pytest",
        "executor": {"model": "m"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": [
            {"id": "one", "instruction": "a", "edit_files": ["x"]},
            {"id": "two", "instruction": "b", "edit_files": ["x"]},
        ],
    }
    data.update(over)
    return parse_config(data)


def state_with(**over):
    base = {
        "run_id": "20260729-1200-thing",
        "target_repo": "/tmp/some-app",
        "base_ref": "main",
        "base_sha": "abc123def456789",
        "branch": "refactor/thing",
        "status": "complete",
        "stage_index": 2,
        "history": [
            {
                "id": "one",
                "kind": "agent",
                "outcome": "complete",
                "commit_range": "abc123def456..def456abc123",
                "wall_seconds": 120.0,
                "test_seconds": 45.0,
                "verify_retries": 1,
                "rework_attempts": 1,
                "flake_reruns": 2,
                "failed_layer": None,
                "review_verdict": "approved",
                "review_summary": "Looks right.",
                "prompt_tokens": 12000,
                "cached_tokens": 11000,
                "completion_tokens": 300,
            }
        ],
        "escalation_reason": None,
    }
    base.update(over)
    return base


class TestUndoability:
    def test_includes_a_single_reset_command(self):
        # The safety requirement: the whole run must be undoable in one move.
        report = build_report(state_with(), cfg_with())
        assert "git -C /tmp/some-app reset --hard abc123def456789" in report

    def test_names_the_branch_and_base(self):
        report = build_report(state_with(), cfg_with())
        assert "refactor/thing" in report
        assert "main" in report


class TestCostVisibility:
    def test_reports_token_totals(self):
        report = build_report(state_with(), cfg_with())
        assert "12,000" in report or "12000" in report

    def test_reports_the_cached_proportion(self):
        # The economic argument for splitting executor from reviewer depends on
        # the prefix actually being cached. A regression must be visible.
        report = build_report(state_with(), cfg_with())
        assert "91" in report or "92" in report

    def test_reports_test_runtime_alongside_tokens(self):
        # On a large suite, verify time rather than token cost is what makes a
        # run expensive.
        report = build_report(state_with(), cfg_with())
        assert "45" in report

    def test_zero_tokens_does_not_divide_by_zero(self):
        history = [dict(state_with()["history"][0], prompt_tokens=0, cached_tokens=0)]
        report = build_report(state_with(history=history), cfg_with())
        assert "0" in report


class TestStageDetail:
    def test_lists_each_stage_with_its_outcome(self):
        report = build_report(state_with(), cfg_with())
        assert "one" in report
        assert "complete" in report

    def test_reports_retries_and_flakes(self):
        report = build_report(state_with(), cfg_with())
        assert "flake" in report.lower()

    def test_reports_the_commit_range(self):
        report = build_report(state_with(), cfg_with())
        assert "abc123def456..def456abc123" in report

    def test_reports_reviewer_issues(self):
        history = [
            dict(
                state_with()["history"][0],
                review_verdict="rework",
                review_summary="Wrong verb.",
            )
        ]
        report = build_report(state_with(history=history), cfg_with())
        assert "Wrong verb." in report

    def test_names_the_failed_layer_on_an_escalation(self):
        history = [
            dict(
                state_with()["history"][0],
                outcome="escalated",
                failed_layer="scope",
                commit_range=None,
            )
        ]
        report = build_report(
            state_with(history=history, status="escalated", escalation_reason="went rogue"),
            cfg_with(),
        )
        assert "scope" in report
        assert "went rogue" in report


class TestStatusFraming:
    def test_complete_run(self):
        assert "complete" in build_report(state_with(), cfg_with()).lower()

    def test_escalated_run_states_why(self):
        report = build_report(
            state_with(status="escalated", escalation_reason="tests kept failing"),
            cfg_with(),
        )
        assert "escalated" in report.lower()
        assert "tests kept failing" in report

    def test_gated_run_tells_the_human_what_to_do(self):
        # A paused run's report is the handoff document.
        report = build_report(
            state_with(status="awaiting_human", stage_index=1, history=[]),
            cfg_with(
                stages=[
                    {"id": "one", "instruction": "a", "edit_files": ["x"]},
                    {
                        "id": "bump",
                        "kind": "manual",
                        "human_steps": "Bump the runtime to 2.4 and deploy.",
                        "checks": ["true"],
                    },
                ]
            ),
        )
        assert "Bump the runtime to 2.4 and deploy." in report
        assert "resume" in report.lower()

    def test_gated_report_names_the_resume_command(self):
        report = build_report(
            state_with(status="awaiting_human", stage_index=1, history=[]),
            cfg_with(
                stages=[
                    {"id": "one", "instruction": "a", "edit_files": ["x"]},
                    {"id": "bump", "kind": "manual", "human_steps": "do it", "checks": ["true"]},
                ]
            ),
        )
        assert "20260729-1200-thing" in report


class TestNoDuplicateRows:
    def test_a_stage_that_escalated_then_completed_appears_once(self):
        # A gated stage can escalate (work not done), then complete on a later
        # resume. failed_stage_id lingers in state; the table must not show the
        # stage twice with contradictory outcomes.
        history = [
            dict(state_with()["history"][0], id="bump", outcome="complete"),
        ]
        report = build_report(
            state_with(history=history, status="complete", failed_stage_id="bump"),
            cfg_with(),
        )
        rows = [ln for ln in report.splitlines() if ln.startswith("| `bump`")]
        assert len(rows) == 1
        assert "complete" in rows[0]

    def test_an_escalated_stage_is_shown_while_the_run_is_stopped(self):
        report = build_report(
            state_with(
                history=[],
                status="escalated",
                failed_stage_id="bump",
                failure_layer="checks",
                escalation_reason="the check failed",
            ),
            cfg_with(),
        )
        assert "bump" in report
        assert "checks" in report


class TestRemainingWork:
    def test_lists_stages_that_never_ran(self):
        # Otherwise an escalated run looks like it covered everything.
        report = build_report(
            state_with(status="escalated", stage_index=0, history=[], escalation_reason="x"),
            cfg_with(),
        )
        assert "two" in report
