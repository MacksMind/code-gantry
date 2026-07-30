"""Project configuration: schema, loading, and structural validation.

Two things make this file the centre of the design's safety story.

**The declarative/executable partition.** The planner may author declarative
fields — instruction text, globs, prose constraints, regexes. It may never
author an executable one. That is enforced here, as an allowlist on what the
planner's structured output is permitted to contain, not as a comment
somewhere. A field the planner may not set should be impossible for it to
return.

**The denylist.** Some commands are refused regardless of operator approval,
because a human skims a sixty-line YAML once, motivated to start a run.

Structural validation is everything checkable from the file alone. The checks
that execute something — clean tree, test command actually passes, Aider's
flags, endpoint reachability — live in `preflight`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

StageKind = Literal["agent", "script"]

# Stage ids name directories and git branches, so they must not contain
# separators, traversal, or anything git rejects in a ref.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Fields the planner is allowed to author. Everything else in Stage is
# operator-only. `nodes` and `planner` both import this; it is the single
# definition of the partition.
# `kind` is deliberately absent: a `script` stage needs an operator-authored
# `command`, and with no static stage list there is nowhere for the operator to
# put one. Every planner-derived stage is an `agent` stage; mechanical
# transforms are expressed as an instruction to write and run a script, which
# Aider does inside its own edit loop.
PLANNER_WRITABLE_FIELDS = frozenset(
    {
        "id",
        "instruction",
        "edit_files",
        "read_files",
        "constraints",
        "acceptance",
        "forbidden_patterns",
        "test_paths",
    }
)

# Refused regardless of operator approval. Each entry is (pattern, why).
#
# These are the moves that are catastrophic rather than merely wrong, and that
# a tired operator would not notice in a config review. Deliberately narrow:
# a denylist that fires on legitimate commands gets disabled.
DENYLIST: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bgit\s+push\b"),
        "the orchestrator must never push; the outer merge is the operator's",
    ),
    (
        re.compile(r"\bgit\s+(checkout|switch)\b"),
        "changing branches from inside a command would break the stage's "
        "branch identity; the orchestrator manages checkout itself",
    ),
    (
        re.compile(r"\bgit\s+merge\b"),
        "merging from inside a command bypasses the review gate",
    ),
    (
        re.compile(r"\bgit\s+(reset|clean)\b"),
        "resetting from inside a command would destroy the stage baseline the "
        "scope guard and the reviewer both measure against",
    ),
    (
        re.compile(r"\bgem\s+install\b"),
        "installing outside the bundle mutates the host's global gems; use "
        "bundle install",
    ),
    (
        re.compile(r"\b(cap|kubectl|terraform|serverless|fly|heroku)\b"),
        "deployment is out of scope and must not happen unattended",
    ),
    (
        re.compile(r"\bsudo\b"),
        "nothing here needs elevated privileges",
    ),
    (
        re.compile(r"\brm\s+-[a-zA-Z]*[rf]"),
        "recursive or forced deletion is never needed in a declared command",
    ),
)


class ConfigError(Exception):
    """Carries every problem found, not just the first, so one `validate` run
    is enough to fix the file."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


class _Strict(BaseModel):
    """Rejects unknown keys. A misspelled config key that silently does
    nothing is worse than an error — the operator would believe a gate was
    active when it was not."""

    model_config = ConfigDict(extra="forbid")


class _EndpointConfig(_Strict):
    """Where a model role is reached, and how that address is supplied.

    `api_base_env` exists because a hostname is an infrastructure fact rather
    than a project decision. `projects/<slug>/config.yaml` is a tracked file
    that gets hashed for approval; a Spark hostname, an internal gateway, or a
    private port has no business in it.

    Naming a variable rather than interpolating one into a string is
    deliberate. General `${VAR}` substitution would reach command fields too,
    and then `test_command: "${CMD}"` would let the denylist scan a harmless
    literal while something else entirely ran. Restricting the mechanism to a
    single non-command field keeps every command in an approved config exactly
    what the operator read.
    """

    api_base: str | None = None
    api_base_env: str | None = None

    def resolve_api_base(self) -> str | None:
        """The address, reading the environment if that is where it lives.

        Deliberately not resolved at load time: `status` and `resume` load a
        config to read a report, and failing that on a machine that never
        exports the variable would be gratuitous. Preflight checks it, so a
        missing export fails `validate` rather than stage 1.
        """
        if self.api_base_env:
            value = os.environ.get(self.api_base_env)
            if not value:
                raise KeyError(
                    f"api_base_env names {self.api_base_env}, which is not set "
                    "in the environment"
                )
            return value
        return self.api_base


