"""Node logic with stubbed planner, executor, and reviewer.

These are the decisions that make the loop behave: which failures route to the
executor, which to the planner, and which to a human; whether a scope violation
throws work away; and whether the expensive half of the merge gate runs only
when the cheap half passed.
"""

import json
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
from orchestrator.state import RunState, fresh_stage_fields, new_state

# The operator's pattern, as a real project would configure it.
RSPEC_PATTERN = r"^\s*rspec\s+'?\.?/?([^'\s\[:]+_spec\.rb)"


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
    cache_keys: list = field(default_factory=list)

    def review(self, messages, cache_key=None):
        self.cache_keys.append(cache_key)
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

    log: str = "executor log"
    dropped_reads: list = field(default_factory=list)

    def run_agent_stage(self, stage, prompt, history_dir=None):
        self.prompts.append(prompt)
        self.history_dirs.append(history_dir)
        self._apply()
        return ExecutionResult(
            ok=self.ok, log=self.log, timed_out=self.timed_out,
            dropped_reads=list(self.dropped_reads),
        )

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
        # As `cli.run` does it: measured against the base, read from the branch.
        plan_sha=rt.git.rev_parse("proj"),
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


def _digest(rt, state):
    """The fingerprint verify records when the full suite passes."""
    from orchestrator.verify import diff_digest

    return diff_digest(rt.git, state["stage_start_sha"])


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

    def test_planner_cached_tokens_are_recorded(self, repo, tmp_path):
        # The client extracts cache_read_input_tokens and it was being thrown
        # away, so the report could not show whether the planner's cacheable
        # prefix was working — which is the design's economic premise.
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "r", "e",
                    stage_fields=planned_stage(),
                    usage=PlannerUsage(12_000, 11_200, 300),
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["run_usage"]["planner_cached_tokens"] == 11_200

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

    def test_verify_hands_the_gate_what_it_needs_to_skip(self, repo, tmp_path):
        # End to end, because the isolated version of this test passed while the
        # feature did nothing: verify wrote `full_suite_digest`, the graph's
        # state schema did not declare it, the key was dropped in transit, and
        # the gate never saw it. Seeding the key by hand tests the gate's logic
        # and not the wiring, so this drives the real node and asserts on what
        # verify actually returns.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(
            repo, tmp_path,
            test_command=f"touch {marker}",
            full_test_command=f"touch {marker}",
            scoped_test_command=None,
        )
        state = with_stage(state, rt, test_paths=[])
        (repo / "app.py").write_text("changed\n")

        out = nodes.verify(state, rt)
        assert out["next_hop"] == "review"
        assert out["full_suite_digest"], (
            "verify ran the full suite and must fingerprint the tree it passed on"
        )
        from orchestrator.state import RunState

        assert "full_suite_digest" in RunState.__annotations__, (
            "the key must be declared in the state schema or the graph drops it"
        )

        marker.unlink()
        out = nodes.review({**state, **out}, rt)
        assert out["next_hop"] == "advance"
        assert not marker.exists(), "the gate re-ran a suite verify had just run"


