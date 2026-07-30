"""Node logic, exercised directly with a stubbed executor and reviewer.

These are the decisions that make the loop behave: which failures consume a
retry, which escalate, when a stage resets before rework, and what lands in
the history the report is built from.
"""

import time
from dataclasses import dataclass, field

import pytest

from orchestrator import nodes
from orchestrator.commands import CommandRunner
from orchestrator.config import parse_config
from orchestrator.executor import ExecutionResult
from orchestrator.gitops import Git
from orchestrator.reviewer import Issue, ReviewOutcome, TokenUsage
from orchestrator.runtime import RunPaths, Runtime
from orchestrator.state import new_state


@dataclass
class StubExecutor:
    """Edits the repo in a scripted way instead of calling a model."""

    edits: list = field(default_factory=list)
    ok: bool = True
    timed_out: bool = False
    context_ok: bool = True
    calls: list = field(default_factory=list)
    prompts: list = field(default_factory=list)
    repo: object = None

    def gather_context(self, stage):
        if not stage.context_commands:
            return [], []
        from orchestrator.commands import CommandResult

        result = CommandResult(
            command=stage.context_commands[0],
            exit_code=0 if self.context_ok else 1,
            stdout="index\nshow",
            stderr="",
            duration_seconds=0.0,
        )
        return [(stage.context_commands[0], "index\nshow")], [result]

    def _apply(self):
        if self.edits and self.repo is not None:
            name, text = self.edits.pop(0)
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run_agent_stage(self, stage, prompt):
        self.calls.append(stage.id)
        self.prompts.append(prompt)
        self._apply()
        return ExecutionResult(ok=self.ok, log="executor log", timed_out=self.timed_out)

    def run_script_stage(self, stage):
        self.calls.append(stage.id)
        self._apply()
        return ExecutionResult(ok=self.ok, log="script log")


@dataclass
class StubReviewer:
    outcomes: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def review(self, messages):
        self.calls.append(messages)
        if self.outcomes:
            return self.outcomes.pop(0)
        return ReviewOutcome(verdict="approved", summary="fine", usage=TokenUsage(10, 2, 5))


