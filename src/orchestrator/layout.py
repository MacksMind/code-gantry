"""A compact description of what the repository contains.

The planner authors `edit_files` and `read_files` globs. Given only the plan
documents it has no way to know whether the calculator lives in `calc.py` or
`calculator.py`, so it guesses — and a wrong guess is not a cheap mistake. It
costs a scope violation, a planner intervention to widen the stage, and it does
so on every stage until the planner happens to guess right.

Dumping `git ls-files` does not scale: the target Rails application has 4,289
tracked files and 217KB of paths, more than a thousand of them vendored assets
no stage will ever touch. So this summarises within a line budget, listing
filenames where a directory is small enough for them to be useful and falling
back to counts and examples where it is not.

Two properties matter more than compactness:

- **It must never imply a file exists.** Everything here is derived from the
  git index at the run's base sha; nothing is inferred or completed.
- **It must never imply completeness it lacks.** Whatever is dropped is
  reported as dropped, with counts, so the planner treats the block as a guide
  rather than an inventory.
"""

from __future__ import annotations

from collections import defaultdict

_ROOT = "(repository root)"


def summarize_layout(
    paths: list[str],
    *,
    max_lines: int = 400,
    per_dir_files: int = 8,
    depth: int = 2,
) -> str:
    """Render tracked paths as a directory summary within a line budget.

    Grouped to `depth` path segments, because that is the granularity globs are
    written at — `app/controllers/**`, `spec/models/**`. A per-leaf-directory
    listing of the target Rails app came to 29k tokens and spent most of them on
    662 migration filenames and a vendored rich-text editor's CSS skins, while
    truncating away ninety-nine small directories that a stage might actually
    touch. Depth-grouping inverts that: every top-level area is present, and no
    single one can crowd out the rest.

    Alphabetical, not largest-first. Size ordering puts vendored assets at the
    top and application code below the fold, which is precisely backwards; the
    count is printed either way, so nothing is lost by ordering predictably.
    """
    if not paths:
        return (
            "The repository has no tracked files at this revision. Any path a "
            "stage names will be a new file."
        )

    by_group: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        segments = path.split("/")
        group = "/".join(segments[:depth]) if len(segments) > depth else (
            "/".join(segments[:-1]) or _ROOT
        )
        by_group[group].append(path)

    ordered = sorted(by_group.items())

    header = [
        f"{len(paths):,} tracked files across {len(by_group):,} areas, as of the "
        "revision this run started from.",
        "",
        f"Grouped to {depth} path segments — the granularity globs are written "
        "at. Small areas are listed in full; larger ones show a count and "
        "examples. Write globs that match these real paths: a glob matching "
        "nothing fails the scope gate, and one matching too much fails it too.",
        "",
    ]

    body: list[str] = []
    shown = 0

    for group, members in ordered:
        if len(header) + len(body) + 3 > max_lines:
            break
        members = sorted(members)
        if len(members) <= per_dir_files:
            body.append(f"- `{group}/` ({len(members)})")
            body.append("  " + ", ".join(members))
        else:
            examples = ", ".join(members[:per_dir_files])
            body.append(f"- `{group}/` ({len(members)} files)")
            body.append(f"  e.g. {examples}, …")
        shown += 1

    lines = header + body

    if shown < len(ordered):
        omitted_areas = len(ordered) - shown
        omitted_files = sum(len(m) for _, m in ordered[shown:])
        lines.append("")
        lines.append(
            f"Truncated: {omitted_areas:,} further areas holding "
            f"{omitted_files:,} files are not shown. They exist — this listing "
            "is a guide, not an inventory."
        )

    return "\n".join(lines[:max_lines])
