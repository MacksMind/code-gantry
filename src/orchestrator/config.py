"""Run configuration: schema, loading, and structural validation.

Structural validation is everything checkable without executing anything.
The checks that shell out — clean working tree, test command actually passes,
model endpoints reachable — live in `preflight` because they need a real
target repo and a network.

Both run before any stage does. Failing fast beats failing on stage 6.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

StageKind = Literal["agent", "script", "manual"]

# Stage ids name directories under runs/<run_id>/stages/, so they must not
# contain separators or traversal.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ConfigError(Exception):
    """A config problem the operator must fix. Carries every problem found,
    not just the first, so one run of `validate` is enough to fix the file."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


class _Strict(BaseModel):
    """Rejects unknown keys. A misspelled config key that silently does
    nothing is worse than an error — the operator would believe a gate was
    active when it was not."""

    model_config = ConfigDict(extra="forbid")


class ExecutorConfig(_Strict):
    model: str
    api_base: str | None = None
    api_key_env: str | None = None
    lint_command: str | None = None
    # Repo map off by default: stages declare the files they need, and an
    # unscoped map swamps a local model's context before the task is stated.
    map_tokens: int = 0
    # Aider's flag surface changes between releases. This is the escape hatch
    # for correcting it without waiting on a code change.
    extra_args: list[str] = []


class ReviewerConfig(_Strict):
    # One implementation exists. Typos like "antropic" must not silently pass
    # through to a runtime failure on stage 1.
    provider: Literal["openai"] = "openai"
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    api_base: str | None = None
    request_timeout_seconds: float = 600.0
    max_retries: int = 2


class Limits(_Strict):
    max_test_retries: int = 3
    max_rework_retries: int = 2
    aider_timeout_seconds: int = 1800
    command_timeout_seconds: int = 3600


class Stage(_Strict):
    id: str
    kind: StageKind = "agent"

    instruction: str | None = None  # agent stages
    command: str | None = None  # script stages
    human_steps: str | None = None  # manual stages

    preconditions: list[str] = []
    context_commands: list[str] = []
    setup_command: str | None = None

    edit_files: list[str] = []
    read_files: list[str] = []

    forbidden_patterns: list[str] = []
    constraints: str | None = None
    acceptance: str | None = None

    test_command: str | None = None
    checks: list[str] = []
    require_new_tests: bool = False
    review: bool | None = None

    @property
    def reviews_enabled(self) -> bool:
        """Manual stages default to unreviewed — a human already owns the
        change — but may opt in."""
        if self.review is not None:
            return self.review
        return self.kind != "manual"

    @property
    def scope_guarded(self) -> bool:
        """Manual stages are exempt: a human bump legitimately touches
        whatever the change requires."""
        return self.kind != "manual"

    def effective_test_command(self, cfg: RunConfig) -> str | None:
        return self.test_command if self.test_command else cfg.test_command

    def effective_setup_command(self, cfg: RunConfig) -> str | None:
        return self.setup_command if self.setup_command else cfg.setup_command


class RunConfig(_Strict):
    target_repo: Path
    base_ref: str = "main"
    branch: str

    setup_command: str | None = None
    test_command: str | None = None
    full_test_command: str | None = None

    reference_docs: list[str] = []

    # What counts as a test file for `require_new_tests`. Defaults cover the
    # common conventions; override for a project that names them otherwise.
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
    reviewer: ReviewerConfig
    limits: Limits = Limits()

    rework_strategy: Literal["fresh", "continue"] = "fresh"
    rework_reset: bool = True

    stages: list[Stage]

    def reference_doc_paths(self) -> list[Path]:
        """Relative paths resolve against the target repo, since that is
        usually where a project's plan documents live."""
        out = []
        for ref in self.reference_docs:
            p = Path(ref)
            out.append(p if p.is_absolute() else self.target_repo / p)
        return out

    def stage_by_id(self, stage_id: str) -> Stage | None:
        return next((s for s in self.stages if s.id == stage_id), None)


def parse_config(data: dict) -> RunConfig:
    """Build a RunConfig from a plain dict, then apply the cross-field rules
    pydantic cannot express."""
    if not isinstance(data, dict):
        raise ConfigError(["config must be a YAML mapping"])

    try:
        cfg = RunConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_pydantic_errors(e)) from e

    problems = _structural_problems(cfg)
    if problems:
        raise ConfigError(problems)
    return cfg


def load_config(path: Path | str) -> RunConfig:
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


def _format_pydantic_errors(e: ValidationError) -> list[str]:
    problems = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        msg = err["msg"]
        # Surface the offending key name for unknown-field errors, since
        # pydantic puts it in loc rather than the message.
        problems.append(f"{loc}: {msg}")
    return problems


def _structural_problems(cfg: RunConfig) -> list[str]:
    problems: list[str] = []

    if cfg.branch == cfg.base_ref:
        problems.append(
            f"branch {cfg.branch!r} must differ from base_ref {cfg.base_ref!r}: "
            "the orchestrator must never commit to the branch it cuts from"
        )

    if not cfg.stages:
        problems.append("stages: at least one stage is required")

    seen: set[str] = set()
    for stage in cfg.stages:
        where = f"stage {stage.id!r}"

        if stage.id in seen:
            problems.append(
                f"duplicate stage id {stage.id!r}: stage ids name log "
                "directories and must be unique"
            )
        seen.add(stage.id)

        if not _SAFE_ID.match(stage.id):
            problems.append(
                f"stage id {stage.id!r} is not path-safe: use letters, digits, "
                "dot, dash, underscore"
            )

        problems.extend(_kind_problems(stage, where))

        if stage.scope_guarded and not stage.edit_files:
            problems.append(
                f"{where}: {stage.kind} stages must declare edit_files — the "
                "scope guard is meaningless without it, and a stage that "
                "cannot name its files is too broad to be a stage"
            )

        if not stage.effective_test_command(cfg) and not stage.checks:
            problems.append(
                f"{where}: needs a test_command (its own or the global one) or "
                "at least one entry in checks — otherwise nothing verifies it"
            )

        for pattern in stage.forbidden_patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                problems.append(
                    f"{where}: forbidden_patterns entry {pattern!r} is not a "
                    f"valid regex: {e}"
                )

    for ref, path in zip(cfg.reference_docs, cfg.reference_doc_paths()):
        if not path.is_file():
            problems.append(f"reference_docs entry {ref!r} not found at {path}")

    return problems


# Fields that belong to exactly one stage kind. Presence on the wrong kind is
# a misunderstanding worth failing on rather than ignoring.
_KIND_FIELDS: dict[StageKind, str] = {
    "agent": "instruction",
    "script": "command",
    "manual": "human_steps",
}


def _kind_problems(stage: Stage, where: str) -> list[str]:
    problems = []
    required = _KIND_FIELDS[stage.kind]

    if not getattr(stage, required):
        problems.append(f"{where}: {stage.kind} stages require {required}")

    for kind, field in _KIND_FIELDS.items():
        if kind != stage.kind and getattr(stage, field):
            problems.append(
                f"{where}: {field} belongs to {kind} stages, but this stage is "
                f"kind {stage.kind!r}"
            )

    if stage.kind == "manual" and stage.context_commands:
        problems.append(
            f"{where}: context_commands feed an executor prompt; manual stages "
            "have no executor"
        )

    return problems
