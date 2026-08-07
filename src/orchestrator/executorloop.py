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
    return out


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

    Tracked-only is relaxed for paths the stage may write, and for nothing
    else. The executor must be able to read a file it has just created, which
    is untracked until the cycle's commit. The reason tracked-only exists —
    that `.env` and its kin never reach a third-party API — survives intact,
    because the relaxation is bounded by an allowlist the operator and planner
    chose and the scope gate enforces.
    """
    from orchestrator.repotools import ReadBudget, RepoReader

    reader = RepoReader(
        Git(repo),
        repo,
        ReadBudget(
            max_lines_per_call=cfg.executor.max_read_lines_per_call,
            max_total_lines=cfg.executor.max_read_lines_total,
            max_calls=cfg.executor.max_read_calls,
        ),
    )
    reader.writable_globs = list(stage.edit_files)
    editor = FileEditor(repo=repo, edit_files=list(stage.edit_files))
    return reader, editor
