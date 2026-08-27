"""Everything the nodes need that is not run state.

Nodes receive state and a Runtime, and the collaborators are bound into the
latter. Keeping them here rather than reaching for globals is what lets the
node logic be tested with a stubbed planner, executor, and reviewer and no
network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from code_gantry.cachekey import cache_key
from code_gantry.commands import CommandRunner
from code_gantry.config import ProjectConfig, validate_stage
from code_gantry.executor import Executor
from code_gantry.gitops import Git, GitError
from code_gantry.layout import summarize_layout
from code_gantry.plandoc import PlanDocument, PlanTree, load_snapshot
from code_gantry.planner import PlannerClient
from code_gantry.reviewer import ReviewerClient

class ProjectPaths:
    """Layout of the work directory: everything CodeGantry writes.

    Built from `cfg.work_dir` rather than a slug under a fixed root. The
    CodeGantry's own tree holds code and nothing else, and the work dir
    defaults beside the plan documents in the target repo — the plan says what
    the migration is and this says what happened to it, and a later reader
    wants them together.

    There is no `config` property any more, and its absence is the point. The
    config used to live here and be found from the slug; now it is an input
    that locates everything else, so a path that derived it would be deriving
    the thing that was handed to us.

    `slug` survives as the directory's name, because run ids and log lines
    read better with a short handle than with an absolute path.
    """

    def __init__(self, work_dir: Path | str):
        self.work_dir = Path(work_dir)

    @property
    def slug(self) -> str:
        """What identifies this project: the work directory, as given.

        Not generated. A slug used to be a directory name under `projects/`
        and had to be invented for each project; now the config names its work
        dir outright, and a derived handle would only be a second name for it
        — free to disagree, and one more thing to keep in step.

        It has exactly one load-bearing use, which sets the requirement:
        `nodes` builds the prompt cache key from it, so it must be stable
        across a project's runs and distinct between projects. A path is both
        by construction. Run ids do not use it — those slugify
        `project_branch` — so nothing is lost by it being long.

        The first cut of this returned `work_dir.name`, which is
        `.code_gantry` under the default layout: a cache key shared by every
        project on the machine, and a report line naming nothing.
        """
        return str(self.work_dir)

    @property
    def project_dir(self) -> Path:
        return self.work_dir

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
    def tool_log(self) -> Path:
        """Every role's reads, live, kept out of the timeline.

        `run.log` is a timeline of node transitions and decisions and its
        reader is a person reconstructing why a run stopped; the planner alone
        makes ~25 reads a decision. One file per run rather than per decision,
        because that is what `tail -f` wants.
        """
        return self.run_dir / "tools.log"

    @property
    def report(self) -> Path:
        return self.run_dir / "report.md"

    @property
    def metadata(self) -> Path:
        return self.run_dir / "run.json"

    @property
    def pause_flag(self) -> Path:
        """Written by `code-gantry pause`, read before each planner call.

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

        Read from the worktree rather than a commit. CodeGantry writes
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
                # yields a document whose whole body is a filename. `real_path`
                # follows one hop — `CLAUDE.md -> AGENTS.md` is the usual shape
                # — and the preflight check that asks whether these documents
                # have moved calls the same thing, so it cannot resolve to a
                # different file than the run reads.
                path = self.git.real_path(sha, path)
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
    def live_test_warnings(self) -> str | None:
        """The runner's warning tally as it stands now, or None.

        Read live for the same reason the progress log is: it describes the
        tree rather than the run, and a copy frozen at run start would be
        answering about code that has since been changed by the very stages
        that are meant to act on it.

        Never raises. A project may not keep one, and a planner call is far
        too expensive to fail over a missing file.
        """
        path = getattr(self.cfg, "test_warnings_path", None)
        if not path:
            return None
        try:
            return (Path(self.cfg.target_repo) / path).read_text()
        except OSError:
            return None

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


