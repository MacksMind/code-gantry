"""What `build_runtime` hands to the model clients.

Both wirings here are the same shape of defect: a value computed correctly on
one side, consumed correctly on the other, and never connected. The clients are
constructed before the run log or the project config exist, so anything they
need from either is attached at assembly time and nowhere else — which makes
this the one place a missing line is invisible in unit tests and obvious in
production.
"""

from pathlib import Path

import pytest

from orchestrator.config import parse_config
from orchestrator.planner import AnthropicPlanner
from orchestrator.reviewer import OpenAIReviewer
from orchestrator.runtime import ProjectPaths, RunPaths, build_runtime


def a_config(repo):
    return parse_config(
        {
            "target_repo": str(repo),
            "project_branch": "work",
            "plan_root": "PLAN.md",
            "test_command": "pytest",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        }
    )


@pytest.fixture
def assembled(repo, tmp_path):
    cfg = a_config(repo)
    project = ProjectPaths("proj", root=tmp_path / "projects")
    planner = AnthropicPlanner(cfg.planner, client=object())
    reviewer = OpenAIReviewer(cfg.reviewer, client=object())
    lines: list[str] = []
    build_runtime(
        cfg, project, RunPaths(project, "run-1"), planner, reviewer, log=lines.append
    )
    return planner, reviewer, lines


class TestTheClientsCanSpeak:
    """A silent fifteen-minute wait and a hung process look identical."""

    def test_the_planner_gets_the_run_log(self, assembled):
        planner, _, lines = assembled
        planner.log("waiting")
        assert lines == ["waiting"]

    def test_the_reviewer_gets_the_run_log(self, assembled):
        _, reviewer, lines = assembled
        reviewer.log("waiting")
        assert lines == ["waiting"]


class TestThePlannerCanCheckItsOwnStage:
    """The rule lives in config; the planner has to be handed it.

    Without this the planner never learns its stage was rejected, and one
    uncompilable regex costs a human round trip plus the whole tool loop that
    produced the stage. Checked against the real config rather than a stub,
    because the value crosses two boundaries — planner fields to `Stage`, and
    `Stage` to the project's own rules — and both have dropped a value before.
    """

    def test_an_uncompilable_pattern_is_reported(self, assembled):
        planner, _, _ = assembled
        problems = planner.validate_stage_fields(
            {
                "id": "funnel-links",
                "instruction": "do the thing",
                "edit_files": ["app/**"],
                # `$` is an anchor, so `?` has nothing to quantify. Live: this
                # exact shape escalated a run to a human.
                "forbidden_patterns": [r"\.html_safe\s*$?.*funnel_request"],
            }
        )
        assert any("not a valid regex" in p for p in problems)

    def test_a_good_stage_reports_nothing(self, assembled):
        planner, _, _ = assembled
        assert (
            planner.validate_stage_fields(
                {
                    "id": "funnel-links",
                    "instruction": "do the thing",
                    "edit_files": ["app/**"],
                    "forbidden_patterns": [r"\.html_safe.*funnel_request"],
                }
            )
            == []
        )

    def test_a_spec_that_will_not_build_is_reported_not_raised(self, assembled):
        # Answering again is cheap; a traceback out of the plan node ends the
        # run and loses everything the planner read to get here.
        planner, _, _ = assembled
        problems = planner.validate_stage_fields({"id": None, "edit_files": "app/**"})
        assert problems, "a spec that cannot be built is a problem, not a crash"


class TestTheLiveProgressLog:
    """Handed over on its own so the reviewer can place it after its breakpoint.

    `live_plan` splices the same file into a tree, which is right for the
    planner — Anthropic extends the longest matching cached prefix, so an
    append-only document before a breakpoint gets cheaper as it grows. The
    reviewer's model has no such fallback, so the same arrangement misses on
    every landing. It needs the text, not the tree.
    """

    def test_it_reads_the_file_as_it_stands(self, repo, tmp_path):
        cfg = a_config(repo)
        cfg = cfg.model_copy(update={"plan_addendum_path": "docs/log.md"})
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs/log.md").write_text("## entry\n\nlanded\n")
        rt = _runtime(cfg, tmp_path)
        assert "landed" in rt.live_progress_log

    def test_no_configured_path_is_not_an_error(self, repo, tmp_path):
        rt = _runtime(a_config(repo), tmp_path)
        assert rt.live_progress_log is None

    def test_a_missing_file_is_not_an_error(self, repo, tmp_path):
        # A review is far too expensive to fail over a progress file that has
        # not been written yet.
        cfg = a_config(repo).model_copy(
            update={"plan_addendum_path": "docs/never-written.md"}
        )
        assert _runtime(cfg, tmp_path).live_progress_log is None


def _runtime(cfg, tmp_path):
    project = ProjectPaths("proj", root=tmp_path / "projects")
    return build_runtime(
        cfg,
        project,
        RunPaths(project, "run-1"),
        AnthropicPlanner(cfg.planner, client=object()),
        OpenAIReviewer(cfg.reviewer, client=object()),
    )