class ExecutorConfig(_EndpointConfig):
    model: str
    api_key_env: str | None = None
    lint_command: str | None = None
    # Aider's default varies by model and is usually right for hosted ones. A
    # local model frequently cannot produce a valid diff — the first real run
    # ended in "the LLM did not conform to the edit format" — and `whole` trades
    # tokens for reliability. Left unset so Aider's per-model default applies.
    edit_format: str | None = None
    # A path to Aider's own metadata JSON, passed through unchanged. litellm has
    # no entry for a local model id, so without this Aider guesses at the context
    # window and warns about it. A path rather than the values themselves: the
    # numbers describe the endpoint, not the project, and they are Aider's
    # schema to define rather than ours to mirror.
    model_metadata_file: str | None = None
    # Repo map off by default: stages declare the files they need, and an
    # unscoped map swamps a local model's context before the task is stated.
    map_tokens: int = 0
    # Aider's flag surface changes between releases. This is the escape hatch
    # for correcting it without waiting on a code change.
    extra_args: list[str] = []


class PlannerConfig(_EndpointConfig):
    provider: Literal["anthropic"] = "anthropic"
    model: str
    api_key_env: str = "ANTHROPIC_API_KEY"
    # Anthropic's ephemeral cache lasts about five minutes by default. Between
    # two planner calls sits a whole stage — an executor attempt, a scoped
    # suite, a review, a full suite — which on a large project is comfortably
    # longer than that, so the prefix expires before it is ever reused. "1h"
    # buys a longer window at a higher write cost; leave unset to take the
    # provider default.
    cache_ttl: str | None = None
    # Operator prose appended to the planner's system prompt, fixed for the
    # run. Inline rather than a path to a file: approval hashes this file's
    # bytes, and guidance living elsewhere could be rewritten after approval to
    # change how the planner behaves without invalidating anything.
    guidance: str | None = None
    request_timeout_seconds: float = 900.0
    max_retries: int = 2


class ReviewerConfig(_EndpointConfig):
    provider: Literal["openai"] = "openai"
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    # OpenAI caches automatically on prefix, with a short default lifetime.
    # "24h" extends it, at the cost of the prefix being stored for that long —
    # a data-retention decision for the operator, so there is no default.
    prompt_cache_retention: str | None = None
    request_timeout_seconds: float = 600.0
    max_retries: int = 2


class Limits(_Strict):
    max_test_retries: int = 3
    max_rework_retries: int = 2
    # Global across the run, not per-stage: per-stage caps let a pathological
    # project consume unbounded paid inference one stage at a time.
    max_planner_interventions: int = 12
    max_stages: int = 60
    aider_timeout_seconds: int = 1800
    command_timeout_seconds: int = 3600
    wall_clock_hours: float = 14.0


class Stage(_Strict):
    """One unit of work — the shippable unit.

    Declarative fields may be authored by the planner. Executable and policy
    fields may not; they come from the config's stage defaults.
    """

    id: str
    kind: StageKind = "agent"

    # --- planner-writable (declarative) ---
    instruction: str | None = None
    edit_files: list[str] = []
    read_files: list[str] = []
    constraints: str | None = None
    acceptance: str | None = None
    forbidden_patterns: list[str] = []
    # Extra spec paths the planner expects to be affected beyond those the diff
    # reveals. Paths, never a command — see `scoped_test_command`.
    test_paths: list[str] = []

    # --- operator-only (executable) ---
    command: str | None = None
    preconditions: list[str] = []
    context_commands: list[str] = []
    setup_command: str | None = None
    test_command: str | None = None
    checks: list[str] = []

    # --- operator-only (policy) ---
    require_new_tests: bool = False
    require_scoped_tests: bool = False
    review: bool = True
    full_suite_on_approval: bool | None = None

    def effective_test_command(self, cfg: ProjectConfig) -> str | None:
        return self.test_command or cfg.test_command

    def effective_setup_command(self, cfg: ProjectConfig) -> str | None:
        return self.setup_command or cfg.setup_command

    def full_suite_required(self, cfg: ProjectConfig) -> bool:
        if self.full_suite_on_approval is not None:
            return self.full_suite_on_approval
        return cfg.full_suite_on_approval


class StageDefaults(_Strict):
    """Operator-authored executable and policy fields applied to every
    planner-derived stage.

    The planner invents *what* the work is; it cannot invent *how* to run
    anything. Since there is no static stage list, this is where the executable
    half of a stage comes from.
    """

    preconditions: list[str] = []
    context_commands: list[str] = []
    checks: list[str] = []
    require_new_tests: bool = False
    require_scoped_tests: bool = False
    review: bool = True


