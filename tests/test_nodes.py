"""Node logic with stubbed planner, executor, and reviewer.

These are the decisions that make the loop behave: which failures route to the
executor, which to the planner, and which to a human; whether a scope violation
throws work away; and whether the expensive half of the merge gate runs only
when the cheap half passed.
"""

import time
from dataclasses import dataclass, field

from orchestrator import nodes
from orchestrator.commands import CommandRunner
from orchestrator.config import Stage, parse_config
from orchestrator.executor import ExecutionResult
from orchestrator.gitops import Git
from orchestrator.plandoc import PlanDocument, PlanTree
from orchestrator.planner import PlannerOutcome, PlannerUsage
from orchestrator.reviewer import Issue, ReviewOutcome, TokenUsage
from orchestrator.runtime import ProjectPaths, RunPaths, Runtime
from orchestrator.state import new_state


@dataclass
class StubPlanner:
    outcomes: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def plan(self, messages):
        self.calls.append(messages)
        if self.outcomes:
            return self.outcomes.pop(0)
        return PlannerOutcome(
            verdict="project_complete", reasoning="done", status_entry="e"
        )


@dataclass
class StubReviewer:
    outcomes: list = field(default_factory=list)
    calls: int = 0

    def review(self, messages):
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return ReviewOutcome(
            verdict="approved", summary="fine", usage=TokenUsage(1000, 40, 900)
        )


@dataclass
class StubExecutor:
    repo: object = None
    edits: list = field(default_factory=list)
    ok: bool = True
    timed_out: bool = False
    prompts: list = field(default_factory=list)
    history_dirs: list = field(default_factory=list)

    def gather_context(self, stage):
        return [], []

    def _apply(self):
        if self.edits and self.repo is not None:
            name, text = self.edits.pop(0)
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run_agent_stage(self, stage, prompt, history_dir=None):
        self.prompts.append(prompt)
        self.history_dirs.append(history_dir)
        self._apply()
        return ExecutionResult(ok=self.ok, log="executor log", timed_out=self.timed_out)

    def run_script_stage(self, stage):
        self._apply()
        return ExecutionResult(ok=self.ok, log="script log")


def planned_stage(**over):
    fields = {
        "id": "extract",
        "instruction": "Extract the thing.",
        "edit_files": ["app.py", "src/**"],
    }
    fields.update(over)
    return fields


def make(repo, tmp_path, planner=None, reviewer=None, executor=None, **cfg_over):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "full_test_command": "true",
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(cfg_over)
    cfg = parse_config(data)

    project = ProjectPaths("proj-slug", root=tmp_path / "projects")
    project.ensure()
    paths = RunPaths(project, "r1")
    paths.ensure()

    ex = executor or StubExecutor(repo=repo, edits=[("app.py", "changed\n")])
    ex.repo = repo
    rt = Runtime(
        cfg=cfg,
        project=project,
        paths=paths,
        git=Git(repo),
        runner=CommandRunner(cwd=repo, timeout=60),
        executor=ex,
        planner=planner or StubPlanner(),
        reviewer=reviewer or StubReviewer(),
    )
    rt._plan = PlanTree(root=PlanDocument(path="PLAN.md", content="# The plan"))

    base_sha = rt.git.ensure_project_branch("proj", "main")
    state = new_state(
        run_id="r1",
        project_slug="proj-slug",
        config_hash="hash",
        target_repo=str(repo),
        base_ref="main",
        base_sha=base_sha,
        project_branch="proj",
        started_at=time.time(),
    )
    return cfg, rt, state


def with_stage(state, rt, **over):
    """Put a stage in flight with a cut branch, as precheck would."""
    stage = Stage(**planned_stage(**over))
    state["current"] = stage.model_dump()
    branch = rt.cfg.stage_branch(state["stage_index"], stage.id)
    state["stage_branch"] = branch
    state["stage_start_sha"] = rt.git.cut_stage_branch(branch, "proj")
    state["stage_started_at"] = time.time()
    return state


