"""The planner client.

The planner derives the next stage from the plan document, revises a stage that
turned out to be wrongly drawn, and decides when the project is done. It is the
component that makes unattended operation possible: most failures route here
rather than to a human.

**It may write declarative fields and only declarative fields.** That is
enforced twice — once by the structured-output schema, which has no field for a
command, and again by `ProjectConfig.stage_from_planner`, which filters the
response against an allowlist. Belt and braces, because this is the invariant
the whole design rests on: a model that could author shell would make
"unattended" mean something very different.

`kind` is deliberately *not* planner-writable. A `script` stage needs an
operator-authored `command`, and there is no static stage list for the operator
to put one in — so every planner-derived stage is an `agent` stage. Mechanical
transforms across hundreds of files are expressed as an agent stage whose
instruction says to write and run a script: Aider doing that inside its own
edit loop is Aider's business, and the orchestrator still never executes
model-authored shell itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from orchestrator.config import PlannerConfig

Verdict = Literal["next_stage", "revise", "project_complete", "blocked"]
RevisionMode = Literal["extend", "restart"]


class PlannedStage(BaseModel):
    """A stage spec, restricted to declarative fields.

    There is no `command`, no `checks`, no `preconditions`, no `test_command`.
    Those are unrepresentable rather than merely discouraged — the planner
    cannot return what the schema has no room for.
    """

    id: str = Field(
        description=(
            "Short kebab-case identifier, unique within the project. Becomes a "
            "git branch name and a log directory, so letters, digits, dot, "
            "dash and underscore only."
        )
    )
    instruction: str = Field(
        description=(
            "What the executor must do, in full. It has no memory of previous "
            "stages and cannot see the plan document, so this must stand alone."
        )
    )
    edit_files: list[str] = Field(
        description=(
            "Globs the executor may edit. Enforced: a diff touching anything "
            "outside these fails the stage. Include the tests that will need "
            "to change. Be as narrow as the work allows."
        )
    )
    read_files: list[str] = Field(
        default_factory=list,
        description=(
            "Globs the executor may read for context but not edit — base "
            "classes, route tables, configuration it must respect."
        ),
    )
    constraints: str = Field(
        default="",
        description=(
            "Invariants the reviewer will enforce as reject-criteria. This is "
            "where a version boundary goes: 'must remain valid on X; reject "
            "any API introduced later'."
        ),
    )
    acceptance: str = Field(
        default="",
        description=(
            "What done means, for work that creates new behaviour rather than "
            "preserving existing behaviour."
        ),
    )
    forbidden_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Regexes barred from the diff's added lines, checked mechanically "
            "before any test runs. Use for later-stage syntax that must not "
            "appear yet."
        ),
    )
    test_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Test files you expect this stage to affect beyond those it edits "
            "— specs that exercise the changed code without being changed "
            "themselves. Paths only; the operator owns the test command."
        ),
    )


class PlannerResponse(BaseModel):
    verdict: Verdict = Field(
        description=(
            "next_stage: a new stage. revise: a corrected spec for the stage "
            "that just failed. project_complete: the plan is executed. "
            "blocked: a human is required."
        )
    )
    reasoning: str = Field(description="Why. Recorded in status.md.")
    status_entry: str = Field(
        description=(
            "One entry for the append-only log: goal, expected, actual, "
            "divergence, next goal."
        )
    )
    stage: PlannedStage | None = Field(
        default=None, description="Required for next_stage and revise."
    )
    revision_mode: RevisionMode | None = Field(
        default=None,
        description=(
            "Required for revise. extend: the existing branch's work is still "
            "correct and merely incomplete — keep it. restart: the approach "
            "was wrong — discard the branch and re-cut."
        ),
    )


@dataclass
class PlannerUsage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class PlannerOutcome:
    verdict: Verdict
    reasoning: str
    status_entry: str
    stage_fields: dict | None = None
    revision_mode: RevisionMode | None = None
    usage: PlannerUsage = field(default_factory=PlannerUsage)
    # True when `blocked` is ours rather than the planner's, so the report does
    # not imply a judgement the model never made.
    failed: bool = False


class PlannerClient(Protocol):
    def plan(self, messages: list[dict]) -> PlannerOutcome: ...


def _blocked(reason: str) -> PlannerOutcome:
    return PlannerOutcome(
        verdict="blocked",
        reasoning=reason,
        status_entry=f"Planner unavailable: {reason}",
        failed=True,
    )


class AnthropicPlanner:
    def __init__(self, cfg: PlannerConfig, client=None):
        self.cfg = cfg
        self._client = client if client is not None else _build_anthropic_client(cfg)

    def plan(self, messages: list[dict]) -> PlannerOutcome:
        try:
            response = self._client.messages.parse(
                model=self.cfg.model,
                # Generous: thinking is on by default on current models and
                # counts against max_tokens along with the response, so a tight
                # budget truncates the verdict rather than the reasoning.
                max_tokens=16_000,
                output_config={"effort": "high"},
                system=_system_blocks(),
                messages=messages,
                output_format=PlannerResponse,
            )
        except Exception as e:  # noqa: BLE001 - any failure means "no plan"
            return _blocked(f"the planner call failed: {e}")

        usage = _extract_usage(getattr(response, "usage", None))

        # A refusal or a truncation is not a plan. Check before reading output.
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            outcome = _blocked("the planner refused to answer")
            outcome.usage = usage
            return outcome
        if stop_reason == "max_tokens":
            outcome = _blocked(
                "the planner's response was truncated, so its verdict cannot "
                "be trusted"
            )
            outcome.usage = usage
            return outcome

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            outcome = _blocked("the planner returned no parsable verdict")
            outcome.usage = usage
            return outcome

        problem = _semantic_problem(parsed)
        if problem:
            outcome = _blocked(problem)
            outcome.usage = usage
            return outcome

        return PlannerOutcome(
            verdict=parsed.verdict,
            reasoning=parsed.reasoning,
            status_entry=parsed.status_entry,
            stage_fields=parsed.stage.model_dump() if parsed.stage else None,
            revision_mode=parsed.revision_mode,
            usage=usage,
            failed=False,
        )


def _semantic_problem(parsed: PlannerResponse) -> str | None:
    """Contradictions the schema cannot express."""
    if parsed.verdict in ("next_stage", "revise") and parsed.stage is None:
        return f"the planner returned {parsed.verdict!r} without a stage spec"
    if parsed.verdict == "revise" and parsed.revision_mode is None:
        return (
            "the planner returned 'revise' without a revision_mode, so there "
            "is no way to know whether the existing branch should be kept"
        )
    return None


def make_planner(cfg: PlannerConfig) -> PlannerClient:
    if cfg.provider == "anthropic":
        return AnthropicPlanner(cfg)
    raise RuntimeError(f"unsupported planner provider {cfg.provider!r}")


def _build_anthropic_client(cfg: PlannerConfig):
    import anthropic

    if cfg.api_key_env not in os.environ:
        raise RuntimeError(
            f"planner.api_key_env names {cfg.api_key_env}, which is not set in "
            "the environment"
        )
    return anthropic.Anthropic(
        api_key=os.environ[cfg.api_key_env],
        base_url=cfg.resolve_api_base(),
        timeout=cfg.request_timeout_seconds,
        max_retries=cfg.max_retries,
    )


def _extract_usage(usage) -> PlannerUsage:
    if usage is None:
        return PlannerUsage()
    return PlannerUsage(
        prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
        cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        completion_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


PLANNER_SYSTEM_PROMPT = """\
You are the planner in an unattended refactoring loop. A local model makes the
edits; a reviewer inspects each finished stage; you decide what the next stage
should be, and whether the last one was drawn correctly.

