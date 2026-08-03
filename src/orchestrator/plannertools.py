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
                    # No example path. This string is sent to the planner, and
                    # an example drawn from one stack is a hint about a repo it
                    # may not be looking at — the layout block already shows it
                    # what this project's paths look like.
                    "description": "Repository-relative path.",
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
            "Search tracked files for a regular expression, over the working "
            "tree as it stands. Returns path:line:text.\n\n"
            "Exact, current, and authoritative. Use it to count occurrences "
            "and to find identifiers you can already name. When a plan "
            "document states how many of something exist, that is a claim "
            "about the code as it was when someone wrote it down; this tool is "
            "the code as it is. Where they disagree, the code wins.\n\n"
            "Its limit is that it can only find what you can already spell. A "
            "sweep is only as complete as the list of patterns you thought to "
            "search for — so when the question is *what else is like this*, or "
            "*is my list complete*, that is `semantic_search`, not this. "
            "Observed: a removal was declared finished after searching for the "
            "five spellings the plan named, and the sixth kind of usage — one "
            "nobody had listed — broke the build."
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
        "path:start-end citations with a score and a two-line snippet.\n\n"
        "Ask it questions phrased as behaviour or concept, not as names: "
        "'where is the CSV feed built' when the method is called `feed`. Its "
        "best use is the question `search` cannot answer — **have I found "
        "every kind of this?** Before declaring a sweep complete, or before "
        "trusting a plan document's list of call sites, ask for the concept "
        "and see whether anything comes back that your patterns did not "
        "match.\n\n"
        "Do not reach for it merely because an identifier is unfamiliar. If "
        "you can spell the thing, `search` is faster and exact.\n\n"
        "Three limits. It ranks by similarity and always returns its closest "
        "guesses, so a result is not evidence that anything matched — judge by "
        "whether the snippets answer the question, never by rank or by the "
        "fact that something came back. It is not an existence check: use "
        "`list_files`. And it is not proof of content: every result is a "
        "pointer, so `read_file` the lines before relying on them.\n\n"
        "It searches an index the project maintains separately from the "
        "repository, so it is a snapshot rather than the current tree, and how "
        "closely it tracks the code is a property of that project rather than "
        "of this tool. Use it to find out what exists; confirm what the code "
        "says with `search` or `read_file`. The index includes plan documents, "
        "which state claims about the code that may be out of date."
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


def openai_tool_schemas(semantic: SemanticSearch | None) -> list[dict[str, Any]]:
    """The same tools, in the shape the other provider's API wants.

    One definition, two renderings. The descriptions are the part that matters
    — they carry the rules that keep a lookup from becoming a belief — and they
    must not fork, because a reviewer told something different from the planner
    would be reasoning from a different contract about the same repository.

    Only the envelope differs: Anthropic takes `input_schema` at the top level,
    OpenAI wraps the whole thing in a `function` object and calls it
    `parameters`.

    The tools must be `strict`, and not by preference — the SDK refuses to
    auto-parse a structured response otherwise, with "Only `strict` function
    tools can be auto-parsed". Strict mode in turn requires every property to
    appear in `required` and `additionalProperties: false`, which these schemas
    do not satisfy: `read_file` takes an optional line range, `search` an
    optional path filter.

    So the optional ones are made nullable and required, which is the shape
    strict mode provides for "may be omitted". `dispatch` already reads them
    with `.get`, so a null arrives as a missing argument and nothing
    downstream can tell the difference.
    """
    out = []
    for tool in tool_schemas(semantic):
        schema = tool["input_schema"]
        properties = {}
        for name, spec in (schema.get("properties") or {}).items():
            if name in (schema.get("required") or []):
                properties[name] = spec
                continue
            kind = spec.get("type", "string")
            properties[name] = {
                **spec,
                "type": [kind, "null"] if isinstance(kind, str) else kind,
            }
        out.append(
            {
                "type": "function",
                # Flat, not nested under a `function` object. That nesting is
                # the chat/completions shape; the Responses API takes the name,
                # description and parameters at the top level of the tool.
                "name": tool["name"],
                "description": tool["description"],
                "strict": True,
                "parameters": {
                    **schema,
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
            }
        )
    return out


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
