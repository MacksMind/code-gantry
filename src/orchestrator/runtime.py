"""Everything the nodes need that is not run state.

LangGraph nodes receive only state, so the collaborators are bound in via a
Runtime the graph closes over. Keeping them here rather than reaching for
globals is what lets the node logic be tested with a stubbed planner, executor,
and reviewer and no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

from orchestrator.commands import CommandRunner
from orchestrator.config import ProjectConfig, validate_stage
from orchestrator.executor import Executor
from orchestrator.gitops import Git, GitError
from orchestrator.layout import summarize_layout
from orchestrator.plandoc import PlanDocument, PlanTree, load_snapshot
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

    @property
    def live_plan(self) -> PlanTree:
        """The snapshot, with the progress log as it stands right now.

        The freeze above is right for every document that says what the work
        *is*: a run should not have its instructions change underneath it. It
        is exactly wrong for the one that says what has been *done*. The log
        was only frozen because it happens to be reachable by a markdown link
        from the plan root, not because anyone decided progress should be
        immutable.

        Measured on the first long run: the snapshot held 6,680 bytes of
        progress log while the file on disk had reached 114,554. The planner
        was reading 6% of the record, and the prompt's instruction to fetch the
        rest with `read_file` was taken twice in forty-nine derivations — so in
        practice the log did not inform planning at all. What kept the planner
        accurate was the live completed-stage history and its own searches over
        the code, which is why it kept rediscovering counts the log already
        knew.

        Read from the worktree rather than a commit. The orchestrator writes
        this file itself and commits it inside each stage, so between stages
        the worktree copy is the project branch's, and during one it is the
        same file the last landing left. There is no revision at which it is
        more current.

        The reviewer keeps the frozen *tree* — it judges a diff against what
        the stage was asked to do, and the plan documents must not move
        underneath it — but takes the live log separately, after its cache
        breakpoint. See `live_progress_log`.
        """
        tree = self.plan
        path = self.cfg.plan_addendum_path
        if not path:
            return tree
        try:
            content = (Path(self.cfg.target_repo) / path).read_text()
        except OSError:
            # Not yet written, or unreadable. The snapshot's copy — possibly
            # nothing — is still the best available answer, and a planner call
            # is far too expensive to fail over a missing progress file.
            return tree

        live = PlanDocument(path=path, content=content)
        children = [d for d in tree.children if d.path != path] + [live]
        if tree.root is not None and tree.root.path == path:
            return PlanTree(root=live, children=list(tree.children),
                            problems=tree.problems, skipped=tree.skipped)
        return PlanTree(
            root=tree.root,
            children=children,
            problems=tree.problems,
            skipped=tree.skipped,
        )

    def operations_context(self, sha: str) -> str:
        """The operational documents, for the planner alone.

        Same reader as `agent_context` and deliberately a separate
        entry point rather than a flag: the two audiences differ, and a
        boolean at the call site is how the executor ends up with the
        half it cannot act on.
        """
        return self._read_docs(sha, self.cfg.effective_operations_context)

    def agent_context(self, sha: str) -> str:
        """Conventions the repository documents for whoever works in it.

        A project with agents working in it keeps a file saying how it runs —
        what the container does on a Gemfile change, which files make a diff
        look wrong. Those facts had to be hand-copied into `planner.guidance`
        before this existed, and the copy drifted: one project's `AGENTS.md`
        recorded that editing the Gemfile reinstalls the bundle, the guidance
        said nothing, and the plan asserted the opposite across five items
        nobody drew because they read as blocked.

        Read from the commit rather than the worktree, for the reason the plan
        is: a run should reason about one fixed set of conventions rather than
        a set that moves under it while stages land.

        Deduplicated by content. `CLAUDE.md` is very often a symlink to
        `AGENTS.md`, and git stores the resolved text, so both paths come back
        byte-identical — the default would otherwise bill the same file twice.
        """
        return self._read_docs(sha, self.cfg.effective_agent_context)

    def _read_docs(self, sha: str, paths: list[str]) -> str:
        blocks: list[str] = []
        seen: set[str] = set()
        for path in paths:
            try:
                # A symlink's blob is its target path, so reading it as content
                # yields a document whose whole body is a filename. Follow it
                # once — `CLAUDE.md -> AGENTS.md` is the usual shape — and give
                # up rather than chase a chain.
                if self.git.is_symlink(sha, path):
                    target = self.git.show_file(sha, path).strip()
                    path = str(PurePosixPath(path).parent / target).lstrip("./")
                text = self.git.show_file(sha, path)
            except GitError:
                # Absent at this sha. The default names two files and most
                # projects have one, so this is the ordinary case, not a
                # problem worth reporting.
                continue
            body = (text or "").strip()
            if not body or body in seen:
                continue
            seen.add(body)
            blocks.append(f"### `{path}`\n\n{body}")
        return "\n\n".join(blocks)

    @property
    def live_progress_log(self) -> str | None:
        """The addendum as it stands now, or None if there isn't one.

        The same file `live_plan` splices in, handed over on its own so a
        caller can place it where it belongs. The reviewer needs that: the
        document has to sit *after* its cache breakpoint, because it grows on
        every landing and GPT-5.6 does not fall back to the longest matching
        prefix — inside the cached region it would miss on every stage.

        Never raises. A review is far too expensive to fail over a missing
        progress file, and a project without one is an ordinary case.
        """
        path = self.cfg.plan_addendum_path
        if not path:
            return None
        try:
            return (Path(self.cfg.target_repo) / path).read_text()
        except OSError:
            return None

    def layout(self, plan_sha: str) -> str:
        """What the repository contains, read once and held.

        The planner authors globs; without this it guesses at paths, and a
        wrong guess costs a scope violation and an intervention per stage.
        Held for the run so it stays a stable, cacheable prompt prefix rather
        than shifting as stages add files.

        Read at the project branch rather than the base: files the branch has
        added or moved are the ones a stage is most likely to touch next, and a
        layout describing the base would be wrong in exactly those places.
        Stability comes from reading once, not from which commit is read.
        """
        if self._layout is None:
            try:
                paths = self.git.tracked_paths(plan_sha)
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


def _stage_problems(cfg: ProjectConfig, fields: dict) -> list[str]:
    """The same check `nodes.py` applies, phrased for the planner.

    Building the stage can fail on its own — an id that is not a string, a
    glob list that is not a list — and that is as much a reason to answer
    again as a pattern that will not compile. Reported rather than raised, so
    a malformed field costs one more call instead of ending the run.
    """
    try:
        return validate_stage(cfg.stage_from_planner(fields), cfg)
    except Exception as e:  # noqa: BLE001 - any failure here is the model's
        return [f"the stage spec could not be read: {e}"]


def build_runtime(
    cfg: ProjectConfig,
    project: ProjectPaths,
    paths: RunPaths,
    planner: PlannerClient,
    reviewer: ReviewerClient,
    log: Callable[[str], None] | None = None,
) -> Runtime:
    logger = log or (lambda _msg: None)
    # The model clients wait out network outages themselves and have to be
    # able to say so — a silent fifteen-minute wait and a hung process look
    # identical from outside. They are built before the runtime exists, so
    # the log reaches them here rather than through their constructors.
    for client in (planner, reviewer):
        if hasattr(client, "log"):
            client.log = logger
    # The planner cannot check its own stage against project rules — they live
    # in config, and the schema has no way to express "this string compiles as
    # a regex". Told what is wrong it can usually fix it in one more call; not
    # told, a single bad character escalates to a human and discards the whole
    # tool loop that produced the stage. `nodes.py` still rejects the stage if
    # the second attempt is no better.
    if hasattr(planner, "validate_stage_fields"):
        planner.validate_stage_fields = lambda fields: _stage_problems(cfg, fields)
    runner = CommandRunner(
        cwd=cfg.target_repo,
        timeout=cfg.limits.command_timeout_seconds,
        log=logger,
    )
    git = Git(cfg.target_repo)
    return Runtime(
        cfg=cfg,
        project=project,
        paths=paths,
        git=git,
        runner=runner,
        # The executor takes git only to list tracked paths, which is what tells
        # the mention shield what counts as a path in the prompt it is handed.
        executor=Executor(cfg, runner, git=git),
        planner=planner,
        reviewer=reviewer,
        log=logger,
    )
