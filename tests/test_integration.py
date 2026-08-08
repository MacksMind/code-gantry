"""End-to-end runs through the real driver.

The real checkpointer, real git operations, real verify layers, real branch
topology. Only the three model calls are stubbed: a fake `aider` on PATH that
edits files, a scripted planner, and a scripted reviewer.
"""

import json
import pathlib
import os
import stat
import time
from dataclasses import dataclass, field

import pytest

from orchestrator.config import parse_config
from orchestrator.gitops import Git
from orchestrator.driver import default_max_steps, open_checkpointer
from orchestrator.driver import drive as drive_graph
from orchestrator.plandoc import PlanDocument, PlanTree
from orchestrator.planner import PlannerOutcome, PlannerUsage
from orchestrator.report import build_report
from orchestrator.reviewer import ReviewOutcome, TokenUsage
from orchestrator.runtime import ProjectPaths, RunPaths, Runtime
from orchestrator.state import new_state


@dataclass
class ScriptedPlanner:
    outcomes: list = field(default_factory=list)
    calls: int = 0

    def plan(self, messages):
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return PlannerOutcome(
            "project_complete", "plan executed", "e", usage=PlannerUsage(800, 700, 60)
        )


@dataclass
class ScriptedReviewer:
    outcomes: list = field(default_factory=list)
    calls: int = 0
    cache_keys: list = field(default_factory=list)

    def review(self, messages, cache_key=None):
        self.cache_keys.append(cache_key)
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return ReviewOutcome(
            verdict="approved", summary="fine", usage=TokenUsage(9000, 60, 8500)
        )


def stage_spec(**over):
    fields = {
        "id": "extract",
        "instruction": "Extract the thing.",
        "edit_files": ["app.py", "src/**"],
    }
    fields.update(over)
    return fields


@pytest.fixture
def scripted_edits(tmp_path, monkeypatch):
    """A scripted executor model, injected in place of the provider client.

    Replaces a fake `aider` binary on PATH. That worked while the executor was
    a subprocess and stopped meaning anything when it became a library call —
    the tests kept passing only because `provider` still defaulted to the
    subprocess, so they were exercising the path being deleted.

    Edits go through the real `FileEditor` rather than writing files directly.
    That is the point of doing it here: an integration test that bypassed the
    editor would skip scope enforcement, the `touched` bookkeeping the loop
    breaks on, and the refusal path — three things this suite exists to cover
    end to end.
    """
    from orchestrator import executorclient
    from orchestrator.executorclient import ExecutorTurn
    from orchestrator.repotools import ToolError

    queue = tmp_path / "edits.json"
    queue.write_text("[]")

    class Scripted:
        def __init__(self, *a, **kw):
            pass

        def run(self, conversation, reader, editor, semantic=None, cache_key=None):
            out = ExecutorTurn()
            out.turns = 1
            out.stopped = True
            steps = json.loads(queue.read_text())
            if steps:
                step = steps.pop(0)
                queue.write_text(json.dumps(steps))
                for name, text in step.items():
                    target = pathlib.Path(editor.repo) / name
                    try:
                        if target.exists() and target.read_text():
                            editor.delete_file(name)
                        editor.create_file(name, text)
                    except ToolError:
                        # Out of scope, and the tool is right to refuse. Written
                        # directly so the *gate* still sees it — which is the
                        # case the scope layer is documented to exist for now
                        # that the executor cannot get there: a check that
                        # rewrites a file, or a human's work on a resume.
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(text)
            return out

    monkeypatch.setattr(executorclient, "OpenAIExecutorModel", Scripted)
    return queue