def pin_modules() -> None:
    """Load everything a run will need, before it needs it.

    Several modules are imported inside functions rather than at the top of a
    file, to break import cycles — `executorloop` needs `executor`, which needs
    `gates`, and `verify` needs all three. That is fine for correctness and
    creates a hazard for operation: a module not yet imported is read from disk
    at the moment it is first needed, so editing this codebase while a run is
    live can put *new* code in front of an *old* one already in memory.

    That is not hypothetical. `ExecutorConfig` was loaded at process start
    without a field, `executorloop.py` was edited, and the first agent stage
    forty seconds later imported the new file against the old class:
    `AttributeError: 'ExecutorConfig' object has no attribute
    'semantic_search'`, and the run stopped. Had a stage already run, the
    module would have been cached and nothing would have happened — which is
    the worst property of it, because the safety of an edit depended on
    invisible timing.

    So the whole set is pinned here, at the point a run is assembled. After
    this returns, every module the run can reach is in memory and the ordinary
    expectation holds again: edits take effect at the next restart, and a live
    run finishes on the code it started with. `report` and `pricing` are
    included precisely because they load last — a run lasting hours would
    otherwise read them fresh at the end.

    The whole set, and **derived from the package rather than written out**.
    Which modules are reached lazily is a property of every import here and
    changes whenever someone breaks a cycle, so a list is right on the day it
    is written and silently wrong afterwards — `configversion` and
    `projecttools` were both added after this existed and neither reached it,
    leaving a run free to load either fresh, mid-flight, from a file edited
    since it started. Walking the package is the one answer that cannot go
    stale, and it makes adding a module require nothing.

    The test beside this measures in a subprocess. Asked inside the suite it
    reads a `sys.modules` already populated by whatever else ran, which is how
    those two stayed missing: it only failed when a worker happened not to have
    imported them.
    """
    import importlib
    import pkgutil

    import code_gantry

    for module in pkgutil.iter_modules(code_gantry.__path__):
        importlib.import_module(f"code_gantry.{module.name}")


def build_runtime(
    cfg: ProjectConfig,
    project: ProjectPaths,
    paths: RunPaths,
    planner: PlannerClient,
    reviewer: ReviewerClient,
    log: Callable[[str], None] | None = None,
    tool_log: Callable[[str], None] | None = None,
) -> Runtime:
    # First, so a run holds every module it can reach before it starts. See
    # `pin_modules`: without this, editing the codebase during a live run can
    # put new code in front of an old class already in memory.
    pin_modules()
    logger = log or (lambda _msg: None)
    # The model clients wait out network outages themselves and have to be
    # able to say so — a silent fifteen-minute wait and a hung process look
    # identical from outside. They are built before the runtime exists, so
    # the log reaches them here rather than through their constructors.
    for client in (planner, reviewer):
        if hasattr(client, "log"):
            client.log = logger
        # Reads go to their own file so the timeline stays a timeline. Absent
        # in tests that build a runtime directly, where the fallback to the run
        # log is what keeps the reporting visible at all.
        if tool_log is not None and hasattr(client, "tool_log"):
            client.tool_log = tool_log
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
    # An operator-declared tool is argv, and until now only the executor had
    # anything to spawn it with — so a project could offer the planner a tool
    # and the planner would refuse every call to it. Bound here, beside the log
    # and the tool log, because the clients are built before the run exists and
    # this is the only place that has both. The declared menu goes with it: the
    # reviewer's factory never took one at all, and a role holding tools it
    # cannot run is the same defect as a role that was never offered them.
    # The gateway session these two belong to, scoped per run like the
    # executor's. Bound here rather than in their factories for the reason
    # everything else in this loop is: the clients are built before the run
    # exists, and this is the only place that has both. Harmless against a
    # first-party endpoint, where `gateway_body` sends nothing at all.
    for label, client in (("plan", planner), ("review", reviewer)):
        client.session_id = cache_key(label, paths.run_id) if paths.run_id else ""
    for client in (planner, reviewer):
        if hasattr(client, "runner"):
            client.runner = runner
        if hasattr(client, "project_tools") and not getattr(client, "project_tools", None):
            client.project_tools = list(cfg.project_tools or [])
    # A routing policy is *not* resolved here any more. It was, once per run,
    # and the run turned out to be the wrong unit: the answer was re-sampled
    # only when a human restarted, so one run held a model for 30 stages and a
    # different one for the 11 after a resume. `precheck` asks per stage now,
    # which follows a price move mid-run and gives every stage one model to
    # attribute its cost and its rework to. Preflight still probes the
    # endpoint, because "is the router reachable with this key" is a question
    # worth answering before a run starts rather than on stage one.
    #
    # What this costs until `precheck` runs: `dialect_for` refuses a policy, so
    # the executor falls back to whatever its client already speaks. That was
    # true between this line and the first stage before, too.
    git = Git(cfg.target_repo)
    return Runtime(
        cfg=cfg,
        project=project,
        paths=paths,
        git=git,
        runner=runner,
        # The executor takes git only to list tracked paths, which is what tells
        # the mention shield what counts as a path in the prompt it is handed.
        executor=Executor(
            cfg, runner, git=git, log=logger, tool_log=tool_log,
            # Scopes the gateway session to this run. `paths` is the
            # only thing here that knows the run id, and the executor
            # is built before the run has done anything.
            run_id=paths.run_id,
        ),
        planner=planner,
        reviewer=reviewer,
        log=logger,
    )