class TestPlanDocumentsFollowTheProjectBranch:
    """Plan maintenance happens on the branch, and must not need a merge first.

    A migration branch lives for months. Its plan documents get restructured,
    folded and corrected on that branch the whole time, and `main` sees none of
    it until the end. Reading plan documents at `base_ref` meant the run could
    not see the plan it was executing.
    """

    def test_a_child_added_on_the_branch_is_still_protected(
        self, repo, tmp_path, run_git
    ):
        (repo / "PLAN.md").write_text("# Plan\n\nSee [the child](docs/child.md).\n")
        Git(repo).commit_all("plan, on main")

        run_git(repo, "checkout", "-qb", "proj")
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "child.md").write_text("# Child\n\nstep one\n")
        Git(repo).commit_all("a plan child, on the project branch only")

        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt, edit_files=["docs/**"])
        (repo / "docs" / "child.md").write_text("# Child\n\nrewritten\n")

        out = nodes.verify(state, rt)
        assert out["failure_layer"] == "scope"

        from orchestrator.state import RunState

        assert "plan_sha" in RunState.__annotations__, (
            "the key must be declared in the state schema or the graph drops it"
        )

    def test_a_suite_already_green_at_verify_is_not_run_again(self, repo, tmp_path):
        # Nothing mutates the tree between verify and this gate — the reviewer
        # reads a diff, it does not edit — so re-running the identical suite on
        # an identical tree buys no information. Measured on the first real
        # project: three stages, 584 seconds, and because the second run
        # re-rolls every order-dependent example it also produced two of the
        # night's flakes at the gate, where a flake is most expensive.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        state = {**state, "full_suite_digest": _digest(rt, state)}
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert not marker.exists()

    def test_a_changed_tree_still_pays_for_the_suite(self, repo, tmp_path):
        # The skip is keyed to what was actually tested. If anything moved, the
        # recorded pass proves nothing about the tree being merged.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        state = {**state, "full_suite_digest": "stale" * 8}
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert marker.exists()

    def test_a_scoped_verify_leaves_the_gate_to_do_its_job(self, repo, tmp_path):
        # The common case. Verify ran only the stage's own specs, so the full
        # suite has never been run on this tree and the gate is the only thing
        # standing between a scoped pass and a merge.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert marker.exists()

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

    def test_only_the_failing_files_are_re_run_at_the_gate(self, repo, tmp_path):
        # The whole point: on a 2,335-example suite the re-run was as likely to
        # trip over a different order-dependent example as to clear the first
        # one, so the stage was blamed for a property of the repository.
        log = tmp_path / "gate-reran.txt"
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=(
                "echo \"Failed examples:\"; "
                "echo \"rspec './spec/requests/checkout_spec.rb[1:1]' # c\"; exit 1"
            ),
            scoped_test_command=f"echo {{paths}} >> {log}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert out["flake_reruns_review_gate"] == 1
        assert log.read_text().strip() == "spec/requests/checkout_spec.rb"

    def test_the_flaky_files_are_named_for_the_report(self, repo, tmp_path):
        # Excusing a flake and not saying which file leaves the operator with a
        # count and nothing to fix. The list is the whole path back to a suite
        # that does not need this machinery.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=(
                "echo \"Failed examples:\"; "
                "echo \"rspec './spec/requests/checkout_spec.rb[1:1]' # c\"; exit 1"
            ),
            scoped_test_command="true {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["flaky_files"] == ["spec/requests/checkout_spec.rb"]

    def test_a_spec_the_stage_edited_can_still_be_excused(self, repo, tmp_path):
        """Ownership is deliberately not a factor.

        An earlier design refused to excuse a spec the stage had touched. But a
        file that passes whole and standalone has been proven green *including*
        the stage's edits to it, so what makes it fail in the group is a
        property of the suite — separate work, not this stage's to answer for.
        """
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=(
                "echo \"Failed examples:\"; "
                "echo \"rspec './spec/app_spec.rb[1:1]' # c\"; exit 1"
            ),
            scoped_test_command="true {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt, edit_files=["app.py", "spec/**"])
        (repo / "app.py").write_text("changed\n")
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "app_spec.rb").write_text("describe\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert out["flaky_files"] == ["spec/app_spec.rb"]

    def test_a_signalled_full_suite_stops_for_a_human(self, repo, tmp_path):
        # At the merge gate the stage has already been approved and its diff is
        # correct. Reading a killed suite as a rejection would send correct work
        # back for rework, and the rework would run against the same dead
        # environment.
        cfg, rt, state = make(repo, tmp_path, full_test_command="kill -9 $$")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "escalate"
        assert "signal" in out["escalation_reason"].lower()

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

    def test_what_it_lands_carries_no_trailing_whitespace(self, repo, tmp_path):
        # End to end because this is where it mattered: the strip is correct in
        # gitops and the commit is correct in gitops, and the run still died
        # because nothing called one before the other. A pre-commit hook
        # rejecting `git diff --cached --check` turned a landed stage into a
        # crash with a staged merge stranded on the project branch.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_bytes(b"kept   \nwork\t\n")
        nodes.advance(state, rt)
        assert rt.git.show_file("proj", "app.py") == "kept\nwork\n"

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


class TestOperatorPause:
    """Stopping cleanly, on request, without killing work in flight.

    Interrupting the process leaves a half-finished executor and a dirty tree.
    Checked at the same point as the budgets — before the next planner call —
    so the run stops between stages with everything landed and nothing pending.
    """

    def test_a_pause_flag_stops_the_run(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        rt.paths.pause_flag.write_text("paused by operator\n")
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "paused"

    def test_the_reason_says_nothing_is_wrong(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        rt.paths.pause_flag.write_text("")
        reason = nodes.plan(state, rt)["escalation_reason"]
        assert "resume" in reason.lower()
        assert "paused" in reason.lower()

    def test_no_flag_means_no_pause(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.plan(state, rt)["next_hop"] == "finalize"

    def test_the_planner_is_not_called_when_paused(self, repo, tmp_path):
        # The point is to stop before spending, not after.
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.paths.pause_flag.write_text("")
        nodes.plan(state, rt)
        assert planner.calls == []

    def test_a_red_full_suite_is_kept_on_disk(self, repo, tmp_path):
        """The most expensive failure in the loop, and nobody kept its output.

        A rejection here resets reviewer-approved work, so it is the one
        failure that most needs explaining afterwards. It was also the only
        command whose output went nowhere — when the flake gate silently
        stopped matching, the sole copy of the evidence was a file the next run
        truncated.
        """
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command="echo 'DIAGNOSTIC MARKER'; exit 1",
            scoped_test_command="true {paths}",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        logs = list(rt.paths.run_dir.glob("stages/*/full-suite.log"))
        assert logs, "the failing full suite should leave an artifact"
        assert "DIAGNOSTIC MARKER" in logs[0].read_text()

    def test_a_green_full_suite_writes_nothing(self, repo, tmp_path):
        # Nothing to diagnose, and the output is megabytes.
        cfg, rt, state = make(repo, tmp_path, full_test_command="echo fine")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        assert not list(rt.paths.run_dir.glob("stages/*/full-suite.log"))


class TestProgressGuardAfterAFlakyMergeGate:
    """End to end: does `full_suite` actually reach the progress guard?

    The exemption was added and unit-tested against `run_verify` directly,
    which proves the guard honours the flag but not that anything ever sets it.
    This drives the real sequence — approved, full suite red, rework, identical
    diff — through the nodes, because that is where it silently would not work.
    """

    def test_a_merge_gate_failure_is_recorded_as_full_suite(self, repo, tmp_path):
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command="exit 1",
            scoped_test_command="false {paths}",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["failure_layer"] == "full_suite"

    def test_the_identical_redo_then_survives_verify(self, repo, tmp_path):
        # The whole point: the reviewer approved this diff, the suite was red
        # for reasons elsewhere, and doing it again is the correct answer.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command="exit 1",
            scoped_test_command="false {paths}",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")

        first = nodes.verify(state, rt)
        state = {**state, **first}
        rejected = nodes.review(state, rt)
        state = {**state, **rejected}
        assert state["failure_layer"] == "full_suite"

        # rework_reset threw the work away; the executor reproduces it exactly.
        (repo / "app.py").write_text("changed\n")
        again = nodes.verify(state, rt)
        assert again.get("failure_layer") != "progress"
        assert again["next_hop"] in ("review", "advance")