class ProjectConfig(_Strict):
    # Documentation, not behaviour: these commands assume a particular
    # machine's Docker, Ruby, and paths. Recording it stops a future reader
    # running this config elsewhere and misreading the failures.
    host: str | None = None

    target_repo: Path
    base_ref: str = "main"
    project_branch: str

    # Repo-relative path to the plan document. A document, not a directory:
    # pointing at `docs/` would sweep every runbook and ADR into every review
    # prompt.
    plan_root: str

    setup_command: str | None = None
    test_command: str | None = None
    full_test_command: str | None = None
    # Optional. `{paths}` is filled by the orchestrator from the stage diff.
    scoped_test_command: str | None = None

    full_suite_on_approval: bool = True

    # What counts as a test file for `require_new_tests`.
    test_file_patterns: list[str] = [
        "**/test_*.py",
        "**/*_test.py",
        "**/*_test.go",
        "**/*_test.rb",
        "**/*_spec.rb",
        "**/*.test.ts",
        "**/*.test.tsx",
        "**/*.test.js",
        "**/*.spec.ts",
        "**/*.spec.js",
        "test/**",
        "tests/**",
        "spec/**",
    ]

    executor: ExecutorConfig
    planner: PlannerConfig
    reviewer: ReviewerConfig
    limits: Limits = Limits()

    stage_defaults: StageDefaults = StageDefaults()

    rework_strategy: Literal["fresh", "continue"] = "fresh"
    rework_reset: bool = True

    @property
    def plan_root_path(self) -> Path:
        return self.target_repo / self.plan_root

    @property
    def stage_branch_namespace(self) -> str:
        """Child branches live in a namespace *beside* the project branch, not
        under it.

        Git refs are filesystem paths, so `refs/heads/upgrade/rails-5` (a file)
        and `refs/heads/upgrade/rails-5/stage-001-x` (a directory) cannot
        coexist — git refuses with "cannot lock ref". Appending `-stage` makes
        it a sibling path component, which keeps the group greppable
        (`git branch --list 'upgrade/rails-5-stage/*'`) and deletable together
        while remaining a legal ref.
        """
        return f"{self.project_branch}-stage"

    def stage_branch(self, index: int, stage_id: str) -> str:
        return f"{self.stage_branch_namespace}/{index:03d}-{stage_id}"

    def all_commands(self) -> list[tuple[str, str]]:
        """Every executable string in the config, as (where, command).

        Used by the denylist check and by preflight. If a new executable field
        is added and not listed here, the denylist silently stops covering it —
        so this is deliberately exhaustive rather than reflective.
        """
        out: list[tuple[str, str]] = []
        for label, command in (
            ("setup_command", self.setup_command),
            ("test_command", self.test_command),
            ("full_test_command", self.full_test_command),
            ("scoped_test_command", self.scoped_test_command),
            ("executor.lint_command", self.executor.lint_command),
        ):
            if command:
                out.append((label, command))
        for i, command in enumerate(self.stage_defaults.preconditions):
            out.append((f"stage_defaults.preconditions[{i}]", command))
        for i, command in enumerate(self.stage_defaults.context_commands):
            out.append((f"stage_defaults.context_commands[{i}]", command))
        for i, command in enumerate(self.stage_defaults.checks):
            out.append((f"stage_defaults.checks[{i}]", command))
        return out

    def stage_from_planner(self, fields: dict) -> Stage:
        """Build a Stage from planner output, merging in operator-only fields.

        The planner's fields are filtered against the allowlist rather than
        trusted, so even a schema failure upstream cannot introduce an
        executable field.
        """
        safe = {k: v for k, v in fields.items() if k in PLANNER_WRITABLE_FIELDS}
        defaults = self.stage_defaults
        return Stage(
            **safe,
            preconditions=list(defaults.preconditions),
            context_commands=list(defaults.context_commands),
            checks=list(defaults.checks),
            require_new_tests=defaults.require_new_tests,
            require_scoped_tests=defaults.require_scoped_tests,
            review=defaults.review,
        )


def denylist_violations(commands: list[tuple[str, str]]) -> list[str]:
    """Refused regardless of operator approval."""
    problems = []
    for where, command in commands:
        for pattern, why in DENYLIST:
            if pattern.search(command):
                problems.append(
                    f"{where}: command {command!r} matches the denylist "
                    f"(/{pattern.pattern}/) — {why}. This is refused regardless "
                    "of approval."
                )
    return problems


