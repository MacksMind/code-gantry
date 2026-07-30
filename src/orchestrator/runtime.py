"""Everything the nodes need that is not run state.

LangGraph nodes receive only state, so the collaborators are bound in via a
Runtime the graph closes over. Keeping them here rather than reaching for
globals is what lets the node logic be tested with a stubbed planner, executor,
and reviewer and no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orchestrator.commands import CommandRunner
from orchestrator.config import ProjectConfig
from orchestrator.executor import Executor
from orchestrator.gitops import Git, GitError
from orchestrator.layout import summarize_layout
from orchestrator.plandoc import PlanTree, load_snapshot
from orchestrator.planner import PlannerClient
from orchestrator.reviewer import ReviewerClient

PROJECTS_ROOT = Path("projects")


class ProjectPaths:
    """Layout of `projects/<slug>/`.

    Everything the orchestrator owns lives here. The target repo receives
    product code and plan-document revisions, and nothing else.
    """

    def __init__(self, slug: str, root: Path | str = PROJECTS_ROOT):
        self.slug = slug
        self.root = Path(root)

    @property
    def project_dir(self) -> Path:
        return self.root / self.slug

    @property
    def config(self) -> Path:
        return self.project_dir / "config.yaml"

    @property
    def plan_snapshot(self) -> Path:
        return self.project_dir / "plan-snapshot"

    @property
    def status(self) -> Path:
        return self.project_dir / "status.md"

    @property
    def runs_dir(self) -> Path:
        return self.project_dir / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def ensure(self) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(exist_ok=True)


class RunPaths:
    def __init__(self, project: ProjectPaths, run_id: str):
        self.project = project
        self.run_id = run_id

    @property
    def run_dir(self) -> Path:
        return self.project.run_dir(self.run_id)

    @property
    def state_db(self) -> Path:
        return self.run_dir / "state.db"

    @property
    def run_log(self) -> Path:
        return self.run_dir / "run.log"

    @property
    def report(self) -> Path:
        return self.run_dir / "report.md"

    @property
    def metadata(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def pause_flag(self) -> Path:
        """Written by `orchestrator pause`, read before each planner call.

        A file rather than a signal: the run may be on another terminal, in a
        different session, or under nohup, and a file is the one channel that
        reaches it in all three without the process having to be found first.
        """
        return self.run_dir / "paused"

    def attempt_dir(
        self, index: int, stage_id: str, revision: int, attempt: int
    ) -> Path:
        return (
            self.run_dir
            / "stages"
            / f"{index:03d}-{stage_id}-rev-{revision}-attempt-{attempt}"
        )

    def ensure(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "stages").mkdir(exist_ok=True)


@dataclass
class Runtime:
    cfg: ProjectConfig
    project: ProjectPaths
    paths: RunPaths
    git: Git
    runner: CommandRunner
    executor: Executor
    planner: PlannerClient
    reviewer: ReviewerClient
    log: Callable[[str], None] = field(default=lambda _msg: None)
    _plan: PlanTree | None = None
    _layout: str | None = None

    @property
    def plan(self) -> PlanTree:
        """The run's plan snapshot, read once and held.

        The reviewer and planner judge against the plan as it stood when the run
        began; the planner's own revisions land in the live documents and show up
        as divergence in status.md. Nothing is silently substituted underneath
        them mid-run.
        """
        if self._plan is None:
            self._plan = load_snapshot(self.paths.project.plan_snapshot)
        return self._plan

    def layout(self, base_sha: str) -> str:
        """What the repository contains, read once and held.

        The planner authors globs; without this it guesses at paths, and a
        wrong guess costs a scope violation and an intervention per stage.
        Held for the run so it stays a stable, cacheable prompt prefix rather
        than shifting as stages add files.
        """
        if self._layout is None:
            try:
                paths = self.git.tracked_paths(base_sha)
            except GitError:
                # A layout we cannot read is not worth failing a run over; the
                # planner simply goes back to having no picture of the repo.
                self._layout = ""
            else:
                self._layout = summarize_layout(paths)
        return self._layout

    def write_artifact(
        self,
        index: int,
        stage_id: str,
        revision: int,
        attempt: int,
        name: str,
        body: str,
    ) -> Path:
        directory = self.paths.attempt_dir(index, stage_id, revision, attempt)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body)
        return path


def build_runtime(
    cfg: ProjectConfig,
    project: ProjectPaths,
    paths: RunPaths,
    planner: PlannerClient,
    reviewer: ReviewerClient,
    log: Callable[[str], None] | None = None,
) -> Runtime:
    logger = log or (lambda _msg: None)
    runner = CommandRunner(
        cwd=cfg.target_repo,
        timeout=cfg.limits.command_timeout_seconds,
        log=logger,
    )
    return Runtime(
        cfg=cfg,
        project=project,
        paths=paths,
        git=Git(cfg.target_repo),
        runner=runner,
        executor=Executor(cfg, runner),
        planner=planner,
        reviewer=reviewer,
        log=logger,
    )