class TestPlanDerivation:
    def test_a_new_stage_goes_to_precheck(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "first", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "precheck"
        assert out["current"]["id"] == "extract"

    def test_project_complete_goes_to_finalize(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.plan(state, rt)["next_hop"] == "finalize"

    def test_blocked_escalates(self, repo, tmp_path):
        planner = StubPlanner([PlannerOutcome("blocked", "plan contradicts itself", "e")])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "contradicts" in out["escalation_reason"]

    def test_an_invalid_spec_escalates_rather_than_running(self, repo, tmp_path):
        # No edit_files means the scope guard is meaningless.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"})]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "edit_files" in out["escalation_reason"]

    def test_operator_stage_defaults_are_applied(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(
            repo, tmp_path, planner=planner,
            stage_defaults={"checks": ["true"], "require_new_tests": True},
        )
        out = nodes.plan(state, rt)
        assert out["current"]["checks"] == ["true"]
        assert out["current"]["require_new_tests"] is True

    def test_writes_a_status_entry_every_invocation(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        nodes.plan(state, rt)
        assert "done" in rt.project.status.read_text()

    def test_records_planner_usage_on_the_run(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("project_complete", "r", "e", usage=PlannerUsage(500, 400, 90))]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["run_usage"]["planner_prompt_tokens"] == 500


class TestPlannerBudgets:
    def test_exhausted_interventions_escalate(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, limits={"max_planner_interventions": 2})
        state = with_stage(state, rt)
        state["planner_interventions"] = 2
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "budget is exhausted" in out["escalation_reason"]

    def test_the_budget_is_global_not_per_stage(self, repo, tmp_path):
        # Per-stage caps let a pathological project consume unbounded paid
        # inference one stage at a time.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="restart")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        state["planner_interventions"] = 5
        out = nodes.plan(state, rt)
        assert out["planner_interventions"] == 6

    def test_max_stages_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, limits={"max_stages": 2})
        state["completed"] = [{"id": "a"}, {"id": "b"}]
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "max_stages" in out["escalation_reason"]

    def test_aider_history_lands_in_the_attempt_directory(self, repo, tmp_path):
        # Not in the repository under test: Aider's scratch files fail the
        # scope gate there, and they are worth keeping as artifacts anyway.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "first", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        destination = rt.executor.history_dirs[-1]
        assert destination is not None
        assert repo not in destination.parents
        assert destination.is_dir()

    def test_the_wall_clock_budget_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, limits={"wall_clock_hours": 2})
        state["session_started_at"] = time.time() - 3 * 3600
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "wall_clock_hours" in out["escalation_reason"]

    def test_inside_the_wall_clock_budget_proceeds(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "first", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(
            repo, tmp_path, planner=planner, limits={"wall_clock_hours": 8}
        )
        state["session_started_at"] = time.time() - 3600
        assert nodes.plan(state, rt)["next_hop"] == "precheck"

    def test_the_deadline_reports_the_underlying_failure(self, repo, tmp_path):
        # Escalating for time must not lose why the stage was being revised.
        cfg, rt, state = make(repo, tmp_path, limits={"wall_clock_hours": 1})
        state = with_stage(state, rt)
        state["session_started_at"] = time.time() - 2 * 3600
        state["last_failure"] = {"layer": "tests", "summary": "3 specs red"}
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "3 specs red" in out["escalation_reason"]

    def test_a_missing_session_start_falls_back_to_the_run_start(self, repo, tmp_path):
        # A checkpoint written before this field existed must not read as a
        # session that began at the epoch and blow the budget instantly.
        cfg, rt, state = make(repo, tmp_path, limits={"wall_clock_hours": 8})
        state.pop("session_started_at", None)
        state["started_at"] = time.time() - 60
        assert nodes.plan(state, rt)["next_hop"] == "finalize"

    def test_deriving_a_first_stage_costs_no_intervention(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        assert nodes.plan(state, rt)["planner_interventions"] == 0


class TestRevision:
    def test_revise_keeps_the_stage_identity(self, repo, tmp_path):
        # So the report reads "stage 14 revision 2" rather than pretending
        # stages 14-16 were different work.
        planner = StubPlanner(
            [PlannerOutcome("revise", "too narrow", "e",
                            stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert out["revision"] == 1
        assert out["current"]["id"] == "extract"
        # stage_index is untouched — a revision is the same stage, not the next.
        assert "stage_index" not in out

    def test_extend_keeps_the_branch(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert "stage_branch" not in out

    def test_restart_discards_the_branch(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="restart")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert out["stage_branch"] is None
        assert out["stage_start_sha"] == ""

    def test_revision_clears_stale_feedback(self, repo, tmp_path):
        # It was written against an instruction that no longer applies.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        state["review_feedback"] = ["old rejection"]
        assert nodes.plan(state, rt)["review_feedback"] == []


class TestScopeQuarantine:
    """A scope violation must not throw away hours of correct work."""

    def test_adopted_paths_keep_their_work(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "widening", "e",
                            stage_fields=planned_stage(edit_files=["app.py", "wandered.py"]),
                            revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("in scope work\n")
        (repo / "wandered.py").write_text("adopted work\n")
        state["last_failure"] = {
            "layer": "scope", "summary": "s", "detail": "d",
            "out_of_scope_paths": ["wandered.py"], "failing_paths": [],
        }
        nodes.plan(state, rt)
        assert (repo / "wandered.py").read_text() == "adopted work\n"
        assert (repo / "app.py").read_text() == "in scope work\n"

    def test_unadopted_paths_are_reverted_but_the_rest_survives(self, repo, tmp_path):
        # The whole point: one unexpected spec file must not cost the stage.
        planner = StubPlanner(
            [PlannerOutcome("revise", "declining", "e",
                            stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("hours of good work\n")
        (repo / "wandered.py").write_text("should go\n")
        state["last_failure"] = {
            "layer": "scope", "summary": "s", "detail": "d",
            "out_of_scope_paths": ["wandered.py"], "failing_paths": [],
        }
        nodes.plan(state, rt)
        assert not (repo / "wandered.py").exists()
        assert (repo / "app.py").read_text() == "hours of good work\n"

    def test_no_reverting_when_there_was_no_scope_violation(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        state["last_failure"] = {"layer": "tests", "summary": "s", "detail": "d"}
        nodes.plan(state, rt)
        assert (repo / "app.py").read_text() == "work\n"


class TestPrecheck:
    def test_cuts_the_child_branch_from_the_project_tip(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state["current"] = planned_stage()
        out = nodes.precheck(state, rt)
        assert out["stage_branch"] == "proj-stage/000-extract"
        assert out["next_hop"] == "execute"

    def test_does_not_recut_an_existing_branch(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        pinned = state["stage_start_sha"]
        out = nodes.precheck(state, rt)
        assert "stage_start_sha" not in out or out["stage_start_sha"] == pinned

    def test_a_failed_precondition_goes_to_the_planner(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state["current"] = planned_stage(preconditions=["false"])
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["layer"] == "precondition"

    def test_the_precondition_failure_names_the_command(self, repo, tmp_path):
        # A mis-written precondition is unfixable by the planner, so the
        # escalation must eventually say which one never passed.
        cfg, rt, state = make(repo, tmp_path)
        state["current"] = planned_stage(preconditions=["test -f nope"])
        out = nodes.precheck(state, rt)
        assert "test -f nope" in out["last_failure"]["summary"]

    def test_failed_setup_escalates_to_a_human(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, setup_command="false")
        state["current"] = planned_stage()
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "setup"


class TestExecute:
    def test_a_successful_attempt_goes_to_verify(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        assert nodes.execute(state, rt)["next_hop"] == "verify"

    def test_writes_the_prompt_under_a_revision_scoped_path(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state["revision"] = 2
        nodes.execute(state, rt)
        assert (rt.paths.attempt_dir(0, "extract", 2, 0) / "prompt.md").exists()

    def test_feedback_reaches_the_prompt(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state["review_feedback"] = ["Wrong verb on the route."]
        nodes.execute(state, rt)
        assert "Wrong verb on the route." in ex.prompts[0]

    def test_executor_failure_retries_then_goes_to_the_planner(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, ok=False)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        assert nodes.execute(state, rt)["next_hop"] == "execute"
        state["verify_attempt"] = 3
        assert nodes.execute(state, rt)["next_hop"] == "plan"


class TestVerifyRouting:
    def test_pass_goes_to_review(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "review"

    def test_scope_violation_goes_to_the_planner_with_the_paths(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "wandered.py").write_text("x\n")
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["out_of_scope_paths"] == ["wandered.py"]

    def test_branch_identity_failure_escalates_to_a_human(self, repo, tmp_path):
        # A containment breach does not negotiate.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("x\n")
        rt.git.commit_all("work")
        rt.git.checkout("proj")
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "branch"

    def test_failing_tests_retry_then_go_to_the_planner(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, test_command="exit 1")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "execute"
        state["verify_attempt"] = 3
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "plan"
        assert "max_test_retries" in out["last_failure"]["summary"]

    def test_skips_review_when_the_stage_opts_out(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt, review=False)
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "advance"


class TestReviewGate:
    """Reviewer approval AND a green full suite, cheapest first."""

    def test_approval_plus_green_suite_advances(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        assert nodes.review(state, rt)["next_hop"] == "advance"

    def test_a_rejected_stage_never_pays_for_the_full_suite(self, repo, tmp_path):
        # The economic point: the reviewer is seconds and pennies, the suite is
        # minutes.
        marker = tmp_path / "suite-ran"
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(
            repo, tmp_path, reviewer=reviewer,
            full_test_command=f"touch {marker}",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        assert not marker.exists()

    def test_approved_but_red_suite_does_not_advance(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, full_test_command="exit 1")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "execute"

    def test_a_flaky_full_suite_does_not_block_a_good_stage(self, repo, tmp_path):
        # The full suite has far more surface for ordering flakes, and a flake
        # here wastes a planner intervention rather than an executor attempt.
        flag = tmp_path / "suite-flag"
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=f"if [ -f {flag} ]; then exit 0; else touch {flag}; exit 1; fi",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert out["flake_reruns_review_gate"] == 1

    def test_the_full_suite_can_be_switched_off_per_stage(self, repo, tmp_path):
        marker = tmp_path / "suite-ran-2"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt, full_suite_on_approval=False)
        (repo / "app.py").write_text("changed\n")
        assert nodes.review(state, rt)["next_hop"] == "advance"
        assert not marker.exists()

    def test_blocked_goes_to_the_planner_not_a_human(self, repo, tmp_path):
        reviewer = StubReviewer([ReviewOutcome(verdict="blocked", summary="instruction wrong")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["layer"] == "review"

    def test_rework_loops_with_feedback(self, repo, tmp_path):
        reviewer = StubReviewer(
            [ReviewOutcome(
                verdict="rework", summary="One problem.",
                issues=[Issue(severity="major", file="app.py", description="Fix it.")],
            )]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "execute"
        assert "Fix it." in out["review_feedback"][0]

    def test_exhausted_rework_goes_to_the_planner(self, repo, tmp_path):
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="still wrong")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        state["rework_attempt"] = 2
        out = nodes.review(state, rt)
        assert out["next_hop"] == "plan"
        assert "max_rework_retries" in out["last_failure"]["summary"]

    def test_rework_resets_the_tree_by_default(self, repo, tmp_path):
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("rejected\n")
        nodes.review(state, rt)
        assert (repo / "app.py").read_text() == "def hello():\n    return 1\n"

    def test_accumulates_reviewer_usage(self, repo, tmp_path):
        reviewer = StubReviewer(
            [ReviewOutcome(verdict="approved", summary="ok", usage=TokenUsage(100, 20, 80))]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["stage_usage"]["prompt_tokens"] == 100
        assert out["stage_usage"]["cached_tokens"] == 80


class TestAdvance:
    def test_squash_merges_to_the_project_branch(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        out = nodes.advance(state, rt)
        assert rt.git.current_branch() == "proj"
        assert "[extract]" in rt.git.commit_subject()
        assert out["completed"][0]["merge_sha"]

    def test_deletes_the_child_branch(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        nodes.advance(state, rt)
        assert not rt.git.branch_exists("proj-stage/000-extract")

    def test_lands_exactly_one_commit(self, repo, tmp_path):
        # Aider commits before it tests, so the child branch has red commits.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        before = rt.git.rev_parse("proj")
        for i in range(3):
            (repo / "app.py").write_text(f"attempt {i}\n")
            rt.git.commit_all(f"intermediate {i}")
        nodes.advance(state, rt)
        assert rt.git.rev_parse("proj~1") == before

    def test_records_history_and_returns_to_plan(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        state["revision"] = 1
        state["verify_attempt"] = 2
        out = nodes.advance(state, rt)
        entry = out["completed"][0]
        assert entry["revisions"] == 1
        assert entry["verify_retries"] == 2
        assert out["next_hop"] == "plan"
        assert out["current"] is None
        assert out["stage_index"] == 1

    def test_clears_per_stage_state(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        state["review_feedback"] = ["old"]
        out = nodes.advance(state, rt)
        assert out["review_feedback"] == []
        assert out["stage_branch"] is None


class TestFinalize:
    def test_green_suite_completes(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.finalize(state, rt)["status"] == "complete"

    def test_red_suite_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, full_test_command="exit 1")
        out = nodes.finalize(state, rt)
        assert out["next_hop"] == "escalate"
        assert "project branch tip" in out["escalation_reason"]


class TestEscalate:
    def test_records_the_failed_stage(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state["escalation_reason"] = "because"
        out = nodes.escalate(state, rt)
        assert out["status"] == "escalated"
        assert out["failed_stage_id"] == "extract"

    def test_does_not_append_to_completed_history(self, repo, tmp_path):
        # completed holds completions only; a stage that escalates, gets fixed,
        # and lands must appear once.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        assert "completed" not in nodes.escalate(state, rt)