class TestAFailureThatPredatesTheStage:
    """Whose failure is it?

    `adjudicate` answers "flake or real", and the gate treated every real
    failure as the stage's. It is not. Observed live: a stage renamed four
    macros in one controller, was approved by the reviewer three times with a
    byte-identical diff, and was sent back three times because a
    commission-report spec in a file it never touched started failing when the
    clock crossed into the 31st. The executor was scoped to that controller and
    could not have fixed the spec under any instruction, so the whole rework
    budget bought nothing. Sixteen minutes, three Aider runs, three reviews,
    three full suites.

    One scoped run against the base tree separates the two cases.
    """

    FAILED = (
        "echo 'Failed examples:'; "
        "echo \"rspec './spec/a_spec.rb[1:1]' # boom\"; exit 1"
    )

    def _land(self, run_git, repo, text="changed\n", extra=None):
        """Commit the stage's work, as the executor's own commit would."""
        (repo / "app.py").write_text(text)
        if extra:
            (repo / extra).write_text("x\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "stage work")

    def test_it_goes_to_the_planner_rather_than_the_executor(
        self, repo, tmp_path, run_git
    ):
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        self._land(run_git, repo)
        out = nodes.review(state, rt)

        assert out["next_hop"] == "plan", (
            "the executor is scoped to its own files and cannot repair a spec "
            "that was already red; reworking it spends attempts to learn nothing"
        )
        assert out["failure_layer"] == "full_suite"
        assert out["last_failure"]["failing_paths"] == ["spec/a_spec.rb"]
        assert "predates" in out["last_failure"]["detail"]

    def test_a_failure_the_stage_caused_still_goes_to_rework(
        self, repo, tmp_path, run_git
    ):
        # The guard has to stay narrow. Green at the base means the stage did
        # break it, and that is exactly what rework is for.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            # Green at the base, red at the tip: the stage committed the file.
            scoped_test_command="echo {paths} >/dev/null; test ! -f broke.txt",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        self._land(run_git, repo, extra="broke.txt")
        out = nodes.review(state, rt)

        assert out["next_hop"] == "execute"
        assert out["failure_layer"] == "full_suite"

    def test_the_tree_is_left_where_it_was_found(self, repo, tmp_path, run_git):
        # The baseline run reverts to the base and back. Getting the second
        # half wrong would silently delete an approved stage's work.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        self._land(run_git, repo, text="the approved work\n")
        head = rt.git.head_sha()

        nodes.review(state, rt)

        assert (repo / "app.py").read_text() == "the approved work\n"
        assert rt.git.head_sha() == head

    def test_setup_runs_on_both_sides_of_the_revert(self, repo, tmp_path, run_git):
        """The environment follows the tree, or the answer is about the wrong one.

        Reverting to the base with containers still built for the tip tests the
        base tree against the stage's environment — and it fails in the
        direction that produces a false "pre-existing", which routes correct
        work to the planner. The restoring run is not optional either: leaving
        the environment at the base is the same mistake pointed the other way.
        """
        ran = tmp_path / "setup-ran"
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
            setup_command=f"echo up >> {ran}",
        )
        state = with_stage(state, rt)
        self._land(run_git, repo)
        out = nodes.review(state, rt)

        assert out["next_hop"] == "plan"
        assert ran.read_text().count("up") == 2, (
            "once at the base before running it, once after restoring the tree"
        )

    def test_a_base_that_will_not_build_answers_nothing(self, repo, tmp_path, run_git):
        # "Setup failed at the base" is not evidence that the specs were
        # already red. Blaming the tree for it would route a stage that may
        # well be at fault to the planner instead of to rework.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
            setup_command="exit 3",
        )
        state = with_stage(state, rt)
        self._land(run_git, repo)
        out = nodes.review(state, rt)

        assert out["next_hop"] == "execute"

    def test_an_uncommitted_tree_is_never_reset(self, repo, tmp_path):
        """Restoring by sha restores what is committed.

        So on a dirty tree the check declines to run rather than destroying
        work the caller may be about to rework *forward* from. The executor
        commits, so this is the unusual case — but it is the one where being
        wrong loses work rather than time.
        """
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
            rework_reset=False,
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("uncommitted\n")
        out = nodes.review(state, rt)

        assert (repo / "app.py").read_text() == "uncommitted\n"
        assert out["next_hop"] == "execute"


class TestPlannerArtifactRecordsCacheWrites:
    """The write premium has to be visible per call, not just collected.

    `PlannerUsage` gained `cache_write_tokens` when Anthropic's disjoint counts
    were normalised, but `planner.json` still recorded only prompt, cached and
    completion. Anthropic bills a cache write above base rate, so a run that
    writes the prefix every call and never reads it is more expensive than not
    caching — and per-call is the only place that is diagnosable, since the
    run totals average it away.
    """

    def test_the_artifact_carries_the_write_count(self, repo, tmp_path):
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "project_complete", "done", "e",
                    usage=PlannerUsage(
                        prompt_tokens=5_000,
                        cached_tokens=1_000,
                        completion_tokens=400,
                        cache_write_tokens=3_600,
                    ),
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        import json

        written = json.loads(
            next(rt.paths.run_dir.glob("stages/*/planner.json")).read_text()
        )
        assert written["usage"]["cache_write_tokens"] == 3_600


