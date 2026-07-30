"""Preflight: the validation that has to execute something.

Config validation covers everything checkable from the file alone. These need a
real target repo, a working environment, and a network — is the tree clean, does
the test command actually pass, is `setup_command` genuinely idempotent, does
Aider still have the flags we build, are both models reachable.

All of it runs at the start of `run` as well as under `validate`. Failing fast
beats failing on stage 30.

**These commands are host-specific.** `setup_command`, `test_command`, and
`full_test_command` assume a particular machine's Docker, runtime, and paths, so
this validates *this host* — not the config in the abstract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from orchestrator.approval import approval_problem
from orchestrator.commands import CommandRunner
from orchestrator.config import ProjectConfig
from orchestrator.executor import AIDER_FLAGS
from orchestrator.gitops import Git, GitError
from orchestrator.plandoc import resolve_plan_tree


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    # A warning is worth printing but does not block: an unmet precondition for
    # a stage the planner has not derived yet is not an error.
    fatal: bool = True

    @property
    def blocking(self) -> bool:
        return not self.ok and self.fatal


def run_preflight(
    cfg: ProjectConfig,
    runner: CommandRunner | None = None,
    *,
    project_dir=None,
    run_tests: bool = True,
    check_aider: bool = True,
    check_models: bool = True,
    check_approval: bool = True,
    for_resume: bool = False,
) -> list[Check]:
    runner = runner or CommandRunner(
        cwd=cfg.target_repo, timeout=cfg.limits.command_timeout_seconds
    )
    git = Git(cfg.target_repo)
    checks: list[Check] = []

    if cfg.host:
        checks.append(
            Check(
                "host",
                True,
                f"this config declares host {cfg.host!r}; its commands assume "
                "that machine's Docker, runtime, and paths",
                fatal=False,
            )
        )

    checks.extend(_repo_checks(cfg, git, for_resume=for_resume))
    if any(c.blocking for c in checks):
        # Everything below needs a working repo; running it would only produce
        # confusing secondary failures.
        return checks

    checks.extend(_plan_checks(cfg, git))
    checks.extend(_endpoint_checks(cfg))
    checks.extend(_environment_checks(cfg, runner, run_tests=run_tests))

    if check_aider:
        checks.extend(check_aider_flags(runner))
    if check_models:
        checks.extend(_model_checks(cfg))
    if check_approval and project_dir is not None:
        checks.append(_approval_check(cfg, project_dir))

    return checks


def _repo_checks(cfg: ProjectConfig, git: Git, *, for_resume: bool) -> list[Check]:
    checks = []

    if not cfg.target_repo.is_dir():
        return [Check("target repo exists", False, f"{cfg.target_repo} is not a directory")]
    checks.append(Check("target repo exists", True, str(cfg.target_repo)))

    if not git.is_repo():
        return checks + [Check("target repo is a git repo", False, str(cfg.target_repo))]
    checks.append(Check("target repo is a git repo", True))

    if for_resume:
        # Start-only. A run is resumed precisely because a human just fixed
        # something, and that fix is normally uncommitted — enforcing a clean
        # tree here would make every escalation unrecoverable.
        checks.append(
            Check(
                "working tree state",
                True,
                "not required on resume: a human's fix after an escalation is "
                "expected to be uncommitted",
                fatal=False,
            )
        )
    else:
        clean = git.is_clean()
        checks.append(
            Check(
                "working tree is clean",
                clean,
                "" if clean else "commit or stash before starting; a run must "
                "begin from a known state so its diffs mean something",
            )
        )

    try:
        base_sha = git.rev_parse(cfg.base_ref)
        checks.append(Check(f"base_ref {cfg.base_ref!r} exists", True, base_sha[:12]))
    except GitError as e:
        return checks + [Check(f"base_ref {cfg.base_ref!r} exists", False, str(e))]

    if git.branch_exists(cfg.project_branch):
        checks.append(
            Check(
                f"project branch {cfg.project_branch!r} exists",
                True,
                "continuing an existing project",
                fatal=False,
            )
        )
    else:
        checks.append(Check(f"project branch {cfg.project_branch!r} is new", True))
        matches = git.head_sha() == base_sha
        checks.append(
            Check(
                f"HEAD matches base_ref {cfg.base_ref!r}",
                matches,
                "" if matches
                else f"HEAD is {git.head_sha()[:12]}, {cfg.base_ref} is "
                f"{base_sha[:12]} — the project branch would be cut from "
                "somewhere unexpected",
            )
        )

    leftovers = git.branches_matching(cfg.stage_branch_namespace + "/")
    if leftovers:
        checks.append(
            Check(
                "no leftover stage branches",
                False,
                "found child branches from an earlier run: "
                + ", ".join(leftovers[:6])
                + ". They are harmless but suggest a run ended mid-stage; "
                f"`git branch -D $(git branch --list '{cfg.stage_branch_namespace}/*')` "
                "clears them as a group",
                fatal=False,
            )
        )

    return checks


def _plan_checks(cfg: ProjectConfig, git: Git) -> list[Check]:
    """The plan tree resolves, and no child escapes the root's directory."""
    try:
        base_sha = git.rev_parse(cfg.base_ref)
    except GitError as e:  # pragma: no cover - caught upstream
        return [Check("plan resolves", False, str(e))]

    tree = resolve_plan_tree(git, cfg.plan_root, base_sha)
    checks = [
        Check(
            f"plan root {cfg.plan_root!r} resolves",
            tree.root is not None,
            "\n".join(tree.problems) if tree.problems else "",
        )
    ]
    if tree.root is None:
        return checks

    if tree.problems:
        checks.append(
            Check(
                "every plan child stays inside the plan root's directory",
                False,
                "\n".join(tree.problems),
            )
        )
    else:
        checks.append(
            Check(
                "plan children resolve",
                True,
                f"{len(tree.children)} child document(s)"
                + (f"; {len(tree.skipped)} linked but not yet written" if tree.skipped else ""),
            )
        )
    return checks


