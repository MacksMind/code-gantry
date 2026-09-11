"""Preflight: the validation that has to execute something.

Config validation covers everything checkable from the file alone. These need a
real target repo, a working environment, and a network — is the tree clean, does
the test command actually pass, is `setup_command` genuinely idempotent, does
every declared `checks` entry run, are both models reachable.

All of it runs at the start of `run` as well as under `validate`. Failing fast
beats failing on stage 30.

**These commands are host-specific.** `setup_command`, `full_test_command`
and every `stage_defaults.checks` entry assume a particular
machine's Docker, runtime, and paths, so this validates *this host* — not the
config in the abstract. The `checks` were missing from that list for as long as
they were missing from this file, which is the same omission written twice.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime

from code_gantry.apistatus import classify
from code_gantry.configversion import committed_sha, problem_starting
from code_gantry.commands import CommandResult, CommandRunner, truncate_middle
from code_gantry.config import ProjectConfig
from code_gantry.flake import FlakeVerdict, adjudicate, append_flakes
from code_gantry.gitops import Git, GitError


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

    checks.append(_ledger_check(cfg, project_dir))
    if cfg.plan_addendum_path:
        checks.append(
            Check(
                "plan_addendum_path",
                False,
                f"{cfg.plan_addendum_path!r} is no longer read; the ledger "
                "holds what the progress log held. Remove the setting",
                fatal=False,
            )
        )
    checks.append(_read_budget_check(cfg, git))
    checks.extend(_endpoint_checks(cfg))
    checks.extend(check_wire_match(cfg))
    if check_endpoint:
        checks.extend(check_executor_endpoint(cfg))

    if check_models:
        checks.extend(_model_checks(cfg))
    if check_approval and config_path is not None:
        checks.append(_approval_check(cfg, config_path))
    if recorded_base_sha:
        checks.append(_baseline_still_reachable(cfg, recorded_base_sha))

    # The suites go last, and not at all once something already blocks.
    #
    # Everything above is a git read, an environment lookup or a single HTTP
    # call. `_environment_checks` runs `setup_command` and both test commands,
    # which on a real project is minutes. It used to sit *ahead* of
    # `_model_checks`, whose first act is `env_var not in os.environ` — so a run
    # launched without credentials in the shell paid for a full green suite to
    # be told a variable was unset. Measured 2026-08-18: 5m30s for an answer
    # that was available before this function did anything.
    #
    # `CLAUDE.md` already states the rule, written about `branch_identity_
    # problems` asking a startup question from inside `verify` — a guard belongs
    # where its question can first be answered, not where its answer is
    # convenient. It did not catch this one because a rule is checked against
    # new work and nothing re-reads the code that predates it.
    #
    # Skipped rather than run-and-reported, because all three callers exit on a
    # blocking check: the suites would be paid for and then thrown away. And
    # said out loud rather than quietly omitted — a check that renders as
    # nothing is indistinguishable from one that passed.
    blocking = [c for c in checks if c.blocking]
    if blocking:
        checks.append(
            Check(
                "setup and the test suites",
                False,
                f"not run: {len(blocking)} blocking problem(s) above already stop "
                "this command, so minutes of suite would only delay the same "
                "exit. Fix those and run again.",
                fatal=False,
            )
        )
        return checks

    checks.extend(
        _environment_checks(
            cfg, runner, run_tests=run_tests, project_dir=project_dir
        )
    )

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


def _ledger_check(cfg: ProjectConfig, project_dir) -> Check:
    """The ledger holds a plan: at least one document, with something drawable."""
    from code_gantry.ledger import read_ledger
    from code_gantry.runtime import ProjectPaths

    if cfg.ledger is None:
        return Check(
            "ledger configured", False,
            "no `ledger:` section in the config; set `ledger.key_prefix` and "
            "import the plan with `code-gantry plan import`",
        )
    paths = (
        project_dir if isinstance(project_dir, ProjectPaths)
        else ProjectPaths(project_dir or cfg.work_dir)
    )
    if not paths.ledger.is_file():
        # Ordinary before the first import; `run` refuses on its own.
        return Check(
            "ledger holds a plan", False,
            f"no ledger at {paths.ledger} yet; `code-gantry plan import` "
            "creates it, and `run` refuses until it holds a plan",
            fatal=False,
        )
    views = read_ledger(paths.ledger).views()
    documents = views.documents()
    if not documents:
        return Check(
            "ledger holds a plan", False,
            f"{paths.ledger} holds no plan; import one with `code-gantry plan import`",
        )
    items = [n for n in views.walk() if n.kind == "item"]
    drawable = [
        n for n in items if n.owner == "pipeline" and views.is_open(n.key)
    ]
    human = sum(1 for n in items if n.owner == "human")
    findings = len(views.open_findings())
    return Check(
        "ledger holds a plan",
        True,
        f"{len(documents)} document(s), {len(items)} item(s), {len(drawable)} open "
        f"and drawable, {human} for a person; {findings} open finding(s)",
    )


def _declared_checks(cfg: ProjectConfig, runner: CommandRunner) -> list[Check]:
    """Every `stage_defaults.checks` entry, run here rather than by stage 000.

    These are operator-declared host commands exactly as `setup_command` is,
    and until this existed nothing proved one could start. What hid the gap is
    that the three commands this function already ran — setup and both test
    commands — all went through `docker compose` on the project it was found
    on, so a `checks` entry that runs on the *host* was first invoked by the
    first stage of the run. `bin/rubocop` could not materialise its bundle on
    a newly-provisioned machine and exited 1 in a fifth of a second;
    `_layer_checks` routes an ordinary non-zero back to the executor as "a
    required check failed", so two planner revisions and three attempts were
    spent telling a model its work was wrong by an environment that was never
    there. `CLAUDE.md` states the rule this breaks — a guard belongs where its
    question can first be answered — and it did not catch this one because a
    rule is checked against new work and nothing re-reads what predates it.

    Run in full, unlike `CommandRunner.run_all`, which stops at the first
    failure because for a stage the rest cannot change the verdict. Here the
    verdict is not the point: an operator repairing a machine wants every
    broken entry named in one pass, and in that same incident the second entry
    sat behind the first and went unproven for the life of the run.

    Not skipped by `--skip-preflight-tests`. That flag buys back minutes of
    suite; these are seconds, and the question they answer is about the host,
    which is not what the flag offers to skip.
    """
    commands = cfg.stage_defaults.checks
    if not commands:
        return []

    git = Git(cfg.target_repo)
    # Snapshotted rather than assumed. `working tree is clean` is blocking and
    # already ran, but `setup_command` has run since, so dirt found afterwards
    # is only attributable to the checks if they inherited a clean tree.
    was_clean = git.is_clean()

    checks: list[Check] = []
    for command in commands:
        one_line = " ".join(command.split())
        label = one_line if len(one_line) <= 60 else one_line[:57] + "..."
        result = runner.run(command)
        checks.append(
            Check(
                f"check runs: {label}",
                result.ok,
                "" if result.ok else _excerpt(result.output),
            )
        )

    if was_clean and not git.is_clean():
        dirtied = git.diff_names(git.head_sha())
        listed = ", ".join(dirtied[:8]) or "(unknown)"
        checks.append(
            Check(
                "checks leave the tree clean",
                False,
                f"the checks rewrote: {listed}\n"
                "an autocorrecting check found work to do on a tree no stage "
                "has touched yet. Starting now would carry those bytes into "
                "the first stage's diff with nothing to attribute them to. "
                "Commit them or revert them, then start again",
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

    checks.extend(_declared_checks(cfg, runner))
    if any(c.blocking for c in checks):
        # Said out loud rather than quietly omitted, for the reason the
        # top-level skip is: a check that renders as nothing is
        # indistinguishable from one that passed. Every caller exits on a
        # blocking check, so the suites would be paid for and thrown away.
        checks.append(
            Check(
                "the test suites",
                False,
                "not run: a declared check above already stops this command",
                fatal=False,
            )
        )
        return checks

    if not run_tests:
        checks.append(
            Check("test commands", True, "skipped at your request", fatal=False)
        )
        return checks

    # One everything-command, so one run. This was a loop over two labels with
    # a cache keyed on the command text, because `test_command` and
    # `full_test_command` were usually the same script and running a full suite
    # twice cost 23 minutes to learn one thing. The dedup went out with the
    # second name: there is nothing left to deduplicate against.
    label = "full_test_command"
    command = cfg.full_test_command
    if command:
        result = runner.run(command)

        # The same adjudication the merge gate uses, for the same reason. A
        # large legacy suite is rarely order-independent, and preflight ran the
        # whole thing with no gate at all — so it failed the run on a file the
        # pipeline would have re-run alone and forgiven. Observed on a spec that
        # had already been excused twenty-one times.
        verdict = None
        if not result.ok:
            verdict = adjudicate(
                output=result.output, command=command, cfg=cfg, runner=runner
            )
        flaked = bool(verdict and verdict.flaked)

        if flaked and project_dir is not None:
            # Into the same file the merge gate writes to. That file earns its
            # keep by being countable — the spec that motivated this was
            # identifiable as noise because it already had twenty-one entries —
            # so an excusal made here and not written down undercounts the next
            # one.
            #
            # `origin` rather than a sentinel in `stage_id`, which is where
            # "preflight" used to go: there is genuinely no stage here, and a
            # baseline flake is a different finding from a stage's suite going
            # red. `run_id` is left null for the same reason — preflight runs
            # before one is assigned, so null is the fact rather than a gap.
            #
            # The locators were missing here for as long as they have existed,
            # because this call was written before the argument was and nothing
            # made it follow. That is why the record is a dataclass now.
            append_flakes(
                _project_root(project_dir),
                None,
                verdict.files,
                verdict.seeds,
                datetime.now().astimezone().isoformat(timespec="seconds"),
                examples=verdict.examples,
                origin="preflight",
            )

        if not result.ok and project_dir is not None:
            # Every byte the adjudication decided on, kept whenever the suite
            # was not green — including the re-runs, because "it passed alone"
            # is the claim being made and the re-run is its evidence.
            #
            # Written from the *failure*, not from `flaked`: a preflight that
            # stops the run wants explaining just as much as one that forgives
            # it, and the stopping case is where an operator is already reading.
            #
            # Observed live: a red suite was excused naming `(unnamed)`, the
            # extraction having found no locator at all, and the output that
            # would have said why had been parsed and dropped. `last-run.out`
            # carried 63 lines after the run header and not one `Failed
            # examples`, so "the failure produced no locators" and "the pattern
            # stopped matching" were indistinguishable an hour later. This is
            # the only gate that can wave a red repository through, and it was
            # the only one with no artifact behind it.
            _keep_suite_output(
                _project_root(project_dir), label, command, result, verdict
            )

        if flaked:
            files = ", ".join(verdict.files) or "(unnamed)"
            detail = (
                "red as a whole, green file by file — excused as a suite flake "
                f"rather than a red repository: {files}. The merge gate applies "
                "the same rule, so a run started here would not have been "
                "stopped by this."
            )
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
                fatal=not flaked,
            )
        )

    checks.append(_tidiness_check(cfg))
    return checks


PREFLIGHT_SUITE_LOG = "preflight-suite.log"


def _keep_suite_output(
    project_dir: Path | str,
    label: str,
    command: str,
    result: CommandResult,
    verdict: FlakeVerdict | None,
) -> None:
    """Append what a non-green preflight ran and what came back.

    Appended rather than overwritten, and timestamped, because preflight runs
    once per run start and the question is usually "was it red last time too" —
    which a file replaced on every start cannot answer. Only written when the
    suite was not green, which is what bounds it: a project whose preflight is
    green never grows this file at all.

    The re-runs are included with their exit statuses. A flake verdict is the
    claim "red as a whole, green file by file", and `verdict.results` is the
    only evidence for the second half.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    when = datetime.now().astimezone().isoformat(timespec="seconds")

    parts = [f"\n=== {when} {label} ===\n$ {command}\n  exit {result.exit_code}\n"]
    parts.append(result.output)
    for extra in (verdict.results if verdict else []):
        parts.append(
            f"\n--- re-run: {extra.command}\n  exit {extra.exit_code}\n{extra.output}"
        )
    if verdict is not None:
        # The parse, beside the bytes it was parsed from. `(unnamed)` next to
        # output full of locators means the pattern; `(unnamed)` next to output
        # with none means the runner never printed one, and that distinction is
        # the whole reason this file exists.
        parts.append(
            f"\n--- adjudication: flaked={verdict.flaked} "
            f"files={verdict.files or '(unnamed)'} seeds={verdict.seeds}\n"
        )

    with (project_dir / PREFLIGHT_SUITE_LOG).open("a") as fh:
        fh.write("".join(parts))


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