def make(repo, tmp_path, stage_overrides=None, executor=None, reviewer=None, **cfg_over):
    stage = {"id": "s1", "instruction": "do it", "edit_files": ["app.py", "src/**"]}
    stage.update(stage_overrides or {})
    data = {
        "target_repo": str(repo),
        "branch": "work",
        "test_command": "true",
        "executor": {"model": "m"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": [stage],
    }
    data.update(cfg_over)
    cfg = parse_config(data)

    ex = executor or StubExecutor(repo=repo, edits=[("app.py", "changed\n")])
    ex.repo = repo
    rt = Runtime(
        cfg=cfg,
        paths=RunPaths(tmp_path / "runs", "r1"),
        git=Git(repo),
        runner=CommandRunner(cwd=repo, timeout=60),
        executor=ex,
        reviewer=reviewer or StubReviewer(),
    )
    rt.paths.ensure()

    state = new_state(
        run_id="r1",
        config_path="c.yaml",
        target_repo=str(repo),
        base_ref="main",
        base_sha=Git(repo).head_sha(),
        branch="work",
        stage_ids=[s.id for s in cfg.stages],
    )
    state["stage_start_sha"] = Git(repo).head_sha()
    state["stage_started_at"] = time.time()
    return cfg, rt, state


class TestPrecheck:
    def test_pins_the_stage_baseline_on_first_entry(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state["stage_start_sha"] = ""
        out = nodes.precheck(state, rt)
        assert out["stage_start_sha"] == Git(repo).head_sha()

    def test_does_not_move_the_baseline_on_re_entry(self, repo, tmp_path):
        # A rework must be measured against the same baseline, or the diff
        # shrinks to just the correction.
        cfg, rt, state = make(repo, tmp_path)
        pinned = state["stage_start_sha"]
        out = nodes.precheck(state, rt)
        assert "stage_start_sha" not in out or out["stage_start_sha"] == pinned

    def test_routes_agent_stage_to_execute(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.precheck(state, rt)["next_hop"] == "execute"

    def test_routes_manual_stage_to_gate(self, repo, tmp_path):
        cfg, rt, state = make(
            repo,
            tmp_path,
            stage_overrides={
                "id": "bump",
                "kind": "manual",
                "instruction": None,
                "human_steps": "bump it",
                "edit_files": [],
                "checks": ["true"],
            },
        )
        assert nodes.precheck(state, rt)["next_hop"] == "gate"

    def test_failed_precondition_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, stage_overrides={"preconditions": ["false"]})
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "precondition"

    def test_failed_setup_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, setup_command="false")
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "setup"

    def test_setup_runs_before_the_executor(self, repo, tmp_path):
        # The executor runs the test command itself via --auto-test, so it
        # cannot be handed a stale environment.
        marker = tmp_path / "setup-ran"
        cfg, rt, state = make(repo, tmp_path, setup_command=f"touch {marker}")
        nodes.precheck(state, rt)
        assert marker.exists()


class TestExecute:
    def test_successful_attempt_goes_to_verify(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.execute(state, rt)["next_hop"] == "verify"

    def test_writes_the_prompt_and_log_as_artifacts(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        nodes.execute(state, rt)
        directory = rt.paths.attempt_dir(0, "s1", 0)
        assert (directory / "prompt.md").exists()
        assert (directory / "executor.log").read_text() == "executor log"

    def test_feedback_reaches_the_prompt_on_rework(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state["review_feedback"] = ["Wrong verb on the route."]
        nodes.execute(state, rt)
        assert "Wrong verb on the route." in ex.prompts[0]

    def test_script_stage_runs_the_command_not_the_model(self, repo, tmp_path):
        from orchestrator.executor import Executor

        cfg, rt, state = make(
            repo,
            tmp_path,
            stage_overrides={
                "id": "annotate",
                "kind": "script",
                "instruction": None,
                "command": "echo done > app.py",
                "edit_files": ["app.py"],
            },
        )
        # The real executor here: the point is that a script stage runs its
        # declared command, so stubbing the thing under test proves nothing.
        rt.executor = Executor(cfg, rt.runner)
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "verify"
        assert (repo / "app.py").read_text().strip() == "done"

    def test_executor_failure_consumes_a_retry(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, ok=False)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "execute"
        assert out["verify_attempt"] == 1

    def test_executor_failure_escalates_when_retries_are_spent(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, ok=False)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state["verify_attempt"] = 3
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "escalate"

    def test_failed_context_command_escalates(self, repo, tmp_path):
        # Building a prompt from a work list that failed to generate would have
        # the executor invent one.
        ex = StubExecutor(repo=repo, context_ok=False)
        cfg, rt, state = make(
            repo, tmp_path, stage_overrides={"context_commands": ["bin/inventory"]}, executor=ex
        )
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "escalate"


class TestVerifyRouting:
    def test_pass_goes_to_review(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "review"

    def test_pass_skips_review_when_disabled(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, stage_overrides={"review": False})
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "advance"

    def test_retryable_failure_loops_to_execute(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, test_command="exit 1")
        (repo / "app.py").write_text("changed\n")
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "execute"
        assert out["verify_attempt"] == 1

    def test_non_retryable_failure_escalates_immediately(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "wandered.py").write_text("out of scope\n")
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "scope"

    def test_non_retryable_failure_does_not_consume_a_retry(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "wandered.py").write_text("out of scope\n")
        assert "verify_attempt" not in nodes.verify(state, rt)

    def test_exhausted_retries_escalate(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, test_command="exit 1")
        (repo / "app.py").write_text("changed\n")
        state["verify_attempt"] = 3
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "escalate"
        assert "max_test_retries" in out["escalation_reason"]

    def test_accumulates_flake_reruns_across_attempts(self, repo, tmp_path):
        flag = tmp_path / "flag"
        cfg, rt, state = make(
            repo,
            tmp_path,
            test_command=f"if [ -f {flag} ]; then exit 0; else touch {flag}; exit 1; fi",
        )
        (repo / "app.py").write_text("changed\n")
        state["flake_reruns"] = 2
        assert nodes.verify(state, rt)["flake_reruns"] == 3


class TestReviewRouting:
    def test_approved_advances(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"

    def test_rework_loops_to_execute_with_feedback(self, repo, tmp_path):
        reviewer = StubReviewer(
            outcomes=[
                ReviewOutcome(
                    verdict="rework",
                    summary="One problem.",
                    issues=[Issue(severity="major", file="app.py", description="Wrong.")],
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "execute"
        assert out["rework_attempt"] == 1
        assert "Wrong." in out["review_feedback"][0]

    def test_blocked_escalates_without_consuming_a_rework(self, repo, tmp_path):
        # The problem is upstream of the executor; grinding through rework
        # attempts will not fix it.
        reviewer = StubReviewer(
            outcomes=[ReviewOutcome(verdict="blocked", summary="Instruction is wrong.")]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "escalate"
        assert "rework_attempt" not in out
        assert "Instruction is wrong." in out["escalation_reason"]

    def test_exhausted_rework_escalates(self, repo, tmp_path):
        reviewer = StubReviewer(
            outcomes=[ReviewOutcome(verdict="rework", summary="Still wrong.")]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        (repo / "app.py").write_text("changed\n")
        state["rework_attempt"] = 2
        out = nodes.review(state, rt)
        assert out["next_hop"] == "escalate"
        assert "max_rework_retries" in out["escalation_reason"]

    def test_rework_resets_the_tree_by_default(self, repo, tmp_path):
        # So the next attempt produces one clean single-purpose diff.
        reviewer = StubReviewer(outcomes=[ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        (repo / "app.py").write_text("rejected\n")
        nodes.review(state, rt)
        assert (repo / "app.py").read_text() == "def hello():\n    return 1\n"

    def test_rework_reset_can_be_disabled(self, repo, tmp_path):
        reviewer = StubReviewer(outcomes=[ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer, rework_reset=False)
        (repo / "app.py").write_text("rejected\n")
        nodes.review(state, rt)
        assert (repo / "app.py").read_text() == "rejected\n"

    def test_accumulates_token_usage(self, repo, tmp_path):
        reviewer = StubReviewer(
            outcomes=[
                ReviewOutcome(
                    verdict="approved", summary="ok", usage=TokenUsage(100, 20, 80)
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        (repo / "app.py").write_text("changed\n")
        state["stage_usage"] = {"prompt_tokens": 5, "cached_tokens": 1, "completion_tokens": 2}
        out = nodes.review(state, rt)
        assert out["stage_usage"]["prompt_tokens"] == 105
        assert out["stage_usage"]["cached_tokens"] == 81

    def test_writes_the_verdict_as_an_artifact(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        assert (rt.paths.attempt_dir(0, "s1", 0) / "review.json").exists()

    def test_reference_documents_reach_the_reviewer(self, repo, tmp_path):
        (repo / "PLAN.md").write_text("THE AUTHORITY")
        reviewer = StubReviewer()
        cfg, rt, state = make(
            repo, tmp_path, reviewer=reviewer, reference_docs=["PLAN.md"]
        )
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        assert "THE AUTHORITY" in "".join(m["content"] for m in reviewer.calls[0])


class TestAdvance:
    def test_commits_leftover_work(self, repo, tmp_path):
        # A script stage leaves its transform uncommitted.
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        nodes.advance(state, rt)
        assert Git(repo).is_clean()

    def test_records_the_commit_range(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        out = nodes.advance(state, rt)
        assert ".." in out["history"][0]["commit_range"]

    def test_appends_to_history(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        out = nodes.advance(state, rt)
        assert out["history"][0]["id"] == "s1"
        assert out["history"][0]["outcome"] == "complete"

    def test_carries_counters_into_history_before_resetting(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        state["verify_attempt"] = 2
        state["rework_attempt"] = 1
        state["flake_reruns"] = 3
        out = nodes.advance(state, rt)
        assert out["history"][0]["verify_retries"] == 2
        assert out["history"][0]["rework_attempts"] == 1
        assert out["history"][0]["flake_reruns"] == 3
        assert out["verify_attempt"] == 0
        assert out["rework_attempt"] == 0

    def test_clears_feedback_for_the_next_stage(self, repo, tmp_path):
        # Carrying a previous stage's rejections forward would poison the
        # next stage's prompt.
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        state["review_feedback"] = ["old feedback"]
        assert nodes.advance(state, rt)["review_feedback"] == []

    def test_finalizes_when_the_stage_list_is_exhausted(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        (repo / "app.py").write_text("changed\n")
        assert nodes.advance(state, rt)["next_hop"] == "finalize"

    def test_continues_to_the_next_stage(self, repo, tmp_path):
        cfg, rt, state = make(
            repo,
            tmp_path,
            stages=[
                {"id": "one", "instruction": "a", "edit_files": ["app.py"]},
                {"id": "two", "instruction": "b", "edit_files": ["app.py"]},
            ],
        )
        (repo / "app.py").write_text("changed\n")
        out = nodes.advance(state, rt)
        assert out["next_hop"] == "precheck"
        assert out["stage_index"] == 1


class TestGate:
    def test_pauses_the_run_rather_than_failing_it(self, repo, tmp_path):
        cfg, rt, state = make(
            repo,
            tmp_path,
            stage_overrides={
                "id": "bump",
                "kind": "manual",
                "instruction": None,
                "human_steps": "bump it",
                "edit_files": [],
                "checks": ["true"],
            },
        )
        out = nodes.gate(state, rt)
        assert out["status"] == "awaiting_human"
        assert out["next_hop"] == "end"


class TestFinalize:
    def test_completes_when_the_full_suite_passes(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, full_test_command="true")
        out = nodes.finalize(state, rt)
        assert out["status"] == "complete"

    def test_escalates_when_the_full_suite_fails(self, repo, tmp_path):
        # Every stage passed on its own; their composition did not.
        cfg, rt, state = make(repo, tmp_path, full_test_command="exit 1")
        out = nodes.finalize(state, rt)
        assert out["next_hop"] == "escalate"

    def test_completes_when_no_full_suite_is_configured(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.finalize(state, rt)["status"] == "complete"


class TestEscalate:
    def test_records_which_stage_failed(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state["escalation_reason"] = "because"
        state["failure_layer"] = "scope"
        out = nodes.escalate(state, rt)
        assert out["status"] == "escalated"
        assert out["failed_stage_id"] == "s1"

    def test_does_not_append_to_history(self, repo, tmp_path):
        # history records completed stages. A run that escalates, gets fixed,
        # and is resumed would otherwise carry both an "escalated" and a
        # "complete" row for the same stage.
        cfg, rt, state = make(repo, tmp_path)
        state["history"] = [{"id": "earlier", "outcome": "complete"}]
        out = nodes.escalate(state, rt)
        assert "history" not in out or out["history"] == state["history"]

    def test_handles_escalating_after_the_last_stage(self, repo, tmp_path):
        # A full-suite failure in finalize runs with stage_index past the end.
        cfg, rt, state = make(repo, tmp_path)
        state["stage_index"] = 1
        out = nodes.escalate(state, rt)
        assert out["status"] == "escalated"
        assert out["failed_stage_id"] is None