def _environment_checks(
    cfg: ProjectConfig, runner: CommandRunner, *, run_tests: bool
) -> list[Check]:
    checks = []

    if cfg.setup_command:
        first = runner.run(cfg.setup_command)
        checks.append(Check("setup_command succeeds", first.ok, first.output[-2000:]))
        if not first.ok:
            return checks

        # It runs at least twice per stage — once before the executor, once
        # before verify — so idempotency is a contract, and this is where it is
        # checked rather than assumed.
        second = runner.run(cfg.setup_command)
        checks.append(
            Check(
                "setup_command is idempotent",
                second.ok,
                "" if second.ok
                else "it succeeded once and failed when re-run. It runs at least "
                "twice per stage, so this will fail mid-run.\n"
                + second.output[-2000:],
            )
        )
        if not second.ok:
            return checks

    if not run_tests:
        checks.append(
            Check("test commands", True, "skipped at your request", fatal=False)
        )
        return checks

    for label, command in (
        ("test_command", cfg.test_command),
        ("full_test_command", cfg.full_test_command),
    ):
        if not command:
            continue
        result = runner.run(command)
        checks.append(
            Check(
                f"{label} passes on a clean tree",
                result.ok,
                "" if result.ok
                else "a target repo that is already red makes every subsequent "
                f"verdict meaningless\n{result.output[-2000:]}",
            )
        )

    checks.append(_tidiness_check(cfg))
    return checks


def _tidiness_check(cfg: ProjectConfig) -> Check:
    """Did running the tests dirty the working tree?

    Caches and build artifacts a project does not gitignore will fail the scope
    guard on every stage, and make `run` refuse to start after a `validate`.
    Worth reporting once, here, with the paths named.
    """
    git = Git(cfg.target_repo)
    if git.is_clean():
        return Check("test run leaves the tree clean", True)

    dirtied = git.diff_names(git.head_sha())
    listed = ", ".join(dirtied[:8]) or "(unknown)"
    return Check(
        "test run leaves the tree clean",
        False,
        f"running the tests created or modified: {listed}\n"
        "add these to .gitignore — otherwise they fail the scope guard on every "
        "stage, and `run` will refuse to start after a `validate`",
        fatal=False,
    )


