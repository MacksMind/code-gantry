"""Preflight: the validation that has to execute something.

Config validation covers everything checkable from the file alone. These are
the checks that need a real target repo, a working environment, and a network:
is the tree clean, does the test command actually pass, does Aider still have
the flags we build, is the reviewer reachable.

All of it runs at the start of `run` as well as under `validate`. Failing fast
beats failing on stage 6.

Aider's flag surface changes between releases, and PLAN.md says to verify it
rather than trust a list. `check_aider_flags` does that by parsing
`aider --help`, which turns a documented manual step into an automated gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.commands import CommandRunner
from orchestrator.config import RunConfig
from orchestrator.executor import AIDER_FLAGS
from orchestrator.gitops import Git, GitError


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    # A warning is worth printing but does not block a run: an unmet
    # precondition for stage 5 may well be satisfied by stage 4.
    fatal: bool = True

    @property
    def blocking(self) -> bool:
        return not self.ok and self.fatal


def run_preflight(
    cfg: RunConfig,
    runner: CommandRunner | None = None,
    *,
    run_tests: bool = True,
    check_aider: bool = True,
    check_reviewer: bool = True,
    for_resume: bool = False,
) -> list[Check]:
    runner = runner or CommandRunner(
        cwd=cfg.target_repo, timeout=cfg.limits.command_timeout_seconds
    )
    git = Git(cfg.target_repo)
    checks: list[Check] = []

    checks.extend(_repo_checks(cfg, git, for_resume=for_resume))
    if any(c.blocking for c in checks):
        # Everything below needs a working repo; running it would only produce
        # confusing secondary failures.
        return checks

    checks.extend(_environment_checks(cfg, runner, run_tests=run_tests))

    if check_aider and any(s.kind == "agent" for s in cfg.stages):
        checks.extend(check_aider_flags(runner))

    if check_reviewer:
        checks.append(_reviewer_check(cfg))

    checks.extend(_precondition_checks(cfg, runner))
    return checks


def _repo_checks(cfg: RunConfig, git: Git, *, for_resume: bool) -> list[Check]:
    checks = []

    if not cfg.target_repo.is_dir():
        return [Check("target repo exists", False, f"{cfg.target_repo} is not a directory")]
    checks.append(Check("target repo exists", True, str(cfg.target_repo)))

    if not git.is_repo():
        return checks + [Check("target repo is a git repo", False, str(cfg.target_repo))]
    checks.append(Check("target repo is a git repo", True))

    if for_resume:
        # Deliberately not checked on resume. A run paused at a manual stage is
        # resumed precisely because a human just did work, and that work is
        # normally uncommitted — requiring a clean tree here would make manual
        # stages unusable.
        checks.append(
            Check(
                "working tree state",
                True,
                "not required on resume: a gated stage's human work is "
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
                "begin from a known state so its diff means something",
            )
        )

    try:
        base_sha = git.rev_parse(cfg.base_ref)
        checks.append(Check(f"base_ref {cfg.base_ref!r} exists", True, base_sha[:12]))
    except GitError as e:
        return checks + [Check(f"base_ref {cfg.base_ref!r} exists", False, str(e))]

    branch_exists = git.branch_exists(cfg.branch)
    current = git.current_branch()

    if for_resume:
        checks.append(
            Check(
                f"on branch {cfg.branch!r}",
                current == cfg.branch,
                f"currently on {current!r}",
            )
        )
        return checks

    if branch_exists:
        checks.append(
            Check(
                f"branch {cfg.branch!r} is safe to use",
                current == cfg.branch,
                f"branch already exists and is not checked out (on {current!r}); "
                "either resume the existing run or choose a new branch",
            )
        )
    else:
        checks.append(Check(f"branch {cfg.branch!r} is new", True))
        # We are about to cut the branch, so HEAD must be where base_ref is or
        # the run silently starts from somewhere else.
        checks.append(
            Check(
                f"HEAD matches base_ref {cfg.base_ref!r}",
                git.head_sha() == base_sha,
                f"HEAD is {git.head_sha()[:12]}, {cfg.base_ref} is {base_sha[:12]}",
            )
        )

    return checks


def _environment_checks(
    cfg: RunConfig, runner: CommandRunner, *, run_tests: bool
) -> list[Check]:
    checks = []

    if cfg.setup_command:
        result = runner.run(cfg.setup_command)
        checks.append(
            Check("setup_command succeeds", result.ok, result.output[-2000:])
        )
        if not result.ok:
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


def _tidiness_check(cfg: RunConfig) -> Check:
    """Did running the tests dirty the working tree?

    Caches and build artifacts a project does not gitignore will fail the
    scope guard on every stage and make `run` refuse to start after a
    `validate`. Worth reporting once, here, with the paths named.
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
        "add these to .gitignore — otherwise they fail the scope guard on "
        "every stage, and `run` will refuse to start after a `validate`",
        # Not fatal: the operator may intend it, and the message is enough to
        # act on. Blocking here would be a surprising place to stop.
        fatal=False,
    )


def check_aider_flags(runner: CommandRunner) -> list[Check]:
    """Confirm the flags we build still exist.

    A renamed flag would otherwise surface as an opaque Aider usage error on
    the first stage of a real run.
    """
    result = runner.run("aider --help")
    if not result.ok:
        return [
            Check(
                "aider is installed",
                False,
                "could not run `aider --help`; agent stages cannot execute "
                f"without it\n{result.output[-1000:]}",
            )
        ]

    help_text = result.output
    missing = [flag for flag in AIDER_FLAGS if flag not in help_text]
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


def _reviewer_check(cfg: RunConfig) -> Check:
    import os

    if cfg.reviewer.api_key_env not in os.environ:
        return Check(
            f"{cfg.reviewer.api_key_env} is set",
            False,
            "the reviewer cannot be called without it",
        )

    try:
        from orchestrator.reviewer import make_reviewer

        make_reviewer(cfg.reviewer)
        return Check("reviewer client builds", True, cfg.reviewer.model)
    except Exception as e:  # noqa: BLE001
        return Check("reviewer client builds", False, str(e))


def _precondition_checks(cfg: RunConfig, runner: CommandRunner) -> list[Check]:
    """Evaluate every stage's preconditions now.

    Unmet ones are warnings, not errors — an earlier stage may satisfy them —
    but reporting them here is what makes a mis-ordered stage list visible
    before anything runs.
    """
    checks = []
    for stage in cfg.stages:
        for command in stage.preconditions:
            result = runner.run(command)
            checks.append(
                Check(
                    f"precondition for {stage.id!r}: {command}",
                    result.ok,
                    "" if result.ok
                    else "not met against the current tree; fine if an earlier "
                    "stage satisfies it, a mis-ordered stage list if not",
                    fatal=False,
                )
            )
    return checks


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
