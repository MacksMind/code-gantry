"""Tool definitions and dispatch for the planner's look at the repository.

The schemas here are the planner's entire understanding of what it may ask.
They are prompt surface as much as interface, so the descriptions carry the two
rules that keep a lookup from becoming a belief:

**A document is a claim; the code is the fact.** The first long run blocked a
stage because the planner took "3 sites" from an upgrade checklist and the file
disagreed. That checklist is indexed, so semantic search will hand the same
stale sentence back with a high score. Counting is `search`'s job.

**Semantic search is not an existence check.** Asked which specs cover a
controller that has none, it returns the controller and two documents — correct
behaviour, four confident-looking lines, and no. `list_files` answers existence;
nearest-neighbour ranking cannot.

Dispatch converts a `ToolError` into a readable result rather than an
exception. A refusal is information the planner can act on — correct the path,
narrow the search, stop asking — and killing the call would throw away the
reasoning it had already done.
"""

from __future__ import annotations

from typing import Any

from orchestrator.repotools import RepoReader, ToolError
from orchestrator.semantic import SemanticSearch

READ_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a tracked file, or a range of its lines. Output is line "
            "numbered so you can cite what you saw.\n\n"
            "This is how a lead becomes a fact. Before you assert that a file "
            "contains something, that a spec covers something, or that a count "
            "is correct, read the lines and see."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path, e.g. app/controllers/order_controller.rb",
                },
                "start": {"type": "integer", "description": "First line, 1-based."},
                "end": {"type": "integer", "description": "Last line, inclusive."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List tracked files, optionally filtered by a glob such as "
            "'spec/**/*.rb'.\n\n"
            "This is the only tool that answers whether something exists. An "
            "empty list means it does not — do not infer a path from a naming "
            "convention and do not treat a semantic hit as proof of one. A "
            "stage that names a spec which does not exist costs an executor "
            "attempt that cannot succeed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "glob": {"type": "string", "description": "Optional glob filter."}
            },
        },
    },
    {
        "name": "search",
        "description": (
            "Search tracked files for a regular expression. Returns "
            "path:line:text.\n\n"
            "Use this to count things and to find exact identifiers. When a "
            "plan document states how many occurrences exist, that is a claim "
            "about the code as it was when someone wrote it down; this tool is "
            "the code as it is. Where they disagree, the code wins."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression."},
                "path_glob": {
                    "type": "string",
                    "description": "Optional path restriction, e.g. app/controllers.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "git_show",
        "description": "Read a file as it stood at a git ref.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["ref", "path"],
        },
    },
    {
        "name": "git_diff",
        "description": (
            "What changed between refs, optionally for one path. Use it to see "
            "what earlier stages actually did rather than what they claimed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "other": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["ref"],
        },
    },
]

SEMANTIC_TOOL: dict[str, Any] = {
    "name": "semantic_search",
    "description": (
        "Find code by meaning rather than by name. Returns ranked "
        "path:start-end citations with a two-line snippet.\n\n"
        "Use it when you do not know what the code is called — 'where is the "
        "CSV feed built' when the method is named `feed`. It ranks by "
        "similarity, so it always returns its closest guesses even when "
        "nothing relevant exists.\n\n"
        "Two things it cannot do. It is not an existence check: use "
        "list_files. And it is not evidence: every result is a pointer into a "
        "file, so read the lines before you rely on them. Its index includes "
        "plan documents, which state claims about the code that may be out of "
        "date."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What you are looking for, in plain language.",
            }
        },
        "required": ["question"],
    },
}


def tool_schemas(semantic: SemanticSearch | None) -> list[dict[str, Any]]:
    """What this project offers. Semantic search only when configured."""
    return [*READ_TOOLS, SEMANTIC_TOOL] if semantic else list(READ_TOOLS)


def dispatch(
    name: str,
    args: dict,
    reader: RepoReader,
    semantic: SemanticSearch | None,
) -> str:
    """Run one tool call and render its result as text.

    Every failure becomes a readable string rather than an exception. The
    planner must be able to recover from a bad path or an exhausted budget by
    answering with what it has.
    """
    try:
        if name == "read_file":
            return reader.read_file(
                args.get("path", ""), args.get("start"), args.get("end")
            )
        if name == "list_files":
            found = reader.list_files(args.get("glob"))
            return "\n".join(found) if found else "(no tracked files match)"
        if name == "search":
            hits = reader.search(args.get("pattern", ""), args.get("path_glob"))
            return "\n".join(hits) if hits else "(no matches)"
        if name == "git_show":
            return reader.git_show(args.get("ref", ""), args.get("path", ""))
        if name == "git_diff":
            return reader.git_diff(
                args.get("ref", ""), args.get("other"), args.get("path")
            )
        if name == "semantic_search" and semantic is not None:
            found = semantic.query(args.get("question", ""))
            return "\n".join(found) if found else "(nothing similar found)"
        return f"unknown tool {name!r}"
    except ToolError as e:
        return f"cannot do that: {e}"
