"""Command line interface.

    code-gantry init <plan-doc> [config]   draft a config from a plan document
    code-gantry validate <config>          prove the config works on this host
    code-gantry run <config>               start a run
    code-gantry pause <config> [run_id]    stop cleanly at the next boundary
    code-gantry resume <config> [run_id]   continue after an interruption
    code-gantry status <config> [run_id]   where a run stopped and why

Every command takes the path to a project's config, because a config decides
what runs unattended and guessing which one was meant is the class of mistake
this design is arranged against. `CODE_GANTRY_CONFIG` supplies it once per
shell. `init` is the exception, and only because it produces a config rather
than consuming one — its config path is optional and defaults beside the plan.

A run id is optional wherever it appears: the newest run in the config's work
directory is the one meant, almost always.

Exit codes: 0 complete, 1 failed or escalated.

`init` may prompt — it is a human at a terminal doing one-time setup. `run` and
`resume` execute unattended and must never block on input.

"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from code_gantry.configversion import blob_sha, problem_resuming
from code_gantry.config import ConfigError, ProjectConfig, load_config
from code_gantry.discover import derive_target_repo, draft_config
from code_gantry.gitops import Git, GitError
from code_gantry.driver import (
    UnreadableCheckpoint,
    default_max_steps,
    drive,
    last_step,
    load_state,
    open_checkpointer,
)
from code_gantry.ledger import LedgerError, open_ledger, read_ledger, resolve_scope
from code_gantry.planner import make_planner
from code_gantry.preflight import format_checks, run_preflight
from code_gantry.report import build_report
from code_gantry.reviewer import make_reviewer
from code_gantry.runlog import RunLog
from code_gantry.runtime import ProjectPaths, RunPaths, build_runtime
from code_gantry.state import new_state, resume_input

EXIT_OK = 0
EXIT_FAILED = 1


@click.group()
def main() -> None:
    """Drive a long refactor with an executor, a planner, and a reviewer."""



CONFIG_ENV = "CODE_GANTRY_CONFIG"


def _config_argument(value: Path | None) -> Path:
    """The config path every command needs, or a refusal.

    There is no default and no search. A config carries the commands that run
    unattended for hours, and guessing which one an operator meant is the
    class of mistake this whole design is arranged against — so an omitted
    path is an error, not a lookup. `init` is the one exception, because it is
    the command that produces a config rather than consuming one.

    `CODE_GANTRY_CONFIG` exists so the path is typed once per shell rather
    than once per command. It is still explicit: something named it.
    """
    if value is not None:
        return Path(value)
    from_env = os.environ.get(CONFIG_ENV)
    if from_env:
        return Path(from_env)
    raise click.UsageError(
        "no config given. Pass the path to the project's config, or set "
        f"{CONFIG_ENV}. There is no default: a config decides what runs "
        "unattended, so it is named rather than found."
    )


def _project_for(config_path: Path) -> tuple[ProjectConfig, ProjectPaths]:
    cfg = _load(config_path)
    _apply_env_file(cfg, config_path)
    return cfg, ProjectPaths.for_config(cfg)


def _apply_env_file(cfg: ProjectConfig, config_path: Path) -> None:
    """Load the credentials the config points at, before anything needs them.

    Here rather than in `parse_config`, which must stay a reader: a config that
    *names* a credentials file is data, and loading it is an action a caller
    takes deliberately. `load_state` created its table before reading and
    destroyed the evidence it was about to look for; a parser with a side
    effect on the process environment is the same shape.

    And here rather than in each command, because there are six of them and the
    seventh would be the one that forgot — the reason the transcript is a
    `list` subclass and the price map memoises inside its loader.
    """
    from code_gantry.envfile import apply_env_file

    if cfg.env_file is None:
        return
    try:
        applied = apply_env_file(cfg.env_file)
    except ConfigError as e:
        click.echo(f"config problems in {config_path}:\n{e}", err=True)
        sys.exit(EXIT_FAILED)
    if applied:
        # What it contributed, not what it contained — the shell wins, so a
        # line built from the file's contents would claim credit for a
        # variable it never set. Names only; a credential must not be one
        # `code-gantry status` away from a terminal transcript.
        click.echo(f"env_file supplied {', '.join(applied)}", err=True)


def _resolve_run_id(project: ProjectPaths, run_id: str | None) -> str:
    """The run being asked about: the one named, or the newest in the work dir.

    Newest by run id, which sorts chronologically because it is stamped. A
    directory with no runs is an error rather than an empty answer — "resume"
    with nothing to resume is a typo in the config path far more often than it
    is a real request.
    """
    if run_id:
        return run_id
    if project.runs_dir.is_dir():
        candidates = sorted(
            d.name for d in project.runs_dir.iterdir()
            if (d / "run.json").is_file()
        )
        if candidates:
            return candidates[-1]
    raise click.UsageError(f"no runs found under {project.runs_dir}")


@main.command()
@click.argument("plan_doc", type=click.Path(exists=True, path_type=Path))
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
def init(plan_doc: Path, config_path: Path | None) -> None:
    """Draft a project config from a plan document.

    Discovery splits along the same line as the planner's write permissions:
    executable fields come from deterministic repo inspection, never a model.

    The one command whose config path is optional, because it is the one that
    produces a config rather than consuming one. Defaulted from the plan: the
    documents and the machinery that acts on them are the same project, and a
    later reader wants them in one directory.
    """
    repo = derive_target_repo(plan_doc)
    if repo is None:
        click.echo(
            f"{plan_doc} is not inside a git repository.\n\n"
            "The plan document must live in the target repo: the repo copy is "
            "what CodeGantry operates against and what the planner "
            "revises. Copy it in, commit it, and re-run init against the copy.",
            err=True,
        )
        sys.exit(EXIT_FAILED)

    plan_rel = plan_doc.resolve().relative_to(repo.resolve()).as_posix()
    target = Path(config_path) if config_path else plan_doc.resolve().parent / "code_gantry.yaml"

    if target.exists() and not click.confirm(
        f"{target} already exists. Overwrite?", default=False
    ):
        click.echo("left alone")
        sys.exit(EXIT_OK)

    target.parent.mkdir(parents=True, exist_ok=True)
    draft, notes = draft_config(repo, plan_rel)
    target.write_text(draft)

    # Relative where it helps. An operator runs these from the repo, and a
    # 120-character absolute path is a command nobody types twice.
    try:
        shown = target.resolve().relative_to(Path.cwd())
    except ValueError:
        shown = target
    click.echo(f"wrote {shown}\n")
    for note in notes:
        click.echo(f"  {note}")
    ignore_hint = ""
    work = target.parent / ".code_gantry"
    if work.is_relative_to(repo) and not Git(repo).is_ignored(
        f"{work.relative_to(repo)}/"
    ):
        ignore_hint = (
            f"\n\nAdd `.code_gantry/` to a .gitignore beside the plan. Every "
            "run writes there, and a tracked work dir means no stage can ever "
            "cut a branch."
        )
    click.echo(
        "\nEvery discovered field carries a provenance comment, so reviewing it "
        "is a check of reasoning rather than of values. Read it, fix what is "
        f"wrong, commit it, then:\n\n  code-gantry validate {shown}\n"
        f"  code-gantry run {shown}" + ignore_hint
    )


@main.command()
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
@click.option("--skip-tests", is_flag=True, help="Do not run the test suites.")
def validate(config_path: Path | None, skip_tests: bool) -> None:
    """Prove the config works against this host, before a run."""
    config_path = _config_argument(config_path)
    cfg, project = _project_for(config_path)
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
        f"Commit it, then: code-gantry run {config_path}"
    )


@main.command()
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
@click.option(
    "--dry-run", is_flag=True, help="Print the observations without writing them."
)
def reconcile(config_path: Path | None, dry_run: bool) -> None:
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
    config_path = _config_argument(config_path)
    cfg, project = _project_for(config_path)
    git = Git(cfg.target_repo)

    if cfg.ledger is None:
        raise click.ClickException(
            "no `ledger:` section in the config; nowhere to record observations"
        )

    planner = make_planner(cfg.planner, cfg.target_repo, cfg.project_tools)
    if planner.reader is None:
        raise click.ClickException(
            "planner.repo_access must be on: reconciling means checking the "
            "plan against the repository, which needs the read tools"
        )

    landed = git.commits_between(cfg.base_ref, cfg.project_branch)
    click.echo(
        f"reconciling the ledger against {len(landed)} commit(s) on "
        f"{cfg.project_branch}"
    )

    from code_gantry.render import render_plan, render_projection

    views = read_ledger(project.ledger).views()
    if not views.documents():
        raise click.ClickException(
            f"the ledger at {project.ledger} holds no plan; import one first"
        )
    outcome = planner.plan(
        _reconcile_prompt(
            cfg,
            render_plan(views),
            render_projection(views, note_chars=cfg.ledger.note_chars),
        )
    )

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
    if not outcome.reads_answered:
        raise click.ClickException(
            "the planner answered without reading anything, so its verdict is "
            "worth nothing either way. Nothing was written."
        )

    if not outcome.plan_notes:
        click.echo(
            f"\nnothing to add, after {outcome.reads_answered} read(s); "
            "the log already reflects what the branch has done"
        )
        return

    click.echo(f"\n{len(outcome.plan_notes)} observation(s):")
    for note in outcome.plan_notes:
        click.echo(f"\n  {note.get('key')} — {note.get('subject')}")
        click.echo(f"    observed:  {note.get('observation')}")

    if dry_run:
        click.echo("\n--dry-run: nothing written")
        return

    from code_gantry.ledgercli import actor, origin
    from code_gantry.nodes import open_findings

    led = open_ledger(project.ledger, origin=origin(), actor=actor())
    try:
        opened = open_findings(led, git, outcome.plan_notes, by="reconcile",
                               stage_id="reconcile", run_id=None)
    finally:
        led.close()
    click.echo(f"\n{opened} finding(s) opened in {project.ledger}")


def _reconcile_prompt(cfg: ProjectConfig, plan_text: str, projection: str) -> list[dict]:
    """The reconcile instruction — catch the ledger up with the branch.

    Runs outside a stage, so it is the one place that can record work landed
    before the ledger existed, or by hand, or by someone else.
    """
    from code_gantry.prompts import _plan_intro, _projection_block

    return [
        {
            "role": "user",
            "content": (
                _plan_intro(cfg)
                + plan_text
                + "\n\n"
                + (_projection_block(projection) + "\n\n" if projection.strip() else "")
                + f"The branch {cfg.project_branch!r} carries work that "
                f"{cfg.base_ref!r} does not, and the ledger may not record all "
                "of it. Catch it up.\n\n"
                "**Read the open findings above first.** Re-reporting one is "
                "the one way this pass does damage — a reader cannot tell a "
                "duplicate from a second, independent confirmation.\n\n"
                "Use `git_diff` between "
                f"{cfg.base_ref} and {cfg.project_branch} to see what the "
                "branch actually did, and `search` to check the plan's "
                "specific claims — counts, file lists, 'occurrences across N "
                "files' — against the code as it is now. A count in an item "
                "is a claim about the moment someone wrote it; the code is the "
                "fact.\n\n"
                "Return `project_complete`, and put every finding in "
                "`plan_notes` — one entry per plan item whose state the ledger "
                "does not yet reflect: work that has been done, a count that "
                "has moved, a claim that was wrong when written. Key each to "
                "the item, and state where it stands now, as a total.\n\n"
                "`plan_notes` is the entire output of this pass. A finding "
                "described in your reasoning and left out of `plan_notes` is "
                "discarded. Do not propose a stage; nothing is being built "
                "here.\n\n"
                "Report only what you verified against the code. A note nobody "
                "can check is worse than none, because someone will act on it."
            ),
        }
    ]


@main.command()
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
@click.option("--run-id", default=None, help="Override the generated run id.")
@click.option(
    "--skip-preflight-tests",
    is_flag=True,
    help="Skip the suites during preflight. Faster, but an already-red repo "
    "will not be caught.",
)
@click.option(
    "--scope", "scope", multiple=True, metavar="KEY",
    help="A plan key this run may draw from, with everything under it. "
    "Repeatable. Without it the run draws from the whole plan.",
)
def run(
    config_path: Path | None, run_id: str | None, skip_preflight_tests: bool,
    scope: tuple[str, ...],
) -> None:
    """Start a run against a project."""
    config_path = _config_argument(config_path)
    cfg, project = _project_for(config_path)
    project.ensure()
    slug = project.slug


    # Before preflight, not after. Preflight is the slow thing — containers,
    # the test database, the whole suite — and printing only once it returns
    # is what made a starting run indistinguishable from a hung one.
    click.echo(
        _startup_banner("run", slug, cfg, run_tests=not skip_preflight_tests)
    )

    # And the run's own log exists before preflight too, which is the half that
    # was missing. The banner above goes to stdout, so it lands in whatever the
    # operator redirected — a file appended across every run, where the only
    # way to tell this run's lines from the last one's is to count. The file
    # named after this run held nothing at all until preflight returned, which
    # on this project is three minutes of suites: tailing it showed an empty
    # file, which is what a hung run also shows.
    #
    # Safe to create early. `_locate_run` keys on `run.json`, written only once
    # preflight has passed, so a directory left by a refused start is a record
    # rather than something `resume` or `status` can trip over.
    run_id = run_id or _generate_run_id(cfg)
    paths = RunPaths(project, run_id)
    paths.ensure()
    start_log = RunLog(paths.run_log, echo=None)
    start_log.record(
        f"=== {run_id} — {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"pid {os.getpid()} ==="
    )
    start_log("[preflight] starting" + (
        "" if skip_preflight_tests else " (running the suites, which take minutes)"
    ))

    checks = run_preflight(cfg, project_dir=project, run_tests=not skip_preflight_tests,
                            config_path=config_path)
    click.echo(format_checks(checks))
    blocking = [c for c in checks if c.blocking]
    start_log(
        f"[preflight] {'failed' if blocking else 'passed'}: "
        f"{len(checks)} check(s)"
        + ("; " + "; ".join(c.name for c in blocking) if blocking else "")
    )
    start_log.close()
    if blocking:
        click.echo("\npreflight failed; nothing was run", err=True)
        sys.exit(EXIT_FAILED)

    git = Git(cfg.target_repo)
    # Rework discards child branches, and the reflog is the only recovery path
    # for an attempt the operator later wants to inspect.
    previous_gc = git.disable_gc()
    base_sha = git.ensure_project_branch(cfg.project_branch, cfg.base_ref)
    # The commit the repository's own documents — conventions, operations,
    # layout — are read at: the project branch, where they are maintained.
    plan_sha = git.rev_parse(cfg.project_branch)

    views = read_ledger(project.ledger).views()
    documents = views.documents()
    if not documents:
        click.echo(
            f"the ledger at {project.ledger} holds no plan; import one with "
            "`code-gantry plan import`",
            err=True,
        )
        git.restore_gc(previous_gc)
        sys.exit(EXIT_FAILED)

    key_scope: list[str] | None = None
    if scope:
        try:
            key_scope = sorted(resolve_scope(views, scope))
        except LedgerError as e:
            click.echo(f"--scope: {e}", err=True)
            git.restore_gc(previous_gc)
            sys.exit(EXIT_FAILED)

    state = new_state(
        run_id=run_id,
        project_slug=slug,
        config_hash=blob_sha(config_path),
        target_repo=str(cfg.target_repo),
        base_ref=cfg.base_ref,
        base_sha=base_sha,
        plan_sha=plan_sha,
        project_branch=cfg.project_branch,
        started_at=time.time(),
    )
    if key_scope:
        state = {**state, "key_scope": key_scope}
    _write_metadata(paths, slug, run_id)

    click.echo(
        f"\nrun {run_id} on {cfg.project_branch} "
        f"(plan: {len(documents)} document(s) in the ledger)\n"
    )
    try:
        code = _drive(cfg, project, paths, state, _warnings(checks))
    finally:
        git.restore_gc(previous_gc)
    sys.exit(code)


@main.command()
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
@click.argument("run_id", required=False)
@click.option("--note", default="", help="Why, recorded for when you come back.")
def pause(config_path: Path | None, run_id: str | None, note: str) -> None:
    """Ask a running run to stop at the next stage boundary.

    Not a kill. The flag is read before each planner call, so the run finishes
    whatever stage is in flight, lands it or fails it normally, and stops with
    nothing half-done and a clean tree. Interrupting the process instead leaves
    a partially applied executor edit and a stage branch nobody owns.
    """
    config_path = _config_argument(config_path)
    cfg, project = _project_for(config_path)
    run_id = _resolve_run_id(project, run_id)
    paths = RunPaths(project, run_id)
    if not paths.run_dir.is_dir():
        click.echo(f"no such run: {run_id}", err=True)
        sys.exit(EXIT_FAILED)

    paths.pause_flag.write_text(note)
    click.echo(
        f"{run_id} will stop after the stage in flight finishes.\n"
        "A stage can take a while — watch the run log, or `code-gantry status "
        f"{run_id}` once it stops.\n"
        f"Continue with: {cfg.resume_command(run_id)}"
    )


@main.command()
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
@click.argument("run_id", required=False)
def unpause(config_path: Path | None, run_id: str | None) -> None:
    """Withdraw a pause from a run that is still going.

    `pause` only writes a flag, and the run reads it at a stage boundary, so a
    pause regretted before that boundary can be taken back and the run never
    knows. This deletes the flag and nothing else — it starts no process and
    touches neither the tree nor the checkpoint.

    `resume` clears the same flag on its way in, which is why this did not
    exist. But resume is for a run that has *stopped*: run it against a live
    one and there are two processes on the repository, which is a five-minute
    window rather than a crash and is not survivable when the second reaches
    the tree. Whether a run is still going cannot be settled from here without
    a race, so both cases are named in the output rather than guessed at.

    Idempotent: no flag is not an error, because "make sure this run is not
    paused" is the thing a caller actually wants.
    """
    config_path = _config_argument(config_path)
    cfg, project = _project_for(config_path)
    run_id = _resolve_run_id(project, run_id)
    paths = RunPaths(project, run_id)
    if not paths.run_dir.is_dir():
        click.echo(f"no such run: {run_id}", err=True)
        sys.exit(EXIT_FAILED)

    if not paths.pause_flag.exists():
        click.echo(f"no pause was set on {run_id}; nothing to withdraw.")
        return

    paths.pause_flag.unlink(missing_ok=True)
    click.echo(
        f"pause withdrawn from {run_id}.\n"
        "A run still going will carry on to the next stage and never see it. "
        "A run that already stopped is not restarted by this — "
        f"{cfg.resume_command(run_id)} is what continues it."
    )


@main.command()
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
@click.argument("run_id", required=False)
@click.option(
    "--reset-progress-budget",
    is_flag=True,
    help="Clear the without-landing intervention counter. Use when you have "
    "changed something that makes the earlier failures no longer apply.",
)
def resume(config_path: Path | None, run_id: str | None, reset_progress_budget: bool) -> None:
    """Continue after an interruption or an escalation a human has fixed.

    `--reset-progress-budget` exists because `max_interventions_without_landing`
    is otherwise terminal: it is checked before the planner is called, so a run
    that hits it re-escalates on every resume without anything running, and the
    counter only clears when a stage lands.

    That terminality is deliberate — a budget an operator can clear by
    re-running is not a budget, and the failure it guards against is exactly
    resume-in-a-loop. So the reset is explicit and human-asserted rather than
    inferred from anything. A config change or a code fix does not clear it by
    itself; someone has to say that the earlier failures no longer apply.

    Observed: three interventions were spent on a stage whose instruction
    required a file restoration that had already been done, and on quoted code
    carrying four spaces of Markdown indentation. Both causes were fixed. The
    run had no way to be told.
    """
    config_path = _config_argument(config_path)
    cfg, project = _project_for(config_path)
    run_id = _resolve_run_id(project, run_id)
    paths = RunPaths(project, run_id)

    try:
        saved = _load_state(paths, run_id)
    except UnreadableCheckpoint as exc:
        click.echo(f"cannot resume {run_id}: {exc}", err=True)
        sys.exit(EXIT_FAILED)
    if saved is None:
        click.echo(f"no checkpoint for run {run_id}", err=True)
        sys.exit(EXIT_FAILED)

    # A run reads one config for its whole life. If the file has moved on,
    # this is not the run that config describes — the stages already landed
    # were produced by different commands, and nothing in the record would say
    # where the change fell.
    problem = problem_resuming(config_path, saved.get("config_hash", ""))
    if problem:
        click.echo(f"refusing to resume: {problem}", err=True)
        sys.exit(EXIT_FAILED)

    click.echo(_startup_banner("resume", run_id, cfg, run_tests=False))

    checks = run_preflight(cfg, project_dir=project, run_tests=False, for_resume=True,
                            config_path=config_path,
                            recorded_base_sha=saved.get("base_sha", ""))
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
        code = _drive(
            cfg,
            project,
            paths,
            resume_input(
                saved,
                stage_has_work=_stage_has_work(git, saved),
                reset_progress_budget=reset_progress_budget,
            ),
            _warnings(checks),
        )
    finally:
        git.restore_gc(previous_gc)
    sys.exit(code)


@main.command()
@click.argument("config_path", required=False, type=click.Path(path_type=Path))
@click.argument("run_id", required=False)
def status(config_path: Path | None, run_id: str | None) -> None:
    """Show where a run stopped and why."""
    config_path = _config_argument(config_path)
    cfg, project = _project_for(config_path)
    run_id = _resolve_run_id(project, run_id)
    paths = RunPaths(project, run_id)
    try:
        saved = _load_state(paths, run_id)
    except UnreadableCheckpoint as exc:
        click.echo(str(exc), err=True)
        sys.exit(EXIT_FAILED)
    if saved is None:
        click.echo(f"run {run_id} has no checkpoint yet", err=True)
        sys.exit(EXIT_FAILED)
    click.echo(build_report(saved, cfg))
    sys.exit(_exit_code(saved))


# --- internals -----------------------------------------------------------


def _warnings(checks) -> list[str]:
    """Non-blocking preflight findings, one line each."""
    return [
        f"{c.name}: {' '.join((c.detail or '').split())}"
        for c in checks
        if not c.ok and not c.fatal
    ]


def _drive(
    cfg: ProjectConfig,
    project: ProjectPaths,
    paths: RunPaths,
    graph_input: dict,
    warnings: list[str] | None = None,
) -> int:
    checkpoint, conn = open_checkpointer(paths.state_db)
    log = RunLog(paths.run_log)
    # Its own file, and deliberately not echoed: the terminal carries the
    # timeline, and this is what the timeline is being kept free of.
    tools = RunLog(paths.tool_log, echo=None)
    # Non-blocking preflight findings, repeated into the run log. They are
    # already printed to stdout, which for an unattended run is a nohup file
    # nobody opens unless something has gone wrong — so a warning that only
    # lives there is a warning that gets missed. The run log is the artifact an
    # operator actually tails.
    for warning in warnings or []:
        log(f"[preflight] {warning}")
    rt = None
    try:
        rt = build_runtime(
            cfg,
            project,
            paths,
            planner=make_planner(cfg.planner, cfg.target_repo, cfg.project_tools),
            reviewer=make_reviewer(cfg.reviewer, cfg.target_repo, project_tools=cfg.project_tools),
            log=log,
            tool_log=tools,
            key_scope=graph_input.get("key_scope"),
        )
        # Said once, in the timeline, so the file is discoverable without
        # knowing it exists. A log nobody can find is not visibility.
        log(f"[run] tool reads are streaming to {paths.tool_log}")
        if rt.key_scope:
            log(f"[run] scope: {len(rt.key_scope)} key(s): {' '.join(sorted(rt.key_scope))}")
        final = drive(
            rt,
            graph_input,
            checkpoint=checkpoint,
            start_step=last_step(paths.state_db, graph_input.get("run_id", "")),
            max_steps=default_max_steps(
                cfg.limits.max_stages,
                cfg.limits.max_test_retries,
                cfg.limits.max_rework_retries,
                cfg.limits.max_planner_interventions,
            ),
        )
    except Exception as exc:
        # The run log is the artifact an operator tails, and until now it said
        # nothing at all about a crash: the traceback went to stdout, which for
        # an unattended run is a nohup file that the next resume overwrites.
        #
        # Observed: `advance` raised inside `squash_merge` — the project repo's
        # pre-commit hook rejected a line the executor had written with
        # trailing whitespace — and the log simply stopped mid-stage after
        # "recorded 5 plan observation(s)". The repository was left with the
        # merge staged and uncommitted, and it took reading SQUASH_MSG off the
        # filesystem to reconstruct what had happened. The traceback that would
        # have said so in one line was already gone.
        log(f"[crash] {type(exc).__name__}: {exc}")
        for line in traceback.format_exc().splitlines():
            log(f"[crash] {line}")
        raise
    else:
        # Built before the log closes so the timeline can name it. The report
        # is an artifact and `report.md` is where it lives; what the timeline
        # gets is the event — a report was written, and where.
        #
        # It used to go into `run.log` in full, on the argument that a durable
        # per-run record should not stop before the conclusion. True, and
        # answered by the file itself: `report.md` is equally durable and sits
        # in the same directory, so a copy in the timeline is a second thing to
        # keep in sync rather than a safeguard — and it is the copy that cannot
        # be re-read as markdown, since a report interleaved with stamped
        # events is neither.
        #
        # Nothing urgent is lost. Why a run stopped reaches the timeline through
        # `[escalate]` at the moment it happens, well before this.
        report = build_report(final, cfg)
        paths.report.write_text(report)
        log(f"[run] report written to {paths.report}")
    finally:
        log.close()
        tools.close()
        conn.close()
        if rt is not None and rt.ledger is not None:
            rt.ledger.close()

    # The path, not the document. Same reasoning as the log line above: the
    # report is a file to open, and reprinting sixty lines of markdown under a
    # timeline is how the one line that says where it is gets scrolled past.
    click.echo("")
    click.echo(f"report written to {paths.report}")
    return _exit_code(final)


def _exit_code(state: dict) -> int:
    return EXIT_FAILED if state.get("status") != "complete" else EXIT_OK


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


def _load_state(paths, run_id) -> dict | None:
    """The last checkpoint, read without building anything.

    This used to construct a throwaway runtime, because LangGraph would only
    hand back state through a compiled graph — so reading a report meant
    assembling the nodes it would have run. The checkpoint is a table now, and
    `load_state` opens and closes its own connection.
    """
    return load_state(paths.state_db, run_id)


def _startup_banner(
    command: str,
    subject: str,
    cfg: ProjectConfig,
    *,
    pid: int | None = None,
    run_tests: bool = True,
    now: str | None = None,
) -> str:
    """What is starting, printed before anything slow happens.

    Two problems, and the second is the one that has cost time.

    An operator watching the console saw nothing. `run_preflight` is the first
    thing either command does, and on a fresh run it starts the containers,
    prepares the test database and runs the whole suite — minutes — before the
    first `click.echo`. The run id is not generated until after it, so during
    that window there was no id to look up and no run directory to list, and
    alive, hung and dead all looked identical.

    And `last-run.out` is appended across every invocation with nothing marking
    where one begins. That is the append-only-log trap the project
    instructions already record: a monitor grepping it for a pause matched 24
    historical ones and reported a stop that had not happened. Anchoring
    correctly meant taking `wc -l` out of band beforehand and remembering to.
    A fixed marker carrying the wall clock and the pid is an anchor a later
    reader can find by itself.

    The pid is here because the timestamp is not enough on its own: two
    invocations inside one second is exactly what a resume loop produces.

    A pure string, so the thing printed before a slow call can be tested
    without making one.
    """
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    suites = (
        "including the test suites, which take minutes"
        if run_tests
        else "test suites skipped"
    )
    return (
        f"=== code-gantry {command} {subject} — {stamp} pid {pid or os.getpid()} ===\n"
        f"target: {cfg.target_repo} on {cfg.project_branch}\n"
        f"preflight: starting ({suites})"
    )


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


from code_gantry.ledgercli import ledger as _ledger_group, plan as _plan_group  # noqa: E402

main.add_command(_plan_group, "plan")
main.add_command(_ledger_group, "ledger")


if __name__ == "__main__":  # pragma: no cover
    main()
