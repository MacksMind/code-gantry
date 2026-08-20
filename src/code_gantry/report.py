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

from code_gantry.config import ProjectConfig
from code_gantry.pricing import (
    cached_price_map,
    entry_for,
    price_usage,
)
from code_gantry.state import RunState


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
        lines.extend(_escalation_section(state, cfg))

    lines.extend(_stage_table(state))
    lines.extend(_stage_details(state))
    lines.extend(_flaky_section(state))
    lines.extend(_cost_section(state, cfg))
    return "\n".join(lines) + "\n"


def _flaky_section(state: RunState) -> list[str]:
    """Files excused as suite flakes, named.

    Each one is a stage that nearly failed to land for a reason it did not
    cause. They are listed together because the fix is not per-stage: it is a
    property of the suite, and this is the work list for removing the need for
    the excuse in the first place.
    """
    files = state.get("flaky_files") or []
    if not files:
        return []

    lines = ["## Files excused as suite flakes", ""]
    lines.append(
        "Each of these failed as part of a broader run and passed when re-run "
        "whole, on its own. That makes them order- or parallelism-dependent, "
        "and each one blocked a stage that had done nothing wrong."
    )
    lines.append("")
    for path in files:
        lines.append(f"- `{path}`")
    lines.append("")
    return lines


def _escalation_section(state: RunState, cfg: ProjectConfig) -> list[str]:
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
        f"Fix whatever this describes, then `{cfg.resume_command()}`. A "
        "repository-state failure re-enters at verify so your fix is checked "
        "rather than discarded; a planning failure re-enters at the planner."
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

    if completed:
        # From the stages themselves, not from the session clock. `elapsed`
        # resets on every resume while `completed` spans the whole run, so
        # dividing one by the other billed 30 stages against five minutes and
        # reported a minute a stage. These two sum what each stage actually
        # took — derivation plus the stage itself — and survive a kill, because
        # they are written per landing rather than accumulated in a counter.
        measured = sum(
            e.get("wall_seconds", 0.0) + e.get("plan_seconds", 0.0) for e in completed
        )
        per_stage = measured / len(completed) / 3600.0
        projected = per_stage * cfg.limits.max_stages
        if projected > budget:
            # Reported as a fact about the work, not as a misconfiguration.
            # These two limits measure different things: `wall_clock_hours`
            # bounds one unattended stretch — how long the operator is willing
            # to let the run go without looking at it — and `max_stages` bounds
            # the project. Stopping on the clock and resuming is the designed
            # behaviour, so every long project trips this arithmetic. The
            # earlier wording said "one of them needs raising", which told an
            # operator to change a setting that was correctly set.
            sessions = projected / budget if budget else 0
            lines.append(
                f"> At {per_stage:.2f}h per landed stage, `max_stages` "
                f"({cfg.limits.max_stages}) would take about {projected:.0f}h — "
                f"roughly {sessions:.0f} sessions at the {budget:g}h "
                "supervision window. Nothing is wrong: the run stops on the "
                "clock and `resume` continues it. Raise `wall_clock_hours` only "
                "if you want longer unattended stretches."
            )
            lines.append("")

    return lines


def _dollars(
    prices: dict, model: str, prompt: int, cached: int, writes: int, completion: int
) -> str:
    """A figure, or the reason there isn't one.

    Never "$0.00" for an unpriced model. Zero for "not priced" and zero for
    "free" are indistinguishable in a record, and this section exists to
    settle an argument about effort, so a number that might mean two things is
    worse than no number.
    """
    cost = price_usage(entry_for(prices, model), prompt, cached, writes, completion)
    if cost is None:
        return f"not priced (no rate for `{model}`)"
    return f"${cost:,.2f}"


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

    cache_writes = run_usage.get("cache_write_tokens", 0)
    planner_writes = run_usage.get("planner_cache_write_tokens", 0)
    prices = cached_price_map(cfg)

    lines = [
        "## Cost",
        "",
        f"**Reviewer** ({cfg.reviewer.model}, effort {cfg.reviewer.effort})",
        "",
        f"- Prompt tokens: {prompt:,} ({cached:,} cached, {cached_pct:.0f}%)",
        f"- Uncached prompt tokens: {prompt - cached:,}",
        f"- Completion tokens: {completion:,}",
        f"- Estimated cost: "
        + _dollars(prices, cfg.reviewer.model, prompt, cached, cache_writes, completion),
        "",
        f"**Planner** ({cfg.planner.model}, effort {cfg.planner.effort})",
        "",
        f"- Interventions used: {state.get('planner_interventions', 0)} of "
        f"{cfg.limits.max_planner_interventions}",
        f"- Prompt tokens: {planner_prompt:,} "
        f"({planner_cached:,} cached, {planner_cached_pct:.0f}%)",
        f"- Uncached prompt tokens: {planner_prompt - planner_cached:,}",
        f"- Completion tokens: {planner_completion:,}",
        f"- Estimated cost: "
        + _dollars(
            prices, cfg.planner.model, planner_prompt, planner_cached,
            planner_writes, planner_completion,
        ),
        "",
    ]

    exec_prompt = run_usage.get("executor_prompt_tokens", 0)
    if exec_prompt:
        exec_cached = run_usage.get("executor_cached_tokens", 0)
        exec_writes = run_usage.get("executor_cache_write_tokens", 0)
        exec_completion = run_usage.get("executor_completion_tokens", 0)
        exec_pct = exec_cached / exec_prompt * 100
        lines += [
            f"**Executor** ({cfg.executor.model}, effort "
            f"{cfg.executor.reasoning_effort or 'default'})",
            "",
            f"- Prompt tokens: {exec_prompt:,} "
            f"({exec_cached:,} cached, {exec_pct:.0f}%)",
            f"- Uncached prompt tokens: {exec_prompt - exec_cached:,}",
            f"- Completion tokens: {exec_completion:,}",
            "- Estimated cost: "
            + _dollars(
                prices, cfg.executor.model, exec_prompt, exec_cached,
                exec_writes, exec_completion,
            ),
            "",
        ]

    lines += [
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
