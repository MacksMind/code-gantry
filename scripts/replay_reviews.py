"""Re-review stages that already landed, with a reviewer that can read.

Out of band and advisory. It changes nothing, gates nothing, and touches no
branch — it answers one question: what would the reviewer have said if it could
have looked?

That question is worth asking because 8 of the 31 stages of run
20260803-032610 deleted an `attr_accessible` declaration, which is safe exactly
when a permit list elsewhere covers the same attributes. That file is not in
the diff, and the reviewer had no tools, so all 8 were approved by a gate that
could not have said anything else.

**Fidelity is the whole point, and it is easy to get wrong in a flattering
direction.** Three things are pinned to the stage rather than taken from now:

- The repository is read at the stage's own landing commit. Reading the branch
  tip would let a review approve a deletion because a permit list landed
  twenty stages later — the right answer for the wrong reason, and
  indistinguishable from judgement.
- The progress log is taken from the *parent* commit. Review runs before
  `advance`, so the log the reviewer saw did not yet contain the entry its own
  stage was about to add.
- The diff excludes the plan addendum, for the same reason: that hunk is
  written after the verdict.

The plan snapshot is the run's own, so it is already the tree every stage was
judged against.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.config import load_config  # noqa: E402
from orchestrator.gitops import Git  # noqa: E402
from orchestrator.plandoc import load_snapshot  # noqa: E402
from orchestrator.prompts import build_review_messages  # noqa: E402
from orchestrator.repotools import ReadBudget, RepoReader  # noqa: E402
from orchestrator.reviewer import OpenAIReviewer, _build_openai_client  # noqa: E402
from orchestrator.runtime import ProjectPaths, RunPaths  # noqa: E402
from orchestrator.semantic import SemanticSearch, SemanticSearchConfig  # noqa: E402


def landed_stages(run_log: Path) -> list[tuple[str, str]]:
    """(stage_id, merge_sha) in landing order, read from the run log.

    The log is the record of what actually happened, which is what this is
    replaying. `completed` in the checkpoint would do as well; the log needs no
    database open and is legible when this disagrees with expectation.
    """
    out = []
    for line in run_log.read_text(errors="replace").splitlines():
        if "[advance] " in line and " landed as " in line:
            body = line.split("[advance] ", 1)[1]
            stage_id, sha = body.split(" landed as ", 1)
            out.append((stage_id.strip(), sha.strip()))
    return out


def stage_spec(stages_dir: Path, stage_id: str) -> dict | None:
    """The stage as the planner authored it.

    Read from `planner.json` rather than reconstructed, so the replay judges
    the instruction that was actually given — including any revision, since the
    last plan artifact naming this stage is the one it landed under.
    """
    found = None
    for path in sorted(stages_dir.glob("*/planner.json")):
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        stage = data.get("stage") or {}
        if stage.get("id") == stage_id:
            found = stage
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id")
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", default=None, help="Where to write the verdicts.")
    ap.add_argument("--limit", type=int, default=0, help="Replay only the first N.")
    ap.add_argument("--only", default="", help="Replay one stage id.")
    args = ap.parse_args()

    project = ProjectPaths(args.project)
    cfg = load_config(project.config)
    paths = RunPaths(project, args.run_id)
    git = Git(cfg.target_repo)

    if not cfg.reviewer.repo_access:
        print("reviewer.repo_access is off; the replay would prove nothing", file=sys.stderr)
        return 2

    plan = load_snapshot(project.plan_snapshot)
    addendum = cfg.plan_addendum_path
    stages = landed_stages(paths.run_log)
    if args.only:
        stages = [s for s in stages if s[0] == args.only]
    if args.limit:
        stages = stages[: args.limit]

    print(f"replaying {len(stages)} stage(s) of {args.run_id}\n")

    client = _build_openai_client(cfg.reviewer)
    results = []
    completed: list[dict] = []

    for index, (stage_id, sha) in enumerate(stages):
        fields = stage_spec(paths.run_dir / "stages", stage_id)
        if fields is None:
            print(f"  {stage_id}: no stage spec found, skipped")
            continue
        stage = cfg.stage_from_planner(fields)

        # The diff the reviewer saw: the stage's own work, without the plan
        # note that `advance` appends after the verdict.
        diff_args = ["diff", f"{sha}^", sha]
        if addendum:
            diff_args += ["--", ".", f":(exclude){addendum}"]
        diff = git._run(*diff_args, check=False).stdout

        # The log as it stood before this stage landed.
        log_text = None
        if addendum:
            try:
                log_text = git.show_file(f"{sha}^", addendum)
            except Exception:
                log_text = None

        # A reader pinned to this stage's tree, with the configured budget.
        reader = RepoReader(
            git,
            cfg.target_repo,
            ReadBudget(
                max_lines_per_call=cfg.reviewer.max_read_lines_per_call,
                max_total_lines=cfg.reviewer.max_read_lines_total,
                max_calls=cfg.reviewer.max_read_calls,
            ),
            at_sha=sha,
        )
        search_cfg = SemanticSearchConfig.from_mapping(cfg.reviewer.semantic_search)
        semantic = (
            SemanticSearch(search_cfg, calls=reader.calls) if search_cfg else None
        )

        messages = build_review_messages(
            stage=stage,
            cfg=cfg,
            diff=diff,
            plan=plan,
            completed=completed,
            progress_log=log_text,
        )
        reviewer = OpenAIReviewer(
            cfg.reviewer, client=client, reader=reader, semantic=semantic
        )
        # A cache key of its own: this is not the run's prompt sequence and
        # should not evict or be confused with it.
        outcome = reviewer.review(messages, cache_key=f"replay:{args.run_id}")

        mark = {"approved": "  ok  ", "rework": "REWORK", "blocked": "BLOCK "}.get(
            outcome.verdict, "  ?   "
        )
        print(
            f"[{mark}] {stage_id}  ({len(outcome.tool_calls)} read, "
            f"{len(outcome.observations)} obs, {outcome.usage.prompt_tokens} prompt, "
            f"{outcome.usage.cached_tokens} cached)"
        )
        if outcome.verdict != "approved":
            print(f"          {outcome.summary}")
        for obs in outcome.observations:
            print(f"          + {obs.file}: {obs.finding}")

        results.append(
            {
                "index": index,
                "stage_id": stage_id,
                "merge_sha": sha,
                "verdict": outcome.verdict,
                "summary": outcome.summary,
                "issues": [i.model_dump() for i in outcome.issues],
                "observations": [o.model_dump() for o in outcome.observations],
                "tool_calls": outcome.tool_calls,
                "client_failure": outcome.failed,
                "usage": {
                    "prompt_tokens": outcome.usage.prompt_tokens,
                    "cached_tokens": outcome.usage.cached_tokens,
                    "completion_tokens": outcome.usage.completion_tokens,
                },
            }
        )

        # The history grows as it did during the run, so each replayed review
        # sees what its original saw.
        completed.append(
            {
                "id": stage_id,
                "index": index,
                "instruction": stage.instruction,
                "merge_sha": sha,
                "revisions": 0,
            }
        )

    out = Path(args.out) if args.out else paths.run_dir / "replay-reviews.json"
    out.write_text(json.dumps(results, indent=2))

    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"\n{counts} across {len(results)} stage(s)")
    print(f"observations: {sum(len(r['observations']) for r in results)}")
    print(f"written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
