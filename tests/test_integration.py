"""End-to-end runs against a fixture repo with a stubbed reviewer.

Everything below drives the real graph, the real checkpointer, the real
verify layers and the real git operations. Only the two model calls are
stubbed: the executor is replaced with a script that edits files, and the
reviewer returns canned verdicts.
"""

import json
import os
import stat
from dataclasses import dataclass, field

import pytest

from orchestrator.config import parse_config
from orchestrator.gitops import Git
from orchestrator.graph import build_graph, open_checkpointer, recursion_limit
from orchestrator.report import build_report
from orchestrator.reviewer import Issue, ReviewOutcome, TokenUsage
from orchestrator.runtime import RunPaths, build_runtime
from orchestrator.state import new_state


@dataclass
class ScriptedReviewer:
    outcomes: list = field(default_factory=list)
    calls: int = 0

    def review(self, messages):
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return ReviewOutcome(
            verdict="approved", summary="fine", usage=TokenUsage(1000, 40, 900)
        )


@pytest.fixture
def fake_aider(tmp_path, monkeypatch):
    """An `aider` that performs whatever edit the current step demands.

    It reads a queue of edits from a file so the test can script a sequence of
    attempts — a bad first attempt, a good second one.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    queue = tmp_path / "edits.json"
    queue.write_text("[]")
    script = bindir / "aider"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"q = pathlib.Path({str(queue)!r})\n"
        "edits = json.loads(q.read_text())\n"
        "if edits:\n"
        "    step = edits.pop(0)\n"
        "    q.write_text(json.dumps(edits))\n"
        "    for name, text in step.items():\n"
        "        p = pathlib.Path(name)\n"
        "        p.parent.mkdir(parents=True, exist_ok=True)\n"
        "        p.write_text(text)\n"
        "print('aider done')\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return queue


def drive(repo, tmp_path, stages, reviewer=None, run_id="r1", state=None, **cfg_over):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "branch": "work",
        "test_command": "true",
        "executor": {"model": "openai/local"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": stages,
    }
    data.update(cfg_over)
    cfg = parse_config(data)

    paths = RunPaths(tmp_path / "runs", run_id)
    paths.ensure()
    saver, conn = open_checkpointer(paths.state_db)
    try:
        rt = build_runtime(cfg, paths, reviewer or ScriptedReviewer())
        graph = build_graph(rt, checkpointer=saver)
        if state is None:
            state = new_state(
                run_id=run_id,
                config_path="c.yaml",
                target_repo=str(repo),
                base_ref="main",
                base_sha=Git(repo).head_sha(),
                branch="work",
                stage_ids=[s.id for s in cfg.stages],
            )
        final = graph.invoke(
            state,
            {
                "configurable": {"thread_id": run_id},
                "recursion_limit": recursion_limit(len(cfg.stages), 3, 2),
            },
        )
        return cfg, paths, final
    finally:
        conn.close()


AGENT_STAGE = {
    "id": "extract",
    "instruction": "Extract the thing.",
    "edit_files": ["app.py", "src/**"],
}


class TestTwoStageRun:
    def test_completes_and_commits_each_stage(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(
            json.dumps([{"app.py": "first stage\n"}, {"src/two.py": "second stage\n"}])
        )
        stages = [
            dict(AGENT_STAGE, id="one"),
            dict(AGENT_STAGE, id="two"),
        ]
        cfg, paths, final = drive(repo, tmp_path, stages)

        assert final["status"] == "complete"
        assert [e["id"] for e in final["history"]] == ["one", "two"]
        assert all(e["outcome"] == "complete" for e in final["history"])
        assert Git(repo).is_clean()

    def test_each_stage_gets_its_own_commit(self, repo, tmp_path, fake_aider, run_git):
        fake_aider.write_text(
            json.dumps([{"app.py": "first\n"}, {"src/two.py": "second\n"}])
        )
        stages = [dict(AGENT_STAGE, id="one"), dict(AGENT_STAGE, id="two")]
        drive(repo, tmp_path, stages)
        subjects = run_git(repo, "log", "--pretty=%s", "-3")
        assert "[one]" in subjects
        assert "[two]" in subjects

    def test_reviewer_is_called_once_per_stage(self, repo, tmp_path, fake_aider):
        # The economic premise: the paid model fires at checkpoints, not
        # continuously.
        fake_aider.write_text(json.dumps([{"app.py": "a\n"}, {"src/b.py": "b\n"}]))
        reviewer = ScriptedReviewer()
        drive(
            repo,
            tmp_path,
            [dict(AGENT_STAGE, id="one"), dict(AGENT_STAGE, id="two")],
            reviewer=reviewer,
        )
        assert reviewer.calls == 2

    def test_writes_artifacts_per_attempt(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"app.py": "a\n"}]))
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE])
        directory = paths.attempt_dir(0, "extract", 0)
        assert (directory / "prompt.md").exists()
        assert (directory / "executor.log").exists()
        assert (directory / "verify.log").exists()
        assert json.loads((directory / "review.json").read_text())["verdict"] == "approved"


class TestReworkLoop:
    def test_a_rejected_attempt_is_retried_and_then_approved(
        self, repo, tmp_path, fake_aider
    ):
        fake_aider.write_text(
            json.dumps([{"app.py": "bad attempt\n"}, {"app.py": "good attempt\n"}])
        )
        reviewer = ScriptedReviewer(
            outcomes=[
                ReviewOutcome(
                    verdict="rework",
                    summary="Not quite.",
                    issues=[Issue(severity="major", file="app.py", description="Fix it.")],
                ),
                ReviewOutcome(verdict="approved", summary="Better."),
            ]
        )
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE], reviewer=reviewer)

        assert final["status"] == "complete"
        assert final["history"][0]["rework_attempts"] == 1
        assert (repo / "app.py").read_text() == "good attempt\n"

    def test_rework_feedback_reaches_the_second_prompt(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"app.py": "bad\n"}, {"app.py": "good\n"}]))
        reviewer = ScriptedReviewer(
            outcomes=[
                ReviewOutcome(verdict="rework", summary="DISTINCTIVE FEEDBACK"),
                ReviewOutcome(verdict="approved", summary="ok"),
            ]
        )
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE], reviewer=reviewer)
        second = (paths.attempt_dir(0, "extract", 1) / "prompt.md").read_text()
        assert "DISTINCTIVE FEEDBACK" in second

    def test_reset_leaves_one_clean_diff_not_two_attempts(
        self, repo, tmp_path, fake_aider, run_git
    ):
        # rework_reset exists so the stage's commit is single-purpose.
        fake_aider.write_text(
            json.dumps([{"app.py": "bad\n", "src/junk.py": "junk\n"}, {"app.py": "good\n"}])
        )
        reviewer = ScriptedReviewer(
            outcomes=[
                ReviewOutcome(verdict="rework", summary="no"),
                ReviewOutcome(verdict="approved", summary="yes"),
            ]
        )
        drive(repo, tmp_path, [AGENT_STAGE], reviewer=reviewer)
        assert not (repo / "src" / "junk.py").exists()

    def test_exhausted_rework_escalates(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(
            json.dumps([{"app.py": "a\n"}, {"app.py": "b\n"}, {"app.py": "c\n"}])
        )
        reviewer = ScriptedReviewer(
            outcomes=[
                ReviewOutcome(verdict="rework", summary="no 1"),
                ReviewOutcome(verdict="rework", summary="no 2"),
                ReviewOutcome(verdict="rework", summary="no 3"),
            ]
        )
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE], reviewer=reviewer)
        assert final["status"] == "escalated"
        assert "max_rework_retries" in final["escalation_reason"]


class TestEscalationPaths:
    def test_blocked_verdict_stops_immediately(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"app.py": "a\n"}]))
        reviewer = ScriptedReviewer(
            outcomes=[ReviewOutcome(verdict="blocked", summary="The plan is wrong.")]
        )
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE], reviewer=reviewer)
        assert final["status"] == "escalated"
        assert reviewer.calls == 1
        assert "The plan is wrong." in final["escalation_reason"]

    def test_out_of_scope_edit_escalates_without_calling_the_reviewer(
        self, repo, tmp_path, fake_aider
    ):
        # The scope guard is a containment gate and runs before any paid call.
        fake_aider.write_text(json.dumps([{"wandered/off.py": "nope\n"}]))
        reviewer = ScriptedReviewer()
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE], reviewer=reviewer)
        assert final["status"] == "escalated"
        assert final["failed_stage_id"] == "extract"
        assert final["failure_layer"] == "scope"
        assert reviewer.calls == 0

    def test_forbidden_pattern_escalates_after_retries(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"app.py": "optional: true\n"}] * 5))
        reviewer = ScriptedReviewer()
        stage = dict(AGENT_STAGE, forbidden_patterns=["optional: true"])
        cfg, paths, final = drive(repo, tmp_path, [stage], reviewer=reviewer)
        assert final["status"] == "escalated"
        assert final["failure_layer"] == "patterns"
        assert reviewer.calls == 0

    def test_failing_tests_escalate_after_retries(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"app.py": f"try {i}\n"} for i in range(6)]))
        cfg, paths, final = drive(
            repo, tmp_path, [AGENT_STAGE], test_command="exit 1"
        )
        assert final["status"] == "escalated"
        assert final["verify_attempt"] == 3

    def test_failed_precondition_escalates_before_the_executor_runs(
        self, repo, tmp_path, fake_aider
    ):
        fake_aider.write_text(json.dumps([{"app.py": "should not happen\n"}]))
        stage = dict(AGENT_STAGE, preconditions=["false"])
        cfg, paths, final = drive(repo, tmp_path, [stage])
        assert final["status"] == "escalated"
        assert (repo / "app.py").read_text() == "def hello():\n    return 1\n"

    def test_full_suite_failure_at_the_end_escalates(self, repo, tmp_path, fake_aider):
        # Each stage passed on its own; their composition did not.
        fake_aider.write_text(json.dumps([{"app.py": "a\n"}]))
        cfg, paths, final = drive(
            repo,
            tmp_path,
            [AGENT_STAGE],
            test_command="true",
            full_test_command="exit 1",
        )
        assert final["status"] == "escalated"
        assert "full suite failed" in final["escalation_reason"]


class TestScriptStage:
    def test_runs_without_an_executor_and_is_still_reviewed(self, repo, tmp_path, fake_aider):
        reviewer = ScriptedReviewer()
        stages = [
            {
                "id": "annotate",
                "kind": "script",
                "command": "echo annotated > app.py",
                "edit_files": ["app.py"],
            }
        ]
        cfg, paths, final = drive(repo, tmp_path, stages, reviewer=reviewer)
        assert final["status"] == "complete"
        assert reviewer.calls == 1
        assert not fake_aider.parent.joinpath("aider-was-called").exists()

    def test_uncommitted_script_output_is_still_seen_by_the_gates(
        self, repo, tmp_path, fake_aider
    ):
        # A script leaves its transform uncommitted. With a `<sha>..HEAD` diff
        # the scope guard would pass on nothing.
        stages = [
            {
                "id": "annotate",
                "kind": "script",
                "command": "echo x > wandered.py",
                "edit_files": ["app.py"],
            }
        ]
        cfg, paths, final = drive(repo, tmp_path, stages)
        assert final["status"] == "escalated"
        assert final["failure_layer"] == "scope"


class TestManualGateAndResume:
    STAGES = [
        {
            "id": "bump",
            "kind": "manual",
            "human_steps": "Bump the runtime and deploy.",
            "checks": ["test -f bumped.txt"],
        },
        {"id": "after", "instruction": "Do the follow-up.", "edit_files": ["src/**"]},
    ]

    def test_run_pauses_at_the_manual_stage(self, repo, tmp_path, fake_aider):
        cfg, paths, final = drive(repo, tmp_path, self.STAGES)
        assert final["status"] == "awaiting_human"
        assert final["history"] == []

    def test_pausing_does_not_run_later_stages(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"src/after.py": "should not happen\n"}]))
        drive(repo, tmp_path, self.STAGES)
        assert not (repo / "src" / "after.py").exists()

    def test_resume_verifies_the_human_work_and_continues(self, repo, tmp_path, fake_aider):
        cfg, paths, first = drive(repo, tmp_path, self.STAGES)
        assert first["status"] == "awaiting_human"

        # The human does the work the gate asked for.
        (repo / "bumped.txt").write_text("done\n")
        fake_aider.write_text(json.dumps([{"src/after.py": "follow-up\n"}]))

        cfg, paths, final = drive(
            repo, tmp_path, self.STAGES, state={"next_hop": ""}, run_id="r1"
        )
        assert final["status"] == "complete"
        assert [e["id"] for e in final["history"]] == ["bump", "after"]

    def test_resume_escalates_if_the_human_work_is_not_there(
        self, repo, tmp_path, fake_aider
    ):
        drive(repo, tmp_path, self.STAGES)
        # Resume without doing the work: the check must fail.
        cfg, paths, final = drive(
            repo, tmp_path, self.STAGES, state={"next_hop": ""}, run_id="r1"
        )
        assert final["status"] == "escalated"

    def test_manual_stage_is_not_reviewed(self, repo, tmp_path, fake_aider):
        cfg, paths, first = drive(repo, tmp_path, self.STAGES)
        (repo / "bumped.txt").write_text("done\n")
        fake_aider.write_text(json.dumps([{"src/after.py": "x\n"}]))
        reviewer = ScriptedReviewer()
        cfg, paths, final = drive(
            repo,
            tmp_path,
            self.STAGES,
            reviewer=reviewer,
            state={"next_hop": ""},
            run_id="r1",
        )
        # Only the agent stage that follows it.
        assert reviewer.calls == 1


class TestCheckpointSurvival:
    def test_state_is_readable_after_the_run(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"app.py": "a\n"}]))
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE])

        saver, conn = open_checkpointer(paths.state_db)
        try:
            rt = build_runtime(cfg, paths, reviewer=None)
            graph = build_graph(rt, checkpointer=saver)
            snapshot = graph.get_state({"configurable": {"thread_id": "r1"}})
            assert snapshot.values["status"] == "complete"
        finally:
            conn.close()


class TestReportOnRealRun:
    def test_report_describes_a_completed_run(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"app.py": "a\n"}]))
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE])
        report = build_report(final, cfg)
        assert "extract" in report
        assert "reset --hard" in report
        assert "cached" in report

    def test_report_describes_an_escalation(self, repo, tmp_path, fake_aider):
        fake_aider.write_text(json.dumps([{"wandered/off.py": "x\n"}]))
        cfg, paths, final = drive(repo, tmp_path, [AGENT_STAGE])
        report = build_report(final, cfg)
        assert "escalated" in report.lower()
        assert "scope" in report
