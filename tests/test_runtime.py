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

from code_gantry.config import parse_config
from code_gantry.planner import AnthropicPlanner
from code_gantry.reviewer import OpenAIReviewer
from code_gantry.runtime import ProjectPaths, RunPaths, build_runtime


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
    project = ProjectPaths(tmp_path / "projects" / "proj")
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
    project = ProjectPaths(tmp_path / "projects" / "proj")
    return build_runtime(
        cfg,
        project,
        RunPaths(project, "run-1"),
        AnthropicPlanner(cfg.planner, client=object()),
        OpenAIReviewer(cfg.reviewer, client=object()),
    )


class TestAgentContextDocuments:
    """Repository conventions, read at the plan sha and frozen.

    A project that has agents working in it keeps a file telling them how it
    works. The planner could not read it, so the same facts had to be
    hand-copied into `planner.guidance` — and the copy drifted: `AGENTS.md`
    recorded that editing the Gemfile reinstalls the bundle, the guidance said
    nothing, and the plan asserted the opposite across five items nobody drew.

    Read from the commit, not the worktree, for the same reason the plan is:
    the run should be reasoning about one fixed set of conventions, not a set
    that moves under it while stages land.
    """

    def _repo_with(self, repo, run_git, files: dict, sha_only=True):
        for name, body in files.items():
            (repo / name).write_text(body)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "conventions")
        return run_git(repo, "rev-parse", "HEAD")

    def _rt(self, repo, tmp_path, **over):
        cfg = a_config(repo).model_copy(update=over)
        project = ProjectPaths(tmp_path / "projects" / "proj")
        return build_runtime(
            cfg, project, RunPaths(project, "run-1"),
            AnthropicPlanner(cfg.planner, client=object()),
            OpenAIReviewer(cfg.reviewer, client=object()),
        )

    def test_it_reads_the_configured_documents(self, repo, run_git, tmp_path):
        sha = self._repo_with(repo, run_git, {"AGENTS.md": "bundle installs itself"})
        rt = self._rt(repo, tmp_path)
        assert "bundle installs itself" in rt.agent_context(sha)

    def test_a_missing_document_is_skipped_silently(self, repo, run_git, tmp_path):
        # The default names two files and most projects have one.
        sha = self._repo_with(repo, run_git, {"AGENTS.md": "only this one"})
        assert "only this one" in self._rt(repo, tmp_path).agent_context(sha)

    def test_none_present_yields_nothing(self, repo, run_git, tmp_path):
        sha = run_git(repo, "rev-parse", "HEAD")
        assert self._rt(repo, tmp_path).agent_context(sha) == ""

    def test_identical_documents_are_read_once(self, repo, run_git, tmp_path):
        # CLAUDE.md is very often a symlink to AGENTS.md; git stores the
        # resolved content, so both paths come back byte-identical and the
        # planner would otherwise pay for the same file twice.
        sha = self._repo_with(
            repo, run_git, {"AGENTS.md": "same text", "CLAUDE.md": "same text"}
        )
        assert self._rt(repo, tmp_path).agent_context(sha).count("same text") == 1

    def test_each_document_is_named(self, repo, run_git, tmp_path):
        # The planner should be able to say where a convention came from.
        sha = self._repo_with(repo, run_git, {"AGENTS.md": "a convention"})
        assert "AGENTS.md" in self._rt(repo, tmp_path).agent_context(sha)

    def test_an_empty_configured_list_reads_nothing(self, repo, run_git, tmp_path):
        sha = self._repo_with(repo, run_git, {"AGENTS.md": "ignored"})
        rt = self._rt(repo, tmp_path, agent_context=[])
        assert rt.agent_context(sha) == ""

    def test_a_symlinked_document_is_followed(self, repo, run_git, tmp_path):
        # `CLAUDE.md -> AGENTS.md` is the common shape, and git stores a
        # symlink as a blob containing its target. Read naively, the planner
        # gets a convention document whose entire body is the word
        # "AGENTS.md".
        (repo / "AGENTS.md").write_text("the bundle installs itself\n")
        (repo / "CLAUDE.md").symlink_to("AGENTS.md")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "conventions")
        sha = run_git(repo, "rev-parse", "HEAD")

        text = self._rt(repo, tmp_path).agent_context(sha)
        assert text.count("the bundle installs itself") == 1
        assert "\n\nAGENTS.md" not in text, "the link target leaked in as content"

    def test_a_dangling_symlink_is_skipped(self, repo, run_git, tmp_path):
        (repo / "CLAUDE.md").symlink_to("nothing-here.md")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "dangling")
        sha = run_git(repo, "rev-parse", "HEAD")
        assert self._rt(repo, tmp_path).agent_context(sha) == ""


