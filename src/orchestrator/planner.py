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
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, Field

from orchestrator.config import PlannerConfig
from orchestrator.retry import Backoff, with_transport_retry
from orchestrator.plannertools import dispatch, tool_schemas

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
            "stages and cannot see the plan document, so this must stand "
            "alone.\n\n"
            "**Quote code in fenced blocks, never indented ones.** The "
            "executor reads this as raw text, not rendered Markdown, so the "
            "four spaces that make an indented block are indistinguishable "
            "from four spaces of source. Asked to match a line exactly, it "
            "matches what it was shown — including your formatting — and the "
            "edit silently fails to apply.\n\n"
            "Observed: a stage quoting two lines of a model file as an "
            "indented block presented them at six spaces where the file has "
            "two. Four attempts produced no edit at all, the stage exhausted "
            "its budget without a single diff reaching review, and the "
            "instruction had said 'keep the run of spaces exactly as shown'. "
            "A fenced block would have shown the file's own bytes.\n\n"
            "This matters most for the code you want matched character for "
            "character, which is exactly the code most likely to be indented "
            "for readability."
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
            "Regexes barred from the diff's **added lines**, checked "
            "mechanically before any test runs. Use for what must not be "
            "*introduced*: later-stage syntax, an API that does not exist yet, "
            "a shortcut this stage is meant to avoid.\n\n"
            "This cannot tell you that something is gone. A line the executor "
            "never touched is not an added line, so an occurrence it simply "
            "missed matches nothing here. For 'none may remain', use "
            "must_not_remain — the two read almost identically in prose and "
            "are opposites in a diff."
        ),
    )
    must_not_remain: list[str] = Field(
        default_factory=list,
        description=(
            "Regexes that must not survive anywhere in edit_files once the "
            "stage is done, checked by reading the files rather than the diff. "
            "This is how a sweep states its own goal: converting every "
            "`render text:` in a file means declaring `render\\s+text:` here, "
            "and the stage cannot pass while one is left.\n\n"
            "Free, deterministic, and it runs before the tests — so an "
            "incomplete conversion costs nothing to find instead of a review "
            "turn. Scoped to edit_files, so declare the ground you mean to "
            "leave clean. Leave empty when the stage is not removing anything."
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
    require_new_tests: bool = Field(
        default=False,
        description=(
            "Set true to make this stage fail unless its diff adds or changes "
            "a test file. Naming a spec in `edit_files` only permits one; this "
            "is what requires it.\n\n"
            "Use it when you are fixing something the suite did not catch. A "
            "regression that reached the branch proves no test asserts the "
            "behaviour, so a fix without one leaves the same gap open and the "
            "same mistake shippable. Use it too when a stage adds behaviour "
            "rather than preserving it.\n\n"
            "Do not set it for a pure mechanical sweep whose existing specs "
            "already cover the behaviour — there the requirement only invites "
            "a spec written to be written.\n\n"
            "You can raise this requirement but never waive it: if the "
            "operator requires tests on every stage, false here changes "
            "nothing."
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


class PlanNote(BaseModel):
    """How the plan learns what is done.

    A plan document lists work. Nothing in it knows which of that work has
    happened — and a run starts with an empty history, so without a record the
    next one re-derives a stage that already landed. This is the record. Each
    note says what a plan item looks like now that this stage has landed, and
    is written into that stage's own commit.

    So the usual note is progress: this sweep is complete, this count is down
    to seven, this item can be closed. A correction — the plan was wrong when
    written — is the same mechanism pointed at a different cause, and belongs
    here too. Both answer one question: what does the plan not yet know?

    Append-only, and nothing here rewrites a plan. A later pass folds these
    into the documents and closes the items they report, which is a judgement
    about what the work has become and does not belong mid-run.
    """

    plan_path: str = Field(
        description="Which plan document this is about, as a repo-relative "
        "path. One of the documents shown to you above."
    )
    anchor: str = Field(
        description=(
            "A short exact quote from that document — the sentence, bullet or "
            "table row this note is about. Copy it, do not paraphrase it: it "
            "is matched against the document to work out which lines you mean, "
            "and the line numbers in the entry are derived from where it is "
            "found.\n\n"
            "So do not give line numbers yourself. The documents are shown to "
            "you as prose and counting their lines is not something you can do "
            "reliably — three attempts at it each named a real file, a real "
            "span, and the wrong passage. Quoting is the part you are good at.\n\n"
            "One or two sentences is plenty. Long enough to appear once in the "
            "document rather than anywhere, short enough to copy exactly."
        )
    )
    observation: str = Field(
        description=(
            "A concise summary of what changed and where that leaves the plan "
            "step. Two or three sentences. This is written into the stage's "
            "own commit, so the diff is already there — do not re-describe the "
            "edit line by line or cite what you read to find it. Say what it "
            "means: 'the sweep is complete, no sites remain in app/controllers' "
            "or 'seven of the twenty-four remain, all inline <script> renders "
            "needing a different treatment'.\n\n"
            "State the resulting total, never a change. Write '7 sites remain "
            "in 1 controller', not '17 converted'. These accumulate and are "
            "folded into the plan in batches, so a later note has to override "
            "an earlier one by simply being later — and a total does that "
            "while a delta compounds. Folding the same delta twice would "
            "decrement the plan twice."
        )
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
    plan_notes: list[PlanNote] = Field(
        default_factory=list,
        description=(
            "How the plan learns what has been done. Appended to the progress "
            "log; nothing you write here changes a plan document, and a later "
            "pass folds these in and closes the items they report.\n\n"
            "**Anything you conclude about the state of a plan step goes here, "
            "not in your reasoning.** Reasoning is read by a human reviewing "
            "this one call; only these notes are written down and survive to "
            "the next run. A finding you explain in prose and omit here is a "
            "finding nobody will ever act on.\n\n"
            "One note per plan step whose state the plan does not yet reflect. "
            "Usually that is progress — work is done, a count has moved — "
            "whether it was done by the stage you are deriving now or by "
            "earlier work you are being asked to catch up on. It equally "
            "carries a correction: the plan was wrong when written, a file no "
            "longer exists. All of them answer one question: what does the "
            "plan not yet know?\n\n"
            "This matters because a run begins with no history. Without a "
            "note, the next run reads the same plan, cannot tell the work "
            "happened, and derives it again. Grepping only rescues that where "
            "doneness is visible in the code; an audit, a verification or a "
            "decision leaves no trace to find.\n\n"
            "Leave empty only when there is genuinely nothing the plan does "
            "not already know. Do not restate what an earlier note recorded."
        ),
    )


@dataclass
class PlannerUsage:
    # Total input tokens, cached ones included — normalised to match the
    # reviewer's OpenAI shape so one arithmetic works for both. See
    # `_extract_usage`; the providers do not agree on what these words mean.
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    # Last, so positional construction keeps working — the same ordering as the
    # reviewer's TokenUsage. Billed above base rate, and worth seeing on its
    # own: a prefix written every call and never read back costs more than no
    # caching at all.
    cache_write_tokens: int = 0


@dataclass
class PlannerOutcome:
    verdict: Verdict
    reasoning: str
    status_entry: str
    stage_fields: dict | None = None
    revision_mode: RevisionMode | None = None
    usage: PlannerUsage = field(default_factory=PlannerUsage)
    deferred: list[dict] = field(default_factory=list)
    # What the planner looked at to reach this. Once it chooses its own inputs,
    # this is the only way to explain a stage afterwards — half the debugging on
    # the first long run was reconstructing what it had been told, and that was
    # when the inputs were fixed.
    tool_calls: list[str] = field(default_factory=list)
    # Observations about the plan going stale, appended to the addendum when
    # the stage lands. Carried rather than written here: a note about work that
    # then fails review would be a record of something that did not happen.
    plan_notes: list[dict] = field(default_factory=list)
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


def _transport_errors() -> tuple[type[BaseException], ...]:
    """The exception types that mean "the request never arrived".

    Resolved lazily and defensively: the SDK is imported lazily everywhere
    else in this module, and a version that renamed these should degrade to
    not retrying rather than to not running.

    `APITimeoutError` subclasses `APIConnectionError` in both SDKs, so the
    one entry covers both.
    """
    try:
        from anthropic import APIConnectionError
    except ImportError:  # pragma: no cover - the SDK is a hard dependency
        return ()
    return (APIConnectionError,)


class AnthropicPlanner:
    def __init__(
        self, cfg: PlannerConfig, client=None, reader=None, semantic=None, log=None
    ):
        self.cfg = cfg
        # Set by `build_runtime`, which is where the run log becomes
        # available — the client is constructed before it exists. Waiting
        # out an outage silently is the failure this exists to fix, so a
        # missing log is a degradation, not a detail.
        self.log = log
        # Also set by `build_runtime`. Takes the raw stage fields and returns
        # the reasons the caller cannot use them, which is knowledge this
        # module deliberately does not have: `forbidden_patterns` is a list of
        # strings to the schema, and every invalid regex is a valid string.
        # Injected rather than imported so project rules stay in `config.py`.
        self.validate_stage_fields: Callable[[dict], list[str]] | None = None
        self._client = client if client is not None else _build_anthropic_client(cfg)
        # Absent on a project with no repository access configured, in which
        # case no tools are offered and this is the single-call planner it has
        # always been.
        self.reader = reader
        self.semantic = semantic

    def _tool_log(self) -> list[str]:
        """What was looked at, for the run log.

        Rendered here rather than in the node so the node never has to know
        that two different objects record calls.
        """
        calls = list(getattr(self.reader, "calls", []))
        return [f"{c.tool}({c.detail}) -> {c.lines} line(s)" for c in calls]

    def _max_tool_turns(self) -> int:
        """Backstop on the conversation length.

        The reader's own call budget is the real limit — it refuses past that
        and the planner reads the refusal. This catches a model that ignores
        the refusal and keeps asking, which would otherwise loop until the
        request timeout.
        """
        if self.reader is None:
            return 0
        return self.reader.budget.max_calls + 2

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
        # Reset per decision, not per run: the log line answers "what did it
        # look at to draw *this* stage".
        if self.reader is not None:
            self.reader.calls.clear()
            self.reader._lines_used = 0
        if self.semantic is not None:
            self.semantic.calls.clear()

        for remaining in (_MALFORMED_RETRIES, 0):
            terminal, parsed, usage = self._attempt(conversation)
            billed = _add_usage(billed, usage)

            if terminal is not None:
                terminal.usage = billed
                terminal.tool_calls = self._tool_log()
                return terminal

            problem = _semantic_problem(parsed) or self._unusable(parsed)
            if problem is None:
                return PlannerOutcome(
                    verdict=parsed.verdict,
                    reasoning=parsed.reasoning,
                    status_entry=parsed.status_entry,
                    stage_fields=parsed.stage.model_dump() if parsed.stage else None,
                    revision_mode=parsed.revision_mode,
                    deferred=[d.model_dump() for d in parsed.deferred],
                    plan_notes=[n.model_dump() for n in parsed.plan_notes],
                    usage=billed,
                    tool_calls=self._tool_log(),
                    failed=False,
                )

            if not remaining:
                outcome = _blocked(problem)
                outcome.usage = billed
                outcome.tool_calls = self._tool_log()
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

    def _unusable(self, parsed: PlannerResponse) -> str | None:
        """Why the caller cannot use this stage, if it cannot.

        Every problem, not the first: fixing one and being told about the next
        costs another whole round trip, and the planner has the stage in front
        of it either way.
        """
        if self.validate_stage_fields is None or parsed.stage is None:
            return None
        problems = self.validate_stage_fields(parsed.stage.model_dump())
        if not problems:
            return None
        return "; ".join(problems)

    def _attempt(
        self, messages: list[dict]
    ) -> tuple[PlannerOutcome | None, PlannerResponse | None, PlannerUsage]:
        """One call. Returns (terminal outcome, parsed response, usage).

        A terminal outcome means stop: the transport failed, the model refused,
        or the answer was truncated. Otherwise `parsed` is what came back, still
        to be checked for contradictions the schema cannot express.
        """
        tools = tool_schemas(self.semantic) if self.reader else []
        conversation = list(messages)
        usage = PlannerUsage()
        response = None

        # One turn per tool round trip, plus one for the answer. The ceiling is
        # the reader's own call budget: it refuses past that, the planner reads
        # the refusal and answers. This bound is the backstop for a model that
        # ignores the refusal and keeps asking.
        for _ in range(self._max_tool_turns() + 1):
            try:
                response = with_transport_retry(
                    lambda: self._client.messages.parse(
                        model=self.cfg.model,
                        # Generous: thinking is on by default on current models and
                        # counts against max_tokens along with the response, so a
                        # tight budget truncates the verdict rather than the
                        # reasoning.
                        max_tokens=16_000,
                        output_config={"effort": "high"},
                        system=_system_blocks(self.cfg.cache_ttl, self.cfg.guidance),
                        messages=_with_loop_breakpoint(conversation),
                        output_format=PlannerResponse,
                        **({"tools": tools} if tools else {}),
                    ),
                    retry_on=_transport_errors(),
                    backoff=Backoff(
                        budget_seconds=self.cfg.transport_retry_seconds,
                        max_delay_seconds=self.cfg.transport_retry_max_delay_seconds,
                    ),
                    log=self.log,
                )
            except Exception as e:  # noqa: BLE001 - any failure means "no plan"
                return _blocked(f"the planner call failed: {e}"), None, usage

            usage = _merge_usage(usage, _extract_usage(getattr(response, "usage", None)))

            requests = _tool_requests(response)
            if not requests:
                break

            # Assistant turn verbatim, then one result block per request. The
            # API requires every tool_use to be answered in the next message, in
            # order, or the conversation is malformed.
            conversation = conversation + [
                {"role": "assistant", "content": _content_blocks(response)},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": req["id"],
                            "content": dispatch(
                                req["name"], req["input"], self.reader, self.semantic
                            ),
                        }
                        for req in requests
                    ],
                },
            ]

        if response is None:  # pragma: no cover - loop always runs once
            return _blocked("the planner produced no response"), None, usage

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


def _tool_requests(response) -> list[dict]:
    """The tool_use blocks in a response, normalised.

    Tolerant of shape because this reads an SDK object in one place and a stub
    in another; anything that is not a well-formed tool_use is ignored rather
    than crashing a fourteen-hour run on an attribute error.
    """
    out = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        out.append(
            {
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", None) or {},
            }
        )
    return out


def _content_blocks(response) -> list[dict]:
    """The assistant turn, as blocks the API will accept back.

    Replayed verbatim: a tool_use must be echoed in the conversation for its
    result to be attachable to it.
    """
    blocks = []
    for block in getattr(response, "content", None) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            blocks.append({"type": "text", "text": getattr(block, "text", "")})
        elif kind == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", None) or {},
                }
            )
        elif kind == "thinking":
            # Carried so the model keeps its own reasoning across turns.
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                }
            )
    return blocks


