"""Preflight: the validation that has to execute something.

Config validation covers everything checkable from the file alone. These need a
real target repo, a working environment, and a network — is the tree clean, does
the test command actually pass, is `setup_command` genuinely idempotent, does
are both models reachable.

All of it runs at the start of `run` as well as under `validate`. Failing fast
beats failing on stage 30.

**These commands are host-specific.** `setup_command`, `test_command`, and
`full_test_command` assume a particular machine's Docker, runtime, and paths, so
this validates *this host* — not the config in the abstract.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

from orchestrator.apistatus import classify
from orchestrator.configversion import committed_sha, problem_starting
from orchestrator.commands import CommandResult, CommandRunner, truncate_middle
from orchestrator.config import ProjectConfig
from orchestrator.flake import FlakeVerdict, adjudicate, append_flakes
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
    check_models: bool = True,
    check_approval: bool = True,
    config_path=None,
    recorded_base_sha: str = "",
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
    checks.append(_read_budget_check(cfg, git))
    checks.append(_unfolded_progress_check(cfg))
    checks.extend(_endpoint_checks(cfg))
    if check_endpoint:
        checks.extend(check_executor_endpoint(cfg))
    checks.extend(
        _environment_checks(
            cfg, runner, run_tests=run_tests, project_dir=project_dir
        )
    )

    if check_models:
        checks.extend(_model_checks(cfg))
    if check_approval and config_path is not None:
        checks.append(_approval_check(cfg, config_path))
    if recorded_base_sha:
        checks.append(_baseline_still_reachable(cfg, recorded_base_sha))

    return checks


def _ripgrep_check() -> Check:
    """`rg` is on PATH.

    The `search` tool shells out to it for all three roles, and a missing
    binary would surface as a bare `FileNotFoundError` inside a planner tool
    loop — an exception where the model expects a result, forty minutes into a
    stage. It is the only external command the pipeline itself requires that
    the operator did not name in config, so it is the only one that has to be
    checked rather than simply run.
    """
    found = shutil.which("rg")
    return Check(
        "ripgrep is installed",
        bool(found),
        found or "the search tool needs `rg` on PATH (brew install ripgrep)",
    )


def _work_dir_is_ignored(cfg: ProjectConfig, git: Git) -> Check:
    """Run data inside the target repo must not be tracked by it.

    The work dir defaults under the plan directory, which is committed — the
    plan and the record of what was done to it are one project and belong
    together. The data beside them is not: it is written on every node, and
    every stage would find a dirty tree.

    That failure is self-inflicted and confusing rather than loud. `precheck`
    refuses to cut a stage branch over changes it cannot attribute, so the
    first symptom is a run that stops on a stage with nothing wrong with it —
    which `CLAUDE.md` already records happening for an uncommitted `checks`
    rewrite. Blocking here says it once, at the only moment it is cheap to fix.

    Not a check at all when the work dir is outside the repo, which is where
    this project's own history still lives.
    """
    name = "work dir is git-ignored"
    work = Path(cfg.work_dir)
    if not work.is_relative_to(cfg.target_repo):
        return Check(name, True, f"outside the target repo: {work}", fatal=False)

    rel = work.relative_to(cfg.target_repo)
    # Queried with a trailing slash. `.code_gantry/` is a directory pattern,
    # and for a path that does not exist yet git cannot tell a directory from a
    # file — so the pattern misses without it. Measured: `check-ignore` on
    # `.code_gantry` exits 1 and on `.code_gantry/` exits 0, against the same
    # `.gitignore`. The work dir never exists at the moment this runs for the
    # first time, which is the only moment the check matters.
    if git.is_ignored(f"{rel}/"):
        return Check(name, True, str(rel))
    return Check(
        name,
        False,
        f"{rel} is inside the target repo and not ignored. Every run writes "
        f"there, so the tree would never be clean and no stage could cut a "
        f"branch. Add `{rel.name}/` to a .gitignore beside it.",
    )


def _repo_checks(cfg: ProjectConfig, git: Git, *, for_resume: bool) -> list[Check]:
    checks = [_ripgrep_check()]

    if not cfg.target_repo.is_dir():
        return [Check("target repo exists", False, f"{cfg.target_repo} is not a directory")]
    checks.append(Check("target repo exists", True, str(cfg.target_repo)))

    if not git.is_repo():
        return checks + [Check("target repo is a git repo", False, str(cfg.target_repo))]
    checks.append(Check("target repo is a git repo", True))

    checks.append(_work_dir_is_ignored(cfg, git))

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


def _read_budget_check(cfg: ProjectConfig, git: Git) -> Check:
    """Which files can never be supplied to the executor as reference.

    `read_files` is capped in total by `executor.max_read_lines`. A file longer
    than that cap on its own cannot be delivered in any stage, by any planner,
    in any combination — it is not a tail that got trimmed to fit, it is a
    structural impossibility that the loop rediscovers on every attempt.

    Observed: a 1,443-line model was declared as reference against a 1,200-line
    budget, withheld identically on six consecutive stages, and logged each
    time as though it were a sizing decision. Nothing could learn from it,
    because nothing was variable.

    A warning rather than a failure. The operator may well be content for large
    files to be unreachable — the budget exists because unbounded context
    degraded this executor — and the right response is a decision made once,
    not a run that refuses to start.
    """
    cap = cfg.executor.max_read_lines
    if not cap:
        return Check("read budget", True, "no ceiling on read_files")

    ref = cfg.project_branch if git.branch_exists(cfg.project_branch) else cfg.base_ref
    try:
        paths = git.tracked_paths(git.rev_parse(ref))
    except GitError as e:  # pragma: no cover - the repo checks caught this already
        return Check("read budget", True, f"could not enumerate tracked files: {e}")

    oversized: list[tuple[str, int]] = []
    for path in paths:
        full = cfg.target_repo / path
        try:
            with full.open("rb") as fh:
                head = fh.read(8192)
                # git's own heuristic. Without it this reports .psd and .eps
                # fixtures at the top, which nobody would pass as reference,
                # and the warning reads as noise rather than as the two large
                # source files it is actually about.
                if b"\0" in head:
                    continue
                lines = head.count(b"\n") + sum(
                    chunk.count(b"\n") for chunk in iter(lambda: fh.read(65536), b"")
                )
        except OSError:
            continue
        if lines > cap:
            oversized.append((path, lines))

    if not oversized:
        return Check("read budget", True, f"every tracked file fits in {cap} lines")

    # Smallest first, not largest. The biggest file over the line is usually a
    # fixture nobody would cite; the ones just over it are the plausible
    # references, and they are what a new ceiling would actually recover.
    oversized.sort(key=lambda item: item[1])
    listed = ", ".join(f"{path} ({lines})" for path, lines in oversized[:3])
    return Check(
        "read budget",
        False,
        f"max_read_lines is {cap}; {len(oversized)} tracked file(s) exceed it "
        f"on their own and can never be supplied as read_files, however the "
        f"planner combines them. Closest to the line: {listed}"
        + (", …" if len(oversized) > 3 else "")
        + f". Raising the ceiling past {oversized[0][1]} would recover the "
        "first of them; leaving it means they are withheld silently, on every "
        "attempt of every stage that asks for one.",
        fatal=False,
    )


def _unfolded_progress_check(cfg: ProjectConfig) -> Check:
    """How much the progress log has accumulated since anyone last folded it.

    Folding is the largest single lever on a run's bill and the one step
    nothing in the loop performs — deliberately, because rewriting a plan is a
    judgement about what the work has become and should not happen unattended
    in the middle of doing the work. The consequence is that it only happens if
    someone remembers, and nothing was reminding them.

    The cost is invisible in behaviour. The log is spliced into the plan block,
    which carries a cache breakpoint, so every landing invalidates that block
    and pays to rewrite it: the log does not merely cost its own size, it drags
    the plan tree through the cache with it. Measured between two folds, 292
    bytes to 263KB over 66 landings, with the extra per-stage cost growing with
    the *gap* rather than with the log — so the total is quadratic in how long
    nobody looked. Every stage still lands and every gate still passes.

    Said at preflight because that is the moment acting on it is free: nothing
    is in flight, and folding costs a commit. Never fatal — a run that refuses
    to start until someone rewrites a plan is worse than an expensive one.

    The line itself is the accounting and nothing else. The first version
    explained all of the above on every start, which is four sentences an
    operator reads once and scrolls past forever after, in a preflight block
    they are scanning for the thing that is wrong. The reasoning belongs here,
    where the code beneath it keeps it honest.
    """
    if not cfg.plan_addendum_path:
        return Check("progress log", True, "no addendum configured")

    from orchestrator.addendum import _target

    try:
        target = _target(cfg.target_repo, cfg.plan_addendum_path)
        body = target.read_text()
    except OSError:
        # Not yet written is the normal state of a new project, and an
        # unreadable one is already reported by the plan checks.
        return Check("progress log", True, "no progress log yet")

    entries = [line for line in body.splitlines() if line.startswith("## ")]
    if not entries:
        return Check("progress log", True, "nothing to fold")

    landings = sum(1 for line in entries if line.startswith("## What "))
    size = len(body.encode())
    shown = f"{size / 1024:.0f}KB" if size >= 1024 else f"{size} bytes"
    rel = target.relative_to(cfg.target_repo)
    return Check(
        "progress log",
        False,
        f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} and "
        f"{landings} landing{'' if landings == 1 else 's'} unfolded in "
        f"{rel} ({shown})",
        fatal=False,
    )


def _plan_checks(cfg: ProjectConfig, git: Git) -> list[Check]:
    """The plan tree resolves, and no child escapes the root's directory.

    Resolved at the project branch when there is one — that is where the run
    will read it, and checking the base instead would pass on a plan the run
    is not going to use, or fail on a restructure the branch has already done.
    """
    ref = cfg.project_branch if git.branch_exists(cfg.project_branch) else cfg.base_ref
    try:
        plan_sha = git.rev_parse(ref)
    except GitError as e:  # pragma: no cover - caught upstream
        return [Check("plan resolves", False, str(e))]

    tree = resolve_plan_tree(git, cfg.plan_root, plan_sha)
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
    cfg: ProjectConfig, runner: CommandRunner, *, run_tests: bool, project_dir=None
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
    excused: dict[str, FlakeVerdict] = {}
    for label, command in (
        ("test_command", cfg.test_command),
        ("full_test_command", cfg.full_test_command),
    ):
        if not command:
            continue
        seen = command in already_run
        result = already_run.get(command) or runner.run(command)
        already_run[command] = result

        # The same adjudication the merge gate uses, for the same reason. A
        # large legacy suite is rarely order-independent, and preflight ran the
        # whole thing with no gate at all — so it failed the run on a file the
        # pipeline would have re-run alone and forgiven. Observed on a spec that
        # had already been excused twenty-one times.
        #
        # Adjudicated once per command, not per label: the second label reuses
        # the verdict for the same reason it reuses the result.
        verdict = excused.get(command)
        if not result.ok and verdict is None and not seen:
            verdict = adjudicate(
                output=result.output, command=command, cfg=cfg, runner=runner
            )
            excused[command] = verdict
        flaked = bool(verdict and verdict.flaked)

        if flaked and project_dir is not None:
            # Into the same file the merge gate writes to. That file earns its
            # keep by being countable — the spec that motivated this was
            # identifiable as noise because it already had twenty-one entries —
            # so an excusal made here and not written down undercounts the next
            # one. `preflight` stands in for the stage id, since there is no
            # stage yet.
            append_flakes(
                _project_root(project_dir),
                "preflight",
                verdict.files,
                verdict.seeds,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            )

        if flaked:
            files = ", ".join(verdict.files) or "(unnamed)"
            detail = (
                "red as a whole, green file by file — excused as a suite flake "
                f"rather than a red repository: {files}. The merge gate applies "
                "the same rule, so a run started here would not have been "
                "stopped by this."
            )
        elif seen and result.ok:
            detail = "same command as above; not run twice"
        elif result.ok:
            detail = ""
        else:
            detail = (
                "a target repo that is already red makes every subsequent "
                f"verdict meaningless\n{_excerpt(result.output)}"
            )

        checks.append(
            Check(
                f"{label} passes on a clean tree",
                result.ok or flaked,
                detail,
                # A flake is reported and not enforced: it is real information
                # about the suite, and hiding it would make the next one
                # invisible. `ok` with a detail prints as a pass that says why.
                fatal=not (flaked or (seen and result.ok)),
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

    A routing prefix such as the `openai/` in `openai/qwen3-coder-next` is
    stripped before the request leaves the router. What the server sees — and
    what must match — is the remainder.
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
            client = builder(cfg)
        except Exception as e:  # noqa: BLE001
            checks.append(Check(f"{label} client builds", False, str(e)))
            continue
        checks.append(Check(f"{label} client builds", True))
        checks.append(_credential_check(label, client))
    return checks


def _credential_check(label: str, client) -> Check:
    """Prove the key is live, by any answer at all.

    Building a client proves a variable is set and well-formed. It does not
    prove the key works, and an expired one looks identical until something
    calls it. That happened: a key with a short expiry died mid-session, every
    planner call returned 401 as a generic blocked verdict, and the failure was
    mistaken for the planner declining to use its tools. The local executor
    endpoint had always been called for real here; the two paid, credentialed
    services — the ones with an expiry and a billing state — were taken on
    trust.

    What is being tested is authentication, not a useful completion. So any
    response that is not an auth failure passes: a 400 complaining about a
    token budget means the request was accepted, parsed, and answered, which is
    everything this needs to know. Chasing a clean 200 across providers means
    tracking each one's parameter spellings, and a check that breaks when a
    vendor renames a field is a check that gets skipped.
    """
    try:
        _ping(client)
        return Check(f"{label} credentials work", True)
    except Exception as e:  # noqa: BLE001
        failure = classify(e)
        if failure.is_auth:
            return Check(f"{label} credentials work", False, failure.describe())
        return Check(
            f"{label} credentials work",
            True,
            f"the service answered ({failure.status or 'no status'}), so the "
            "key is live",
        )


def _ping(client) -> None:
    """The cheapest call each provider accepts.

    A small budget rather than one token: a reasoning model spends its budget
    thinking and cannot finish inside one, which produces a 400 that is
    perfectly informative but noisy to read.
    """
    inner = getattr(client, "_client", client)
    model = getattr(getattr(client, "cfg", None), "model", None)
    if hasattr(inner, "messages") and hasattr(inner.messages, "create"):
        inner.messages.create(
            model=model, max_tokens=1, messages=[{"role": "user", "content": "."}]
        )
        return
    inner.chat.completions.create(
        model=model, max_completion_tokens=16,
        messages=[{"role": "user", "content": "."}],
    )


def _build_planner(cfg: ProjectConfig):
    from orchestrator.planner import make_planner

    return make_planner(cfg.planner)


def _build_reviewer(cfg: ProjectConfig):
    from orchestrator.reviewer import make_reviewer

    return make_reviewer(cfg.reviewer)


def _project_root(project_dir):
    """The project directory, from either a `ProjectPaths` or the path itself.

    Imported here rather than at module scope: `runtime` imports this module, so
    a top-level import would close the cycle.
    """
    from orchestrator.runtime import ProjectPaths

    return (
        project_dir.project_dir
        if isinstance(project_dir, ProjectPaths)
        else project_dir
    )


def _baseline_still_reachable(cfg: ProjectConfig, recorded: str) -> Check:
    """Is the commit this run measured itself against still in `base_ref`?

    Asked here rather than only at the merge gate, because it is a property of
    the world at startup and cannot become true part-way through a stage. The
    same question inside `verify` is answered after a planner call and an
    executor attempt have already been paid for — nine minutes and two model
    calls, on one measured resume, to learn something knowable before any work
    began.

    The other branch-identity checks stay where they are: HEAD wandering and a
    stage branch diverging can only happen mid-stage, so mid-stage is where
    they belong. Two questions with different lifetimes, asked in one place,
    and the cheap one was paying the expensive one's price.
    """
    from orchestrator.gitops import Git

    name = "the run's baseline is still in " + cfg.base_ref
    git = Git(cfg.target_repo)
    try:
        current = git.rev_parse(cfg.base_ref)
    except Exception:
        return Check(name, False, f"{cfg.base_ref} does not resolve")
    if current == recorded or git.is_ancestor(recorded, current):
        moved = "" if current == recorded else f"moved on to {current[:12]}"
        return Check(name, True, f"{recorded[:12]} {moved}".strip())
    return Check(
        name,
        False,
        f"{cfg.base_ref} was rewritten: {recorded[:12]} is no longer an "
        f"ancestor of {current[:12]}. Start a new run — this one measured "
        "itself against a commit the branch no longer contains.",
    )


def _approval_check(cfg: ProjectConfig, config_path) -> Check:
    """The config must be committed, and the working copy must match it.

    What replaced `orchestrator approve`. A run is identified by the git sha of
    the config it read, which is the same bytes an approval hashed plus an
    author, a message and whatever review the repository requires — so the
    check is that the sha exists and is current, not that someone ran a
    command.
    """
    if config_path is None or not Path(config_path).exists():
        return Check("config is committed", True, "no config file to check",
                     fatal=False)
    # A config outside the target repo has no commit to cite, so there is
    # nothing to check and nothing to gain by refusing. Warned rather than
    # blocked: this is the layout every project had before the config moved
    # in, and a fatal check here would strand a run mid-migration on a
    # property it was never able to have.
    if not Path(config_path).resolve().is_relative_to(Path(cfg.target_repo).resolve()):
        return Check(
            "config is committed",
            True,
            f"{config_path} is outside the target repo, so it has no git "
            "version. Move it in to get one.",
            fatal=False,
        )
    ref = cfg.project_branch
    if committed_sha(config_path, cfg.target_repo, ref) is None:
        ref = cfg.base_ref
    problem = problem_starting(config_path, cfg.target_repo, ref)
    return Check("config is committed", not problem, problem)


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