def drive(repo, tmp_path, planner=None, reviewer=None, state=None, run_id="r1", **cfg_over):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "full_test_command": "true",
        "executor": {"model": "openai/local"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(cfg_over)
    cfg = parse_config(data)

    project = ProjectPaths("proj-slug", root=tmp_path / "projects")
    project.ensure()
    paths = RunPaths(project, run_id)
    paths.ensure()

    from orchestrator.commands import CommandRunner
    from orchestrator.executor import Executor

    runner = CommandRunner(cwd=repo, timeout=60)
    checkpoint, conn = open_checkpointer(paths.state_db)
    try:
        rt = Runtime(
            cfg=cfg,
            project=project,
            paths=paths,
            git=Git(repo),
            runner=runner,
            executor=Executor(cfg, runner),
            planner=planner or ScriptedPlanner(),
            reviewer=reviewer or ScriptedReviewer(),
        )
        rt._plan = PlanTree(root=PlanDocument(path="PLAN.md", content="# The plan"))

        base_sha = rt.git.ensure_project_branch("proj", "main")
        if state is None:
            state = new_state(
                run_id=run_id,
                project_slug="proj-slug",
                config_hash="hash",
                target_repo=str(repo),
                base_ref="main",
                base_sha=base_sha,
                project_branch="proj",
                started_at=time.time(),
            )
        final = drive_graph(
            rt, state, checkpoint=checkpoint,
            max_steps=default_max_steps(60, 3, 2, 12),
        )
        return cfg, project, paths, final
    finally:
        conn.close()


class TestTwoStageProject:
    def test_completes_and_lands_one_commit_per_stage(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(
            json.dumps([{"app.py": "first stage\n"}, {"src/two.py": "second stage\n"}])
        )
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "first", "e", stage_fields=stage_spec(id="one")),
            PlannerOutcome("next_stage", "second", "e", stage_fields=stage_spec(id="two")),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)

        assert final["status"] == "complete"
        assert [e["id"] for e in final["completed"]] == ["one", "two"]

        g = Git(repo)
        assert g.current_branch() == "proj"
        log = g._out("log", "--pretty=%s", "-3")
        assert "[one]" in log and "[two]" in log

        # One commit per stage, counted rather than sampled. A project with
        # `plan_addendum_path` set adds a second commit per stage that produced
        # observations — deliberately separate, so the commit the reviewer
        # approved and the suite went green on stays exactly what landed. This
        # config has no addendum, so the count here is the bare invariant.
        assert len(g._out("log", "--oneline", "main..proj").splitlines()) == 2

    def test_child_branches_are_deleted_after_landing(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        drive(repo, tmp_path, planner=planner)
        assert Git(repo).branches_matching("proj-stage/") == []

    def test_base_ref_is_never_touched(self, repo, tmp_path, scripted_edits):
        before = Git(repo).rev_parse("main")
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        drive(repo, tmp_path, planner=planner)
        assert Git(repo).rev_parse("main") == before

    def test_reviewer_is_called_once_per_landed_stage(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}, {"src/b.py": "b\n"}]))
        reviewer = ScriptedReviewer()
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec(id="one")),
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec(id="two")),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        drive(repo, tmp_path, planner=planner, reviewer=reviewer)
        assert reviewer.calls == 2

    def test_writes_artifacts_per_revision_and_attempt(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        directory = paths.attempt_dir(0, "extract", 0, 0)
        assert (directory / "prompt.md").exists()
        assert (directory / "executor.log").exists()
        assert (directory / "verify.log").exists()
        assert json.loads((directory / "review.json").read_text())["verdict"] == "approved"

    def test_what_the_reviewer_read_reaches_the_artifact(
        self, repo, tmp_path, scripted_edits
    ):
        # The whole journey, not its endpoints. `tool_calls` is computed in the
        # client, carried on ReviewOutcome and rendered by as_dict into
        # review.json — three hops, and every previous value lost in transit
        # here passed its unit tests on both ends.
        #
        # It is also the only record of whether a verdict was reached by
        # looking. An approval from a reviewer that read the permit list and one
        # from a reviewer that read nothing are indistinguishable without it,
        # and those are the two cases worth telling apart.
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        reviewer = ScriptedReviewer(outcomes=[
            ReviewOutcome(
                verdict="approved",
                summary="checked the permit list",
                usage=TokenUsage(9000, 60, 8500),
                tool_calls=["read_file(app/models/discount.rb)", "search(permit)"],
            )
        ])
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner,
                                           reviewer=reviewer)
        written = json.loads(
            (paths.attempt_dir(0, "extract", 0, 0) / "review.json").read_text()
        )
        assert written["tool_calls"] == [
            "read_file(app/models/discount.rb)",
            "search(permit)",
        ]

    def test_verify_log_records_why_a_commandless_gate_failed(
        self, repo, tmp_path, scripted_edits
    ):
        # The scope and pattern gates fail without running a command, so a
        # verify.log built only from command results comes out empty — leaving
        # the artifact that should explain an escalation blank. Found during
        # the first live run, where diagnosing a pattern failure meant reading
        # planner.json instead.
        scripted_edits.write_text(json.dumps([{"outside.py": "leaked\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome(
                "next_stage", "r", "e",
                stage_fields=stage_spec(edit_files=["app.py"]),
            ),
            PlannerOutcome("blocked", "giving up", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        log = (paths.attempt_dir(0, "extract", 0, 0) / "verify.log").read_text()
        assert log.strip(), "a failing gate must record why"
        assert "scope" in log.lower()
        assert "outside.py" in log, "and must name what actually went wrong"

    def test_status_log_accumulates_planner_decisions(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "deriving", "FIRST ENTRY", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "SECOND ENTRY"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        body = project.status.read_text()
        assert "FIRST ENTRY" in body and "SECOND ENTRY" in body
        assert body.index("FIRST ENTRY") < body.index("SECOND ENTRY")


class TestPlannerInterventionLoop:
    def test_a_scope_violation_is_recovered_by_widening(self, repo, tmp_path, scripted_edits):
        # The executor touches a file outside its box; the planner widens the
        # stage; the existing work stands and the stage lands.
        scripted_edits.write_text(
            json.dumps([{"app.py": "in scope\n", "extra.py": "out of scope\n"}])
        )
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome(
                "revise", "those files belong here", "e",
                stage_fields=stage_spec(edit_files=["app.py", "src/**", "extra.py"]),
                revision_mode="extend",
            ),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)

        assert final["status"] == "complete"
        assert final["completed"][0]["revisions"] == 1
        # The work survived rather than being redone.
        assert (repo / "extra.py").exists()

    def test_a_declined_path_is_reverted_but_the_stage_still_lands(
        self, repo, tmp_path, scripted_edits
    ):
        scripted_edits.write_text(
            json.dumps([
                {"app.py": "hours of work\n", "extra.py": "wandered\n"},
                {"app.py": "hours of work, still here\n"},
            ])
        )
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome(
                "revise", "that file does not belong", "e",
                stage_fields=stage_spec(), revision_mode="extend",
            ),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        assert final["status"] == "complete"
        assert not (repo / "extra.py").exists()

    def test_an_extend_revision_lands_without_running_the_executor_again(
        self, repo, tmp_path, scripted_edits
    ):
        # The reason the branch is kept. A second scripted edit is queued and
        # must still be there at the end: the gates read what is already on the
        # branch instead of asking for it a second time. Before this, a stage
        # whose work was complete went back to the executor under an instruction
        # that still described it as undone, and the executor deleted the line
        # above its target to produce a change that had already been made.
        scripted_edits.write_text(
            json.dumps([
                {"app.py": "correct work\n", "extra.py": "wandered\n"},
                {"app.py": "the executor should never be asked for this\n"},
            ])
        )
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome(
                "revise", "that file does not belong", "e",
                stage_fields=stage_spec(), revision_mode="extend",
            ),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)

        assert final["status"] == "complete"
        assert not (repo / "extra.py").exists()
        assert (repo / "app.py").read_text() == "correct work\n"
        assert json.loads(scripted_edits.read_text()) == [
            {"app.py": "the executor should never be asked for this\n"}
        ]

    def test_a_blocked_review_routes_to_the_planner_not_a_human(
        self, repo, tmp_path, scripted_edits
    ):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}, {"app.py": "b\n"}]))
        reviewer = ScriptedReviewer([
            ReviewOutcome(verdict="blocked", summary="instruction is wrong"),
            ReviewOutcome(verdict="approved", summary="better"),
        ])
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome(
                "revise", "rewriting the instruction", "e",
                stage_fields=stage_spec(instruction="Extract it properly."),
                revision_mode="restart",
            ),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner, reviewer=reviewer
        )
        assert final["status"] == "complete"
        assert final["planner_interventions"] == 1

    def test_restart_discards_the_branch_and_re_cuts(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(
            json.dumps([{"app.py": "wrong approach\n"}, {"app.py": "right approach\n"}])
        )
        reviewer = ScriptedReviewer([
            ReviewOutcome(verdict="blocked", summary="wrong approach"),
            ReviewOutcome(verdict="approved", summary="ok"),
        ])
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome(
                "revise", "start over", "e",
                stage_fields=stage_spec(), revision_mode="restart",
            ),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner, reviewer=reviewer
        )
        assert final["status"] == "complete"
        assert (repo / "app.py").read_text() == "right approach\n"

    def test_exhausted_test_retries_reach_the_planner(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": f"try {i}\n"} for i in range(8)]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("blocked", "the tests cannot pass as specified", "e"),
        ])
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner, test_command="exit 1"
        )
        assert final["status"] == "escalated"
        assert planner.calls == 2


