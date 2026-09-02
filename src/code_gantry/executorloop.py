"""The in-process edit cycle: model, then the hook, then lint, then commit, then gates.

One cycle is *edit until the model stops asking for things*, then ask the
commit hook, then lint, then commit, then the gates it can act on. Ordering is
load-bearing at every step:

- **The hook first, because it is the only question that expires.** A
  pre-commit hook reads the index and refuses; once the commit has been
  attempted and refused there is nothing left to do but escalate, which is what
  used to happen. Asked before the commit, the same refusal is feedback the
  model acts on in session. It cannot go later and it cannot be an operator's
  `checks` entry, because those run after the commit it would be repairing.
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

Against everything except the repository itself, which can refuse. A commit
hook broke that guarantee once and said nothing: `commits: []` against 15
edits, `in_loop_failures: []`, and an attempt that reported itself clean, after
which `verify` swept the model's work up under a message crediting it to the
linter. `commit_refused` carries it out now and `execute` escalates on it —
because the caller cannot ask `git.is_clean()` here, on the strength of this
very paragraph.

`ok=False` means *the executor itself broke* — transport, auth, an unhandled
exception. Every substantive verdict still comes from verify. The gates run
here to save round trips, not to reach judgements.
"""

from __future__ import annotations

import time
from pathlib import Path

from code_gantry import gates
from code_gantry.repotools import ToolError
from code_gantry.config import ProjectConfig, Stage
from code_gantry.edittools import FileEditor
from code_gantry.executor import ExecutionResult
from code_gantry.gitops import Git, GitError


def _digest(git: Git, since_sha: str) -> str:
    from code_gantry.verify import diff_digest

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
        # The high-water mark, across cycles as well as turns, for the reason
        # stage sizing needs: what bounds the next stage is the peak, not the
        # last figure it happened to print. Keeping the same quantity is what
        # lets the series continue across the cutover rather than silently
        # changing instrument. A rework cycle routinely loads more than the
        # first did, so `max` spans them.
        out.context_tokens = max(out.context_tokens, turn.usage.peak_prompt_tokens)
        if turn.usage is not None:
            out.usage = _merge(out.usage, turn.usage)
        # Accumulated across cycles as well as turns, because a rework is the
        # likeliest place for a router to change its mind: minutes have passed,
        # and the stickiness that keeps a conversation on one model is a
        # five-minute window.
        for served, count in turn.served_models.items():
            out.served_models[served] = out.served_models.get(served, 0) + count
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
        out.turn_end = turn.turn_end
        out.empty_finishes += turn.empty_finishes
        # Assigned rather than accumulated: the question this answers is why
        # the attempt is over, and only the cycle that ended it has an answer.
        out.unproductive_stop = turn.unproductive_stop or out.unproductive_stop
        if turn.unproductive_stop and not out.log:
            # Only when the model left no account of its own. An attempt that
            # said something before it started going in circles has already
            # said the more useful thing; an attempt that did not would
            # otherwise reach the planner as the scope gate's "produced no
            # changes", which is the misdiagnosis this field exists to end.
            out.log = turn.unproductive_stop

        if turn.replan_kind:
            # Before the no-changes check below, because the two shapes overlap
            # exactly where it matters: an executor that finds the stage
            # unsatisfiable often has nothing to commit, and letting that fall
            # through would report the useful reason as "produced no changes"
            # — which is the misdiagnosis this tool exists to end.
            out.replan_kind = turn.replan_kind
            out.replan_reason = turn.replan_reason
            break

        if not editor.touched:
            # The model stopped without changing anything. Not adjudicated
            # here: the scope gate already owns the sentence "the attempt
            # produced no changes", and two places saying it is how they drift.
            #
            # What the gate cannot supply is which nothing this was. An attempt
            # that ended in silence and one that decided there was nothing left
            # to do arrive at it identically, and the artifact that exists to
            # answer *why did this attempt end* carried `log: ""` for every
            # one of the six measured on one run. The routing is unchanged; the
            # reason travels beside it.
            if out.empty_finishes and not out.log:
                out.log = (
                    f"the model ended {out.empty_finishes} turn(s) with no tool "
                    "calls and no text, and changed nothing"
                )
            break

        try:
            failure = _gate_cycle(stage, cfg, git, runner, out, since_sha, log=log)
        except ToolError as e:
            # The gate could not be built, which is a different thing from the
            # gate failing: a `ToolError` here is `build_argv` refusing the
            # pipeline's own command, and no cycle of the model's can change
            # what the config can express. Recorded and stopped, not raised —
            # raised, it ended the process. `_gate_cycle` commits before it
            # gates, so the work is on the branch for the resume.
            if log:
                log(f"[execute] {stage.id}: a gate could not be run: {e}")
            out.gate_unrunnable = str(e)
            break
        if failure is None:
            # `break`, not `return`. Returning here skipped the two statements
            # below, so the *success* path — an attempt whose gates passed
            # first try — was the one that never got priced, and only attempts
            # that failed a gate or edited nothing carried a cost at all.
            # Measured across one run's 57 recorded attempts: 41 had real usage
            # and `cost_usd == 0`, together 17,943,722 prompt tokens against
            # 77,402,051 billed, the largest single one 2,319,957.
            #
            # Three tests asserted the pricing and all were green, because
            # their fixture makes no edits and so leaves on `not
            # editor.touched` — out of the bottom, where the pricing is. The
            # branch they were written for was never the branch that ran.
            #
            # `_commit_if_dirty` running now is a no-op: `_gate_cycle` commits
            # before it gates, so a passing gate leaves a clean tree.
            break

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

    _commit_if_dirty(git, stage, out, log=log)
    out.cost_usd = _price(cfg, out.usage, cfg.executor.model)
    return out


