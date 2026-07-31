"""Command line interface.

    orchestrator init <plan-doc>      draft a project config from a plan document
    orchestrator validate <project>   prove the config works on this host
    orchestrator approve <project>    record that a human read it
    orchestrator run <project>        start a run
    orchestrator pause <run_id>       stop cleanly at the next stage boundary
    orchestrator resume <run_id>      continue after an interruption or escalation
    orchestrator status <run_id>      where a run stopped and why

Exit codes: 0 complete, 1 failed or escalated, 2 complete with deferred steps.

`init` may prompt — it is a human at a terminal doing one-time setup. `run` and
`resume` execute unattended and must never block on input.

"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from orchestrator.addendum import append_notes
from orchestrator.approval import approval_problem, config_hash, record_approval
from orchestrator.config import ConfigError, ProjectConfig, load_config
from orchestrator.discover import derive_target_repo, draft_config
from orchestrator.gitops import Git, GitError
from orchestrator.graph import build_graph, open_checkpointer, recursion_limit
from orchestrator.plandoc import resolve_plan_tree, snapshot_tree
from orchestrator.planner import make_planner
from orchestrator.preflight import format_checks, run_preflight
from orchestrator.report import build_report
from orchestrator.reviewer import make_reviewer
from orchestrator.runlog import RunLog
from orchestrator.runtime import PROJECTS_ROOT, ProjectPaths, RunPaths, build_runtime
from orchestrator.state import new_state

EXIT_OK = 0
EXIT_FAILED = 1
# Complete, but the planner skipped part of the plan. Distinct from both, so a
# script can tell "finished" from "finished, with work outstanding" without
# treating a deferral as a failure or waving it through as a success.
EXIT_DEFERRED = 2


@click.group()
def main() -> None:
    """Drive a long refactor with a local executor, a planner, and a reviewer."""


@main.command()
@click.argument("plan_doc", type=click.Path(exists=True, path_type=Path))
@click.option("--slug", default=None, help="Project directory name under projects/.")
def init(plan_doc: Path, slug: str | None) -> None:
    """Draft a project config from a plan document.

    Discovery splits along the same line as the planner's write permissions:
    executable fields come from deterministic repo inspection, never a model.
    """
    repo = derive_target_repo(plan_doc)
    if repo is None:
        click.echo(
            f"{plan_doc} is not inside a git repository.\n\n"
            "The plan document must live in the target repo: the repo copy is "
            "what the orchestrator operates against and what the planner "
            "revises. Copy it in, commit it, and re-run init against the copy.",
            err=True,
        )
        sys.exit(EXIT_FAILED)

    plan_rel = plan_doc.resolve().relative_to(repo.resolve()).as_posix()
    slug = slug or _slugify(plan_doc.stem)
    project = ProjectPaths(slug)

    if project.config.exists() and not click.confirm(
        f"{project.config} already exists. Overwrite?", default=False
    ):
        click.echo("left alone")
        sys.exit(EXIT_OK)

    project.ensure()
    draft, notes = draft_config(repo, plan_rel)
    project.config.write_text(draft)

    click.echo(f"wrote {project.config}\n")
    for note in notes:
        click.echo(f"  {note}")
    click.echo(
        "\nEvery discovered field carries a provenance comment, so reviewing it "
        "is a check of reasoning rather than of values. Read it, fix what is "
        f"wrong, then:\n\n  orchestrator validate {slug}\n"
        f"  orchestrator approve {slug}"
    )


@main.command()
@click.argument("slug")
@click.option("--skip-tests", is_flag=True, help="Do not run the test suites.")
def validate(slug: str, skip_tests: bool) -> None:
    """Prove the config works against this host, before approval."""
    project = ProjectPaths(slug)
    cfg = _load(project.config)
    checks = run_preflight(
        cfg, project_dir=project, run_tests=not skip_tests, check_approval=False
    )
    click.echo(format_checks(checks))

    blocking = [c for c in checks if c.blocking]
    warnings = [c for c in checks if not c.ok and not c.fatal]
    click.echo("")
    if blocking:
        click.echo(f"{len(blocking)} blocking problem(s)", err=True)
        sys.exit(EXIT_FAILED)
    click.echo(
        f"config works on this host ({len(warnings)} warning(s)). "
        f"Approve it with: orchestrator approve {slug}"
    )


@main.command()
@click.argument("slug")
@click.option(
    "--dry-run", is_flag=True, help="Print the observations without writing them."
)
def reconcile(slug: str, dry_run: bool) -> None:
    """Check the plan against what the branch actually did, and record the drift.

    Plan documents are written before the work and go stale during it. After
    thirteen landed stages on the first real project the 4.2 checklist still
    claimed twenty-four `render text:` sites across nine controllers, when seven
    remained in one; and three `alias_method_chain` sites, when none did.

    A run's `plan_notes` catch drift as it happens. This is for drift that
    already happened — work landed before the mechanism existed, or by hand, or
    by someone else.

    Deliberately separate from `run`. Reconciling is a judgement about what the
    work has become, and doing it mid-run would let a run rewrite its own
    premises. It changes no plan document either: it appends observations for a
    later pass to fold in.
    """
    project = ProjectPaths(slug)
    cfg = _load(project.config)
    git = Git(cfg.target_repo)

    if not cfg.plan_addendum_path and not dry_run:
        raise click.ClickException(
            "no plan_addendum_path configured; nowhere to record observations"
        )

    planner = make_planner(cfg.planner, cfg.target_repo)
    if planner.reader is None:
        raise click.ClickException(
            "planner.repo_access must be on: reconciling means checking the "
            "plan against the repository, which needs the read tools"
        )

    landed = git.commits_between(cfg.base_ref, cfg.project_branch)
    click.echo(
        f"reconciling {cfg.plan_root} against {len(landed)} commit(s) on "
        f"{cfg.project_branch}"
    )

    outcome = planner.plan(_reconcile_prompt(cfg))

    # A failed call is not a verdict. This was learned the hard way: an expired
    # API key produced `blocked` with an empty tool log, which the first
    # version of this code read as "the planner chose not to look" — and I
    # wrote a retry, and a commit message calling the planner stochastic, on
    # three data points that were all 401s.
    if outcome.failed:
        raise click.ClickException(f"the planner could not be reached: {outcome.reasoning}")

    for line in outcome.tool_calls:
        click.echo(f"  {line}")

    # An answer reached without looking is not an answer, whichever way it
    # went. Recording "checked, nothing found" would be worse than recording
    # nothing: it reads as evidence and stops anyone looking again.
    if not outcome.tool_calls:
        raise click.ClickException(
            "the planner answered without reading anything, so its verdict is "
            "worth nothing either way. Nothing was written."
        )

    if not outcome.plan_notes:
        click.echo(
            f"\nnothing to add, after {len(outcome.tool_calls)} read(s); "
            "the log already reflects what the branch has done"
        )
        return

    click.echo(f"\n{len(outcome.plan_notes)} observation(s):")
    for note in outcome.plan_notes:
        click.echo(f"\n  {note.get('plan_step')}")
        if note.get("supersedes"):
            click.echo(f"    plan says: {note['supersedes']}")
        click.echo(f"    observed:  {note.get('observation')}")

    if dry_run:
        click.echo("\n--dry-run: nothing written")
        return

    written = append_notes(
        cfg.target_repo,
        cfg.plan_addendum_path,
        outcome.plan_notes,
        stage_id="reconcile",
    )
    click.echo(f"\nrecorded in {written}")
    click.echo(
        "Not committed: read it, then commit it yourself. Folding these into "
        "the plan documents is a separate judgement."
    )


def _reconcile_prompt(cfg: ProjectConfig) -> list[dict]:
    """The reconcile instruction — catch the progress log up with the branch.

    Runs outside a stage, so it is the one place that can record work landed
    before the log existed, or by a run whose planner did not write an entry.
    """
    log = cfg.plan_addendum_path or "the progress log"
    plan_dir = cfg.plan_root.rsplit("/", 1)[0] if "/" in cfg.plan_root else "."
    return [
        {
            "role": "user",
            "content": (
                f"The branch {cfg.project_branch!r} carries work that "
                f"{cfg.base_ref!r} does not, and the progress log may not "
                "record all of it. Catch the log up.\n\n"
                f"**Read `{log}` first.** It is the record of what has already "
                "been reported, and re-reporting something it covers is the "
                "one way this pass does damage — a reader cannot tell a "
                "duplicate from a second, independent confirmation.\n\n"
                f"Then read the plan, starting at `{cfg.plan_root}` and the "
                "documents it links. Use `git_diff` between "
                f"{cfg.base_ref} and {cfg.project_branch} to see what the "
                "branch actually did, and `search` to check the plan's "
                "specific claims — counts, file lists, 'occurrences across N "
                "files' — against the code as it is now. A count in a document "
                "is a claim about the moment someone wrote it; the code is the "
                "fact.\n\n"
                f"**Changes under `{plan_dir}/` are not progress and are not "
                "work.** The branch range carries every edit the plan corpus "
                "has ever had — documents renamed or relocated, cross-refs "
                "rewritten, entries added to the log itself. All of it will "
                "show up in the diff and none of it advances a plan step. "
                "Report what changed in the *code*, and what that means for "
                "the plan. Ignore what changed in the plan.\n\n"
                "Return `project_complete`, and put every finding in "
                "`plan_notes` — one entry per plan step whose state the log "
                "does not yet reflect: work that has been done, a count that "
                "has moved, a claim that was wrong when written. State where "
                "the step stands now, as a total.\n\n"
                "`plan_notes` is the entire output of this pass. It is the "
                "only part that gets written to the log; a finding described "
                "in your reasoning and left out of `plan_notes` is discarded. "
                "Do not propose a stage; nothing is being built here.\n\n"
                "Report only what you verified against the code. A note nobody "
                "can check is worse than none, because someone will act on it."
            ),
        }
    ]


@main.command()
@click.argument("slug")
def approve(slug: str) -> None:
    """Record that a human read this config.

    There is no `approved: true` field, because such a field could be set by
    anything. Approval is a hash of the exact bytes reviewed.
    """
    project = ProjectPaths(slug)
    cfg = _load(project.config)

    click.echo("Commands this config will run unattended:\n")
    for label, command in cfg.all_commands():
        click.echo(f"  {label}: {command}")
    click.echo("")

    approval = record_approval(
        project.project_dir, project.config, now=datetime.now(timezone.utc).isoformat()
    )
    click.echo(f"approved {approval.config_sha256[:16]} at {approval.approved_at}")
    click.echo("Editing the config invalidates this and requires approving again.")


@main.command()
@click.argument("slug")
@click.option("--run-id", default=None, help="Override the generated run id.")
@click.option(
    "--skip-preflight-tests",
    is_flag=True,
    help="Skip the suites during preflight. Faster, but an already-red repo "
    "will not be caught.",
)
def run(slug: str, run_id: str | None, skip_preflight_tests: bool) -> None:
    """Start a run against a project."""
    project = ProjectPaths(slug)
    cfg = _load(project.config)

    problem = approval_problem(project.project_dir, project.config)
    if problem:
        click.echo(f"refusing to start: {problem}", err=True)
        sys.exit(EXIT_FAILED)

    checks = run_preflight(cfg, project_dir=project, run_tests=not skip_preflight_tests)
    click.echo(format_checks(checks))
    if any(c.blocking for c in checks):
        click.echo("\npreflight failed; nothing was run", err=True)
        sys.exit(EXIT_FAILED)

    run_id = run_id or _generate_run_id(cfg)
    paths = RunPaths(project, run_id)
    paths.ensure()

    git = Git(cfg.target_repo)
    # Rework discards child branches, and the reflog is the only recovery path
    # for an attempt the operator later wants to inspect.
    previous_gc = git.disable_gc()
    base_sha = git.ensure_project_branch(cfg.project_branch, cfg.base_ref)
    # Read the plan from the project branch, not the base. A long migration
    # maintains its documents on the branch for months and merges once at the
    # end; reading at base_ref shows the plan as it was before any of that.
    plan_sha = git.rev_parse(cfg.project_branch)

    # Snapshot it as it stands at run start. The reviewer and planner judge
    # against this snapshot, so nothing is substituted underneath them mid-run.
    tree = resolve_plan_tree(git, cfg.plan_root, plan_sha)
    if not tree.ok:
        click.echo("plan could not be resolved:\n" + "\n".join(tree.problems), err=True)
        git.restore_gc(previous_gc)
        sys.exit(EXIT_FAILED)
    snapshot_tree(tree, project.plan_snapshot)

    state = new_state(
        run_id=run_id,
        project_slug=slug,
        config_hash=config_hash(project.config),
        target_repo=str(cfg.target_repo),
        base_ref=cfg.base_ref,
        base_sha=base_sha,
        plan_sha=plan_sha,
        project_branch=cfg.project_branch,
        started_at=time.time(),
    )
    _write_metadata(paths, slug, run_id)

    click.echo(
        f"\nrun {run_id} on {cfg.project_branch} "
        f"(plan: {len(tree.documents)} document(s))\n"
    )
    try:
        code = _drive(cfg, project, paths, state)
    finally:
        git.restore_gc(previous_gc)
    sys.exit(code)


@main.command()
@click.argument("run_id")
@click.option("--note", default="", help="Why, recorded for when you come back.")
def pause(run_id: str, note: str) -> None:
    """Ask a running run to stop at the next stage boundary.

    Not a kill. The flag is read before each planner call, so the run finishes
    whatever stage is in flight, lands it or fails it normally, and stops with
    nothing half-done and a clean tree. Interrupting the process instead leaves
    a partially applied executor edit and a stage branch nobody owns.
    """
    project, _cfg = _locate_run(run_id)
    paths = RunPaths(project, run_id)
    if not paths.run_dir.is_dir():
        click.echo(f"no such run: {run_id}", err=True)
        sys.exit(EXIT_FAILED)

    paths.pause_flag.write_text(note)
    click.echo(
        f"{run_id} will stop after the stage in flight finishes.\n"
        "A stage can take a while — watch the run log, or `orchestrator status "
        f"{run_id}` once it stops.\n"
        f"Continue with: orchestrator resume {run_id}"
    )


@main.command()
@click.argument("run_id")
def resume(run_id: str) -> None:
    """Continue after an interruption or an escalation a human has fixed."""
    project, cfg = _locate_run(run_id)
    paths = RunPaths(project, run_id)

    saved = _load_state(cfg, project, paths, run_id)
    if saved is None:
        click.echo(f"no checkpoint for run {run_id}", err=True)
        sys.exit(EXIT_FAILED)

    problem = approval_problem(project.project_dir, project.config)
    if problem:
        click.echo(f"refusing to resume: {problem}", err=True)
        sys.exit(EXIT_FAILED)

    checks = run_preflight(cfg, project_dir=project, run_tests=False, for_resume=True)
    click.echo(format_checks(checks))
    if any(c.blocking for c in checks):
        click.echo("\npreflight failed; nothing was resumed", err=True)
        sys.exit(EXIT_FAILED)

    # Clear the pause before starting, or the run would stop again at the first
    # planner call and look like it had ignored the resume.
    paths.pause_flag.unlink(missing_ok=True)

    git = Git(cfg.target_repo)
    previous_gc = git.disable_gc()
    click.echo(f"\nresuming {run_id} (last failure: {saved.get('failure_layer')})\n")
    try:
        # `resuming` tells the entry router how to re-enter: verify for a
        # repository-state failure, so the human's fix is checked rather than
        # discarded; plan for a planning failure.
        #
        # The session clock restarts. `wall_clock_hours` bounds one unattended
        # stretch, and the hours between an escalation and a human getting to it
        # were not spent working — measuring from the original run start would
        # make a run escalated overnight impossible to resume.
        code = _drive(
            cfg,
            project,
            paths,
            {
                "resuming": True,
                "next_hop": "",
                "session_started_at": time.time(),
                # Did the interrupted stage get far enough to commit? If so the
                # resume verifies that work rather than asking the executor to
                # redo it — asked to redo a finished stage, it has nothing to
                # produce and no way to say so.
                "stage_has_work": _stage_has_work(git, saved),
            },
        )
    finally:
        git.restore_gc(previous_gc)
    sys.exit(code)


@main.command()
@click.argument("run_id")
def status(run_id: str) -> None:
    """Show where a run stopped and why."""
    project, cfg = _locate_run(run_id)
    paths = RunPaths(project, run_id)
    saved = _load_state(cfg, project, paths, run_id)
    if saved is None:
        click.echo(f"run {run_id} has no checkpoint yet", err=True)
        sys.exit(EXIT_FAILED)
    click.echo(build_report(saved, cfg))
    sys.exit(_exit_code(saved))


# --- internals -----------------------------------------------------------


def _drive(
    cfg: ProjectConfig, project: ProjectPaths, paths: RunPaths, graph_input: dict
) -> int:
    saver, conn = open_checkpointer(paths.state_db)
    log = RunLog(paths.run_log)
    try:
        rt = build_runtime(
            cfg,
            project,
            paths,
            planner=make_planner(cfg.planner, cfg.target_repo),
            reviewer=make_reviewer(cfg.reviewer),
            log=log,
        )
        graph = build_graph(rt, checkpointer=saver)
        final = graph.invoke(
            graph_input,
            {
                "configurable": {"thread_id": paths.run_id},
                "recursion_limit": recursion_limit(
                    cfg.limits.max_stages,
                    cfg.limits.max_test_retries,
                    cfg.limits.max_rework_retries,
                    cfg.limits.max_planner_interventions,
                ),
            },
        )
    finally:
        log.close()
        conn.close()

    report = build_report(final, cfg)
    paths.report.write_text(report)
    click.echo("")
    click.echo(report)
    click.echo(f"report written to {paths.report}")
    return _exit_code(final)


def _exit_code(state: dict) -> int:
    from orchestrator.state import outstanding_deferrals

    if state.get("status") != "complete":
        return EXIT_FAILED
    return EXIT_DEFERRED if outstanding_deferrals(state.get("deferred")) else EXIT_OK


def _load(config_path: Path) -> ProjectConfig:
    try:
        return load_config(config_path)
    except ConfigError as e:
        click.echo(f"config problems in {config_path}:\n{e}", err=True)
        sys.exit(EXIT_FAILED)


def _stage_has_work(git: Git, saved: dict) -> bool:
    """Does the interrupted stage branch carry commits beyond its baseline?"""
    start = saved.get("stage_start_sha")
    branch = saved.get("stage_branch")
    if not start or not branch:
        return False
    try:
        return bool(git.diff_names(start))
    except GitError:
        return False


def _load_state(cfg, project, paths, run_id) -> dict | None:
    saver, conn = open_checkpointer(paths.state_db)
    try:
        # A throwaway runtime: reading state needs the graph shape, not models,
        # and building them would demand API keys just to read a report.
        rt = build_runtime(cfg, project, paths, planner=None, reviewer=None)
        graph = build_graph(rt, checkpointer=saver)
        snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
        return dict(snapshot.values) if snapshot and snapshot.values else None
    finally:
        conn.close()


def _locate_run(run_id: str) -> tuple[ProjectPaths, ProjectConfig]:
    """Find which project owns a run id."""
    if PROJECTS_ROOT.is_dir():
        for candidate in sorted(PROJECTS_ROOT.iterdir()):
            if (candidate / "runs" / run_id / "run.json").is_file():
                project = ProjectPaths(candidate.name)
                return project, _load(project.config)
    click.echo(f"no such run: {run_id}", err=True)
    sys.exit(EXIT_FAILED)


def _generate_run_id(cfg: ProjectConfig) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", cfg.project_branch).strip("-").lower()
    return f"{stamp}-{slug}" if slug else stamp


def _write_metadata(paths: RunPaths, slug: str, run_id: str) -> None:
    paths.metadata.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project_slug": slug,
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def _slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower() or "project"


if __name__ == "__main__":  # pragma: no cover
    main()