class TestEscalationPaths:
    def test_a_planner_block_escalates(self, repo, tmp_path, scripted_edits):
        planner = ScriptedPlanner([PlannerOutcome("blocked", "the plan contradicts itself", "e")])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        assert final["status"] == "escalated"
        assert "contradicts" in final["escalation_reason"]

    def test_setup_failure_escalates_to_a_human(self, repo, tmp_path, scripted_edits):
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec())
        ])
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner, setup_command="exit 1"
        )
        assert final["status"] == "escalated"
        assert final["failure_layer"] == "setup"

    def test_exhausted_planner_budget_escalates(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": f"try {i}\n"} for i in range(20)]))
        # A planner that keeps revising without ever fixing anything.
        planner = ScriptedPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec())]
            + [
                PlannerOutcome(
                    "revise", "again", "e", stage_fields=stage_spec(),
                    revision_mode="restart",
                )
                for _ in range(10)
            ]
        )
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner, test_command="exit 1",
            limits={"max_planner_interventions": 2, "max_test_retries": 1},
        )
        assert final["status"] == "escalated"
        assert "budget is exhausted" in final["escalation_reason"]

    def test_a_red_full_suite_at_the_end_escalates(self, repo, tmp_path, scripted_edits):
        planner = ScriptedPlanner([PlannerOutcome("project_complete", "done", "e")])
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner, full_test_command="exit 1"
        )
        assert final["status"] == "escalated"
        assert "project branch tip" in final["escalation_reason"]


