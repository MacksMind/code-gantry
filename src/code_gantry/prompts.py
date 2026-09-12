"""Prompt assembly for the executor, the reviewer, and the planner.

The words live in the repository's `prompts/` directory, one Markdown file
per block; this module decides which files a call carries, in what order,
and fills their placeholders from config and state. Pure, so it can be
tested without a model.

**Ordering is the caching strategy, not presentation.** What is fixed for a
run leads and sits inside the cache mark; what changes per stage follows it.
Reordering for readability multiplies the cost of every call.
"""

from __future__ import annotations

from pathlib import Path

from code_gantry.config import ProjectConfig, Stage
from code_gantry.planner import cache_control
from code_gantry.promptfiles import render, text
from code_gantry.state import FailureDetail, StageResult

REVIEW_SYSTEM_PROMPT = render(
    "reviewer/system",
    repository_text_is_evidence=text("shared/repository_text_is_evidence"),
)
REVIEW_TOOLS_PROMPT = "\n" + render(
    "reviewer/tools", state_not_change=text("shared/state_not_change")
)

_RETRY_OPENING_REVIEW = text("executor/retry_review")
_RETRY_OPENING_GATE = text("executor/retry_gate")


def build_executor_prompt(
    stage: Stage,
    cfg: ProjectConfig,
    context: list[tuple[str, str]] | None = None,
    feedback: list[str] | None = None,
    failure_layer: str | None = None,
    cumulative_diff: str | None = None,
    excerpts: list[tuple[str, str]] | None = None,
) -> str:
    """The message handed to the executor: the stage, restated in full.

    A rework is a fresh invocation, so everything it needs is here. The
    opening depends on which gate sent it back: a review rejection means the
    work is wrong and is to be replaced, a verify failure means the sweep is
    unfinished. Both arrive with the previous attempt committed on the branch.
    """
    parts: list[str] = []

    if feedback:
        parts.append(
            _RETRY_OPENING_REVIEW if failure_layer == "review" else _RETRY_OPENING_GATE
        )

    parts.append(render("executor/task", instruction=stage.instruction))

    if stage.constraints:
        parts.append(render("executor/constraints", constraints=stage.constraints))

    if stage.acceptance:
        parts.append(render("executor/acceptance", acceptance=stage.acceptance))

    if stage.require_new_tests:
        parts.append(text("executor/tests_required"))

    if stage.edit_files:
        # Which of these exist is a fact computed here, so the model knows
        # whether it is reaching for `edit` or `create_file`.
        missing = [g for g in stage.edit_files if _is_missing_path(g, cfg)]
        listed = "\n".join(
            f"- {glob} (does not exist yet)" if glob in missing else f"- {glob}"
            for glob in stage.edit_files
        )
        body = render("executor/edit_files", listed=listed)
        if missing:
            body += "\n\n" + text("executor/edit_files_missing")
        parts.append(body)

    if stage.read_files:
        listed = "\n".join(f"- {glob}" for glob in stage.read_files)
        parts.append(render("executor/read_files", listed=listed))

    if excerpts:
        # The code span holds the path and range; the note follows outside it.
        blocks = []
        for label, body in excerpts:
            ref, _, note = label.partition(" — ")
            heading = f"`{ref}`" + (f" — {note}" if note else "")
            blocks.append(f"### {heading}\n\n```\n{body}\n```")
        # Excerpts are read at the stage's starting commit; once the stage has
        # changed something they may have moved.
        currency = (
            text("executor/excerpts_moved")
            if cumulative_diff and cumulative_diff.strip()
            else text("executor/excerpts_current")
        )
        parts.append(
            render("executor/excerpts", currency=currency, blocks="\n\n".join(blocks))
        )

    if stage.forbidden_patterns:
        listed = "\n".join(f"- /{p}/" for p in stage.forbidden_patterns)
        parts.append(render("executor/forbidden", listed=listed))

    if context:
        blocks = [
            f"### `{command}`\n\n```\n{output.strip()}\n```"
            for command, output in context
        ]
        parts.append(render("executor/context", blocks="\n\n".join(blocks)))

    if cumulative_diff:
        parts.append(render("executor/diff_so_far", diff=cumulative_diff.strip()))

    if feedback:
        listed = "\n\n".join(
            f"{i}. {item}" for i, item in enumerate(feedback, start=1)
        )
        parts.append(render("executor/feedback", listed=listed))

    return "\n\n".join(parts)