class TestPlannerArtifactRecordsWhatItLookedAtAndSaid:
    """Silence has to be distinguishable from absence.

    `planner.json` recorded the verdict and the stage but neither the reads nor
    the plan notes, so a stage that produced no observations looked exactly
    like a feature that never ran — the key was simply missing either way.
    That matters most for the case worth auditing: the planner read the
    repository, compared it to the plan, and found nothing to correct. That is
    the plan being accurate, and it is evidence, not an empty field.
    """

    def _artifact(self, rt):
        import json

        return json.loads(
            next(rt.paths.run_dir.glob("stages/*/planner.json")).read_text()
        )

    def test_reads_and_notes_are_recorded(self, repo, tmp_path):
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "project_complete", "done", "e",
                    tool_calls=["search(render text: in app) -> 7 line(s)"],
                    plan_notes=[
                        {
                            "plan_step": "item 17",
                            "observation": "7 sites remain, not 24",
                            "supersedes": "checklist says 24",
                        }
                    ],
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        written = self._artifact(rt)
        assert written["tool_calls"] == ["search(render text: in app) -> 7 line(s)"]
        assert written["plan_notes"][0]["observation"] == "7 sites remain, not 24"

    def test_both_keys_exist_when_there_was_nothing_to_say(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        nodes.plan(state, rt)
        written = self._artifact(rt)
        assert written["tool_calls"] == []
        assert written["plan_notes"] == []


class TestPlanNotesSurviveFromDerivationToLanding:
    """The planner emits a note; the commit has to contain it.

    Driven end to end, because the isolated halves both passed while the
    feature did nothing. `plan` accumulated the notes into its returned state
    and `advance` wrote whatever it was handed — but `plan`'s return spread
    `fresh_stage_fields()` *after* that state, and the reset zeroes
    `pending_plan_notes`. So every note the planner produced while deriving a
    stage was discarded microseconds later, and `advance` had nothing to write.

    Live for two stages before anyone noticed, because the only visible symptom
    is a commit that quietly lacks a progress entry — and the artifact test
    proves the planner *said* something, not that it survived.
    """

    A_NOTE = {
        "plan_path": "PLAN.md",
        "anchor": "24 sites across 9 controllers.",
        "observation": "This sweep is complete; 0 sites remain in app/controllers.",
    }

    def test_the_note_survives_stage_derivation(self, repo, tmp_path):
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "next", "e",
                    stage_fields=planned_stage(),
                    plan_notes=[self.A_NOTE],
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["pending_plan_notes"] == [self.A_NOTE], (
            "the per-stage reset must not discard notes the planner just wrote"
        )

    def test_the_note_lands_in_the_stage_commit(self, repo, tmp_path, run_git):
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "next", "e",
                    stage_fields=planned_stage(),
                    plan_notes=[self.A_NOTE],
                )
            ]
        )
        # A real plan document, committed before the run measures anything, so
        # the note's line reference has something to resolve against.
        (repo / "PLAN.md").write_text(
            "# The plan\n\n## Render sweeps\n24 sites across 9 controllers.\n"
        )
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "plan")
        cfg, rt, state = make(
            repo, tmp_path, planner=planner, plan_addendum_path="docs/progress_log.md"
        )
        state = {**state, **nodes.plan(state, rt)}
        state = with_stage(state, rt, **planned_stage())
        (repo / "app.py").write_text("stage work\n")
        nodes.advance(state, rt)

        log = repo / "docs" / "progress_log.md"
        assert log.exists(), "the stage landed without recording what it did"
        text = log.read_text()
        assert "0 sites remain" in text
        # The heading is lifted from the cited document at `plan_sha`, so the
        # reference has to survive the same trip the observation does — and be
        # resolvable against the commit once it arrives.
        assert "## Render sweeps — `PLAN.md#L4`" in text, (
            "the quote must reach the writer and be located in the plan"
        )
        # Inside the stage's own commit, not trailing after it.
        assert "progress_log.md" in rt.git._out("show", "--stat", "HEAD")


class TestExecutorFeedbackIsBounded:
    """A 98KB executor log must not become the next prompt.

    CommandRunner used to cap output at 20,000 characters, which bounded
    `result.log` incidentally. Raising that cap so the flake gate could see a
    whole test run removed the bound, and a real attempt produced 97,883
    characters of Aider transcript that went verbatim into the next executor
    prompt and into planner feedback.
    """

    def test_a_huge_executor_log_is_clipped_into_feedback(self, repo, tmp_path):
        executor = StubExecutor(repo=repo, ok=False)
        executor.log = "X" * 200_000
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        joined = "".join(out.get("review_feedback") or [])
        assert len(joined) < 20_000, "feedback must be bounded"
        assert "truncated" in joined

    def test_the_artifact_keeps_the_whole_log(self, repo, tmp_path):
        # Bounded where it is used, whole where it is read afterwards.
        executor = StubExecutor(repo=repo, ok=False)
        executor.log = "Y" * 200_000
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        logs = list(rt.paths.run_dir.glob("stages/*/executor.log"))
        assert logs and len(logs[0].read_text()) > 150_000