class TestResume:
    def test_a_repo_state_failure_resumes_at_verify(self, repo, tmp_path, scripted_edits):
        # The human fixes the repo; the run must check the fix rather than
        # re-running the stage and discarding it.
        scripted_edits.write_text(json.dumps([{"app.py": "work\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            # The planner cannot fix a failing operator-authored check, so it
            # correctly hands the run to a human.
            PlannerOutcome("blocked", "the check needs a file only a human can make", "e"),
        ])
        cfg, project, paths, first = drive(
            repo, tmp_path, planner=planner,
            stage_defaults={"checks": ["test -f fixed.txt"]},
            limits={"max_test_retries": 0},
        )
        assert first["status"] == "escalated"

        # The human does the fix, leaving it uncommitted as they would.
        (repo / "fixed.txt").write_text("done\n")
        planner2 = ScriptedPlanner([PlannerOutcome("project_complete", "done", "e")])
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner2,
            stage_defaults={"checks": ["test -f fixed.txt"]},
            limits={"max_test_retries": 0},
            state={"resuming": True, "next_hop": ""},
            run_id="r1",
        )
        assert final["status"] == "complete"

    def test_state_is_readable_after_the_run(self, repo, tmp_path, scripted_edits):
        planner = ScriptedPlanner([PlannerOutcome("project_complete", "done", "e")])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)

        checkpoint, conn = open_checkpointer(paths.state_db)
        try:
            from orchestrator.commands import CommandRunner
            from orchestrator.executor import Executor

            runner = CommandRunner(cwd=repo, timeout=60)
            rt = Runtime(
                cfg=cfg, project=project, paths=paths, git=Git(repo), runner=runner,
                executor=Executor(cfg, runner), planner=None, reviewer=None,
            )
            # Read back from the checkpoint table rather than through a
            # compiled graph: state is a row now, and reading it needs no
            # runtime at all.
            from orchestrator.driver import load_state

            assert load_state(paths.state_db, "r1")["status"] == "complete"
        finally:
            conn.close()


