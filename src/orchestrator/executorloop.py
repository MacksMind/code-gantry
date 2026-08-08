"""The in-process edit cycle: model, then lint, then commit, then gates.

One cycle is *edit until the model stops asking for things*, then lint, then
commit, then the gates it can act on. Ordering is load-bearing at every step:

- **Gates run when the model stops, not after each edit.** A test run per edit
  is unaffordable, and the natural division is that the model decides when it
  has finished editing and the loop decides whether that is true.
- **Lint first, because it rewrites.** `rubocop -A` and its kin exit zero
  *after* changing files, so patterns and residue must read the corrected tree.
  The rewrite is fed back explicitly: an `old_string` that matched pre-lint
  bytes will now fail, and without being told the model reads that refusal as
  its own mistake.
- **Commit before tests.** Squash-merge is what makes "every commit on the
  project branch is green" and "the executor commits before it tests" both
  true, and the graph reads a committed tree.
- **Cheap gates before the suite.** Patterns and residue are regexes; the
  suite is minutes.

Budget exhaustion still commits. The loop is in-process and cooperative, so
there is no external killer and no mid-write kill — which turns "the executor
committed before verify" from an inference drawn from `git.is_clean()` into a
guarantee.

`ok=False` means *the executor itself broke* — transport, auth, an unhandled
exception. Every substantive verdict still comes from verify. The gates run
here to save round trips, not to reach judgements.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from orchestrator import gates
from orchestrator.config import ProjectConfig, Stage
from orchestrator.edittools import FileEditor
from orchestrator.executor import ExecutionResult
from orchestrator.gitops import Git, GitError


def _digest(git: Git, since_sha: str) -> str:
    from orchestrator.verify import diff_digest

    return diff_digest(git, since_sha)


def run_loop(
    stage: Stage,
    cfg: ProjectConfig,
    git: Git,
    runner,
    model,
    reader,
    editor: FileEditor,
    semantic=None,
    since_sha: str = "",
    conversation: list | None = None,
    cache_key: str | None = None,
    log=None,
) -> ExecutionResult:
    """Run edit cycles until the gates pass or a budget runs out."""
    out = ExecutionResult(ok=True)
    conversation = conversation if conversation is not None else []
    started = time.time()
    deadline = started + max(cfg.executor.request_timeout_seconds, 1.0)
    previous_digest: str | None = None

    for cycle in range(max(cfg.executor.max_cycles, 1)):
        if time.time() >= deadline:
            out.timed_out = True
            break

        turn = model.run(
            conversation,
            reader=reader,
            editor=editor,
            semantic=semantic,
            cache_key=cache_key,
        )
        out.model_turns += turn.turns
        out.cycles = cycle + 1
        if cycle == 0:
            out.first_prompt_tokens = turn.first_prompt_tokens
            out.first_cached_tokens = turn.first_cached_tokens
        # The high-water mark, across cycles as well as turns. Aider reported
        # this and `context_tokens_from_log` took the largest for the reason
        # its docstring gives: what bounds the next stage is the peak, not the
        # last figure it happened to print. Keeping the same quantity is what
        # lets the series continue across the cutover rather than silently
        # changing instrument. A rework cycle routinely loads more than the
        # first did, so `max` spans them.
        out.context_tokens = max(out.context_tokens, turn.peak_prompt_tokens)
        if turn.usage is not None:
            out.usage = _merge(out.usage, turn.usage)
        out.log = turn.text or out.log

        if turn.failure:
            # The executor itself broke. Whatever it had already committed
            # stands; this is the one path that reports `ok=False`.
            out.ok = False
            out.log = turn.failure
            break

        out.edits_applied = sum(
            c.lines for c in editor.calls if c.tool == "edit" and not c.refusal
        )
        out.edit_refusals = [
            f"{c.tool}({c.detail}): {c.refusal}" for c in editor.calls if c.refusal
        ]

        # Read at last. Its own comment said the loop must not treat this as
        # finished, and the loop did exactly that because nothing consulted it.
        out.turns_exhausted = not turn.stopped

        if not editor.touched:
            # The model stopped without changing anything. Not adjudicated
            # here: the scope gate already owns the sentence "the attempt
            # produced no changes", and two places saying it is how they drift.
            break

        failure = _gate_cycle(stage, cfg, git, runner, out, since_sha, log=log)
        if failure is None:
            return out

        out.in_loop_failures.append(f"cycle {cycle + 1}: {failure.summary}")

        digest = _digest(git, since_sha)
        if previous_digest is not None and digest == previous_digest:
            # Two cycles, same tree. `_layer_progress`'s reasoning one level
            # down: spending another cycle to learn nothing is the same waste
            # at either altitude.
            break
        previous_digest = digest

        conversation.append(
            {
                "role": "user",
                "content": [{"type": "input_text", "text": failure.feedback}],
            }
        )

    _commit_if_dirty(git, stage, out)
    out.cost_usd = _price(out.usage, cfg.executor.model)
    return out


# The rate table, fetched at most once per process. `load_price_map` reaches
# the network on every call and only then falls back to its cache file — fine
# for the report, which runs once, and wrong here: this runs per attempt, and a
# 90-stage run would make hundreds of HTTP calls to price something whose rates
# do not change while it runs. Rebuilt on the next start, which is when a new
# rate would matter anyway.
_PRICES: dict | None = None


def _prices() -> dict:
    global _PRICES
    if _PRICES is None:
        from orchestrator.pricing import load_price_map
        from orchestrator.report import PRICE_MAP_FILENAME

        _PRICES = load_price_map(
            os.environ.get("ORCHESTRATOR_PRICE_MAP") or PRICE_MAP_FILENAME
        )
    return _PRICES


def _price(usage, model: str | None) -> float | None:
    """What this attempt cost, from the provider's own counts.

    `None` for an unpriced model rather than `0.0`, which is the whole reason
    to compute this instead of reading a tool's report: a zero has meant "no
    rate for this model" as often as it has meant "free", and a local endpoint
    and a missing price were indistinguishable in the record.

    Priced here rather than in `nodes` because this is where the usage is, and
    the same reasoning that put `price_usage` in one place applies: the report
    already bills the planner and reviewer through it, and a second arithmetic
    for the executor would be a second thing to get wrong. `advance` guards on
    this being truthy before writing `stage-costs.md`, so an unpriced model
    still lands there on the strength of `context_tokens`.
    """
    if usage is None:
        return None
    from orchestrator.pricing import entry_for, price_usage

    prices = _prices()
    return price_usage(
        entry_for(prices, model),
        getattr(usage, "prompt_tokens", 0),
        getattr(usage, "cached_tokens", 0),
        getattr(usage, "cache_write_tokens", 0),
        getattr(usage, "completion_tokens", 0),
    )


def _gate_cycle(stage, cfg, git, runner, out: ExecutionResult, since_sha, log=None):
    """Lint, commit, then the gates — in that order, for the reasons above."""
    lint = gates.run_checks(stage, runner)
    changed = _commit_if_dirty(git, stage, out, why=", after checks")
    if changed and log:
        log(f"[execute] checks rewrote files; committed as {changed[:12]}")
    if not lint.ok:
        out.gate_records.pop("checks", None)
        return lint

    # The checks passed on the tree as it stands *after* their own rewrites
    # were committed, which is the tree the gate will see.
    out.gate_records["checks"] = {"command": "", "head_sha": git.head_sha()}

    for name, found in (
        ("patterns", gates.check_patterns(stage, cfg, git, since_sha)),
        ("residue", gates.check_residue(stage, cfg, git)),
        ("new_tests", gates.check_new_tests(stage, cfg, git, since_sha)),
        (
            "tests",
            gates.run_tests(
                stage, cfg, git, runner, since_sha,
                # The editor refuses an out-of-scope write, so the reason the
                # subprocess path was denied the full suite does not apply.
                for_loop=True, allow_full_suite=True,
            ),
        ),
    ):
        if not found.ok:
            # Recorded, not discarded. A failure is as much an answer as a
            # pass: the gate would run the same command on the same tree and
            # reach the same verdict, which is 14s of specs to learn something
            # already known. Only greens were kept at first, which left the
            # duplication in place on exactly the path where attempts are
            # slowest — a stage that is struggling runs the gate most often.
            out.gate_records.pop("tests", None)
            if found.command:
                out.gate_records[name] = {
                    "command": found.command,
                    "head_sha": found.head_sha or git.head_sha(),
                    "failed": True,
                    "summary": found.summary,
                    "feedback": found.feedback,
                    "failing_paths": list(found.failing_paths),
                }
            else:
                out.gate_records.pop(name, None)
            return found
        if found.command:
            out.gate_records[name] = {
                "command": found.command,
                "head_sha": found.head_sha or git.head_sha(),
            }
    return None


def _commit_if_dirty(
    git: Git, stage: Stage, out: ExecutionResult, why: str = ""
) -> str | None:
    """Commit whatever is in the tree, and remember the sha.

    Called after the checks and again on the way out, so a loop that ran out of
    budget still leaves committed work rather than a dirty tree the next
    stage's precheck refuses to cut a branch over.

    The message carries the cycle and what prompted the commit. All of these
    are squashed on landing, so the project branch is unaffected — but a stage
    branch is what you read when a stage misbehaves, and five commits all
    saying `[stage-id] executor` cannot tell cycle 1 from cycle 3, or the
    model's edits from a formatter's rewrite of them.
    """
    try:
        if git.is_clean():
            return None
        sha = git.commit_all(f"[{stage.id}] executor cycle {out.cycles}{why}")
    except GitError:
        # A failure to commit is not a failure of the work, and the gates
        # judge the tree either way. Reported through the log rather than
        # turned into a verdict here.
        return None
    if sha:
        out.commits.append(sha)
    return sha


def _merge(left, right):
    from orchestrator.openaiclient import TokenUsage, merge_usage

    if left is None:
        return right
    if not isinstance(left, TokenUsage):
        return right
    return merge_usage(left, right)


def build_loop_parts(stage: Stage, cfg: ProjectConfig, repo: Path):
    """The reader and editor one attempt gets.

    The reader is *live*, not pinned to a sha: the executor is editing the
    working tree, and a pinned reader would show it the file as it was before
    its own edit.

    Nothing here relaxes what may be read. `RepoReader` admits a file that
    is untracked but unignored for every caller, because that is git's own
    line between "part of the project" and "deliberately kept out" — and the
    executor is simply the caller that most often has such files, having just
    written them.
    """
    from orchestrator.repotools import ReadBudget, RepoReader
    from orchestrator.semantic import SemanticSearch, SemanticSearchConfig

    reader = RepoReader(
        Git(repo),
        repo,
        ReadBudget(
            max_lines_per_call=cfg.executor.max_read_lines_per_call,
            max_total_lines=cfg.executor.max_read_lines_total,
            max_calls=cfg.executor.max_read_calls,
        ),
    )
    editor = FileEditor(repo=repo, edit_files=list(stage.edit_files))

    # The index, when one is configured — as a locator for failed edits only,
    # never as a tool. `editor.calls` is shared so the lookup appears in the
    # attempt's ledger under its own name rather than vanishing.
    semantic = None
    search_cfg = SemanticSearchConfig.from_mapping(cfg.executor.semantic_search)
    if search_cfg is not None:
        # One instance for two jobs: the tool the model may call, and the
        # locator consulted when an edit misses. Sharing `editor.calls` means
        # both land in the attempt's ledger, distinguished by name — the
        # model's own lookups as `semantic_search`, the locator's as `locate`.
        semantic = SemanticSearch(search_cfg, calls=editor.calls)
        editor.locator = lambda path, want: semantic_locator(semantic, path, want)

    return reader, editor, semantic


def semantic_locator(semantic, path: str, want: str) -> list[str]:
    """Known-real text from the index, for the editor to anchor on.

    Returns the chunks' own content rather than their line spans. Spans were
    the first design and were the wrong shape twice over: a span is a chunk
    boundary rather than the start of what was wanted, and narrowing the same
    matcher to a region it had already scanned buys only a lower threshold.

    What the index actually has that is worth anything here is *text that was
    once in the file*. By this point the model's `old_string` has failed to
    match, so it is known wrong; the indexed content is known to have been
    right at some commit. Its lines are the better anchor, and the caller
    resolves them against the working tree, so nothing stale is returned.
    """
    if semantic is None:
        return []
    try:
        return semantic.chunks_for(want, path)
    except Exception:  # noqa: BLE001 - a locator that fails is simply no locator
        return []
