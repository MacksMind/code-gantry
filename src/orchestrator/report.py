"""Report generation.

`report.md` is the artifact the operator actually reads after an unattended run.
Each stage record is shaped like a pull request, because that is the mental
model: a list of individually verified, individually green commits on the
project branch, from which the operator decides what to deploy.

Two things it must make easy: undoing the whole project branch in one command,
and confirming the paid models were invoked at checkpoints rather than
continuously. The second is the economic premise of the whole design, so a
regression in cached-token proportion should show up here rather than on the
invoice.
"""

from __future__ import annotations

from orchestrator.config import ProjectConfig
from orchestrator.state import RunState


def build_report(state: RunState, cfg: ProjectConfig) -> str:
    lines: list[str] = []
    status = state.get("status", "running")

    lines.append(f"# Run report: {state.get('run_id', '(unknown)')}")
    lines.append("")
    lines.append(f"**Status:** {status}")
    lines.append("")
    lines.append(f"- Project: `{state.get('project_slug')}`")
    lines.append(f"- Target repo: `{state.get('target_repo')}`")
    lines.append(
        f"- Project branch: `{state.get('project_branch')}` "
        f"(cut from `{state.get('base_ref')}`)"
    )
    lines.append(f"- Base sha at run start: `{state.get('base_sha')}`")
    lines.append("")
    lines.append(
        "The project branch has not been merged anywhere — that is your call. "
        "To discard the whole run:"
    )
    lines.append("")
    lines.append("```")
    lines.append(
        f"git -C {state.get('target_repo')} branch -D {state.get('project_branch')}"
    )
    lines.append("```")
    lines.append("")

    if status == "escalated":
        lines.extend(_escalation_section(state))

    lines.extend(_stage_table(state))
    lines.extend(_stage_details(state))
    lines.extend(_cost_section(state, cfg))
    return "\n".join(lines) + "\n"


def _escalation_section(state: RunState) -> list[str]:
    lines = ["## Why it stopped", ""]
    failed = state.get("failed_stage_id")
    layer = state.get("failure_layer")
    if failed:
        lines.append(f"Stage `{failed}` at the **{layer}** gate.")
        lines.append("")
    lines.append("```")
    lines.append((state.get("escalation_reason") or "no reason recorded").strip())
    lines.append("```")
    lines.append("")
    lines.append(
        "Fix whatever this describes, then `orchestrator resume "
        f"{state.get('run_id')}`. A repository-state failure re-enters at verify "
        "so your fix is checked rather than discarded; a planning failure "
        "re-enters at the planner."
    )
    lines.append("")
    return lines