def _price(cfg, usage, model: str | None) -> float | None:
    """What this attempt cost, from the provider's own counts.

    `None` for an unpriced model rather than `0.0`, which is the whole reason
    to compute this instead of reading a tool's report: a zero has meant "no
    rate for this model" as often as it has meant "free", and a local endpoint
    and a missing price were indistinguishable in the record.

    The memo that used to sit above this function is `cached_price_map` now.
    It was here because this runs per attempt and the loader reaches the
    network on every call; it is in the loader because `nodes` ran the same
    risk per landing and had no memo at all.

    Priced here rather than in `nodes` because this is where the usage is, and
    the same reasoning that put `price_usage` in one place applies: the report
    already bills the planner and reviewer through it, and a second arithmetic
    for the executor would be a second thing to get wrong. `advance` guards on
    this being truthy before writing `stage-costs.md`, so an unpriced model
    still lands there on the strength of `context_tokens`.
    """
    if usage is None:
        return None

    # A gateway that bills us reports what it billed, and that beats deriving
    # the same number from a rate table — the fact rather than the label. Under
    # a router it is not merely better, it is the only answer available:
    # `openrouter/pareto-code` picks the model per request and has no price of
    # its own, so `entry_for` would be asked about a model that resolves after
    # the call it is meant to price.
    #
    # Checked with `is not None` rather than for truthiness, because a reported
    # zero is a real answer and the whole point of this function is that a zero
    # from nowhere is not.
    from code_gantry import pricing

    reported = getattr(usage, "provider_cost_usd", None)
    if reported is not None:
        return reported

    cached_price_map = pricing.cached_price_map
    entry_for = pricing.entry_for
    price_usage = pricing.price_usage

    prices = cached_price_map(cfg)
    return price_usage(
        entry_for(prices, model),
        getattr(usage, "prompt_tokens", 0),
        getattr(usage, "cached_tokens", 0),
        getattr(usage, "cache_write_tokens", 0),
        getattr(usage, "completion_tokens", 0),
    )


LINT_REWRITE_CHARS = 4_000


