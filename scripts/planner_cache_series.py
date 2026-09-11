"""The planner's cache behaviour per derivation, as a series.

Reads every planner attempt under a project's runs and prints one row per
derivation: what the call cost in tokens, how much of it was written to the
cache and under which TTL, how big the cached block and the block after it
were, and how many stages the derivation returned. A series rather than a
mean, because the question this answers — did a change to what the planner is
sent move the per-derivation cost — is answered by comparing rows drawn under
the same conditions, and a mean over mixed conditions is a reading about the
mix.

Usage:

    uv run python scripts/planner_cache_series.py <work_dir> [--csv out.csv]

`work_dir` is the project's `.code_gantry` directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

_BLOCK = re.compile(
    r"^- message (\d+) \((\w+)\) block (\d+): ([\d,]+) chars(\s+\[cache breakpoint\])?",
    re.M,
)
_USAGE_KEYS = (
    "prompt_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
    "completion_tokens",
    "peak_prompt_tokens",
)


def _blocks(prompt_md: Path) -> list[dict]:
    """Block sizes from the rendered prompt's own header, in order."""
    if not prompt_md.is_file():
        return []
    head = prompt_md.read_text(errors="replace")[:4000]
    return [
        {
            "message": int(m.group(1)),
            "role": m.group(2),
            "block": int(m.group(3)),
            "chars": int(m.group(4).replace(",", "")),
            "breakpoint": bool(m.group(5)),
        }
        for m in _BLOCK.finditer(head)
    ]


def rows(work_dir: Path) -> list[dict]:
    out: list[dict] = []
    for run_dir in sorted((work_dir / "runs").glob("*")):
        stages = run_dir / "stages"
        if not stages.is_dir():
            continue
        for attempt in sorted(stages.glob("*-plan-rev-*-attempt-*")):
            record = attempt / "planner.json"
            if not record.is_file():
                continue
            data = json.loads(record.read_text())
            usage = data.get("usage") or {}
            blocks = _blocks(attempt / "planner-prompt.md")
            first_user = [b for b in blocks if b["message"] == 0]
            marked = [b for b in first_user if b["breakpoint"]]
            after = [b for b in first_user if not b["breakpoint"]]
            row = {
                "run": run_dir.name,
                "attempt": attempt.name,
                "verdict": data.get("verdict"),
                "stages_returned": (
                    1 + len(data.get("additional_stages") or [])
                    if data.get("stage")
                    else 0
                ),
                "block0_chars": marked[0]["chars"] if marked else None,
                "after_chars": sum(b["chars"] for b in after) if after else None,
                "blocks": len(blocks),
            }
            for key in _USAGE_KEYS:
                row[key] = usage.get(key)
            out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("--csv", type=Path, help="also write the series here")
    args = parser.parse_args(argv)

    series = rows(args.work_dir)
    if not series:
        print("no planner attempts found", file=sys.stderr)
        return 1

    fields = list(series[0].keys())
    if args.csv:
        with args.csv.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(series)

    widths = {f: max(len(f), *(len(str(r[f])) for r in series)) for f in fields}
    print("  ".join(f.ljust(widths[f]) for f in fields))
    for r in series:
        print("  ".join(str(r[f]).ljust(widths[f]) for f in fields))
    return 0


if __name__ == "__main__":
    sys.exit(main())