class TestStuckWithoutLanding:
    """Three planner passes with nothing landing, and the run stops.

    A flat global cap needs a stage count nobody has: the orchestrator's stages
    are not the plan document's stages, and the planner derives them as it
    goes. An allowance that accrues per landed stage fixes that but builds a
    reserve, which then gets spent all at once on the very stage it should have
    caught.

    Consecutive passes without a landing measures the thing directly. A run
    that keeps landing work can continue indefinitely — bounded by the wall
    clock, not by a number picked in advance. A run that has been round the
    planner three times with nothing to show for it is stuck, and more
    interventions will not unstick it.
    """

    def test_three_interventions_without_a_landing_stops_the_run(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "again", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, limits={"max_interventions_without_landing": 3})
        state = with_stage(state, rt)
        state["interventions_since_landing"] = 3
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "without landing" in out["escalation_reason"].lower()

    def test_a_landing_resets_the_counter(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state["interventions_since_landing"] = 2
        (repo / "app.py").write_text("changed\n")
        out = nodes.advance(state, rt)
        assert out["interventions_since_landing"] == 0

    def test_an_intervention_increments_it(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "redraw", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert out["interventions_since_landing"] == 1

    def test_a_run_that_keeps_landing_is_not_stopped(self, repo, tmp_path):
        # Two interventions, then a landing, then two more: never three in a row.
        cfg, rt, state = make(repo, tmp_path, limits={"max_interventions_without_landing": 3})
        state = with_stage(state, rt)
        state["interventions_since_landing"] = 2
        (repo / "app.py").write_text("changed\n")
        landed = nodes.advance(state, rt)
        assert landed["interventions_since_landing"] == 0


class TestStageCostOutlivesTheRun:
    """Driven through advance, because the halves passing proves nothing.

    Twice today a value was computed correctly, written correctly, and lost in
    transit — `full_suite_digest` to an undeclared schema key, `plan_notes` to
    a reset applied after them. The cost figure takes the same journey, so it
    gets the same end-to-end test rather than a unit test of the writer.
    """

    def test_a_landed_stage_records_what_it_cost(self, repo, tmp_path):
        from orchestrator.planner import recent_stage_costs

        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "executor_context_tokens": 13_000}

        nodes.advance(state, rt)

        costs = recent_stage_costs(rt.project.project_dir)
        assert len(costs) == 1
        assert costs[0]["context_tokens"] == 13_000
        # Keyed by something that still resolves once the branch is gone.
        assert rt.git.commit_subject().startswith("[extract]")
        assert costs[0]["merge_sha"]

    def test_a_stage_with_no_measurement_records_nothing(self, repo, tmp_path):
        from orchestrator.planner import recent_stage_costs

        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        nodes.advance(state, rt)
        assert recent_stage_costs(rt.project.project_dir) == []


class TestTheReadBudgetIsToldToThePlanner:
    """The planner declares `read_files`; the tool silently cuts the tail.

    Observed live: four consecutive stages each asked for one reference file
    too many — `user.rb`, `schedule.rb`, `item.rb`, then a form template — and
    each time the executor worked without a file the instruction went on to
    reason about. Nothing failed, which is what makes it worth fixing: the
    planner was choosing blind and had no way to learn it.

    Same shape as the executor-token gap before it. A measurement the tool has
    and the planner does not is a decision made on a guess.
    """

    def test_it_survives_from_the_executor_to_the_planners_history(
        self, repo, tmp_path
    ):
        """End to end, because every part of this worked in isolation before.

        Three defects this session were values computed correctly, written
        correctly, and lost in transit — dropped by a schema that did not
        declare them, or by a reset spread over the top of them. A unit test
        on each end would have passed for all three.
        """
        executor = StubExecutor(
            repo=repo,
            edits=[("app.py", "changed\n")],
            dropped_reads=["app/models/user.rb"],
        )
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)

        state = {**state, **nodes.execute(state, rt)}
        assert state["withheld_reads"] == ["app/models/user.rb"], (
            "the execute node must carry what the executor withheld"
        )

        state = {**state, **nodes.verify(state, rt)}
        state = {**state, **nodes.review(state, rt)}
        state = {**state, **nodes.advance(state, rt)}
        landed = state["completed"][-1]
        assert landed["withheld_reads"] == ["app/models/user.rb"], (
            "and the landed stage must keep it, or the planner never sees it"
        )

        nodes.plan(state, rt)
        prompt = "\n".join(
            m["content"] if isinstance(m["content"], str) else str(m["content"])
            for m in rt.planner.calls[-1]
        )
        assert "app/models/user.rb" in prompt
        assert "max_read_lines" in prompt

    def test_it_is_declared_on_the_state_schema(self, repo, tmp_path):
        # The graph drops keys the schema does not know. `full_suite_digest`
        # shipped without this line once: written every time, discarded every
        # time, and silently never used.
        assert "withheld_reads" in RunState.__annotations__

    def test_it_does_not_leak_into_the_next_stage(self, repo, tmp_path):
        # Carried forward it would report a withholding the next stage never
        # suffered, and the planner would trim a reference list that fits.
        assert fresh_stage_fields()["withheld_reads"] == []

    def test_a_stage_that_fit_says_nothing(self, repo, tmp_path):
        # Rendered as an empty list it would read as a budget problem with no
        # files, which is worse than silence.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert "withheld_reads" not in out


