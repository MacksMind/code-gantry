"""Report generation.

Written on completion, gating, or escalation. Two things it must make easy:
undoing the entire run in one command, and confirming the paid model was
invoked at checkpoints rather than continuously. The second is the whole
economic premise of splitting executor from reviewer, so if the cached
proportion regresses the report should show it rather than the invoice.
"""

from __future__ import annotations

from orchestrator.config import RunConfig
from orchestrator.state import RunState


def build_report(state: RunState, cfg: RunConfig) -> str:
    lines: list[str] = []
    status = state.get("status", "running")

    lines.append(f"# Run report: {state.get('run_id', '(unknown)')}")
    lines.append("")
    lines.append(f"**Status:** {status}")
    lines.append("")
    lines.append(f"- Target repo: `{state.get('target_repo')}`")
    lines.append(f"- Branch: `{state.get('branch')}` (cut from `{state.get('base_ref')}`)")
    lines.append(f"- Base sha at run start: `{state.get('base_sha')}`")
    lines.append("")
    lines.append("Undo the entire run:")
    lines.append("")
    lines.append("```")
    lines.append(
        f"git -C {state.get('target_repo')} reset --hard {state.get('base_sha')}"
    )
    lines.append("```")
    lines.append("")

    if status == "escalated":
        lines.append("## Why it stopped")
        lines.append("")
        lines.append("```")
        lines.append((state.get("escalation_reason") or "no reason recorded").strip())
        lines.append("```")
        lines.append("")

    if status == "awaiting_human":
        lines.extend(_gate_section(state, cfg))

    lines.extend(_stage_table(state))
    lines.extend(_cost_section(state))
    lines.extend(_remaining_section(state, cfg))

    return "\n".join(lines) + "\n"


def _gate_section(state: RunState, cfg: RunConfig) -> list[str]:
    index = state.get("stage_index", 0)
    stage = cfg.stages[index] if index < len(cfg.stages) else None
    lines = ["## Waiting on you", ""]
    if stage is not None:
        lines.append(f"Stage `{stage.id}` is a manual stage. Do this:")
        lines.append("")
        lines.append((stage.human_steps or "").strip())
        lines.append("")
    lines.append("Then resume the run:")
    lines.append("")
    lines.append("```")
    lines.append(f"orchestrator resume {state.get('run_id')}")
    lines.append("```")
    lines.append("")
    lines.append(
        "Resuming re-enters at verify, so the run confirms your work is green "
        "before advancing."
    )
    lines.append("")
    return lines


def _stage_table(state: RunState) -> list[str]:
    status = state.get("status", "running")
    history = state.get("history") or []
    # Only meaningful while the run is actually stopped: a stage that
    # escalated, was fixed, and then completed must appear once, as complete.
    failed_id = state.get("failed_stage_id") if status == "escalated" else None

    if not history and not failed_id:
        return ["## Stages", "", "No stages completed.", ""]

    lines = ["## Stages", ""]
    lines.append(
        "| Stage | Kind | Outcome | Commits | Wall | Tests | Retries | Reworks | Flakes |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for entry in history:
        lines.append(
            "| `{id}` | {kind} | {outcome} | {commits} | {wall:.0f}s | {tests:.1f}s "
            "| {retries} | {reworks} | {flakes} |".format(
                id=entry.get("id"),
                kind=entry.get("kind"),
                outcome=entry.get("outcome"),
                commits=f"`{entry['commit_range']}`" if entry.get("commit_range") else "—",
                wall=entry.get("wall_seconds", 0.0),
                tests=entry.get("test_seconds", 0.0),
                retries=entry.get("verify_retries", 0),
                reworks=entry.get("rework_attempts", 0),
                flakes=entry.get("flake_reruns", 0),
            )
        )

    # The stage that stopped the run is rendered from live state rather than
    # history, so a later resume that completes it leaves one row, not two.
    if failed_id:
        lines.append(
            "| `{id}` | — | **escalated** | — | — | {tests:.1f}s | {retries} "
            "| {reworks} | {flakes} |".format(
                id=failed_id,
                tests=state.get("test_seconds", 0.0) or 0.0,
                retries=state.get("verify_attempt", 0),
                reworks=state.get("rework_attempt", 0),
                flakes=state.get("flake_reruns", 0),
            )
        )
    lines.append("")

    if failed_id and state.get("failure_layer"):
        lines.append(f"### `{failed_id}`")
        lines.append("")
        lines.append(f"- Failed at the **{state['failure_layer']}** gate")
        if state.get("review_verdict"):
            lines.append(
                f"- Reviewer verdict: **{state['review_verdict']}** — "
                f"{state.get('review_summary') or ''}"
            )
        lines.append("")

    for entry in history:
        details = []
        if entry.get("failed_layer"):
            details.append(f"- Failed at the **{entry['failed_layer']}** gate")
        if entry.get("review_verdict"):
            details.append(
                f"- Reviewer verdict: **{entry['review_verdict']}** — "
                f"{entry.get('review_summary') or ''}"
            )
        if entry.get("flake_reruns"):
            details.append(
                f"- {entry['flake_reruns']} flake re-run(s): a test failed then "
                "passed on retry without consuming a retry budget"
            )
        if details:
            lines.append(f"### `{entry.get('id')}`")
            lines.append("")
            lines.extend(details)
            lines.append("")

    return lines


def _cost_section(state: RunState) -> list[str]:
    history = state.get("history") or []
    prompt = sum(e.get("prompt_tokens", 0) for e in history)
    cached = sum(e.get("cached_tokens", 0) for e in history)
    completion = sum(e.get("completion_tokens", 0) for e in history)
    test_seconds = sum(e.get("test_seconds", 0.0) for e in history)
    reviews = sum(1 for e in history if e.get("review_verdict"))

    cached_pct = (cached / prompt * 100) if prompt else 0.0

    return [
        "## Reviewer cost",
        "",
        f"- Reviewer calls: {reviews}",
        f"- Prompt tokens: {prompt:,} ({cached:,} cached, {cached_pct:.0f}%)",
        f"- Uncached prompt tokens: {prompt - cached:,}",
        f"- Completion tokens: {completion:,}",
        "",
        f"Total test-suite runtime: {test_seconds:.0f}s. On a large suite this, "
        "rather than token cost, is usually what makes a run expensive.",
        "",
    ]


def _remaining_section(state: RunState, cfg: RunConfig) -> list[str]:
    done = {e.get("id") for e in (state.get("history") or []) if e.get("outcome") == "complete"}
    remaining = [s.id for s in cfg.stages if s.id not in done]
    if not remaining:
        return []
    listed = "\n".join(f"- `{sid}`" for sid in remaining)
    return [
        "## Stages not completed",
        "",
        listed,
        "",
    ]