def check_wire_match(cfg: ProjectConfig) -> list[Check]:
    """Each role's client against the dialect its model wants.

    A warning rather than a blocker: the call works and what is lost is cache
    control. Worth saying out loud because the failure is otherwise silent —
    a Gemini model on the Responses wire caches once and never grows, and
    nothing in any log names the cause.
    """
    from code_gantry.wirecheck import wire_mismatches

    problems = wire_mismatches(cfg)
    if not problems:
        return [Check("model wires match their clients", True)]
    return [
        Check("model wires match their clients", True, detail)
        for detail in problems
    ]


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

    wanted = cfg.executor.model
    if model_is_offered(wanted, names):
        checks.append(
            Check(f"endpoint offers {wanted!r}", True, f"{len(names)} model(s) available")
        )
    else:
        checks.append(
            Check(
                f"endpoint offers {wanted!r}",
                False,
                f"{label}/models: " + unavailable_detail(wanted, names),
            )
        )
    return checks


def _redact(value, address: str, label: str) -> str:
    """Swap a resolved address out of a message for its variable name."""
    return str(value).replace(address, label).replace(address.rstrip("/"), label)


def _served_model_name(configured: str) -> str:
    """The model name as the server will see it, minus the litellm prefix."""
    return configured.split("/", 1)[1] if "/" in configured else configured


