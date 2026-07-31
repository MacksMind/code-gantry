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


class Deferral(BaseModel):
    """A plan step taken out of order, recorded so it cannot be forgotten.

    The planner may defer a step whose position in the plan is incidental.
    The hazard is that it says so once and then, fifty calls later, reports
    the project complete having quietly dropped the work. Structuring it
    means the orchestrator carries the memory instead of the model.
    """

    plan_step: str = Field(
        description="What in the plan is being skipped, quoted closely enough "
        "that a human can find it."
    )
    reason: str = Field(description="Why it cannot be done now.")
    blocked_on: str = Field(
        default="",
        description="What would unblock it — credentials, an environment, a "
        "human decision.",
    )
    safe_because: str = Field(
        default="",
        description="Why nothing already done or still to come depends on it. "
        "If you cannot say this, the order is required and you must keep it.",
    )
    resolved: bool = Field(
        default=False,
        description="Set true once the step has actually been done. Omitting a "
        "deferral does not clear it; only this does.",
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
    deferred: list[Deferral] = Field(
        default_factory=list,
        description=(
            "Plan steps you are skipping for now, and any you are marking "
            "resolved. These are carried for you between calls — you do not "
            "need to repeat one to keep it alive, and omitting one does not "
            "clear it."
        ),
    )


@dataclass
class PlannerUsage:
    # Total input tokens, cached ones included — normalised to match the
    # reviewer's OpenAI shape so one arithmetic works for both. See
    # `_extract_usage`; the providers do not agree on what these words mean.
    prompt_tokens: int = 0
    cached_tokens: int = 0
    # Billed above base rate, and worth seeing on its own: a run that writes the
    # prefix and never reads it back is more expensive than not caching at all.
    cache_write_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class PlannerOutcome:
    verdict: Verdict
    reasoning: str
    status_entry: str
    stage_fields: dict | None = None
    revision_mode: RevisionMode | None = None
    usage: PlannerUsage = field(default_factory=PlannerUsage)
    deferred: list[dict] = field(default_factory=list)
    # True when `blocked` is ours rather than the planner's, so the report does
    # not imply a judgement the model never made.
    failed: bool = False


class PlannerClient(Protocol):
    def plan(self, messages: list[dict]) -> PlannerOutcome: ...


# One corrective retry for a malformed answer, and no more. Two identical
# failures mean the problem is not transient.
_MALFORMED_RETRIES = 1


def _add_usage(a: PlannerUsage, b: PlannerUsage) -> PlannerUsage:
    """Bill every attempt. A discarded answer still cost tokens."""
    return PlannerUsage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
    )


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
        """One planner decision, with a single corrective retry.

        A malformed answer — the right verdict with a required field missing —
        is not a reason to end an unattended run. It happened on the first live
        run: `revise` arrived without a stage spec and the run escalated on the
        spot. The model is stochastic, this costs one extra call, and the
        alternative is a coin flip on surviving fourteen hours.

        Exactly one retry, and only for malformed output. A refusal is a
        decision and a truncation will recur; neither is worth paying twice for.
        """
        conversation = list(messages)
        billed = PlannerUsage()

        for remaining in (_MALFORMED_RETRIES, 0):
            terminal, parsed, usage = self._attempt(conversation)
            billed = _add_usage(billed, usage)

            if terminal is not None:
                terminal.usage = billed
                return terminal

            problem = _semantic_problem(parsed)
            if problem is None:
                return PlannerOutcome(
                    verdict=parsed.verdict,
                    reasoning=parsed.reasoning,
                    status_entry=parsed.status_entry,
                    stage_fields=parsed.stage.model_dump() if parsed.stage else None,
                    revision_mode=parsed.revision_mode,
                    deferred=[d.model_dump() for d in parsed.deferred],
                    usage=billed,
                    failed=False,
                )

            if not remaining:
                outcome = _blocked(problem)
                outcome.usage = billed
                return outcome

            # Appended, never prepended: the plan and repository layout at the
            # head of the conversation are the cacheable prefix, and rewriting
            # them to carry a correction would discard the cache to say
            # something that belongs at the end anyway.
            conversation = conversation + [
                {
                    "role": "user",
                    "content": (
                        f"Your previous response could not be used: {problem}.\n\n"
                        "Answer again, complete this time. Keep the same "
                        "judgement — this is a formatting correction, not an "
                        "invitation to reconsider."
                    ),
                }
            ]

        raise AssertionError("unreachable")  # pragma: no cover

    def _attempt(
        self, messages: list[dict]
    ) -> tuple[PlannerOutcome | None, PlannerResponse | None, PlannerUsage]:
        """One call. Returns (terminal outcome, parsed response, usage).

        A terminal outcome means stop: the transport failed, the model refused,
        or the answer was truncated. Otherwise `parsed` is what came back, still
        to be checked for contradictions the schema cannot express.
        """
        try:
            response = self._client.messages.parse(
                model=self.cfg.model,
                # Generous: thinking is on by default on current models and
                # counts against max_tokens along with the response, so a tight
                # budget truncates the verdict rather than the reasoning.
                max_tokens=16_000,
                output_config={"effort": "high"},
                system=_system_blocks(self.cfg.cache_ttl, self.cfg.guidance),
                messages=messages,
                output_format=PlannerResponse,
            )
        except Exception as e:  # noqa: BLE001 - any failure means "no plan"
            return _blocked(f"the planner call failed: {e}"), None, PlannerUsage()

        usage = _extract_usage(getattr(response, "usage", None))

        # A refusal or a truncation is not a plan. Check before reading output.
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            return _blocked("the planner refused to answer"), None, usage
        if stop_reason == "max_tokens":
            return (
                _blocked(
                    "the planner's response was truncated, so its verdict "
                    "cannot be trusted"
                ),
                None,
                usage,
            )

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            return _blocked("the planner returned no parsable verdict"), None, usage

        return None, parsed, usage


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
    """Normalise Anthropic's counts to the shape the report assumes.

    The two providers use the same words for different quantities. OpenAI's
    `prompt_tokens` is the total and its cached count is a subset of it.
    Anthropic reports three *disjoint* numbers: `input_tokens` is only what was
    neither read from nor written to the cache, with reads and writes counted
    separately.

    Read as OpenAI's shape, that produced `Uncached prompt tokens: -2,438` and
    a cache hit rate of 251% in a real report. So `prompt_tokens` here means
    total input, and `cached_tokens` is the part of it that was a cache read —
    which makes `prompt - cached` the uncached remainder for either provider.
    """
    if usage is None:
        return PlannerUsage()
    uncached = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return PlannerUsage(
        prompt_tokens=uncached + read + written,
        cached_tokens=read,
        cache_write_tokens=written,
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

- **One deployable increment.** It lands as a single squashed commit on the
  project branch, must pass its tests and the full suite, and must be
  independently shippable to production on its own — not merely a tidy commit,
  but a change the operator could deploy without waiting for the next stage.
  It must also make sense to a human reading the log later.
- **Small enough to land quickly.** If the same mechanical change applies to
  seventy files and each file could deploy on its own, that is closer to
  seventy stages than to one. Prefer many small stages over one large one
  wherever the increments are genuinely independent: a stage that runs for
  hours risks more, reverts worse, and tells you less when it fails. Group
  files into one stage only when they must ship together to keep the tree
  green.
- **Narrow scope.** `edit_files` is enforced: a diff touching anything outside
  it fails the stage. Include the tests that must change. Do not pad the globs
  to be safe — an over-broad stage defeats the guard that protects the run.
- **Self-contained instruction.** The executor cannot see the plan document,
  the other stages, or this conversation. Everything it needs goes in
  `instruction`.
- **Name the specs that cover it.** `test_paths` is how a stage's tests get
  scoped to the specs it affects. Files the stage edits are picked up
  automatically; this is for the ones that exercise the changed code *without*
  changing — which, on a behaviour-preserving refactor, is all of them. Leaving
  it empty on a stage that edits no spec means there is nothing to scope to and
  the whole suite runs instead, on every attempt and every retry. On a large
  project that is the single most expensive mistake you can make here.
- **Constraints as reject-criteria.** If the work is only valid under some
  condition — a platform version, an ordering requirement — say so in
  `constraints`. The reviewer enforces it. Where the condition can be written
  as a regex over added lines, put it in `forbidden_patterns` too; that is
  checked mechanically before anything is run and costs nothing.

## The order of the plan

The plan document is the authority on *what* must happen. It is not
necessarily the authority on *when*. You may take steps out of order, or defer
one and come back to it, when the plan's order is incidental rather than
required — a step needing credentials or access this run does not have is the
usual case, and deferring it is far better than stopping a run that could have
completed forty other stages.

Two obligations come with that latitude:

- **Only when it is safe.** If a later step depends on an earlier one — a
  migration before the code that reads the new column, a version bump before
  the API it enables — the order is required and you must keep it. When in
  doubt, keep the plan's order.
- **Never silently.** Say in `status_entry` that you deferred it and why, and
  repeat it in each subsequent entry until it is done or the run ends. When you
  return `project_complete`, list anything still deferred in `reasoning`.
  "Complete" must never quietly mean "complete except the parts I skipped".

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


def cache_control(ttl: str | None = None) -> dict:
    """The cache_control marker, with an optional longer lifetime.

    The default ephemeral window is about five minutes. A stage takes longer
    than that on any real project, so without a longer TTL the prefix expires
    between planner calls and the marker buys nothing.
    """
    marker = {"type": "ephemeral"}
    if ttl:
        marker["ttl"] = ttl
    return marker


def _system_blocks(
    cache_ttl: str | None = None, guidance: str | None = None
) -> list[dict]:
    """The system prompt as a cacheable block.

    It never changes across a run, so it belongs in the cached prefix along
    with the plan snapshot and completed history — and so does the operator's
    per-project guidance, which is fixed for the run too.

    Guidance is appended rather than interleaved, and labelled as the
    operator's, so the planner can tell project policy from the standing
    contract and weigh a conflict knowingly instead of silently. It cannot
    weaken the safety partition whatever it says: there is no field in the
    response schema for a command, and the allowlist filters the result
    regardless.
    """
    text = PLANNER_SYSTEM_PROMPT
    if guidance and guidance.strip():
        text += (
            "\n\n## Guidance for this project\n\n"
            "Written by the operator for this project specifically. It refines "
            "everything above; where it genuinely conflicts, say so in "
            "`reasoning` rather than choosing in silence.\n\n"
            + guidance.strip()
        )
    return [
        {
            "type": "text",
            "text": text,
            "cache_control": cache_control(cache_ttl),
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
