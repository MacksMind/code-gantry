"""Everything the nodes need that is not run state.

LangGraph nodes receive only state, so the collaborators are bound in via a
Runtime the graph closes over. Keeping them here rather than reaching for
globals is what lets the node logic be tested with a stubbed executor and
reviewer and no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orchestrator.commands import CommandRunner
from orchestrator.config import RunConfig
from orchestrator.executor import Executor
from orchestrator.gitops import Git
from orchestrator.reviewer import ReviewerClient


class RunPaths:
    """Layout of `./runs/<run_id>/`. The target repo stays free of
    orchestrator artifacts."""

    def __init__(self, root: Path | str, run_id: str):
        self.root = Path(root)
        self.run_id = run_id

    @property
    def run_dir(self) -> Path:
        return self.root / self.run_id

    @property
    def state_db(self) -> Path:
        return self.run_dir / "state.db"

    @property
    def run_log(self) -> Path:
        return self.run_dir / "run.log"

    @property
    def report(self) -> Path:
        return self.run_dir / "report.md"

    def attempt_dir(self, index: int, stage_id: str, attempt: int) -> Path:
        return self.run_dir / "stages" / f"{index}-{stage_id}-attempt-{attempt}"

    def ensure(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "stages").mkdir(exist_ok=True)


@dataclass
class Runtime:
    cfg: RunConfig
    paths: RunPaths
    git: Git
    runner: CommandRunner
    executor: Executor
    reviewer: ReviewerClient
    log: Callable[[str], None] = field(default=lambda _msg: None)

    def write_attempt_artifact(
        self, index: int, stage_id: str, attempt: int, name: str, body: str
    ) -> Path:
        directory = self.paths.attempt_dir(index, stage_id, attempt)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body)
        return path


def build_runtime(
    cfg: RunConfig,
    paths: RunPaths,
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
        paths=paths,
        git=Git(cfg.target_repo),
        runner=runner,
        executor=Executor(cfg, runner),
        reviewer=reviewer,
        log=logger,
    )
