"""Command line interface.

    orchestrator run <config.yaml>        start a new run
    orchestrator resume <run_id>          continue an interrupted or gated run
    orchestrator status <run_id>          where a run stopped and why
    orchestrator validate <config.yaml>   check config without executing

Exit codes matter for scripting: 0 on success, 1 on escalation or validation
failure, 2 when a run is paused waiting on a human. A gated run is not a
failure and should not read as one.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from orchestrator.config import ConfigError, RunConfig, load_config
from orchestrator.gitops import Git
from orchestrator.graph import build_graph, open_checkpointer, recursion_limit
from orchestrator.preflight import format_checks, run_preflight
from orchestrator.report import build_report
from orchestrator.reviewer import make_reviewer
from orchestrator.runlog import RunLog
from orchestrator.runtime import RunPaths, build_runtime
from orchestrator.state import new_state

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_AWAITING_HUMAN = 2

RUNS_ROOT = Path("runs")


@click.group()
def main() -> None:
    """Drive a multistage refactor with a local executor and a paid reviewer."""


@main.command()
@click.argument("config_path", type=click.Path(exists=True, path_type=Path))
@click.option("--run-id", default=None, help="Override the generated run id.")
@click.option(
    "--skip-preflight-tests",
    is_flag=True,
    help="Skip running the test suites during preflight. Faster, but a "
    "target repo that is already red will not be caught.",
)
def run(config_path: Path, run_id: str | None, skip_preflight_tests: bool) -> None:
    """Start a new run from a config file."""
    cfg = _load(config_path)

    checks = run_preflight(cfg, run_tests=not skip_preflight_tests)
    click.echo(format_checks(checks))
    if any(c.blocking for c in checks):
        click.echo("\npreflight failed; nothing was run", err=True)
        sys.exit(EXIT_FAILED)

    run_id = run_id or _generate_run_id(cfg)
    paths = RunPaths(RUNS_ROOT, run_id)
    paths.ensure()

    git = Git(cfg.target_repo)
    base_sha = git.rev_parse(cfg.base_ref)
    if not git.branch_exists(cfg.branch):
        git.create_branch(cfg.branch, base=cfg.base_ref)
    else:
        git.checkout(cfg.branch)

    _write_run_metadata(paths, config_path, run_id)

    state = new_state(
        run_id=run_id,
        config_path=str(config_path.resolve()),
        target_repo=str(cfg.target_repo),
        base_ref=cfg.base_ref,
        base_sha=base_sha,
        branch=cfg.branch,
        stage_ids=[s.id for s in cfg.stages],
    )

    click.echo(f"\nrun {run_id}: {len(cfg.stages)} stage(s) on {cfg.branch}\n")
    sys.exit(_drive(cfg, paths, state))


@main.command()
@click.argument("run_id")
def resume(run_id: str) -> None:
    """Continue an interrupted or gated run from its last checkpoint."""
    paths = RunPaths(RUNS_ROOT, run_id)
    metadata = _read_run_metadata(paths)
    cfg = _load(Path(metadata["config_path"]))

    saved = _load_state(cfg, paths, run_id)
    if saved is None:
        click.echo(f"no checkpoint found for run {run_id}", err=True)
        sys.exit(EXIT_FAILED)

    _assert_stages_unchanged(saved, cfg)

    checks = run_preflight(cfg, run_tests=False, for_resume=True)
    click.echo(format_checks(checks))
    if any(c.blocking for c in checks):
        click.echo("\npreflight failed; nothing was resumed", err=True)
        sys.exit(EXIT_FAILED)

    stage_id = saved.get("stage_ids", [])[saved.get("stage_index", 0)]
    click.echo(f"\nresuming run {run_id} at stage {stage_id}\n")
    # The checkpointed state is the input; `resuming` tells the entry router
    # that a manual stage should go straight to verify rather than back
    # through gate, which would pause again without ever checking the work.
    sys.exit(_drive(cfg, paths, {"next_hop": "", "resuming": True}))


@main.command()
@click.argument("run_id")
def status(run_id: str) -> None:
    """Show where a run stopped and why."""
    paths = RunPaths(RUNS_ROOT, run_id)
    try:
        metadata = _read_run_metadata(paths)
    except (OSError, json.JSONDecodeError):
        click.echo(f"no such run: {run_id}", err=True)
        sys.exit(EXIT_FAILED)

    cfg = _load(Path(metadata["config_path"]))
    saved = _load_state(cfg, paths, run_id)
    if saved is None:
        click.echo(f"run {run_id} has no checkpoint yet", err=True)
        sys.exit(EXIT_FAILED)

    click.echo(build_report(saved, cfg))
    sys.exit(_exit_code(saved.get("status", "running")))


@main.command()
@click.argument("config_path", type=click.Path(exists=True, path_type=Path))
@click.option("--skip-tests", is_flag=True, help="Do not run the test suites.")
def validate(config_path: Path, skip_tests: bool) -> None:
    """Check a config without executing any stage."""
    cfg = _load(config_path)
    checks = run_preflight(cfg, run_tests=not skip_tests)
    click.echo(format_checks(checks))

    blocking = [c for c in checks if c.blocking]
    warnings = [c for c in checks if not c.ok and not c.fatal]
    click.echo("")
    if blocking:
        click.echo(f"{len(blocking)} blocking problem(s)", err=True)
        sys.exit(EXIT_FAILED)
    click.echo(f"config is runnable ({len(warnings)} warning(s))")


# --- internals -----------------------------------------------------------


def _drive(cfg: RunConfig, paths: RunPaths, graph_input: dict) -> int:
    saver, conn = open_checkpointer(paths.state_db)
    log = RunLog(paths.run_log)
    try:
        rt = build_runtime(cfg, paths, make_reviewer(cfg.reviewer), log=log)
        graph = build_graph(rt, checkpointer=saver)
        final = graph.invoke(
            graph_input,
            {
                "configurable": {"thread_id": paths.run_id},
                "recursion_limit": recursion_limit(
                    len(cfg.stages),
                    cfg.limits.max_test_retries,
                    cfg.limits.max_rework_retries,
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
    return _exit_code(final.get("status", "running"))


def _exit_code(status: str) -> int:
    if status == "complete":
        return EXIT_OK
    if status == "awaiting_human":
        # Not a failure. A paused run should not read as one to a script.
        return EXIT_AWAITING_HUMAN
    return EXIT_FAILED


def _load(config_path: Path) -> RunConfig:
    try:
        return load_config(config_path)
    except ConfigError as e:
        click.echo(f"config problems in {config_path}:\n{e}", err=True)
        sys.exit(EXIT_FAILED)


def _load_state(cfg: RunConfig, paths: RunPaths, run_id: str) -> dict | None:
    saver, conn = open_checkpointer(paths.state_db)
    try:
        # A throwaway runtime: reading state needs the graph shape, not a
        # reviewer, and building one would demand an API key just to read.
        rt = build_runtime(cfg, paths, reviewer=None)
        graph = build_graph(rt, checkpointer=saver)
        snapshot = graph.get_state({"configurable": {"thread_id": run_id}})
        return dict(snapshot.values) if snapshot and snapshot.values else None
    finally:
        conn.close()


def _assert_stages_unchanged(saved: dict, cfg: RunConfig) -> None:
    """Refuse to resume into a config that has been edited underneath the run.

    Silently applying a changed stage list to a half-finished run would make
    the report describe work that never happened.
    """
    before = saved.get("stage_ids") or []
    after = [s.id for s in cfg.stages]
    if before != after:
        click.echo(
            "the config's stage list has changed since this run started:\n"
            f"  was: {before}\n  now: {after}\n"
            "start a new run rather than resuming into a different plan",
            err=True,
        )
        sys.exit(EXIT_FAILED)


def _generate_run_id(cfg: RunConfig) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", cfg.branch).strip("-").lower()
    return f"{stamp}-{slug}" if slug else stamp


def _write_run_metadata(paths: RunPaths, config_path: Path, run_id: str) -> None:
    (paths.run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "config_path": str(config_path.resolve()),
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def _read_run_metadata(paths: RunPaths) -> dict:
    return json.loads((paths.run_dir / "run.json").read_text())


if __name__ == "__main__":  # pragma: no cover
    main()