def _with_lint_rewrite(failure, diff: str):
    """Tell the model what the checks changed after it stopped editing.

    `checks` autocorrect — `rubocop -A`, `eslint --fix`, `gofmt -w` — and they
    run once the model has stopped asking for things. The tree moves under a
    conversation that is already finished, so the next cycle opens with the
    model holding file contents that are no longer on disk. It cannot see that
    its edit was rewritten; from where it sits it made the change and the gate
    is complaining anyway.

    Measured on one stage: the instruction required `Date.today` and forbade
    `Time.zone.today`, and `Rails/Date` rewrites the first into the second. The
    executor made the edit, the linter undid it, the patterns gate saw the
    forbidden spelling still there, and the same diff came back twice. Three
    planner revisions and about thirty-five minutes, and the planner only
    escaped by inferring the cause from the repetition — nothing told it, and
    nothing told the executor.

    Attributed to the tool in as many words. Handed the diff without being told
    whose it is, a model reads it as its own mistake and tries the same edit
    again, which is the loop this exists to break.
    """
    if not diff.strip():
        return failure
    from code_gantry.commands import clip_for_model

    failure.feedback = (
        f"{failure.feedback}\n\n"
        "After you stopped editing, the project's `checks` ran with "
        "autocorrection and rewrote part of your work. This is their diff, not "
        "yours — the tree now reads as the right-hand side:\n\n"
        f"{clip_for_model(diff, LINT_REWRITE_CHARS)}\n\n"
        "If an instruction requires a spelling the checks rewrite, no edit can "
        "satisfy it. Say so rather than making the same change again."
    )
    return failure


def _note_uncorrectable(failure, diff: str):
    """A check that failed having rewritten nothing is reporting your work.

    The complement of `_with_lint_rewrite`, and only ever applied where that
    one has nothing to say. An autocorrecting linter — `rubocop -A` and its
    kin — that exits non-zero having changed no file is reporting an offence
    it *cannot* fix; this project's own conventions document names the shape,
    a cop with no autocorrection on a strong-parameter permit list. Silence
    leaves that indistinguishable from a check that fixed nothing because
    something is broken, and `_layer_checks` routes an ordinary non-zero to
    the executor either way — which is how an environment failure once spent
    42 minutes being reported to a model as its own defect.

    **On the checks branch alone**, which is the whole of why this is not
    inside `_with_lint_rewrite`. That helper runs on every gate failure in the
    cycle, so a *test* failure with clean checks would otherwise be told the
    checks changed no file — true, irrelevant, and about a gate that passed.
    A test pinned that and caught it.

    States what was observed rather than what it means. Whether a given entry
    autocorrects at all is operator knowledge that reaches no field here, so
    the conclusion is left to the reader who can see the offence above it.
    """
    if diff.strip():
        return failure
    failure.feedback = (
        f"{failure.feedback}\n\nThe project's `checks` ran after you stopped "
        "editing and **changed no file**. Whatever they report above is not "
        "something they can correct for you, so it has to be dealt with in "
        "the code — fixed, or suppressed the way this project suppresses it, "
        "or reported as impossible if it is neither."
    )
    return failure