class TestReportOnRealRun:
    def test_describes_landed_stages_and_costs(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "e", usage=PlannerUsage(900, 800, 70)),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        report = build_report(final, cfg)
        assert "extract" in report
        assert "Stage records" in report
        assert "Reviewer" in report and "Planner" in report
        # The economic check: cached proportion is visible.
        assert "cached" in report

    def test_reports_the_session_against_its_wall_clock_budget(
        self, repo, tmp_path, scripted_edits
    ):
        # The first real run is how an operator learns whether wall_clock_hours
        # and max_stages are compatible numbers, so the figure must be printed
        # on a clean completion, not only when it is exceeded.
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        assert final["status"] == "complete"
        assert final["session_seconds"] >= 0
        report = build_report(final, cfg)
        assert "Session wall clock" in report

    def test_warns_when_max_stages_cannot_fit_the_budget(self, repo, tmp_path, scripted_edits):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}]))
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        # One stage took some measurable time; at 60 stages of that, a budget of
        # a few seconds cannot possibly hold. The time comes from the stage
        # itself — derivation plus the stage — and `session_seconds` is set to
        # something absurd here to prove it is no longer the source: it resets
        # on every resume while `completed` spans the run, and dividing one by
        # the other reported a minute a stage for stages taking eleven.
        final = {
            **final,
            "session_seconds": 1.0,
            "completed": [
                {**final["completed"][0], "wall_seconds": 400.0, "plan_seconds": 200.0}
            ],
        }
        cfg.limits.max_stages = 60
        cfg.limits.wall_clock_hours = 1
        report = build_report(final, cfg)
        assert "limits disagree" in report or "disagree about how" in report
        assert "0.17h per landed stage" in report, report

    def test_a_wall_clock_stop_escalates_with_an_honest_reason(
        self, repo, tmp_path, scripted_edits
    ):
        planner = ScriptedPlanner([
            PlannerOutcome("next_stage", "r", "e", stage_fields=stage_spec()),
        ])
        cfg, project, paths, final = drive(
            repo, tmp_path, planner=planner, limits={"wall_clock_hours": 1e-12}
        )
        assert final["status"] == "escalated"
        assert final["failure_layer"] == "budget"
        assert "wall_clock_hours" in final["escalation_reason"]
        # Not a defect: the report must not imply something broke.
        assert "Nothing is broken" in final["escalation_reason"]

    def test_describes_an_escalation_with_the_resume_hint(self, repo, tmp_path, scripted_edits):
        planner = ScriptedPlanner([PlannerOutcome("blocked", "cannot proceed", "e")])
        cfg, project, paths, final = drive(repo, tmp_path, planner=planner)
        report = build_report(final, cfg)
        assert "Why it stopped" in report
        assert "orchestrator resume" in report