def _stage_table(state: RunState) -> list[str]:
    completed = state.get("completed") or []
    if not completed:
        return ["## Stages landed", "", "None.", ""]

    lines = ["## Stages landed", ""]
    lines.append(
        "| # | Stage | Commit | Revs | Retries | Reworks | Flakes | Wall | Tests |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for entry in completed:
        flakes = entry.get("flake_reruns_iteration", 0) + entry.get(
            "flake_reruns_review_gate", 0
        )
        lines.append(
            "| {index} | `{id}` | `{sha}` | {revs} | {retries} | {reworks} "
            "| {flakes} | {wall:.0f}s | {tests:.1f}s |".format(
                index=entry.get("index", 0),
                id=entry.get("id"),
                sha=(entry.get("merge_sha") or "")[:12] or "—",
                revs=entry.get("revisions", 0),
                retries=entry.get("verify_retries", 0),
                reworks=entry.get("rework_attempts", 0),
                flakes=flakes,
                wall=entry.get("wall_seconds", 0.0),
                tests=entry.get("test_seconds", 0.0),
            )
        )
    lines.append("")
    return lines


def _stage_details(state: RunState) -> list[str]:
    """One PR-shaped record per stage.

    This is what makes the run convertible to real pull requests later, if the
    deliberate no-GitHub choice stops paying off.
    """
    completed = state.get("completed") or []
    if not completed:
        return []

    lines = ["## Stage records", ""]
    for entry in completed:
        lines.append(f"### {entry.get('index', 0)}. `{entry.get('id')}`")
        lines.append("")
        lines.append(
            f"`{(entry.get('base_sha') or '')[:12]}` → "
            f"`{(entry.get('merge_sha') or '')[:12]}`"
            + (f"  ·  config `{entry['config_hash'][:12]}`" if entry.get("config_hash") else "")
        )
        lines.append("")
        instruction = (entry.get("instruction") or "").strip()
        if instruction:
            lines.append("**Asked to do:**")
            lines.append("")
            for line in instruction.splitlines():
                lines.append(f"> {line}")
            lines.append("")

        if entry.get("review_verdict"):
            lines.append(
                f"- Reviewer: **{entry['review_verdict']}** — "
                f"{entry.get('review_summary') or ''}"
            )
        if entry.get("revisions"):
            lines.append(
                f"- Took {entry['revisions']} planner revision(s) before it was "
                "drawn correctly"
            )
        if entry.get("flake_reruns_review_gate"):
            lines.append(
                f"- {entry['flake_reruns_review_gate']} flake re-run(s) **at the "
                "merge gate** — the full suite failed then passed. These are the "
                "expensive kind: a flake here can cost a planner intervention"
            )
        if entry.get("flake_reruns_iteration"):
            lines.append(
                f"- {entry['flake_reruns_iteration']} flake re-run(s) during "
                "iteration (cheap — no retry budget consumed)"
            )
        for note in entry.get("planner_notes") or []:
            lines.append(f"- Planner: {note}")
        lines.append("")

    return lines


def _wall_clock_lines(state: RunState, cfg: ProjectConfig) -> list[str]:
    """How much of the session's time budget the run used.

    Worth printing even on a clean completion: the first real run against a
    large suite is how an operator learns whether `wall_clock_hours` and
    `max_stages` are compatible numbers, and that is far easier to see as
    "3.1h of 14h across 12 stages" than to derive from timestamps.
    """
    budget = cfg.limits.wall_clock_hours
    elapsed = state.get("session_seconds")
    if not budget or budget <= 0 or elapsed is None:
        return []

    completed = state.get("completed") or []
    elapsed_hours = elapsed / 3600.0

    lines = [
        f"Session wall clock: {elapsed_hours:.1f}h of a {budget:g}h budget, "
        f"across {len(completed)} landed stage(s). The budget bounds one "
        "unattended session and is checked before each planner call, so a "
        "stage in flight is never killed mid-attempt; `resume` starts a fresh "
        "one.",
        "",
    ]

    if completed and elapsed_hours:
        per_stage = elapsed_hours / len(completed)
        projected = per_stage * cfg.limits.max_stages
        if projected > budget:
            lines.append(
                f"> At {per_stage:.2f}h per landed stage, `max_stages` "
                f"({cfg.limits.max_stages}) projects to {projected:.0f}h — more "
                f"than the {budget:g}h budget. The two limits disagree about how "
                "big this project is; one of them needs raising, or the run will "
                "stop on time rather than on completion."
            )
            lines.append("")

    return lines


def _cost_section(state: RunState, cfg: ProjectConfig) -> list[str]:
    completed = state.get("completed") or []
    run_usage = state.get("run_usage") or {}

    prompt = run_usage.get("prompt_tokens", 0)
    cached = run_usage.get("cached_tokens", 0)
    completion = run_usage.get("completion_tokens", 0)
    planner_prompt = run_usage.get("planner_prompt_tokens", 0)
    planner_cached = run_usage.get("planner_cached_tokens", 0)
    planner_completion = run_usage.get("planner_completion_tokens", 0)
    planner_cached_pct = (planner_cached / planner_prompt * 100) if planner_prompt else 0.0

    test_seconds = sum(e.get("test_seconds", 0.0) for e in completed)
    cached_pct = (cached / prompt * 100) if prompt else 0.0

    lines = [
        "## Cost",
        "",
        f"**Reviewer** ({cfg.reviewer.model})",
        "",
        f"- Prompt tokens: {prompt:,} ({cached:,} cached, {cached_pct:.0f}%)",
        f"- Uncached prompt tokens: {prompt - cached:,}",
        f"- Completion tokens: {completion:,}",
        "",
        f"**Planner** ({cfg.planner.model})",
        "",
        f"- Interventions used: {state.get('planner_interventions', 0)} of "
        f"{cfg.limits.max_planner_interventions}",
        f"- Prompt tokens: {planner_prompt:,} "
        f"({planner_cached:,} cached, {planner_cached_pct:.0f}%)",
        f"- Uncached prompt tokens: {planner_prompt - planner_cached:,}",
        f"- Completion tokens: {planner_completion:,}",
        "",
        f"Total test-suite runtime: {test_seconds:.0f}s across "
        f"{len(completed)} landed stage(s). On a large suite this, rather than "
        "token cost, is usually what makes a run expensive — the full suite runs "
        "once per stage that lands, so it is linear in stages, not attempts.",
        "",
    ]

    lines.extend(_wall_clock_lines(state, cfg))

    if prompt and cached_pct < 50:
        lines.append(
            f"> Only {cached_pct:.0f}% of reviewer prompt tokens were cached. The "
            "plan snapshot and completed history are supposed to form a stable "
            "cacheable prefix — a low proportion means something is varying in "
            "it, and every review is costing more than it should."
        )
        lines.append("")

    if planner_prompt and planner_cached_pct < 50:
        lines.append(
            f"> Only {planner_cached_pct:.0f}% of planner prompt tokens were "
            "cached. The plan snapshot, the repository layout and the completed "
            "history lead the planner's prompt precisely so they can be cached "
            "behind one breakpoint — a low proportion means the prefix is "
            "varying between calls, or the breakpoint is not being honoured."
        )
        lines.append("")

    return lines