def check_aider_flags(runner: CommandRunner) -> list[Check]:
    """Confirm the flags we build still exist.

    A renamed flag would otherwise surface as an opaque Aider usage error an
    hour into an unattended run.
    """
    result = runner.run("aider --help")
    if not result.ok:
        return [
            Check(
                "aider is installed",
                False,
                "could not run `aider --help`; no agent stage can execute "
                f"without it\n{result.output[-1000:]}",
            )
        ]

    missing = [flag for flag in AIDER_FLAGS if flag not in result.output]
    return [
        Check("aider is installed", True),
        Check(
            "aider still accepts the flags we build",
            not missing,
            "" if not missing
            else f"not found in `aider --help`: {', '.join(missing)}. Aider's CLI "
            "changes between releases — correct these or override with "
            "executor.extra_args",
        ),
    ]


def _endpoint_checks(cfg: ProjectConfig) -> list[Check]:
    """Every `api_base_env` names a variable that is actually exported.

    Resolution is deliberately lazy so that reading a report does not require
    the variable. This is where that laziness gets paid for: an unexported
    hostname fails validation instead of stage 1.
    """
    checks = []
    for role, endpoint in (
        ("executor", cfg.executor),
        ("planner", cfg.planner),
        ("reviewer", cfg.reviewer),
    ):
        if not endpoint.api_base_env:
            continue
        try:
            resolved = endpoint.resolve_api_base()
        except KeyError:
            checks.append(
                Check(
                    f"{role}.api_base_env {endpoint.api_base_env} is set",
                    False,
                    f"the {role} endpoint address lives in the environment, and "
                    f"{endpoint.api_base_env} is not exported",
                )
            )
            continue
        checks.append(
            Check(f"{role} endpoint resolves from {endpoint.api_base_env}", True, resolved)
        )
    return checks


def _model_checks(cfg: ProjectConfig) -> list[Check]:
    checks = []
    for label, env_var, builder in (
        ("planner", cfg.planner.api_key_env, _build_planner),
        ("reviewer", cfg.reviewer.api_key_env, _build_reviewer),
    ):
        if env_var not in os.environ:
            checks.append(
                Check(f"{env_var} is set", False, f"the {label} cannot be called without it")
            )
            continue
        try:
            builder(cfg)
            checks.append(Check(f"{label} client builds", True))
        except Exception as e:  # noqa: BLE001
            checks.append(Check(f"{label} client builds", False, str(e)))
    return checks


def _build_planner(cfg: ProjectConfig):
    from orchestrator.planner import make_planner

    return make_planner(cfg.planner)


def _build_reviewer(cfg: ProjectConfig):
    from orchestrator.reviewer import make_reviewer

    return make_reviewer(cfg.reviewer)


def _approval_check(cfg: ProjectConfig, project_dir) -> Check:
    from orchestrator.runtime import ProjectPaths

    config_path = (
        project_dir.config if isinstance(project_dir, ProjectPaths) else project_dir
    )
    directory = (
        project_dir.project_dir if isinstance(project_dir, ProjectPaths) else project_dir
    )
    problem = approval_problem(directory, config_path)
    return Check("config is approved", problem is None, problem or "")


def format_checks(checks: list[Check]) -> str:
    lines = []
    for check in checks:
        if check.ok:
            mark = "ok  "
        elif check.fatal:
            mark = "FAIL"
        else:
            mark = "warn"
        lines.append(f"[{mark}] {check.name}")
        if check.detail:
            for detail_line in check.detail.strip().splitlines():
                lines.append(f"       {detail_line}")
    return "\n".join(lines)