class TestDeferredPlanSteps:
    """A deferral must outlive the call that made it.

    The whole point is that the planner mentions a skipped step once and the
    orchestrator remembers it thereafter — through later stages, into the
    report, and into the exit code.
    """

    def _run_with_deferral(self, repo, tmp_path, scripted_edits, resolve=False):
        scripted_edits.write_text(json.dumps([{"app.py": "a\n"}, {"src/b.py": "b\n"}]))
        deferral = {
            "plan_step": "Audit CloudWatch logs",
            "reason": "needs AWS credentials this run does not have",
            "blocked_on": "AWS access",
            "safe_because": "nothing later reads the audit output",
        }
        second = dict(deferral, resolved=True) if resolve else {}
        planner = ScriptedPlanner([
            PlannerOutcome(
                "next_stage", "r", "e",
                stage_fields=stage_spec(id="one"), deferred=[deferral],
            ),
            PlannerOutcome(
                "next_stage", "r", "e",
                stage_fields=stage_spec(id="two"),
                deferred=[second] if second else [],
            ),
            PlannerOutcome("project_complete", "done", "e"),
        ])
        return drive(repo, tmp_path, planner=planner)

    def test_it_survives_later_stages_that_never_mention_it(
        self, repo, tmp_path, scripted_edits
    ):
        cfg, project, paths, final = self._run_with_deferral(repo, tmp_path, scripted_edits)
        assert final["status"] == "complete"
        assert [d["plan_step"] for d in final["deferred"]] == ["Audit CloudWatch logs"]

    def test_the_report_says_the_plan_was_not_finished(
        self, repo, tmp_path, scripted_edits
    ):
        cfg, project, paths, final = self._run_with_deferral(repo, tmp_path, scripted_edits)
        report = build_report(final, cfg)
        assert "Deferred plan steps" in report
        assert "AWS access" in report
        assert "not verified" in report

    def test_a_complete_run_with_deferrals_exits_distinctly(
        self, repo, tmp_path, scripted_edits
    ):
        from orchestrator.cli import EXIT_DEFERRED, EXIT_OK, _exit_code

        cfg, project, paths, final = self._run_with_deferral(repo, tmp_path, scripted_edits)
        assert _exit_code(final) == EXIT_DEFERRED
        assert EXIT_DEFERRED != EXIT_OK

    def test_resolving_it_restores_a_clean_exit(self, repo, tmp_path, scripted_edits):
        from orchestrator.cli import EXIT_OK, _exit_code

        cfg, project, paths, final = self._run_with_deferral(
            repo, tmp_path, scripted_edits, resolve=True
        )
        assert final["deferred"][0]["resolved"] is True
        assert _exit_code(final) == EXIT_OK

    def test_the_planner_is_shown_its_own_outstanding_deferrals(
        self, repo, tmp_path, scripted_edits
    ):
        from orchestrator.prompts import build_planner_messages

        messages = build_planner_messages(
            cfg=None,
            plan=PlanTree(root=PlanDocument(path="p.md", content="plan")),
            completed=[],
            deferred=[{"plan_step": "Audit CloudWatch logs", "reason": "no creds"}],
        )
        # Not in the cached prefix: the deferred list changes as the run
        # proceeds, and holding it there evicted the plan and the layout with
        # it on every change. It must still reach the planner, just later.
        leading = messages[0]["content"][0]["text"]
        assert "Audit CloudWatch logs" not in leading
        # Everything after the two cached blocks. The planner now returns a
        # single message whose blocks are plan, history, then the volatile
        # tail, so "the rest" is that tail rather than later messages.
        rest = "".join(b["text"] for b in messages[0]["content"][2:])
        assert "Audit CloudWatch logs" in rest


class TestACrashReachesTheRunLog:
    """A node that raises must say so where an operator will find it.

    `graph.invoke` was wrapped in try/finally with no except, so an exception
    propagated to stdout — which for an unattended run is a nohup file the next
    resume overwrites. The run log, the artifact anyone actually tails, said
    nothing.

    Observed: `advance` raised inside `squash_merge` when the project's own
    pre-commit hook rejected a line the executor had written with trailing
    whitespace. The log stopped mid-stage after "recorded 5 plan
    observation(s)", the repository was left with the merge staged and
    uncommitted, and reconstructing it meant reading SQUASH_MSG off the
    filesystem three hours later.
    """

    def test_the_exception_is_written_to_the_run_log(self, tmp_path):
        from orchestrator.runlog import RunLog

        path = tmp_path / "run.log"
        log = RunLog(path)
        try:
            try:
                raise RuntimeError("squash merge of 'x' into 'y' failed: hook said no")
            except Exception as exc:
                import traceback as tb

                log(f"[crash] {type(exc).__name__}: {exc}")
                for line in tb.format_exc().splitlines():
                    log(f"[crash] {line}")
        finally:
            log.close()

        written = path.read_text()
        assert "[crash] RuntimeError: squash merge" in written
        assert "Traceback" in written, "the traceback is what makes it diagnosable"

    def test_the_driver_has_an_except_clause(self):
        # The defect was structural: try/finally with nothing catching. Pin it,
        # because it reads as complete and is not.
        import inspect
        from orchestrator import cli

        source = inspect.getsource(cli._drive)
        assert "except Exception" in source
        assert "[crash]" in source
        assert "raise" in source, "logging must not swallow the failure"