def _is_missing_path(glob: str, cfg: ProjectConfig | None) -> bool:
    """A literal path in the scope list with no file behind it. Globs name no
    particular file, and anything unresolvable is treated as present, so the
    annotation is only ever added when it is certainly true."""
    if cfg is None or any(ch in glob for ch in "*?["):
        return False
    try:
        return not (Path(cfg.target_repo) / glob).exists()
    except OSError:  # pragma: no cover - unresolvable path claims nothing
        return False


def _checks_block(cfg: ProjectConfig | None) -> str:
    """What runs on a diff after the executor stops: the `checks` from config,
    stated as a fact about the diff to both the planner and the executor."""
    checks = list(getattr(getattr(cfg, "stage_defaults", None), "checks", []) or [])
    if not checks:
        return ""
    listed = "\n".join(f"- `{command}`" for command in checks)
    return render("shared/checks", listed=listed) + "\n\n"


def _history_limit(cfg: ProjectConfig | None) -> int | None:
    """How many landed stages the reviewer is shown, if it is bounded."""
    reviewer = getattr(cfg, "reviewer", None) if cfg else None
    return getattr(reviewer, "history_stages", None) if reviewer else None


def _plan_intro(cfg: ProjectConfig | None) -> str:
    """What the plan text is and how keys are used, ahead of the text itself."""
    prefix = getattr(getattr(cfg, "ledger", None), "key_prefix", None) or "plan"
    return render("planner/plan_intro", prefix=prefix) + "\n\n"


def _projection_block(projection: str | None) -> str:
    if not projection or not projection.strip():
        return ""
    return render("planner/projection", projection=projection.strip())


def _warnings_block(warnings: str | None) -> str:
    """The test runner's tally of warnings, handed over rather than read here."""
    if not warnings or not warnings.strip():
        return ""
    return render("planner/warnings", text=warnings.strip())


def _costs_block(costs: list[dict] | None) -> str:
    """What stages have cost the executor across every run, keyed by the
    landing commit, which is what survives the squash."""
    if not costs:
        return ""
    lines = "\n".join(
        f"- `{c['merge_sha'][:12]}` {c['stage_id']} — "
        f"{c['context_tokens']:,} context tokens, "
        + (
            f"{c['changed']} file(s) changed "
            f"+{c['insertions']} -{c['deletions']}"
            if "changed" in c
            else f"{c['files']} file(s) in scope"
        )
        for c in costs
    )
    return "\n\n" + render("planner/costs", lines=lines)


def _batch_block(cfg) -> str:
    """How many stages this call may return. Fixed for the run, so it sits in
    the cached prefix."""
    planner = getattr(cfg, "planner", None)
    cap = getattr(planner, "max_batch_stages", 1) if planner else 1
    if cap <= 1:
        return text("planner/batch_one") + "\n\n"
    return render("planner/batch", cap=cap, rest=cap - 1) + "\n\n"


def _stage_size_block() -> str:
    """How much work belongs in one stage."""
    return text("planner/stage_size") + "\n\n"


def _history_block(
    completed: list[StageResult],
    limit: int | None = None,
) -> str:
    """What this run has landed, which is not what the project has landed."""
    if not completed:
        return text("planner/history_empty")

    shown = completed
    dropped = 0
    if limit is not None and len(completed) > limit:
        shown = completed[-limit:]
        dropped = len(completed) - limit

    entries = []
    for entry in shown:
        line = f"### Stage {entry.get('index')}: {entry.get('id')}"
        if entry.get("revisions"):
            line += f" (took {entry['revisions'] + 1} revisions)"
        if entry.get("merge_sha"):
            line += f"\n\nLanded as `{entry['merge_sha'][:12]}`."
        elif entry.get("candidate_sha"):
            # What stays true however the composition goes. This block is the
            # cacheable prefix of every later call, so an entry that said
            # "landed" and had to be corrected would rewrite the whole of it.
            line += f"\n\nFinished and pushed as candidate `{entry['candidate_sha'][:12]}`."
        if entry.get("withheld_reads"):
            line += "\n" + render(
                "planner/history_withheld", listed=", ".join(entry["withheld_reads"])
            )
        entries.append(line)

    if dropped:
        head = render(
            "planner/history_truncated_head",
            shown=len(shown), total=len(shown) + dropped, dropped=dropped,
        )
    else:
        head = text("planner/history_head")
    return head + "\n\n" + "\n\n".join(entries)