def model_is_offered(configured: str, names) -> bool:
    """Whether the catalogue contains the configured model, either convention.

    Two exist and they are opposites. A litellm-style route carries the
    provider in the model string and strips it before the request leaves, so
    `openai/qwen3-coder-next` reaches the server as `qwen3-coder-next`. A
    gateway does the reverse: the slug *is* the id, and `openrouter/pareto-code`
    must be matched whole.

    The whole form is tried first and the stripped one only as a fallback, so a
    qualified catalogue is never matched on its suffix — `openai/pareto-code`
    is not `openrouter/pareto-code`, and collapsing them would merge two
    vendors' identically named models.

    Written after the first real gateway config failed this check against a
    catalogue that contained the model. A gate that refuses a correct config is
    worse than one that is merely absent, because the lesson it teaches is to
    turn it off.
    """
    names = set(names)
    if configured in names:
        return True
    stripped = _served_model_name(configured)
    return stripped != configured and stripped in names and not any(
        "/" in name for name in names
    )


def unavailable_detail(configured: str, names) -> str:
    """Why the model was not found, without reciting the whole catalogue.

    The first failure printed 437 names into `last-run.out`, which is appended
    across every resume. What an operator needs is the near miss — a typo is
    the overwhelmingly likely cause — and a count for everything else.
    """
    import difflib

    names = sorted(names)
    close = difflib.get_close_matches(configured, names, n=5, cutoff=0.6)
    if not close:
        close = difflib.get_close_matches(
            _served_model_name(configured), names, n=5, cutoff=0.6
        )
    shown = ", ".join(names) if len(names) <= 12 else ", ".join(close) or "nothing similar"
    return (
        f"{configured!r} is not offered. {len(names)} model(s) available; "
        f"closest: {shown}.\nA model id is matched whole first and then "
        "without its routing prefix, so both `openrouter/pareto-code` and "
        "`openai/qwen3-coder-next` are spelled here exactly as the operator "
        "means them. If this endpoint lists models lazily, this is the check "
        "to reconsider."
    )


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
    # All three, and the executor was the one missing. It had `GET /models`,
    # which proves the endpoint exists and nothing about whether we may call
    # it — while `resolve_policy` happened to make a real authenticated
    # completion whose line printed among these, so the gap read as covered.
    # That probe fired only for a routing policy, never for a concrete model,
    # and it belongs to a stage now. Finding a dead key at stage one costs a
    # derivation and a cut branch before anything says why.
    for label, env_var, builder in (
        ("planner", cfg.planner.api_key_env, _build_planner),
        ("reviewer", cfg.reviewer.api_key_env, _build_reviewer),
        ("executor", cfg.executor.api_key_env, _build_executor),
    ):
        if not env_var:
            # Only the executor can say it needs no key, and a local endpoint
            # that needs none has nothing to prove here — `GET /models` already
            # establishes that it answers. The planner and reviewer both carry
            # a default, so this can never skip them.
            continue
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


def _build_executor(cfg: ProjectConfig):
    """The executor's client, built the way an attempt builds it.

    Through `OpenAIExecutorModel` rather than a client of preflight's own: the
    dialect decides which SDK this is, and a check that picks its own would be
    proving a key against an endpoint the run does not use.
    """
    from code_gantry.executorclient import OpenAIExecutorModel

    return OpenAIExecutorModel(cfg.executor)


def _build_planner(cfg: ProjectConfig):
    from code_gantry.planner import make_planner

    return make_planner(cfg.planner)


def _build_reviewer(cfg: ProjectConfig):
    from code_gantry.reviewer import make_reviewer

    return make_reviewer(cfg.reviewer)


def _project_root(project_dir):
    """The project directory, from either a `ProjectPaths` or the path itself.

    Imported here rather than at module scope: `runtime` imports this module, so
    a top-level import would close the cycle.
    """
    from code_gantry.runtime import ProjectPaths

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
    from code_gantry.gitops import Git

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

    What replaced `code-gantry approve`. A run is identified by the git sha of
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