def _merge_usage(a: PlannerUsage, b: PlannerUsage) -> PlannerUsage:
    """Add up the turns of one planning step.

    A tool loop bills per turn. Reporting only the last one would make a
    conversation that read six files look like a single cheap call.
    """
    return PlannerUsage(
        prompt_tokens=a.prompt_tokens + b.prompt_tokens,
        completion_tokens=a.completion_tokens + b.completion_tokens,
        cached_tokens=a.cached_tokens + b.cached_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
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


def make_planner(cfg: PlannerConfig, target_repo=None) -> PlannerClient:
    """Build the planner, with repository access when the project enables it.

    `target_repo` is optional so preflight can build a client just to prove the
    credentials work, without needing a repo on hand.
    """
    if cfg.provider != "anthropic":
        raise RuntimeError(f"unsupported planner provider {cfg.provider!r}")

    reader = semantic = None
    if cfg.repo_access and target_repo is not None:
        from orchestrator.gitops import Git
        from orchestrator.repotools import ReadBudget, RepoReader
        from orchestrator.semantic import SemanticSearch, SemanticSearchConfig

        reader = RepoReader(
            Git(target_repo),
            target_repo,
            ReadBudget(
                max_lines_per_call=cfg.max_read_lines_per_call,
                max_total_lines=cfg.max_read_lines_total,
                max_calls=cfg.max_read_calls,
            ),
        )
        search_cfg = SemanticSearchConfig.from_mapping(cfg.semantic_search)
        if search_cfg is not None:
            # One list, shared, so the log is chronological. Two lists
            # concatenated said what was looked at but not in what order, and
            # the order is most of how a conclusion was reached — a read that
            # confirmed a semantic hit is a different act from one that
            # preceded it.
            semantic = SemanticSearch(search_cfg, calls=reader.calls)

    return AnthropicPlanner(cfg, reader=reader, semantic=semantic)


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

  This one is a default rather than a law, and it is a statement about the
  executor rather than about the work. It is tuned to a local model with
  modest headroom, where a multi-file sweep deadlocked twice and the same work
  redrawn one file per stage landed first time. An operator whose executor has
  far more room should say so in this project's guidance, which refines
  everything here — under this default they would pay a planner call, a review
  and a full-suite run per file for stages the executor could do whole.
- **Narrow scope.** `edit_files` is enforced: a diff touching anything outside
  it fails the stage. Include the tests that must change. Do not pad the globs
  to be safe — an over-broad stage defeats the guard that protects the run.
- **Self-contained instruction.** The executor cannot see the plan document,
  the other stages, or this conversation. Everything it needs goes in
  `instruction`.
- **The executor cannot run commands.** It reads the files you name and edits
  the files you allow. It cannot run `grep`, or a test, or anything else, and
  it cannot see the result of one. Do not write "find the sites with
  `grep -n ...`", or "when done, re-run the grep and confirm" — it cannot, and
  asking is worse than useless: it will invent the output and argue with itself
  about a file it is already looking at. One such instruction cost ten minutes
  of a model looping over hallucinated grep results.

  Anything you want checked mechanically goes to the orchestrator as a regex,
  deterministic and free, and there are two of them because they answer
  opposite questions. `forbidden_patterns` reads the diff's added lines and
  catches what must not be *introduced*. `must_not_remain` reads the files in
  `edit_files` and catches what must not be *left*. A sweep — "convert every X"
  — needs the second: the sites the executor misses are untouched, so they
  never appear as added lines and the first is blind to them. Getting this
  backwards means an incomplete conversion passes every mechanical gate and is
  caught, if at all, by a paid review turn.

  Describe the *change* to the executor; declare the *check* to the
  orchestrator.
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


def _with_loop_breakpoint(conversation: list[dict]) -> list[dict]:
    """Mark the end of the newest message, so the tool loop caches by increment.

    The fourth and last breakpoint the API allows. Three are static — the
    system prompt, the plan snapshot, the completed history — and everything
    after them was uncached on every turn: the volatile tail, each assistant
    turn, each tool result. A derivation runs ten to twenty-five turns and
    resends all of it each time, so cost grew with the square of the turn
    count. Measured across 125 decisions of one run, a decision making no tool
    calls spent 48k uncached input tokens and one making sixteen or more spent
    1.4M.

    It moves rather than accumulates. Marking each turn's message and leaving
    the mark would pass four breakpoints by the fifth turn and the request
    would be rejected — so this is computed fresh from an unmarked
    conversation and applied to the outgoing copy only. The loop's own
    accumulated conversation never carries a marker, which also keeps the
    corrective retry appending to an unmutated prefix.

    Deliberately the 5-minute default rather than the run's configured `1h`.
    Turns inside a decision are seconds apart — nineteen tool calls in five
    minutes, observed — so the short window suffices, and its writes cost
    1.25x against 2x. The static prefix keeps the long lifetime because that
    is what has to survive a whole stage between decisions.
    """
    if not conversation:
        return conversation
    last = conversation[-1]
    content = last.get("content")
    if not isinstance(content, list) or not content:
        return conversation
    if not isinstance(content[-1], dict):
        return conversation
    blocks = list(content)
    blocks[-1] = {**blocks[-1], "cache_control": cache_control()}
    return conversation[:-1] + [{**last, "content": blocks}]


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


# Its own file, not a section of status.md. status.md is a narrative log of
# expected-versus-actual that runs to hundreds of kilobytes, and the planner
# sees only a tail of it — a cost line appended there scrolls out of view
# within a stage or two, and scanning the whole thing to find one is work that
# grows with the project. This file holds one line per landed stage and nothing
# else, so it stays small enough to read whole however long the project runs.
STAGE_COSTS_FILENAME = "stage-costs.md"
STAGE_COST_PREFIX = "- cost "
_STAGE_COST = re.compile(
    r"^- cost `([0-9a-f]+)` `([^`]*)` — (\d+) file\(s\), ([\d,]+) executor tokens",
    re.MULTILINE,
)


def append_stage_cost(
    project_dir: Path | str,
    stage_id: str,
    merge_sha: str,
    files: int,
    context_tokens: int,
) -> Path:
    """Record what a landed stage cost the executor, durably.

    The figure itself lives on `StageResult`, which lives in the run's state
    database — so a fresh run starts with none of it and sizes its first batch,
    the decision that matters most, from nothing. This is the copy that
    outlives the run.

    Keyed by the merge sha because that is the only identifier that survives:
    the stage branch is deleted and the executor's own commits are squashed
    away, so a cost recorded against either would point at nothing an hour
    later. Against the merge sha, `git show` answers what those files actually
    were, which is the difference between evidence and a number.
    """
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / STAGE_COSTS_FILENAME
    line = (
        f"{STAGE_COST_PREFIX}`{merge_sha}` `{stage_id}` — "
        f"{files} file(s), {context_tokens:,} executor tokens\n"
    )
    with path.open("a") as fh:
        fh.write(line)
    return path


def recent_stage_costs(
    project_dir: Path | str, limit: int = 12
) -> list[dict]:
    """The last few stage costs, oldest first, across every run."""
    path = Path(project_dir) / STAGE_COSTS_FILENAME
    if not path.is_file():
        return []
    found = [
        {
            "merge_sha": sha,
            "stage_id": stage_id,
            "files": int(files),
            "context_tokens": int(tokens.replace(",", "")),
        }
        for sha, stage_id, files, tokens in _STAGE_COST.findall(path.read_text())
    ]
    return found[-limit:] if limit else found
