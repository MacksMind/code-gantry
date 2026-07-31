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

import json
import os
import urllib.request
from dataclasses import dataclass

from orchestrator.approval import approval_problem
from orchestrator.commands import CommandResult, CommandRunner, truncate_middle
from orchestrator.config import ProjectConfig
from orchestrator.executor import AIDER_FLAGS
from orchestrator.gitops import Git, GitError
from orchestrator.plandoc import resolve_plan_tree


# Both ends, not just the tail. A test runner prints its verdict and then keeps
# talking: the real target tallies deprecation warnings after the summary, so
# keeping only the last 2000 characters reported nothing but deprecation noise
# and dropped the "N examples, M failures" line entirely.
_EXCERPT_CHARS = 4_000


def _excerpt(output: str) -> str:
    return truncate_middle(output or "", _EXCERPT_CHARS)


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
    check_endpoint: bool = True,
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
    if check_endpoint:
        checks.extend(check_executor_endpoint(cfg))
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
        # Nothing merges `base_ref` into the project branch — not at run start,
        # not at resume. A branch cut days ago carries whatever the base looked
        # like then, and the run will happily build on it.
        #
        # That is usually harmless and occasionally not. On the first real
        # project, `main` gained a fix to the target repo's own tooling — a
        # post-commit hook that reindexes, changed to stop waking a second
        # inference server on every executor commit. A run on a branch without
        # it would have fired the old path a hundred times.
        #
        # Reported rather than merged. A merge can conflict, and resolving one
        # unattended is exactly the surprise this tool exists to avoid; whether
        # this run should include the latest base is the operator's call.
        behind = git.commits_between(cfg.project_branch, cfg.base_ref)
        if behind:
            checks.append(
                Check(
                    f"{cfg.project_branch!r} includes all of {cfg.base_ref!r}",
                    False,
                    f"{len(behind)} commit(s) on {cfg.base_ref} are not on the "
                    f"project branch: {', '.join(behind[:4])}"
                    + (" ..." if len(behind) > 4 else "")
                    + f". Merge them first if this run should have them — "
                    f"`git -C {cfg.target_repo} checkout {cfg.project_branch} "
                    f"&& git merge {cfg.base_ref}` — or proceed knowing it "
                    "will not.",
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
        checks.append(Check("setup_command succeeds", first.ok, _excerpt(first.output)))
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
                + _excerpt(second.output),
            )
        )
        if not second.ok:
            return checks

    if not run_tests:
        checks.append(
            Check("test commands", True, "skipped at your request", fatal=False)
        )
        return checks

    # Deduplicated by command, not by label: when a project points both at the
    # same script — which is the sensible default — running it twice proves
    # nothing and costs a full suite. On the first real project that is 23
    # minutes to learn one thing.
    # The saving is the second *run*, not the second verdict: a command that
    # came back red is red under both labels, and reporting the twin as a pass
    # would manufacture evidence of green from a run that failed.
    already_run: dict[str, CommandResult] = {}
    for label, command in (
        ("test_command", cfg.test_command),
        ("full_test_command", cfg.full_test_command),
    ):
        if not command:
            continue
        seen = command in already_run
        result = already_run.get(command) or runner.run(command)
        already_run[command] = result
        detail = (
            "same command as above; not run twice" if seen and result.ok
            else "" if result.ok
            else "a target repo that is already red makes every subsequent "
            f"verdict meaningless\n{_excerpt(result.output)}"
        )
        checks.append(
            Check(
                f"{label} passes on a clean tree",
                result.ok,
                detail,
                fatal=not (seen and result.ok),
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
            Check(
                f"{role} endpoint resolves from {endpoint.api_base_env}",
                True,
                # Deliberately not the value: see endpoint_label.
                f"{len(resolved)} characters, kept out of this output",
            )
        )
    return checks


def endpoint_label(endpoint) -> str:
    """How to name an endpoint in output, without giving its address away.

    `api_base_env` exists precisely so a hostname need not be committed.
    Printing the resolved value to the terminal — and from there into logs,
    transcripts and screenshots — hands most of that back. The variable name is
    what an operator needs in order to fix a problem; the address is not.

    A literal `api_base` is echoed as-is: the operator wrote it into the config
    they approved, so it reveals nothing they did not already choose.
    """
    if endpoint.api_base_env:
        return f"${endpoint.api_base_env}"
    return endpoint.api_base or "(unset)"


def check_executor_endpoint(cfg: ProjectConfig) -> list[Check]:
    """The local endpoint answers, and offers the model the config names.

    A mistyped model id fails every single stage, and does it as an opaque
    executor error rather than as anything that names the cause. One HTTP GET
    here turns that into a validation failure that prints the names the server
    actually accepts.

    The `openai/` in `openai/qwen3-coder-next` is a litellm routing prefix,
    stripped before the request leaves Aider. What the server sees — and what
    must match — is the remainder.
    """
    try:
        api_base = cfg.executor.resolve_api_base()
    except KeyError:
        # Reported by _endpoint_checks. Nothing to reach yet.
        return []
    if not api_base:
        # No api_base means the real OpenAI endpoint, which needs no proving.
        return []

    label = endpoint_label(cfg.executor)
    url = api_base.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001 - urllib raises a wide family
        return [
            Check(
                "executor endpoint answers",
                False,
                f"GET {label}/models failed: {_redact(e, api_base, label)}\n"
                "no agent stage can run without it",
            )
        ]

    checks = [Check("executor endpoint answers", True, f"{label}/models")]

    names = _model_names(body)
    if names is None:
        # Not every OpenAI-compatible server implements /v1/models the same way.
        # Refusing to run over that would be worse than not checking.
        checks.append(
            Check(
                "executor model is offered by the endpoint",
                False,
                f"{label}/models answered, but not with a recognisable model "
                "list, so the "
                "model id could not be verified. Check it by hand.",
                fatal=False,
            )
        )
        return checks

    wanted = _served_model_name(cfg.executor.model)
    if wanted in names:
        checks.append(
            Check(f"endpoint offers {wanted!r}", True, f"{len(names)} model(s) available")
        )
    else:
        checks.append(
            Check(
                f"endpoint offers {wanted!r}",
                False,
                f"{label}/models does not list {wanted!r}. It offers: "
                + ", ".join(sorted(names))
                + f".\nexecutor.model is {cfg.executor.model!r}; everything after "
                "the provider prefix must match a name the server accepts. If "
                "this endpoint lists models lazily, this is the check to "
                "reconsider.",
            )
        )
    return checks


def _redact(value, address: str, label: str) -> str:
    """Swap a resolved address out of a message for its variable name."""
    return str(value).replace(address, label).replace(address.rstrip("/"), label)


def _served_model_name(configured: str) -> str:
    """The model name as the server will see it, minus the litellm prefix."""
    return configured.split("/", 1)[1] if "/" in configured else configured


def _model_names(body: str) -> set[str] | None:
    """Every name the endpoint answers to, or None if the shape is unfamiliar.

    Aliases count: llama-swap lets a model respond to names that are not its
    id, and rejecting a configured alias would be a false failure.
    """
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list):
        return None

    names: set[str] = set()
    for entry in parsed["data"]:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("id"), str):
            names.add(entry["id"])
        aliases = (entry.get("meta") or {}).get("llamaswap", {}).get("aliases")
        if isinstance(aliases, list):
            names.update(a for a in aliases if isinstance(a, str))
    return names or None


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