class TestTheProgressLogIsSentLive:
    """The one plan document that must not be frozen.

    Freezing the plan is right: a run should not have its instructions change
    underneath it mid-flight. Applying that to the progress log made it
    useless. Measured on the first long run — the snapshot held 6,680 bytes
    while the file on disk had reached 114,554, so the planner was being shown
    6% of the record of what had been done, and the prompt's instruction to
    fetch the rest with `read_file` was taken twice in forty-nine derivations.

    The log was only frozen because it is reachable by a markdown link from the
    plan root, not because anyone decided progress should be immutable.
    """

    def _rt(self, repo, tmp_path, log="## Landed\n\nzero sites remain\n"):
        cfg, rt, state = make(
            repo, tmp_path, plan_addendum_path="docs/progress_log.md"
        )
        rt._plan = PlanTree(
            root=PlanDocument(path="PLAN.md", content="# The plan"),
            children=[
                PlanDocument(path="docs/progress_log.md", content="stale"),
                PlanDocument(path="runbook.md", content="static"),
            ],
        )
        if log is not None:
            (repo / "docs").mkdir(exist_ok=True)
            (repo / "docs" / "progress_log.md").write_text(log)
        return cfg, rt, state

    def test_the_planner_gets_what_is_on_disk_now(self, repo, tmp_path):
        cfg, rt, state = self._rt(repo, tmp_path)
        payload = rt.live_plan.as_prompt_payload()
        assert "zero sites remain" in payload
        assert "stale" not in payload

    def test_the_snapshot_itself_is_not_mutated(self, repo, tmp_path):
        # `rt.plan` is held for the run and handed to the reviewer. Swapping a
        # document in place would change what the reviewer judges against.
        cfg, rt, state = self._rt(repo, tmp_path)
        rt.live_plan
        assert "stale" in rt.plan.as_prompt_payload()

    def test_the_reviewer_still_judges_against_the_frozen_plan(
        self, repo, tmp_path
    ):
        # Progress is not evidence about whether a diff did what it was asked.
        cfg, rt, state = self._rt(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        sent = "\n".join(str(m) for m in rt.reviewer.cache_keys)
        assert "zero sites remain" not in sent

    def test_a_log_absent_from_the_snapshot_is_added(self, repo, tmp_path):
        # A project whose plan root never links the log would otherwise never
        # show the planner any progress at all.
        cfg, rt, state = self._rt(repo, tmp_path)
        rt._plan = PlanTree(root=PlanDocument(path="PLAN.md", content="# Plan"))
        assert "zero sites remain" in rt.live_plan.as_prompt_payload()

    def test_a_missing_file_falls_back_rather_than_failing(self, repo, tmp_path):
        # A planner call is far too expensive to lose over a progress file that
        # has not been written yet.
        cfg, rt, state = self._rt(repo, tmp_path, log=None)
        assert "stale" in rt.live_plan.as_prompt_payload()

    def test_it_still_sinks_to_the_end(self, repo, tmp_path):
        # It is now both live and the largest growing document, so its position
        # in the cached prefix matters more than before, not less.
        cfg, rt, state = self._rt(repo, tmp_path)
        payload = rt.live_plan.as_prompt_payload(last="docs/progress_log.md")
        assert payload.index("static") < payload.index("zero sites remain")


class TestTheWithoutLandingBudgetIsTerminalAndSaysSo:
    """Hitting it kills the run, and the message has to admit that.

    The check runs before the planner is called, and the counter only clears
    when a stage lands — so a run that hits it re-escalates on every resume
    with nothing having run. That terminality is deliberate: a budget an
    operator can clear by re-running is not a budget, and resume-in-a-loop is
    the exact failure it guards against.

    What was wrong was the silence. Every other escalation means "fix it and
    resume", resume works, and nothing distinguished this one — so the natural
    next move spent a preflight and a container start to print the identical
    message.
    """

    def _stuck(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        return rt, {**state, "interventions_since_landing": 3}

    def test_it_escalates(self, repo, tmp_path):
        rt, state = self._stuck(repo, tmp_path)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"

    def test_it_says_resume_will_not_help(self, repo, tmp_path):
        rt, state = self._stuck(repo, tmp_path)
        reason = nodes.plan(state, rt)["escalation_reason"]
        assert "Resume alone will not clear this" in reason

    def test_it_names_every_way_out(self, repo, tmp_path):
        # A dead end that does not say which doors exist sends the operator to
        # the one door that is locked.
        rt, state = self._stuck(repo, tmp_path)
        reason = nodes.plan(state, rt)["escalation_reason"]
        assert "--reset-progress-budget" in reason
        assert "max_interventions_without_landing" in reason
        assert "fresh run" in reason

    def test_the_planner_is_never_called(self, repo, tmp_path):
        # The whole point: it stops before spending anything.
        rt, state = self._stuck(repo, tmp_path)
        nodes.plan(state, rt)
        assert rt.planner.calls == [], "the budget must stop the call, not judge it"
class TestResumingIsConsumedNotRemembered:
    """`resuming` means "this step", not "this run has been resumed once".

    Nothing cleared it. `cli.resume` set it true and it stayed true, so every
    later reader saw a run that was permanently mid-resume. The progress layer
    treats it as a hard bypass:

        if ctx.resuming:
            return None

    so one resume disabled "the attempt reproduced the previous diff exactly"
    for the rest of the run. Observed: a stage produced byte-identical diffs on
    two attempts, both rejected for the same reason, and the guard whose whole
    job is to redraw such a stage never fired — the repetition was put down to
    a stuck reviewer.
    """

    def test_precheck_consumes_it(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state = {**state, "resuming": True, "stage_branch": None}
        assert nodes.precheck(state, rt)["resuming"] is False

    def test_verify_consumes_it(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.verify({**state, "resuming": True}, rt)
        assert out["resuming"] is False

    def test_the_progress_guard_works_again_on_the_next_attempt(
        self, repo, tmp_path
    ):
        """The behaviour the leak suppressed.

        A resumed run verifies once under the exemption — re-entering at verify
        is re-checking a human's fix, not repeating an attempt — and after that
        an identical diff must route to the planner as it always did.
        """
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")

        first = nodes.verify({**state, "resuming": True}, rt)
        state = {**state, **first}
        assert state["resuming"] is False, "the exemption is spent"

        # The executor reproduces the previous diff exactly.
        again = nodes.verify(state, rt)
        assert again.get("failure_layer") == "progress", (
            "with the flag consumed, an identical redo is caught again"
        )
        assert again["next_hop"] == "plan"


class TestAnUnappliedEditOnAChangedTree:
    """The editor reports blocks it could not apply — including redundant ones.

    Aider names every SEARCH block that did not match, and one reason a block
    does not match is that the work is already there: verbatim, "the REPLACE
    lines are already in Gemfile!". A model that emits one good block and two
    redundant ones lands the change and is recorded as having produced nothing.

    Observed twice on one stage. The gem removal committed its edit, was
    retried, committed it again, and spent ten to fifteen minutes an attempt
    redoing finished work — the first time costing the stage its entire budget
    and producing an escalation whose stated cause was wrong.

    So the tree is asked, not the editor's account of itself.
    """

    def _executor(self, repo, edits):
        ex = StubExecutor(repo=repo, edits=edits)
        ex.ok = False
        return ex

    def test_a_changed_tree_goes_to_verify(self, repo, tmp_path):
        executor = self._executor(repo, [("app.py", "the edit landed\n")])
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)

        def unapplied(stage, prompt, history_dir=None):
            executor._apply()
            return ExecutionResult(ok=False, log="already in Gemfile", unapplied_edit=True)

        executor.run_agent_stage = unapplied
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "verify", (
            "the gates judge a tree better than the editor judges its own blocks"
        )

    def test_an_untouched_tree_still_fails(self, repo, tmp_path):
        # The genuine case: nothing applied, nothing to verify. Unchanged.
        executor = StubExecutor(repo=repo, edits=[])
        executor.run_agent_stage = lambda *a, **k: ExecutionResult(
            ok=False, log="no blocks matched", unapplied_edit=True
        )
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "execute"
        assert "could not apply" in " ".join(out["review_feedback"])

    def test_a_timeout_on_a_dirty_tree_still_fails(self, repo, tmp_path, run_git):
        """Killed mid-write. The edit is half applied and uncommitted.

        "Was it killed" is the wrong discriminator — the editor commits after
        applying, so a kill mid-write leaves the tree dirty and a kill while it
        churns on redundant blocks leaves it clean with commits ahead.
        """
        executor = StubExecutor(repo=repo, edits=[("app.py", "half\n")])

        def timed_out(stage, prompt, history_dir=None):
            executor._apply()  # writes, does not commit
            return ExecutionResult(ok=False, log="killed", timed_out=True)

        executor.run_agent_stage = timed_out
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "execute"
        assert "timed out" in " ".join(out["review_feedback"])

    def test_a_timeout_on_committed_work_goes_to_verify(
        self, repo, tmp_path, run_git
    ):
        """Killed while churning on blocks it had already applied.

        Observed on two consecutive stages: the edit landed, the editor
        committed it, and the model kept re-issuing blocks for work already
        done until the 900s kill. Three attempts and forty-five minutes went on
        redoing finished work.
        """
        executor = StubExecutor(repo=repo, edits=[])

        def committed_then_hung(stage, prompt, history_dir=None):
            (repo / "app.py").write_text("the edit landed\n")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-qm", "editor's own commit")
            return ExecutionResult(ok=False, log="already applied", timed_out=True)

        executor.run_agent_stage = committed_then_hung
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "verify", (
            "a clean tree with commits ahead is finished work, not a half edit"
        )


class TestTheOpeningFailureOutlivesItsConsequences:
    """The first failure of a retry sequence is the diagnosis.

    Live, on stage 130 of a 129-stage run: an `assert_select` assertion failed
    in one spec, the executor reworked twice, aider hit its 900s timeout, and
    the no-progress guard sent the stage to the planner carrying only "the
    attempt reproduced the previous diff exactly". The planner redrew the stage
    knowing nothing about the assertion, and the redraw failed the same way.

    A previous incident was the same shape and cost thirty-five minutes: a
    legible `ArgumentError` naming a file and line became "the executor timed
    out", and the planner reasoned correctly from the wrong failure.

    So the sequence keeps its first failure as well as its last. Not a list —
    the ones in between are the same consequence repeated, and the planner
    prompt is not the place to pay for them.
    """

    def _rt(self, repo, tmp_path, **over):
        _, rt, state = make(repo, tmp_path, **over)
        return rt, state

    def test_the_first_failure_is_recorded_when_the_executor_retries(
        self, repo, tmp_path
    ):
        rt, state = self._rt(repo, tmp_path)
        out = nodes._retry_or_plan(
            state, rt, "tests", "the suite failed", "feedback", "assert_select failed"
        )
        assert out["next_hop"] == "execute", "still within the retry budget"
        assert out["opening_failure"]["summary"] == "the suite failed"
        assert out["opening_failure"]["detail"] == "assert_select failed"

    def test_a_later_failure_does_not_overwrite_it(self, repo, tmp_path):
        rt, state = self._rt(repo, tmp_path)
        first = nodes._retry_or_plan(
            state, rt, "tests", "the suite failed", "f", "assert_select failed"
        )
        state = {**state, **first}
        later = nodes._planner_failure(
            state, "progress", "the attempt reproduced the previous diff", "d"
        )
        assert "opening_failure" not in later or later["opening_failure"] == first[
            "opening_failure"
        ], "the diagnosis is not replaced by its consequence"

    def test_the_planner_receives_both(self, repo, tmp_path):
        rt, state = self._rt(repo, tmp_path)
        state = {
            **state,
            **nodes._retry_or_plan(
                state, rt, "tests", "the suite failed", "f", "assert_select failed"
            ),
        }
        out = nodes._planner_failure(
            state, "progress", "reproduced the previous diff", "no progress"
        )
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["summary"] == "reproduced the previous diff"
        assert state["opening_failure"]["summary"] == "the suite failed"

    def test_a_failure_that_goes_straight_to_the_planner_opens_the_sequence(
        self, repo, tmp_path
    ):
        # Nothing preceded it, so it is both the diagnosis and the consequence.
        rt, state = self._rt(repo, tmp_path)
        out = nodes._planner_failure(state, "scope", "edited outside scope", "d")
        assert out["opening_failure"]["summary"] == "edited outside scope"

    def test_a_revision_clears_it(self, repo, tmp_path):
        # A redrawn stage is a different instruction; the old diagnosis is
        # about work that no longer exists.
        from orchestrator.state import fresh_revision_fields

        assert fresh_revision_fields()["opening_failure"] is None

    def test_a_landed_stage_clears_it(self):
        assert fresh_stage_fields()["opening_failure"] is None

    def test_the_state_schema_declares_it(self):
        # Four defects have been values written correctly and dropped by a
        # schema that did not know the key.
        from orchestrator.state import RunState as Schema

        assert "opening_failure" in Schema.__annotations__

    def test_it_reaches_the_planner_prompt(self, repo, tmp_path):
        """Node to state to prompt, not the endpoints separately.

        Four defects have been values computed correctly, written correctly,
        and lost in transit — dropped by a schema that did not declare the key,
        or zeroed by a reset spread over the top of them. Both are live risks
        here, so the journey is the test.
        """
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)

        # The diagnosis: a real assertion failure, which retries.
        state = {
            **state,
            **nodes._retry_or_plan(
                state,
                rt,
                "tests",
                "the suite failed",
                "feedback for the executor",
                "assert_select('a[href=...]', 'Add Personalization') failed",
                failing_paths=["spec/features/order_funnel_add_item_spec.rb"],
            ),
        }
        # The consequence: retries stop changing anything.
        state = {
            **state,
            **nodes._planner_failure(
                state,
                "progress",
                "the attempt reproduced the previous diff exactly",
                "retrying costs another review for the same result",
            ),
        }

        nodes.plan(state, rt)
        prompt = "".join(
            block["text"]
            for message in planner.calls[0]
            for block in message["content"]
            if block.get("type") == "text"
        )
        assert "Add Personalization" in prompt, "the diagnosis reached the planner"
        assert "order_funnel_add_item_spec.rb" in prompt
        assert "reproduced the previous diff exactly" in prompt


class TestPrecheckRefusesToBuildOnSomebodyElsesChanges:
    """Between stages the tree is clean, because `advance` just committed.

    A dirty tree at precheck means something wrote outside the pipeline — a
    crash between `merge --squash` and `commit`, an editor left open, a human
    mid-edit. Cutting a stage branch over it sweeps those files into the next
    stage's diff, where the scope guard reports them as the executor editing
    out of scope. The executor did nothing of the kind, and the stage pays a
    retry for it.

    Not on a resume. Preflight exempts resume from its own clean-tree check for
    a reason that applies here exactly: a run is resumed because a human just
    fixed something, and that fix is normally uncommitted. Guarding it would
    make every escalation unrecoverable.
    """

    def _dirty(self, repo):
        (repo / "app.py").write_text("someone was editing this\n")

    def test_a_dirty_tree_between_stages_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": False}
        self._dirty(repo)
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "workspace"

    def test_it_names_the_files(self, repo, tmp_path):
        # "the tree is dirty" without the paths sends the operator to run the
        # command themselves.
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": False}
        self._dirty(repo)
        assert "app.py" in nodes.precheck(state, rt)["escalation_reason"]

    def test_a_clean_tree_proceeds(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": False}
        assert nodes.precheck(state, rt)["next_hop"] == "execute"

    def test_a_resume_is_exempt(self, repo, tmp_path):
        # The human's fix is the dirt.
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": True}
        self._dirty(repo)
        assert nodes.precheck(state, rt)["next_hop"] == "execute"

    def test_the_resume_re_enters_at_precheck(self):
        # Not verify: the check runs before a branch is cut, so there is no
        # stage branch to diff against. Back to precheck, which re-checks.
        from orchestrator.state import resume_entry_point

        assert (
            resume_entry_point(
                {
                    "resuming": True,
                    "failure_layer": "workspace",
                    "current": {"id": "s"},
                    "stage_has_work": False,
                }
            )
            == "precheck"
        )

    def test_a_revision_is_exempt(self, repo, tmp_path):
        # A restart re-enters precheck without passing through `advance`, and
        # `advance` is what commits. An uncommitted attempt in the tree is the
        # ordinary state of a redraw, so guarding here would escalate every one.
        cfg, rt, state = make(repo, tmp_path)
        state = {
            **state,
            "current": Stage(**planned_stage()).model_dump(),
            "resuming": False,
            "revision": 2,
        }
        self._dirty(repo)
        assert nodes.precheck(state, rt)["next_hop"] == "execute"

    def test_a_stage_already_holding_a_branch_is_exempt(self, repo, tmp_path):
        # Mid-stage, for the same reason.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state = {**state, "resuming": False}
        self._dirty(repo)
        assert nodes.precheck(state, rt)["next_hop"] == "execute"


class TestTheRejectedAnswerReachesTheArtifact:
    """A field on the dataclass that never reaches the file is worth nothing.

    `planner.json` is what anyone actually opens afterwards. The rejected
    answer exists to be read there, so the test is the journey — planner client
    to node to file — not the dataclass.
    """

    def test_planner_json_carries_the_rejected_answer(self, repo, tmp_path):
        rejected = {"verdict": "revise", "reasoning": "narrow it", "stage": None}
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "blocked",
                    "the planner returned 'revise' without a stage spec",
                    "e",
                    failed=True,
                    raw=rejected,
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)

        written = json.loads(
            (
                rt.paths.attempt_dir(state.get("stage_index", 0), "plan", 0, 0)
                / "planner.json"
            ).read_text()
        )
        assert written["rejected_answer"] == rejected
        assert written["client_failure"] is True

    def test_an_accepted_answer_records_it_as_null(self, repo, tmp_path):
        # Recorded either way. An absent key cannot be told apart from a
        # feature that never ran — the same reason tool_calls is always
        # written, even empty.
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "r", "e", stage_fields=planned_stage()
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        written = json.loads(
            (
                rt.paths.attempt_dir(state.get("stage_index", 0), "plan", 0, 0)
                / "planner.json"
            ).read_text()
        )
        assert "rejected_answer" in written
        assert written["rejected_answer"] is None