The run is expected to proceed for hours without a human. Your job is to keep
it moving: when a stage fails, the usual answer is a better-drawn stage, not an
escalation. Reserve `blocked` for something a human genuinely must resolve —
a contradiction in the plan, a missing prerequisite you cannot express as a
stage, an environment problem.

## What a good stage looks like

- **One shippable unit.** It lands as a single squashed commit on the project
  branch, must pass its tests and the full suite, and must make sense to a
  human reading the log later.
- **Narrow scope.** `edit_files` is enforced: a diff touching anything outside
  it fails the stage. Include the tests that must change. Do not pad the globs
  to be safe — an over-broad stage defeats the guard that protects the run.
- **Self-contained instruction.** The executor cannot see the plan document,
  the other stages, or this conversation. Everything it needs goes in
  `instruction`.
- **Constraints as reject-criteria.** If the work is only valid under some
  condition — a platform version, an ordering requirement — say so in
  `constraints`. The reviewer enforces it. Where the condition can be written
  as a regex over added lines, put it in `forbidden_patterns` too; that is
  checked mechanically before anything is run and costs nothing.

## Mechanical work

For a transform across many files, still write an `agent` stage — instruct the
executor to write and run a script rather than editing by hand. You cannot
author commands yourself, and you should not ask for hundreds of hand edits.

## When a stage fails

You are told which gate failed and why. Choose deliberately:

- **Scope violation.** The executor touched files outside its box. Either the
  stage was drawn too narrowly and those files belong in it — widen
  `edit_files` and `revise` with `revision_mode: "extend"`, and the existing
  work stands — or they do not belong, in which case leave them out and they
  will be reverted while the rest of the stage's work is kept.
- **Tests or checks kept failing.** Decide whether the stage asked for too
  much at once, or whether a prerequisite was skipped. Splitting it, or
  inserting a predecessor stage via `next_stage`, is usually better than
  restating the same instruction.
- **The reviewer blocked it.** The instruction itself was wrong. Revise it.

`revision_mode` matters. `extend` keeps the child branch, so partial work
survives — right when scope was merely too narrow. `restart` discards it —
right when the approach was wrong.

## Limits

You may not author any command, test invocation, or check; those fields do not
exist in your response schema and are supplied by the operator. You may not
overrule the reviewer on a stage it has already approved — your authority is
forward-looking only.

Write `status_entry` for a human reading the run afterwards: what you expected,
what actually happened, and where that leaves the plan.\
"""


def _system_blocks() -> list[dict]:
    """The system prompt as a cacheable block.

    It never changes across a run, so it belongs in the cached prefix along
    with the plan snapshot and completed history.
    """
    return [
        {
            "type": "text",
            "text": PLANNER_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


# --- status.md -----------------------------------------------------------

STATUS_FILENAME = "status.md"


def append_status(
    project_dir: Path | str,
    stage_index: int,
    stage_id: str | None,
    revision: int,
    verdict: str,
    entry: str,
    reasoning: str,
    now: str | None = None,
) -> Path:
    """Append one entry to the project's expected-vs-actual log.

    Append-only, deliberately. This is never rewritten as a status page: the
    divergence over time between what the plan expected and what actually
    happened is the whole value, and a page that always shows the current
    state throws exactly that away.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / STATUS_FILENAME

    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    header = f"## {stamp} — stage {stage_index}"
    if stage_id:
        header += f" `{stage_id}`"
    if revision:
        header += f" (revision {revision})"
    header += f" — {verdict}"

    block = f"\n{header}\n\n{entry.strip()}\n\n**Why:** {reasoning.strip()}\n"

    if not path.exists():
        path.write_text("# Expected vs actual\n\nAppend-only. One entry per planner decision.\n")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(block)
    return path
