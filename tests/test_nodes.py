"""Node logic with stubbed planner, executor, and reviewer.

These are the decisions that make the loop behave: which failures route to the
executor, which to the planner, and which to a human; whether a scope violation
throws work away; and whether the expensive half of the merge gate runs only
when the cheap half passed.
"""

import json
import time

import pytest
from dataclasses import dataclass, field

from orchestrator import nodes
from orchestrator.commands import CommandRunner
from orchestrator.config import Stage, parse_config
from orchestrator.executor import ExecutionResult
from orchestrator.gitops import Git, GitError
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

    messages: list = field(default_factory=list)

    def review(self, messages, cache_key=None):
        self.cache_keys.append(cache_key)
        self.messages.append(messages)
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
        # Through the same parser the real executor uses, not a field set by
        # hand: the value's journey starts in Aider's output, and a stub that
        # skipped that would pin the half of the trip that never broke.
        from orchestrator.executor import cache_tokens_from_log, cost_from_log

        return ExecutionResult(
            ok=self.ok, log=self.log, timed_out=self.timed_out,
            dropped_reads=list(self.dropped_reads),
            cost_usd=cost_from_log(self.log),
            cache_tokens=cache_tokens_from_log(self.log),
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


def _text_of(messages):
    """Every text block of a built prompt, as the model receives it."""
    out = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            out.extend(block.get("text", "") for block in content)
        else:
            out.append(content)
    return "\n\n".join(out)


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

    def test_an_invalid_spec_goes_back_to_the_planner(self, repo, tmp_path):
        # It used to escalate on the first occurrence, which the code's own
        # comment argued against — "a malformed spec is the planner's error to
        # fix". A malformed spec is the definition of a stage drawn wrongly,
        # which is the planner's tier, not a human's. It became urgent when
        # `validate_stage` started rejecting a fenced code block in the
        # instruction: that rule asks a model to break a strong habit, and one
        # slip stopping an unattended overnight run is a bad trade.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"})]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "plan"
        assert "edit_files" in json.dumps(out["last_failure"])

    def test_it_counts_against_the_no_landing_budget(self, repo, tmp_path):
        # What bounds the loop. Without this the run would redraw an invalid
        # stage forever, which is worse than the escalation it replaces.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"})]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["interventions_since_landing"] == 1

    def test_it_escalates_once_the_budget_is_gone(self, repo, tmp_path):
        # The guard that bounds it only fired while a stage was in flight, so
        # on the derive path — where a rejected spec leaves no stage — it was
        # unreachable and the loop had nothing stopping it.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"})]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state["interventions_since_landing"] = 3
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"

    def test_the_problems_reach_the_next_planner_call(self, repo, tmp_path):
        # The journey. A rejection recorded in state but not rendered into the
        # prompt would buy three identical redraws and then escalate anyway,
        # for three times the cost of escalating immediately.
        planner = StubPlanner(
            [
                PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"}),
                PlannerOutcome("project_complete", "r", "e"),
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state.update(nodes.plan(state, rt))
        nodes.plan(state, rt)
        assert "edit_files" in _text_of(planner.calls[1])

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


class TestTheBranchWorkReachesThePlanner:
    """A value computed by git, carried through the node, into a paid prompt.

    The endpoints both worked in isolation: `Git.diff` returns the stage's
    cumulative diff and the prompt renders whatever it is handed. What was
    missing was the wire between them, and a revision written without it
    described the branch as baseline and was blocked for contradicting the
    diff the reviewer judged. That is the journey this pins, not either end.
    """

    def _plan_with_a_stage_in_flight(self, repo, tmp_path, run_git):
        planner = StubPlanner([PlannerOutcome("blocked", "r", "e")])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        with_stage(state, rt)
        (repo / "app.py").write_text("def hello():\n    return SENTINEL\n")
        run_git(repo, "commit", "-aqm", "the failed attempt")
        state["last_failure"] = {"layer": "review", "summary": "rework", "detail": "d"}
        nodes.plan(state, rt)
        return planner.calls[0]

    def test_the_stage_s_own_diff_reaches_the_prompt(self, repo, tmp_path, run_git):
        messages = self._plan_with_a_stage_in_flight(repo, tmp_path, run_git)
        assert "SENTINEL" in _text_of(messages)

    def test_a_derivation_with_no_stage_in_flight_carries_none(self, repo, tmp_path):
        planner = StubPlanner([PlannerOutcome("project_complete", "r", "e")])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        (repo / "app.py").write_text("def hello():\n    return SENTINEL\n")
        nodes.plan(state, rt)
        assert "SENTINEL" not in _text_of(planner.calls[0])


class TestAnUnresolvableExcerptReachesThePlanner:
    """The journey, not the endpoints — the rule this file exists for.

    `resolve_excerpts` raises and `_planner_failure` routes; both are covered
    on their own. What is not covered by either is that `execute` reads the
    range at the stage's start sha and turns a failure into a redraw rather
    than a traceback. With the instruction carrying no code, an excerpt that
    does not resolve is a stage that cannot be attempted, and the participant
    that can fix it is the one that chose the range.
    """

    def test_it_routes_to_the_planner_rather_than_raising(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        with_stage(state, rt, read_excerpts=[{"path": "gone.rb", "start": 1, "end": 5}])
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "plan"
        assert "gone.rb" in json.dumps(out.get("last_failure") or {})

    def test_a_range_is_read_at_the_stage_start_sha(self, repo, tmp_path, run_git):
        # The executor's own prior attempt moves lines. A range chosen against
        # the stage's start and read against the tree is silently the wrong
        # code — and the prompt is where that would land, so assert there.
        executor = StubExecutor(repo=repo, edits=[("app.py", "changed\n")])
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        with_stage(state, rt, read_excerpts=[{"path": "app.py", "start": 1, "end": 1}])
        (repo / "app.py").write_text("MOVED_BY_A_PRIOR_ATTEMPT\n")
        run_git(repo, "commit", "-aqm", "prior attempt")

        nodes.execute(state, rt)
        # The excerpt section only. The cumulative-diff section of the same
        # prompt carries the prior attempt on purpose, so asserting over the
        # whole prompt would fail for a correct reason.
        prompt = executor.prompts[-1]
        after = prompt.split("## Lines from files you may read but not change")[1]
        section = after.split("\n## ")[0]
        assert "def hello" in section  # the line as it stood when the stage began
        assert "MOVED_BY_A_PRIOR_ATTEMPT" not in section


class TestTheConventionsReachTheReviewer:
    """Runtime → node → prompt, for the two that never had it.

    Both ends worked already: `agent_context` reads the documents at the run's
    commit, and the prompt renders whatever it is handed. The wire between them
    is what was missing. The executor reaches the same documents as `--read`
    file arguments rather than through state, so its coverage is in the
    executor's own tests; this is the hop that crosses a schema boundary.
    """

    def _cfg_files(self, repo, run_git):
        (repo / "AGENTS.md").write_text("# How this repo works\n\nSCOPE_BY_TENANT\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "conventions")

    def test_only_the_planner_gets_the_operational_half(self, repo, tmp_path, run_git):
        """The split, enforced where it matters.

        The conventions half binds anything that writes or judges code. The
        operational half — build, test, deploy — is the planner's alone: it
        decides what is possible, and it is the participant that reads a plan
        item as blocked when it does not know the container reinstalls the
        bundle on a Gemfile edit. Handing the same pages to an executor that
        runs no commands is what invites it to narrate one it never ran.
        """
        (repo / "AGENTS.md").write_text("SCOPE_BY_TENANT\n")
        (repo / "OPERATIONS.md").write_text("RUN_BIN_RSPEC\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "split docs")
        executor = StubExecutor(repo=repo, edits=[("app.py", "changed\n")])
        cfg, rt, state = make(
            repo, tmp_path, executor=executor, operations_context=["OPERATIONS.md"]
        )
        state["plan_sha"] = rt.git.rev_parse("proj")
        with_stage(state, rt)

        nodes.execute(state, rt)
        assert "RUN_BIN_RSPEC" not in executor.prompts[-1]

        planner = rt.planner
        nodes.plan(state, rt)
        text = _text_of(planner.calls[-1])
        assert "SCOPE_BY_TENANT" in text
        assert "RUN_BIN_RSPEC" in text

    def test_the_reviewer_prompt_gets_them(self, repo, tmp_path, run_git):
        self._cfg_files(repo, run_git)
        reviewer = StubReviewer()
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state["plan_sha"] = rt.git.rev_parse("proj")
        with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        run_git(repo, "commit", "-aqm", "stage work")
        nodes.review(state, rt)
        assert "SCOPE_BY_TENANT" in _text_of(reviewer.messages[-1])


class TestTheExecutorsCostSurvivesTheJourney:
    """Aider's log → ExecutionResult → state → the durable record.

    Four defects in this project have been values computed correctly and lost
    in transit, every one of them passing its unit tests on both ends. This is
    a new value crossing three boundaries, and the last of them writes a file
    that outlives the run and is fed back to the planner.
    """

    def test_it_accumulates_across_a_stage_s_attempts(self, repo, tmp_path):
        # Each attempt is its own Aider session with its own running total, so
        # replacing would report a four-attempt stage at the price of one.
        executor = StubExecutor(repo=repo, edits=[("app.py", "a\n"), ("app.py", "b\n")])
        executor.log = "Cost: $0.02 message, $0.05 session."
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        with_stage(state, rt)
        state.update(nodes.execute(state, rt))
        state.update(nodes.execute(state, rt))
        assert state["executor_cost_usd"] == 0.10

    def test_a_free_executor_records_nothing(self, repo, tmp_path):
        executor = StubExecutor(repo=repo, edits=[("app.py", "a\n")])
        executor.log = "Tokens: 12k sent, 1.1k received."
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert out.get("executor_cost_usd", 0.0) == 0.0


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

    def test_extend_re_enters_at_verify(self, repo, tmp_path):
        # `extend` means the approach was right and only the scope was too
        # narrow, so the work on the branch stands — that is the mode's whole
        # premise, and `_revert_unadopted` is careful to preserve it. Routing
        # onward to the executor denies it: the diff is already written, and the
        # instruction still describes it as undone. A stage once deleted the
        # line next to its target to produce a change that was already made.
        # Ask the gates whether it passes instead.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        assert nodes.plan(state, rt)["next_hop"] == "verify"

    def test_restart_re_enters_at_precheck(self, repo, tmp_path):
        # Nothing survives a restart, so there is nothing for the gates to read
        # and the branch has to be re-cut before the executor runs.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="restart")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        assert nodes.plan(state, rt)["next_hop"] == "precheck"

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

    def test_a_review_rejection_asks_for_a_replacement_not_an_addition(
        self, repo, tmp_path
    ):
        # This said "do not repeat the rejected approach", which made sense when
        # `rework_reset` was on and the branch went back to the baseline first.
        # With the work now left in place and shown to the executor, that told
        # it to discard the very diff it was handed as the thing to amend — and
        # the observed result was a reviewer rejecting a rework for leaving the
        # original assertion in place and adding the new form beside it.
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state["review_feedback"] = ["Wrong verb on the route."]
        state["failure_layer"] = "review"
        nodes.execute(state, rt)
        prompt = ex.prompts[0]
        assert "rejected" in prompt
        assert "replace" in prompt
        assert "do not repeat the rejected approach" not in prompt.lower()

    def test_a_gate_failure_does_not_call_the_work_rejected(self, repo, tmp_path):
        # Measured over one run of 35 stages: this opening fired about a dozen
        # times and was wrong every one of them, because the reviewer rejected
        # nothing all run. Seven of those were `residue`, where the work is
        # incomplete rather than wrong and repeating the approach on the sites
        # that were missed is exactly the fix — so "do not repeat the rejected
        # approach" sat fifty lines above feedback saying the opposite.
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state["review_feedback"] = ["Two occurrences were never edited."]
        state["failure_layer"] = "residue"
        nodes.execute(state, rt)
        prompt = ex.prompts[0]
        assert "rejected" not in prompt
        assert "Two occurrences were never edited." in prompt

    def test_a_retry_is_shown_what_the_stage_has_changed_so_far(
        self, repo, tmp_path
    ):
        # Editing forward is only reasonable if the executor can see what it is
        # editing forward from. Reviewers leave comments on work; they do not
        # ask for the work again, and an author who cannot see their own diff
        # is not in a position to amend it.
        ex = StubExecutor(repo=repo, edits=[("app.py", "second pass\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work from the first attempt\n")
        state["review_feedback"] = ["The comment contradicts the code."]
        state["failure_layer"] = "review"
        nodes.execute(state, rt)
        assert "work from the first attempt" in ex.prompts[0]

    def test_a_first_attempt_is_shown_no_diff(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        assert "changed so far" not in ex.prompts[0]

    def test_a_first_attempt_has_no_opening_at_all(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        assert "previous attempt" not in ex.prompts[0]

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

    def test_rework_keeps_the_work_by_default(self, repo, tmp_path):
        # A reviewer leaves comments on the work in front of it; it does not
        # ask for the work again. Discarding a rejected attempt was the default
        # until a rejection arrived saying the behaviour and scope were correct
        # and only an explanatory comment was wrong — resetting rebuilt a
        # correct spec from nothing to change one sentence.
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("rejected\n")
        nodes.review(state, rt)
        assert (repo / "app.py").read_text() == "rejected\n"

    def test_rework_still_resets_when_the_operator_asks(self, repo, tmp_path):
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer, rework_reset=True)
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
        # Through the real precheck, not the `with_stage` stand-in: publishing
        # the note is precheck's job now, and a helper that cuts the branch
        # itself would step over the thing under test.
        state = {**state, **nodes.plan(state, rt)}
        state = {**state, **nodes.precheck(state, rt)}
        (repo / "app.py").write_text("stage work\n")
        nodes.advance(state, rt)

        log = repo / "docs" / "progress_log.md"
        assert log.exists(), "the note did not survive derivation"
        text = log.read_text()
        assert "0 sites remain" in text
        # The heading is lifted from the cited document at `plan_sha`, so the
        # reference has to survive the same trip the observation does — and be
        # resolvable against the commit once it arrives.
        assert "## Render sweeps — `PLAN.md#L4`" in text, (
            "the quote must reach the writer and be located in the plan"
        )
        # In its own commit *before* the stage, not inside the squash. That is
        # the point of moving it: a stage that never lands still leaves the
        # finding behind, so it cannot ride in the landing commit.
        assert "progress_log.md" not in rt.git._out("show", "--stat", "HEAD")
        assert "plan observations" in rt.git._out(
            "log", "--format=%s", cfg.project_branch
        )


class TestExecutorFeedbackIsBounded:
    """A 98KB executor log must not become the next prompt.

    CommandRunner used to cap output at 20,000 characters, which bounded
    `result.log` incidentally. Raising that cap so the flake gate could see a
    whole test run removed the bound, and a real attempt produced 97,883
    characters of Aider transcript that went verbatim into the next executor
    prompt and into planner feedback.
    """

    def test_a_huge_executor_log_is_clipped_into_feedback(self, repo, tmp_path):
        # Varied text, because that is what a transcript is. It used to be one
        # character repeated 200,000 times, which the progress-run collapse now
        # reduces to a single note — bounding it, but by the wrong mechanism,
        # so the test stopped exercising truncation while still passing its
        # first assertion. The degenerate case is covered below.
        executor = StubExecutor(repo=repo, ok=False)
        executor.log = "".join(f"line {i} of aider transcript\n" for i in range(8_000))
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        joined = "".join(out.get("review_feedback") or [])
        assert len(joined) < 20_000, "feedback must be bounded"
        assert "truncated" in joined

    def test_a_degenerate_run_is_bounded_by_collapsing(self, repo, tmp_path):
        # The other way output gets large: a progress bar rather than prose.
        # Collapsed rather than truncated, which keeps whatever follows it.
        executor = StubExecutor(repo=repo, ok=False)
        executor.log = "." * 200_000 + "\nTHE_ACTUAL_ERROR"
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        joined = "".join(out.get("review_feedback") or [])
        assert len(joined) < 20_000, "feedback must be bounded"
        assert "THE_ACTUAL_ERROR" in joined

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


class TestAReplyThatNamedAFileLostItsEdits:
    """The same class as an unapplied edit, one cause further out.

    Aider scans the model's answer for path-shaped words, and when it attaches
    one it returns before `apply_updates()` — so a reply that carried a correct
    edit *and* mentioned a file produces an empty tree with a clean exit code.
    Nothing in the executor's own report distinguishes that from a model that
    sat on its hands, so the loop told it "you produced no changes at all" and
    it obligingly wrote the same reply again.

    That is the mistake `unapplied_edit` was added to stop being made about a
    different cause: precision matters here, because the wrong diagnosis invites
    the model to repeat what already worked, louder.
    """

    def test_an_empty_tree_after_an_attach_says_why(self, repo, tmp_path):
        executor = StubExecutor(repo=repo, edits=[])
        executor.run_agent_stage = lambda *a, **k: ExecutionResult(
            ok=True, log="the model replied", attached_files=["bin/rspec"]
        )
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)

        assert out["next_hop"] == "execute"
        feedback = " ".join(out["review_feedback"])
        assert "bin/rspec" in feedback
        assert "produced no changes at all" not in feedback

    def test_a_changed_tree_after_an_attach_is_not_a_loss(self, repo, tmp_path):
        # Aider attaches on the *first* reply of a reflection and applies edits
        # on a later one, so an attach with work on the tree means the loop
        # recovered. Reporting it would fail an attempt that succeeded.
        executor = StubExecutor(repo=repo, edits=[("app.py", "landed\n")])

        def attached_then_edited(stage, prompt, history_dir=None):
            executor._apply()
            return ExecutionResult(
                ok=True, log="the model replied", attached_files=["bin/rspec"]
            )

        executor.run_agent_stage = attached_then_edited
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        assert nodes.execute(state, rt)["next_hop"] == "verify"

    def test_an_empty_tree_with_no_attach_is_unchanged(self, repo, tmp_path):
        # The genuine do-nothing case still reaches verify, which owns the
        # "produced no changes" verdict. This guard must not take it over.
        executor = StubExecutor(repo=repo, edits=[])
        executor.run_agent_stage = lambda *a, **k: ExecutionResult(
            ok=True, log="the model replied"
        )
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        assert nodes.execute(state, rt)["next_hop"] == "verify"


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


class TestTheFindingSurvivesToTheAddendum:
    """Planner response to plan note to state to the file on disk.

    The same journey `plan_notes` itself was lost on once, to a reset spread
    over the top of it. A new field on that note travels the identical path and
    gets the identical test.
    """

    def test_a_finding_reaches_the_written_entry(self, repo, tmp_path):
        note = {
            "plan_path": "PLAN.md",
            "anchor": "24 sites across 9 controllers.",
            "finding": "7 of 24 remain, all inline `<script>` renders",
            "observation": "The mechanical half is done.",
        }
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "next", "e",
                    stage_fields=planned_stage(),
                    plan_notes=[note],
                )
            ]
        )
        cfg, rt, state = make(
            repo, tmp_path, planner=planner,
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "PLAN.md").write_text("# Plan\n\n24 sites across 9 controllers.\n")
        # Committed, because precheck refuses to cut a branch over an
        # unattributable change — the stand-in it replaces did not care.
        rt.git.commit_all("plan")

        state = {**state, **nodes.plan(state, rt)}
        state = {**state, **nodes.precheck(state, rt)}
        (repo / "app.py").write_text("stage work\n")
        nodes.advance(state, rt)

        written = (repo / "docs/progress_log.md").read_text()
        assert "- **found** 7 of 24 remain, all inline `<script>` renders" in written
        assert "The mechanical half is done." in written


class TestAFailedLandingLeavesNothingBehind:
    """Either the stage lands or the tree is as advance found it.

    `advance` mutates in several steps — record what landed, strip whitespace,
    commit, squash-merge — and a failure in any of them used to leave the
    repository part-way through. Two faces of that: a staged merge stranded on
    the project branch, and an uncommitted plan document left in the worktree.

    What is protected here is now the *reviewer's* entries only. The planner's
    notes moved to `precheck`, which commits them before the branch is cut, so
    they are outside this transaction deliberately — see
    `TestPlanNotesArePublishedWhenTheStageIsCut` for why their survival must
    not depend on the stage landing.

    The second is the more confusing one. The next resume re-enters at verify,
    whose scope guard sees a plan document changed during a stage and routes it
    to the planner as the executor editing the record of its own work. That
    never happened, and the planner cannot fix it.
    """

    def _cfg(self, repo, tmp_path):
        cfg, rt, state = make(
            repo, tmp_path, plan_addendum_path="docs/progress_log.md"
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "PLAN.md").write_text("# Plan\n\nsome item\n")
        return cfg, rt, state

    A_NOTE = {
        "plan_path": "PLAN.md",
        "anchor": "some item",
        "observation": "done",
    }

    def test_a_failed_merge_unwinds_what_the_reviewer_recorded(
        self, repo, tmp_path, monkeypatch
    ):
        cfg, rt, state = self._cfg(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "review_record": "what the stage did"}

        def boom(*a, **kw):
            raise GitError("pre-commit hook rejected the commit")

        monkeypatch.setattr(rt.git, "squash_merge", boom)

        with pytest.raises(GitError):
            nodes.advance(state, rt)

        assert not (repo / "docs/progress_log.md").exists(), (
            "the record was written by advance and must not outlive its failure"
        )

    def test_a_successful_landing_keeps_the_record(self, repo, tmp_path):
        cfg, rt, state = self._cfg(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "review_record": "what the stage did"}

        nodes.advance(state, rt)
        assert "what the stage did" in (repo / "docs/progress_log.md").read_text()

    def test_the_planner_note_is_not_advance_s_to_write(self, repo, tmp_path):
        # It belongs to `precheck` now. Handing one to `advance` must not
        # resurrect the old path and put it back inside the transaction.
        cfg, rt, state = self._cfg(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "pending_plan_notes": [self.A_NOTE]}

        nodes.advance(state, rt)
        log = repo / "docs/progress_log.md"
        assert not log.exists() or "done" not in log.read_text()

    def test_a_landing_with_nothing_recorded_is_unaffected(self, repo, tmp_path, monkeypatch):
        # Nothing was written, so there is nothing to unwind and the unwind
        # must not invent a revert of a file that never existed.
        cfg, rt, state = self._cfg(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")

        def boom(*a, **kw):
            raise GitError("nope")

        monkeypatch.setattr(rt.git, "squash_merge", boom)
        with pytest.raises(GitError):
            nodes.advance(state, rt)


class TestTheReviewerIsGivenTheLiveRecord:
    """Runtime to node to the messages the reviewer actually receives.

    The frozen snapshot's copy of the progress log is whatever existed at run
    start — 6,680 bytes against 480,867 on the branch, measured on one long
    run — so the reviewer had no real account of what had been done. Both
    halves of the fix pass through here, and both have somewhere to be dropped
    on the way.
    """

    def _text(self, messages):
        out = []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                out.append(content)
            else:
                out += [b.get("text", "") for b in content]
        return "\n".join(out)

    def test_the_live_log_reaches_the_reviewer(self, repo, tmp_path):
        reviewer = StubReviewer()
        cfg, rt, state = make(
            repo, tmp_path, reviewer=reviewer,
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs/progress_log.md").write_text(
            "## a section\n\n- **found** 7 of 24 remain\n"
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")

        nodes.review(state, rt)
        assert "7 of 24 remain" in self._text(reviewer.messages[0])

    def test_the_history_cap_reaches_the_reviewer(self, repo, tmp_path):
        reviewer = StubReviewer()
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        rt.cfg.reviewer.history_stages = 2
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {
            **state,
            "completed": [
                {"index": i, "id": f"stage-{i}", "instruction": f"work {i}"}
                for i in range(5)
            ],
        }

        nodes.review(state, rt)
        text = self._text(reviewer.messages[0])
        assert "stage-4" in text
        assert "stage-0" not in text


class TestTheAgentContextReachesThePlanner:
    """Repository to runtime to prompt.

    The facts in these documents were previously hand-copied into
    `planner.guidance`, and the copy drifted: one project's `AGENTS.md`
    recorded that editing the Gemfile reinstalls the bundle, the guidance said
    nothing, and the plan asserted the opposite across five items nobody drew.
    A wiring that stops anywhere short of the prompt reproduces exactly that.
    """

    def test_conventions_reach_the_planner_prompt(self, repo, tmp_path, run_git):
        (repo / "AGENTS.md").write_text(
            "# Conventions\n\nEditing the Gemfile reinstalls the bundle.\n"
        )
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "conventions")

        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = {**state, "plan_sha": rt.git.rev_parse("HEAD")}

        nodes.plan(state, rt)
        prompt = "".join(
            block["text"]
            for message in planner.calls[0]
            for block in message["content"]
            if block.get("type") == "text"
        )
        assert "Editing the Gemfile reinstalls the bundle." in prompt

    def test_a_project_without_one_plans_normally(self, repo, tmp_path):
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        assert nodes.plan(state, rt)["next_hop"] in ("precheck", "finalize", "escalate")


class TestReviewerObservationsReachTheLog:
    """The whole journey, not its endpoints.

    A finding is produced in the reviewer client, carried on ReviewOutcome,
    put into state by `review`, and written by `advance`. Four hops. Every
    value this project has lost in transit passed its unit tests on both ends.

    It matters more than most because the log is the only durable home for
    something the reviewer notices outside its stage. Left in the summary it is
    printed once and gone.
    """

    def _reviewer(self, observations):
        from orchestrator.reviewer import Observation, ReviewOutcome

        class Once:
            def review(self, messages, cache_key=None):
                return ReviewOutcome(
                    verdict="approved",
                    summary="fine",
                    observations=[Observation(**o) for o in observations],
                )

        return Once()

    def test_an_observation_is_written_when_the_stage_lands(self, repo, tmp_path):
        reviewer = self._reviewer([
            {
                "file": "app/views/admin/product_types/_form.html.erb",
                "finding": "mailing_service_ids is submitted but never permitted",
                "detail": "The checkbox posts it and no permit list covers it.",
            }
        ])
        cfg, rt, state = make(
            repo, tmp_path, reviewer=reviewer,
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "PLAN.md").write_text("# Plan\n\nsome item\n")

        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)

        written = (repo / "docs/progress_log.md").read_text()
        assert "app/views/admin/product_types/_form.html.erb" in written
        assert "mailing_service_ids is submitted but never permitted" in written
        assert "The checkbox posts it and no permit list covers it." in written

    def test_nothing_is_written_when_there_are_none(self, repo, tmp_path):
        # Most stages have none, so the empty case is the common one and must
        # not leave an empty heading behind.
        cfg, rt, state = make(
            repo, tmp_path, reviewer=self._reviewer([]),
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "PLAN.md").write_text("# Plan\n\nsome item\n")

        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)

        # The log exists, because a landed stage always records what it did.
        # What must not appear is an observation heading with nothing under it.
        text = (repo / "docs/progress_log.md").read_text()
        assert "**observed** by the reviewer" not in text

    def test_a_rework_cycle_does_not_report_the_same_finding_twice(
        self, repo, tmp_path
    ):
        # Every review of a stage sees the whole cumulative diff, so the newest
        # set supersedes the last. Accumulating would report one finding once
        # per attempt, and the log cannot distinguish that from independent
        # confirmation.
        observation = {
            "file": "app/models/cart.rb",
            "finding": "two attr_accessible blocks",
            "detail": "Lines 149 and 396 both declare one.",
        }
        cfg, rt, state = make(
            repo, tmp_path, reviewer=self._reviewer([observation]),
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "PLAN.md").write_text("# Plan\n\nsome item\n")

        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)

        written = (repo / "docs/progress_log.md").read_text()
        assert written.count("two attr_accessible blocks") == 1

    def test_a_stage_that_never_lands_records_nothing(self, repo, tmp_path):
        # A finding from abandoned work must not enter the record: the log is
        # what the next run reads to know what is true.
        cfg, rt, state = make(
            repo, tmp_path,
            reviewer=self._reviewer([
                {"file": "a.rb", "finding": "something", "detail": "detail"}
            ]),
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "PLAN.md").write_text("# Plan\n\nsome item\n")

        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        # No advance: the stage was abandoned.
        assert not (repo / "docs/progress_log.md").exists()
        assert state["pending_observations"][0]["file"] == "a.rb"


class TestChecksThatWrite:
    """A check may fix as well as report, and something has to commit that.

    `rubocop -A`, `eslint --fix`, `gofmt -w`. The executor commits its own work
    before verify starts, so nothing else in the loop commits what a check
    changed. Left uncommitted it survives the stage — swept up silently if the
    stage lands, orphaned if the stage is blocked or reworked away, at which
    point the next stage's precheck refuses to cut a branch over changes it
    cannot attribute. That stopped a run.
    """

    LINTER = "printf 'linted\\n' >> app.py"

    def test_a_check_that_writes_is_committed_to_the_stage_branch(
        self, repo, tmp_path
    ):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt, checks=[self.LINTER])
        (repo / "app.py").write_text("stage work\n")
        nodes.verify(state, rt)
        assert "linted" in (repo / "app.py").read_text()
        assert not rt.git.uncommitted()

    def test_it_can_be_switched_off(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, checks_commit_changes=False)
        state = with_stage(state, rt, checks=[self.LINTER])
        (repo / "app.py").write_text("stage work\n")
        nodes.verify(state, rt)
        assert "linted" in (repo / "app.py").read_text()
        assert rt.git.uncommitted()


class TestTheRecordReachesTheLog:
    """The reviewer's account of the change, end to end.

    Written here rather than only against `append_outcome` because this value
    crosses two schema boundaries — `ReviewOutcome` into `RunState`, and
    `RunState` into the addendum — and every defect of this shape in this
    codebase has been a value computed correctly, written correctly, and
    dropped in transit by a schema that did not declare the key.
    """

    def _reviewer(self, record):
        return StubReviewer(
            [ReviewOutcome(verdict="approved", summary="matches the stage",
                           record=record)]
        )

    def test_it_is_what_the_log_gets(self, repo, tmp_path):
        cfg, rt, state = make(
            repo, tmp_path,
            reviewer=self._reviewer("Widens the permit list to the two id "
                                    "columns, so the selects now save."),
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)

        text = (repo / "docs/progress_log.md").read_text()
        assert "so the selects now save" in text
        # Not the verdict rationale, which is written for a different job.
        assert "matches the stage" not in text

    def test_a_reviewer_that_writes_none_falls_back_to_the_summary(
        self, repo, tmp_path
    ):
        # Better a gate-shaped entry than no entry.
        cfg, rt, state = make(
            repo, tmp_path, reviewer=self._reviewer(""),
            plan_addendum_path="docs/progress_log.md",
        )
        (repo / "docs").mkdir(exist_ok=True)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)
        assert "matches the stage" in (repo / "docs/progress_log.md").read_text()


class TestTheSquashCommitIsAProperCommitMessage:
    """Subject, blank line, wrapped body — not a truncated one-liner.

    The landing commit was `[{stage.id}] {instruction[:70]}` and nothing else:
    a subject cut mid-word, no body, and on a project whose stage ids run to
    forty-odd characters the visible text was down to a clause. Meanwhile the
    reviewer's account of what the stage actually did — the only description
    written by a participant that saw the diff — was already in hand two lines
    above, going to the progress log and nowhere else.

    So `git log` on the project branch described the *intent* of each stage,
    truncated, and never the outcome. `git blame` on the plan is the mechanism
    CLAUDE.md leans on for answering what a stage did from facts rather than
    claims, and it was reading a sentence fragment.

    The body goes through `decode_escapes` for the same reason the addendum
    does, and so the commit body and the log entry are the same bytes.
    """

    def _stage(self, instruction):
        from orchestrator.config import Stage

        return Stage(id="a-fairly-long-stage-id-like-real-ones", instruction=instruction)

    def test_the_subject_is_the_stage_id_alone(self):
        # The id is authored as an identifier, not slugified from a title the
        # tooling then discarded — so appending the instruction's first line
        # repeated in prose what the id already said, and pushed the subject
        # past 72 columns to do it.
        msg = nodes._commit_message(self._stage("Do the thing\n\nmore"), "")
        assert msg.splitlines()[0] == "[a-fairly-long-stage-id-like-real-ones]"

    def test_the_subject_does_not_carry_the_instruction(self):
        # The instruction is written before the work. Keeping it out of the
        # subject is the same call as recording the reviewer's account in the
        # body: the commit should say what happened, not what was asked for.
        msg = nodes._commit_message(self._stage("Close the permit gap"), "did it")
        assert "Close the permit gap" not in msg

    def test_the_body_is_separated_by_exactly_one_blank_line(self):
        msg = nodes._commit_message(self._stage("Subject here"), "The reviewer said this.")
        lines = msg.splitlines()
        assert lines[1] == "", "git needs a blank line after the subject"
        assert lines[2] == "The reviewer said this."

    def test_a_long_body_is_wrapped_rather_than_one_line(self):
        record = (
            "The diff removes only the unused nested package-id input and adds "
            "focused response-body assertions that guard both its absence and the "
            "retained top-level hidden input, preserving the positional request "
            "style and the existing spec structure throughout the file."
        )
        body = nodes._commit_message(self._stage("s"), record).split("\n\n", 1)[1]
        assert len(body.splitlines()) > 1
        assert max(len(line) for line in body.splitlines()) <= 72

    def test_paragraphs_survive_wrapping(self):
        msg = nodes._commit_message(self._stage("s"), "First para.\n\nSecond para.")
        body = msg.split("\n\n", 1)[1]
        assert "First para." in body and "Second para." in body
        assert "" in body.splitlines(), "the paragraph break is kept"

    def test_a_long_path_is_not_broken_across_lines(self):
        # Wrapping a path makes it unsearchable, and these bodies are full of
        # them. Better an over-long line than a path that cannot be grepped.
        record = "It changes " + "a/very/long/path/that/goes/on/" * 4 + "file.rb here."
        body = nodes._commit_message(self._stage("s"), record).split("\n\n", 1)[1]
        assert "a/very/long/path/that/goes/on/a/very/long" in body

    def test_no_body_means_no_trailing_blank_line(self):
        # A reviewer that returned nothing must not produce a commit whose
        # message is a subject followed by whitespace.
        msg = nodes._commit_message(self._stage("Just this"), "")
        assert msg == msg.strip()
        assert "\n" not in msg

    def test_unicode_escapes_are_decoded(self):
        # The same pass the addendum makes, so the commit body and the log
        # entry are the same bytes. It is narrow by design: `\\n` is not a
        # sequence it touches, in either place.
        msg = nodes._commit_message(self._stage("s"), "an em dash \\u2014 here")
        assert "\u2014" in msg
        assert "u2014" not in msg.replace("\u2014", "")


class TestPlanNotesArePublishedWhenTheStageIsCut:
    """Not held to the landing gate, because their truth does not depend on it.

    The notes say things like "the two documents contradict each other" and
    "not drawable" — findings about the *plan*, made by reading it against the
    code while deriving a stage. Nothing about them is contingent on the
    executor succeeding, and a stage that escalates used to take them to the
    grave. That is the expensive direction: a false blocker in a plan makes
    items read as blocked, and an item that reads as blocked is never
    attempted.

    The code already conceded the point for revisions — the notes accumulated
    across a redraw "because a redrawn stage is the same piece of work and its
    observations about the plan are still true". Abandonment is the same
    argument one step further.

    The reviewer's observations stay on the landing gate and are deliberately
    not moved: those are findings about a diff, and an abandoned diff does not
    exist.

    Publishing here also removes an obligation rather than adding one. The note
    used to be written inside `advance`, which then had to unwind it by hand if
    any later step raised — the landing transaction is smaller without it.
    """

    def _notes(self):
        return [{"anchor": "PLAN.md#L1", "observation": "the plan says X", "finding": "found Y"}]

    def test_the_note_lands_before_the_branch_is_cut(self, repo, tmp_path, run_git):
        cfg, rt, state = make(repo, tmp_path, plan_addendum_path="docs/progress_log.md")
        state = {**with_stage(state, rt), "pending_plan_notes": self._notes()}
        out = nodes.precheck(state, rt)

        log = repo / "docs/progress_log.md"
        assert log.exists(), "the note is written at cut time"
        assert "found Y" in log.read_text()
        assert rt.git.is_clean(), "and committed, or the cut sweeps it up"

    def test_the_note_commit_is_on_the_project_branch(self, repo, tmp_path, run_git):
        # Not the stage branch, which is discarded when the stage is abandoned
        # — which is the whole case for moving it.
        cfg, rt, state = make(repo, tmp_path, plan_addendum_path="docs/progress_log.md")
        state = {**with_stage(state, rt), "pending_plan_notes": self._notes()}
        nodes.precheck(state, rt)
        subjects = run_git(repo, "log", "--format=%s", cfg.project_branch).splitlines()
        assert any("log.md" in s or "plan" in s.lower() for s in subjects), subjects

    def test_the_stage_diff_does_not_contain_the_note(self, repo, tmp_path, run_git):
        # It precedes stage_start_sha, so the scope guard never sees it and
        # cannot report it as the executor editing a plan document.
        cfg, rt, state = make(repo, tmp_path, plan_addendum_path="docs/progress_log.md")
        state = {**with_stage(state, rt), "pending_plan_notes": self._notes()}
        out = nodes.precheck(state, rt)
        changed = rt.git.diff_names(out.get("stage_start_sha") or rt.git.head_sha())
        assert "docs/progress_log.md" not in changed

    def test_the_notes_are_cleared_once_written(self, repo, tmp_path, run_git):
        # Otherwise a revision re-enters precheck and republishes everything the
        # first cut already wrote.
        cfg, rt, state = make(repo, tmp_path, plan_addendum_path="docs/progress_log.md")
        state = {**with_stage(state, rt), "pending_plan_notes": self._notes()}
        out = nodes.precheck(state, rt)
        assert out["pending_plan_notes"] == []

    def test_no_notes_means_no_commit(self, repo, tmp_path, run_git):
        cfg, rt, state = make(repo, tmp_path, plan_addendum_path="docs/progress_log.md")
        before = run_git(repo, "rev-parse", "HEAD").strip()
        nodes.precheck({**with_stage(state, rt), "pending_plan_notes": []}, rt)
        assert run_git(repo, "rev-parse", cfg.project_branch).strip() == before