class TestARunHoldsEveryModuleItCanReach:
    """Editing this codebase during a live run must not change that run.

    Several modules are imported inside functions to break cycles, so a module
    not yet needed is read from disk at the moment it first is. That made the
    safety of an edit depend on invisible timing: `ExecutorConfig` was in
    memory without a field, `executorloop.py` was edited, and the next agent
    stage imported the new file against the old class. Had a stage already run,
    the module would have been cached and nothing would have happened.
    """

    def test_no_module_is_left_to_load_later(self):
        """Measured in a fresh process, because this one has imported things.

        Run inside the suite, `sys.modules` already holds whatever other tests
        imported, so the assertion passed while two modules — `configversion`
        and `projecttools` — were absent from the pin list for as long as they
        had existed. It failed only when xdist happened to give this test a
        worker that had not imported them. A guard whose verdict depends on
        what ran before it is not a guard.
        """
        import subprocess
        import sys

        probe = (
            "import pathlib, sys\n"
            "from code_gantry.runtime import pin_modules\n"
            "pin_modules()\n"
            "loaded = {m.split('.')[-1] for m in sys.modules"
            " if m.startswith('code_gantry.')}\n"
            "on_disk = {p.stem for p in pathlib.Path('src/code_gantry').glob('*.py')"
            " if p.stem != '__init__'}\n"
            "print(','.join(sorted(on_disk - loaded)))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        missing = [m for m in out.stdout.strip().split(",") if m]
        assert not missing, f"not pinned: {missing}"

    def test_pinning_happens_when_the_runtime_is_assembled(self):
        # Not at import time and not on first use: the point a run is built is
        # the last moment before it can be affected by an edit.
        import inspect

        from code_gantry.runtime import build_runtime

        body = inspect.getsource(build_runtime)
        assert "pin_modules()" in body.split("logger = log")[0]


class TestEveryRoleGetsBothLogs:
    """Wiring, pinned, because this is where it keeps going missing.

    `Executor.__init__` carries the comment "Assigned by `build_runtime`" and
    `build_runtime` never assigned it: the loop that binds the logger names the
    planner and the reviewer, and the executor is constructed on its own line
    with no `log`. So `self.log` was `None` for the whole of the executor's
    life, and two sites went with it — `run_loop`'s "checks rewrote files;
    committed as", which has been emitted **zero** times across every run, and
    the transport-retry line, whose absence is exactly the case the runtime
    docstring warns about: "a silent fifteen-minute wait and a hung process
    look identical from outside."

    Then the same thing happened again on the way past. `tool_log` was added to
    `OpenAIExecutorModel`, the draining method was written, and nothing passed
    it — so `tools.log` carried the planner and the reviewer and not one
    executor line. Two changes, both correct in isolation, and the capability
    between them was never connected.

    Asserted at the seam rather than by reading either file, because both
    defects review as fine: the constructor takes the argument, the caller
    exists, and only running it shows they are not joined.
    """

    def _built(self, cfg, tmp_path):
        project = ProjectPaths(tmp_path / "projects" / "proj")
        timeline, tools = [], []
        rt = build_runtime(
            cfg,
            project,
            RunPaths(project, "run-1"),
            AnthropicPlanner(cfg.planner, client=object()),
            OpenAIReviewer(cfg.reviewer, client=object()),
            log=timeline.append,
            tool_log=tools.append,
        )
        return rt, timeline, tools

    def test_the_executor_gets_the_run_log(self, repo, tmp_path):
        rt, timeline, _tools = self._built(a_config(repo), tmp_path)
        assert rt.executor.log is not None
        rt.executor.log("hello")
        assert timeline == ["hello"]

    def test_the_executor_gets_the_tool_log(self, repo, tmp_path):
        rt, _timeline, tools = self._built(a_config(repo), tmp_path)
        assert rt.executor.tool_log is not None
        rt.executor.tool_log("read")
        assert tools == ["read"]

    def test_all_three_roles_are_bound(self, repo, tmp_path):
        rt, _t, _s = self._built(a_config(repo), tmp_path)
        for role in (rt.planner, rt.reviewer, rt.executor):
            assert role.log is not None, role
            assert role.tool_log is not None, role

    def test_the_executors_model_is_handed_both(self, repo, tmp_path, monkeypatch):
        # One step further out: the executor holding them is not the same as
        # the provider client receiving them, and that is the join that broke.
        from code_gantry import executorclient

        seen = {}

        class Spy:
            def __init__(self, cfg, client=None, log=None, tool_log=None, **kwargs):
                seen["log"], seen["tool_log"] = log, tool_log

            def run(self, *a, **kw):  # pragma: no cover - never reached
                raise AssertionError

        monkeypatch.setattr(executorclient, "OpenAIExecutorModel", Spy)
        rt, timeline, tools = self._built(a_config(repo), tmp_path)
        from code_gantry.config import Stage

        try:
            rt.executor.run_agent_stage(
                Stage(id="s", instruction="do", edit_files=["a.py"]), "prompt"
            )
        except AssertionError:
            pass
        assert seen["log"] is not None and seen["tool_log"] is not None