def _gate_cycle(stage, cfg, git, runner, out: ExecutionResult, since_sha, log=None):
    """Commit, lint, commit the rewrite, then the gates — in that order.

    The model's work is committed *before* the checks run, so whatever they
    then change is the unstaged remainder and gets a commit of its own. Both
    are squashed on landing, so the project branch is unaffected; what it buys
    is on the stage branch, which is what you read when a stage misbehaves.

    The first version staged instead of committing. That isolated the rewrite
    just as well and held it in a local variable, so it survived only as far as
    the feedback that used it — and on the success path there is no feedback,
    which is every cycle that worked. `git` could not answer "what did the
    linter change here" afterwards, because the two halves had been folded into
    one commit and nothing else had written the split down.
    """
    # Above the commit, because it is the only gate whose answer stops being
    # obtainable once the commit has been attempted. A hook refusing here used
    # to set `commit_refused` and escalate to a human — right, given that by
    # then the loop had nowhere to route it, and unnecessary, because the hook
    # names the file and line and that is exactly what feedback is for. It also
    # cannot be delegated to `checks`: those run *below* this line, so an
    # autocorrecting entry is downstream of the commit being refused.
    hook = gates.check_commit_hook(git)
    if not hook.ok:
        return hook

    _commit_if_dirty(git, stage, out, log=log)
    lint = gates.run_checks(stage, runner)
    rewritten = git.diff_unstaged()
    rewrote = _commit_if_dirty(git, stage, out, why=", after checks", log=log)
    if rewrote and log:
        log(f"[execute] checks rewrote files; committed as {rewrote[:12]}")
    if not lint.ok:
        out.gate_records.pop("checks", None)
        return _with_lint_rewrite(_note_uncorrectable(lint, rewritten), rewritten)

    # The checks passed on the tree as it stands *after* their own rewrites
    # were committed, which is the tree the gate will see.
    out.gate_records["checks"] = {"command": "", "head_sha": git.head_sha()}

    for name, call in (
        ("patterns", lambda: gates.check_patterns(stage, cfg, git, since_sha)),
        ("residue", lambda: gates.check_residue(stage, cfg, git)),
        ("new_tests", lambda: gates.check_new_tests(stage, cfg, git, since_sha)),
        # Immediately before the tests, because they are the only entry that
        # needs it and the cheap gates decide most failures without it.
        ("setup", lambda: gates.run_setup(stage, cfg, runner)),
        (
            "tests",
            lambda: gates.run_tests(
                stage, cfg, git, runner, since_sha,
                # The editor refuses an out-of-scope write, so the reason the
                # subprocess path was denied the full suite does not apply.
                for_loop=True, allow_full_suite=True,
            ),
        ),
    ):
        # Called here rather than built into the tuple. A tuple literal
        # evaluates every element before the loop body sees the first, so this
        # ran the whole suite even when `patterns` had already failed — the
        # ordering was cheapest-first and the saving was never taken. Harmless
        # while every entry was a pure question; not harmless once one of them
        # restarts containers.
        found = call()
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
            # Recorded above without it, deliberately. `gate_records` is a
            # cache keyed on the tree, and a rewrite that is true of this
            # cycle would be stale the moment the tree moves again; the
            # feedback is what the model reads, and only that needs it.
            #
            # This is the branch the incident actually took: `patterns` fired
            # because the linter had put the forbidden spelling back, and the
            # model was shown a gate failure with no way to know why.
            return _with_lint_rewrite(found, rewritten)
        if found.command:
            out.gate_records[name] = {
                "command": found.command,
                "head_sha": found.head_sha or git.head_sha(),
            }
    return None


def _commit_if_dirty(
    git: Git, stage: Stage, out: ExecutionResult, why: str = "", log=None
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
    except GitError as e:
        # A hook refusing staged content is the ordinary cause. Not raised —
        # whatever the model did is still in the tree and the caller decides
        # what that means — but recorded twice over, because this was silent
        # and the silence is what made it expensive.
        #
        # The comment here used to say the log carried it, and the function had
        # no `log` to write to. So an attempt that committed nothing against 15
        # edits reported itself clean, and `verify` later swept the model's
        # work up under a message crediting it to the linter.
        if log:
            log(f"[execute] {stage.id}: the repository refused the commit: {e}")
        out.commit_refused = str(e)
        return None
    if sha:
        out.commits.append(sha)
    return sha


def _merge(left, right):
    from code_gantry.openaiclient import TokenUsage, merge_usage

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
    from code_gantry.repotools import ReadBudget, RepoReader
    from code_gantry.semantic import SemanticSearch, SemanticSearchConfig

    reader = RepoReader(
        Git(repo),
        repo,
        ReadBudget(
            max_lines_per_call=cfg.executor.max_read_lines_per_call,
            max_total_lines=cfg.executor.max_read_lines_total,
                max_total_chars=cfg.executor.max_read_chars_total,
            max_calls=cfg.executor.max_read_calls,
        ),
    )
    editor = FileEditor(
        repo=repo,
        edit_files=list(stage.edit_files),
        no_direct_edit=[
            (entry.path_glob, entry.reason)
            for entry in cfg.executor.no_direct_edit
        ],
    )

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
        semantic = SemanticSearch(search_cfg, ledger=editor.calls)
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