def _review_system_prompt(cfg: ProjectConfig | None) -> str:
    """The contract, plus the tool section when the reviewer can read."""
    reviewer = getattr(cfg, "reviewer", None)
    if reviewer is not None and getattr(reviewer, "repo_access", False):
        return REVIEW_SYSTEM_PROMPT + "\n" + REVIEW_TOOLS_PROMPT
    return REVIEW_SYSTEM_PROMPT


def _conventions_block(
    agent_context: str | None, *, role: str = "reviewer", project_tools=()
) -> str:
    """The repository's own agent-facing documents, framed for who is reading.
    The framing names what the reader can do, so it must match the tools."""
    if not agent_context or not agent_context.strip():
        return ""
    if role == "executor":
        from code_gantry.projecttools import for_role

        procedure = text(
            "shared/conventions_procedure_with_tools"
            if for_role("executor", project_tools)
            else "shared/conventions_procedure_no_tools"
        )
        binding = render("shared/conventions_binding_executor", procedure=procedure)
    else:
        binding = text("shared/conventions_binding_reviewer")
    return render(
        "shared/conventions", binding=binding, agent_context=agent_context.strip()
    ) + "\n\n"


def build_review_messages(
    stage: Stage,
    cfg: ProjectConfig,
    diff: str,
    plan_text: str,
    completed: list[StageResult],
    projection: str | None = None,
    agent_context: str | None = None,
    proposed: list[tuple[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Chat messages for the reviewer, stable payload first.

    The conventions and the plan sit inside the first breakpoint, fixed for
    the run. The projection, the history, the stage and the diff follow it
    and close with the second, so a tool loop re-sends only its own results.
    """
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "input_text", "text": _review_system_prompt(cfg)}
            ],
        }
    ]
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": _conventions_block(agent_context)
                    + _plan_intro(cfg)
                    + plan_text,
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    )

    current: list[str] = []
    if projection and projection.strip():
        current.append(_projection_block(projection))

    current += [
        _history_block(completed, limit=_history_limit(cfg)),
        render("reviewer/stage", stage_id=stage.id, instruction=stage.instruction or ""),
    ]

    if proposed:
        listed = "\n".join(f"- `{fid}` — {claim}" for fid, claim in proposed)
        current.append(render("reviewer/proposed", listed=listed))

    if stage.constraints:
        current.append(render("reviewer/constraints", constraints=stage.constraints))

    if stage.acceptance:
        current.append(render("reviewer/acceptance", acceptance=stage.acceptance))

    current.append(render("reviewer/diff", diff=diff.strip()))
    current.append(text("reviewer/verdict"))

    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "\n\n".join(current),
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    )
    return messages


def _planner_cache_ttl(cfg) -> str | None:
    """The planner role's configured cache lifetime, read through `planner`
    so a config without one raises rather than answering None."""
    if cfg is None:
        return None
    planner = getattr(cfg, "planner", None)
    if planner is None:
        raise AttributeError(
            "config has no `planner`; the planner's cache lifetime cannot be "
            "read. `build_planner_messages` takes the ProjectConfig, and "
            "`cache_ttl` lives on PlannerConfig."
        )
    return getattr(planner, "cache_ttl", None)


def build_planner_messages(
    cfg: ProjectConfig,
    plan_text: str,
    completed: list[StageResult],
    projection: str = "",
    current_stage: Stage | None = None,
    failure: FailureDetail | None = None,
    opening_failure: FailureDetail | None = None,
    gate_history: list[dict] | None = None,
    revision: int = 0,
    interventions_used: int = 0,
    interventions_max: int = 0,
    layout: str | None = None,
    stage_costs: list[dict] | None = None,
    test_warnings: str | None = None,
    agent_context: str | None = None,
    stage_diff: str | None = None,
    stage_queue: list[dict] | None = None,
    batch_notes: list[str] | None = None,
) -> list[dict[str, str]]:
    """Chat messages for the planner: one user message, three blocks.

    Block 0 is everything fixed for the run — conventions, the standing
    blocks, the layout, the plan — and carries the cache mark. Block 1 is
    what the ledger and the test runner say now. Block 2 is the situation:
    costs, the batch, the failure, the task.
    """
    leading = ""
    if agent_context:
        leading += render("planner/conventions", agent_context=agent_context) + "\n\n"
    leading += _stage_size_block()
    leading += _batch_block(cfg)
    leading += _checks_block(cfg)
    if layout:
        leading += render("planner/layout", layout=layout) + "\n\n"
    leading += _plan_intro(cfg) + plan_text

    history = _history_block(completed)
    volatile = _costs_block(stage_costs)

    blocks = [
        {
            "type": "text",
            "text": leading,
            "cache_control": cache_control(_planner_cache_ttl(cfg)),
        },
        {
            "type": "text",
            "text": "\n\n".join(
                x for x in (_projection_block(projection), _warnings_block(test_warnings), history) if x
            ),
        },
    ]

    current: list[str] = [volatile]

    if stage_queue:
        listed = ", ".join(f"`{s.get('id')}`" for s in stage_queue)
        current.append(render("planner/queued", listed=listed))
    if batch_notes:
        listed = "\n".join(f"- {n}" for n in batch_notes)
        current.append(render("planner/batch_notes", listed=listed))

    if current_stage is None:
        if failure and (failure.get("layer") if isinstance(failure, dict) else None) == "validation":
            heading, preamble = _heading_and_preamble("planner/validation_rejected")
            current.append(_failure_block(failure, heading=heading, preamble=preamble))
        current.append(text("planner/derive"))
    else:
        current.append(
            render(
                "planner/revise",
                stage_id=current_stage.id, revision=revision,
                instruction=current_stage.instruction or "",
                scope="\n".join(f"- {g}" for g in current_stage.edit_files),
            )
        )

        if current_stage.constraints:
            current.append(
                render("planner/revise_constraints", constraints=current_stage.constraints)
            )

        # The diagnosis first, then what it went on to cause.
        if opening_failure and opening_failure != failure:
            heading, preamble = _heading_and_preamble("planner/opening_failure")
            current.append(_failure_block(opening_failure, heading=heading, preamble=preamble))

        if failure:
            current.append(
                _failure_block(
                    failure,
                    heading=(
                        text("planner/failure_heading_after_first")
                        if opening_failure and opening_failure != failure
                        else text("planner/failure_heading")
                    ),
                )
            )

        history = format_gate_history(gate_history or [])
        if history:
            current.append(render("planner/gate_history", history=history))

        if stage_diff and stage_diff.strip():
            current.append(render("planner/stage_diff", diff=stage_diff.strip()))

        current.append(text("planner/decide"))
        current.append(text("planner/redraw_lesson"))

    if interventions_max:
        remaining = max(interventions_max - interventions_used, 0)
        current.append(
            render("planner/budget", remaining=remaining, max=interventions_max)
        )

    blocks.append({"type": "text", "text": "\n\n".join(current)})
    return [{"role": "user", "content": blocks}]


def _heading_and_preamble(name: str) -> tuple[str, str]:
    """A failure-block file: its first line is the heading, the rest the
    preamble."""
    heading, _, preamble = text(name).partition("\n\n")
    return heading.strip(), preamble.strip()


def format_gate_history(entries: list[dict]) -> str:
    """Every gate verdict a stage has drawn, one line per revision. `passed`
    and `review` are spelled out so the list cannot be read as layer names."""
    if not entries:
        return ""
    spelled = {"passed": "all gates passed", "review": "review rejected"}
    by_revision: dict[int, list[str]] = {}
    order: list[int] = []
    for entry in entries:
        revision = entry["revision"]
        if revision not in by_revision:
            by_revision[revision] = []
            order.append(revision)
        layer = entry["layer"]
        by_revision[revision].append(spelled.get(layer, layer))
    return "\n".join(
        f"  revision {revision}: {', '.join(by_revision[revision])}"
        for revision in order
    )


def _failure_block(
    failure: FailureDetail,
    heading: str | None = None,
    preamble: str | None = None,
) -> str:
    """The specific damage a stage did, for the planner to draw against.
    `heading` is a parameter because a stage can fail twice for unrelated
    reasons and two blocks with one title read as a contradiction."""
    parts = [
        render(
            "planner/failure",
            heading=heading or text("planner/failure_heading"),
            preamble=f"{preamble}\n\n" if preamble else "",
            layer=failure.get("layer"),
            summary=failure.get("summary"),
        )
    ]

    if failure.get("out_of_scope_paths"):
        listed = "\n".join(f"- {p}" for p in failure["out_of_scope_paths"])
        parts.append(render("planner/failure_scope", listed=listed))

    if failure.get("failing_paths"):
        listed = "\n".join(f"- {p}" for p in failure["failing_paths"])
        parts.append(render("planner/failure_paths", listed=listed))

    if failure.get("detail"):
        parts.append(render("planner/failure_detail", detail=failure["detail"].strip()))

    return "\n\n".join(parts)


def _system_prompt_override(cfg: ProjectConfig | None) -> str:
    """An operator's own executor system prompt, by path. It replaces the
    built-in rather than joining it, and a named file that cannot be read
    raises rather than falling back."""
    if cfg is None:
        return ""
    named = getattr(getattr(cfg, "executor", None), "system_prompt_file", None)
    if not named:
        return ""
    path = Path(cfg.target_repo) / named
    try:
        body = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise FileNotFoundError(
            f"executor.system_prompt_file names {named!r}, which could not be "
            f"read: {e}. Remove the setting to use the built-in prompt."
        ) from e
    if not body:
        raise ValueError(
            f"executor.system_prompt_file names {named!r}, which is empty. "
            "Remove the setting to use the built-in prompt."
        )
    return body


def _no_direct_edit_block(cfg: ProjectConfig | None) -> str:
    """The generated-file rule, built from what declares it or absent."""
    entries = list(getattr(cfg.executor, "no_direct_edit", []) or []) if cfg else []
    if not entries:
        return ""
    lines = "\n".join(f"- `{e.path_glob}` — {e.reason}" for e in entries)
    return "\n\n" + render("executor/no_direct_edit", lines=lines)


def _executor_system_prompt(cfg: ProjectConfig | None) -> str:
    """Everything true of every stage, paid for once inside the cache mark."""
    override = _system_prompt_override(cfg)
    if override:
        return override
    body = render(
        "executor/system",
        no_direct_edit=_no_direct_edit_block(cfg),
        repository_text_is_evidence=text("shared/repository_text_is_evidence"),
    )
    checks = _checks_block(cfg)
    if checks:
        body += "\n\n" + checks
    return body


def build_executor_messages(
    stage: Stage,
    cfg: ProjectConfig,
    prompt: str,
    agent_context: str | None = None,
    feedback: list[str] | None = None,
    failure_layer: str | None = None,
) -> list[dict]:
    """The executor's conversation: the system prompt and the conventions
    inside the breakpoint, the stage after it, feedback as its own turns so
    attempt two shares attempt one's prefix."""
    messages: list[dict] = [
        {
            "role": "system",
            "content": [
                {"type": "input_text", "text": _executor_system_prompt(cfg)}
            ],
        }
    ]

    conventions = _conventions_block(
        agent_context, role="executor", project_tools=cfg.project_tools
    )
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": conventions or text("executor/conventions_none"),
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                }
            ],
        }
    )

    messages.append(
        {"role": "user", "content": [{"type": "input_text", "text": prompt}]}
    )

    items = list(feedback or [])
    if items:
        items.insert(
            0, _RETRY_OPENING_REVIEW if failure_layer == "review" else _RETRY_OPENING_GATE
        )
    for item in items:
        messages.append(
            {"role": "user", "content": [{"type": "input_text", "text": item}]}
        )

    return messages