def parse_config(data: dict) -> ProjectConfig:
    if not isinstance(data, dict):
        raise ConfigError(["config must be a YAML mapping"])

    try:
        cfg = ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_pydantic_errors(e)) from e

    problems = _structural_problems(cfg)
    if problems:
        raise ConfigError(problems)
    return cfg


def load_config(path: Path | str) -> ProjectConfig:
    path = Path(path)
    try:
        raw = path.read_text()
    except OSError as e:
        raise ConfigError([f"cannot read config {path}: {e}"]) from e

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError([f"{path} is not valid YAML: {e}"]) from e

    if not isinstance(data, dict):
        raise ConfigError([f"{path} must contain a YAML mapping at the top level"])

    return parse_config(data)


def validate_stage(stage: Stage, cfg: ProjectConfig) -> list[str]:
    """Well-formedness of a single stage, planner-derived or otherwise.

    Runs before anything acts on a planner-produced spec: a bad stage should
    fail here, cheaply, rather than as a confusing verify failure.
    """
    problems: list[str] = []
    where = f"stage {stage.id!r}"

    if not _SAFE_ID.match(stage.id):
        problems.append(
            f"stage id {stage.id!r} is not safe for a path or a git ref: use "
            "letters, digits, dot, dash, underscore"
        )

    if stage.kind == "agent" and not stage.instruction:
        problems.append(f"{where}: agent stages require an instruction")
    if stage.kind == "script" and not stage.command:
        problems.append(f"{where}: script stages require a command")
    if stage.kind == "agent" and stage.command:
        problems.append(
            f"{where}: `command` belongs to script stages; this stage is an "
            "agent stage"
        )

    if not stage.edit_files:
        problems.append(
            f"{where}: must declare edit_files — the scope guard is meaningless "
            "without it, and a stage that cannot name its files is too broad "
            "to be a stage"
        )

    if not stage.effective_test_command(cfg) and not stage.checks:
        problems.append(
            f"{where}: needs a test command (its own or the project's) or at "
            "least one check — otherwise nothing verifies it"
        )

    for pattern in stage.forbidden_patterns:
        try:
            re.compile(pattern)
        except re.error as e:
            problems.append(
                f"{where}: forbidden_patterns entry {pattern!r} is not a valid "
                f"regex: {e}"
            )

    problems.extend(
        denylist_violations(
            [(f"{where}.command", stage.command)] if stage.command else []
        )
    )

    return problems


def _format_pydantic_errors(e: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in e.errors()
    ]


def _structural_problems(cfg: ProjectConfig) -> list[str]:
    problems: list[str] = []

    if cfg.project_branch == cfg.base_ref:
        problems.append(
            f"project_branch {cfg.project_branch!r} must differ from base_ref "
            f"{cfg.base_ref!r}: the orchestrator must never commit to the "
            "branch it cuts from"
        )

    if cfg.project_branch.endswith("/") or ".." in cfg.project_branch:
        problems.append(
            f"project_branch {cfg.project_branch!r} is not a valid git ref"
        )

    if not cfg.plan_root:
        problems.append("plan_root is required: a project is defined by its plan")
    elif Path(cfg.plan_root).is_absolute() or ".." in Path(cfg.plan_root).parts:
        problems.append(
            f"plan_root {cfg.plan_root!r} must be a repo-relative path that does "
            "not escape the repo"
        )
    elif cfg.plan_root.endswith("/"):
        problems.append(
            f"plan_root {cfg.plan_root!r} looks like a directory. It must be a "
            "single document — pointing at a directory sweeps every runbook and "
            "ADR beside it into every review prompt"
        )

    if cfg.scoped_test_command and "{paths}" not in cfg.scoped_test_command:
        problems.append(
            "scoped_test_command must contain a {paths} placeholder — that is "
            "the slot the orchestrator fills with the stage's changed files"
        )

    if not cfg.test_command and not cfg.stage_defaults.checks:
        problems.append(
            "a project needs test_command, or checks in stage_defaults, or "
            "nothing will verify its stages"
        )

    for pattern in cfg.test_file_patterns:
        if not pattern:
            problems.append("test_file_patterns contains an empty pattern")

    for role, endpoint in (
        ("executor", cfg.executor),
        ("planner", cfg.planner),
        ("reviewer", cfg.reviewer),
    ):
        if endpoint.api_base and endpoint.api_base_env:
            problems.append(
                f"{role} sets both api_base and api_base_env. Pick one — "
                "silently preferring either would hide the mistake, and which "
                "endpoint gets called is not a detail to guess at"
            )

    problems.extend(denylist_violations(cfg.all_commands()))
    return problems
