"""What the executor may ask of the repository, and what it may do to it.

The read half is `plannertools.READ_TOOLS`, imported rather than restated. Those
descriptions carry the rules that keep a lookup from becoming a belief — a
document is a claim and the code is the fact; semantic search is not an
existence check — and a planner and an executor told different things would be
reasoning from different contracts about the same repository. There is one
place those sentences live.

The write half is new and is the whole point of the change: three tools that
state an edit precisely enough to be applied without interpretation.

**There is no tool that runs a command.** Not because a `run_tests` tool would
literally break the invariant — its body would still be operator config — but
because it would hand over the *scheduling*, and the scheduling is what the
budget is spent on. A model that must ask for a test result can also decline to
ask and declare itself finished. The loop runs lint, commit and tests after
every batch, unconditionally, so the executor cannot end a cycle without having
been shown what its edits did. That invariant has its own test.
"""

from __future__ import annotations

from typing import Any

from orchestrator.edittools import Edit, FileEditor
from orchestrator.plannertools import READ_TOOLS, SEMANTIC_TOOL, call_detail
from orchestrator.repotools import RepoReader, ToolError
from orchestrator.semantic import SemanticSearch

EDIT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "edit",
        "description": (
            "Change an existing file by replacing exact text.\n\n"
            "Each edit's `old_string` must appear **exactly once** in the file, "
            "matched byte for byte including indentation. If it appears twice "
            "the edit is refused: include more surrounding lines until it is "
            "unique, or set `replace_all`. If it appears nowhere the edit is "
            "refused: read the file and quote what is actually there.\n\n"
            "Edits apply in order to one buffer and the file is written once. "
            "If any edit in the list fails, none of them are applied and the "
            "file is left exactly as it was — so a refusal never leaves you "
            "reasoning about a file that no longer exists in that form.\n\n"
            "Read before you edit. You have a read tool, the file is in front "
            "of you, and quoting from memory is what makes an edit fail."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative path."},
                "edits": {
                    "type": "array",
                    "description": "Replacements, applied in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {
                                "type": "string",
                                "description": (
                                    "Exact text to replace, including "
                                    "indentation and line breaks."
                                ),
                            },
                            "new_string": {
                                "type": "string",
                                "description": "What to put in its place.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": (
                                    "Replace every occurrence instead of "
                                    "requiring exactly one."
                                ),
                            },
                        },
                        "required": ["old_string", "new_string", "replace_all"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "edits"],
        },
    },
    {
        "name": "create_file",
        "description": (
            "Write a new file with the given contents. Refused if the file "
            "already exists and is not empty — use `edit` for those. Parent "
            "directories are created."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative path."},
                "content": {"type": "string", "description": "The whole file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": (
            "Remove a file. This is the only way to empty one: an `edit` "
            "whose `old_string` you got slightly wrong is refused rather than "
            "silently clearing the file, so removal has to be asked for by "
            "name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative path."},
            },
            "required": ["path"],
        },
    },
]


SEMANTIC_TOOL_FOR_EDITING: dict[str, Any] = {
    "name": SEMANTIC_TOOL["name"],
    "description": (
        "Find code by meaning when you do not know what it is called. Good for "
        "locating the place to work on; useless as a source of text.\n\n"
        "**What it returns is out of date.** The index is rebuilt from commits, "
        "so it does not contain anything you have edited in this session, and "
        "it may be several commits behind the working tree besides. The "
        "snippets it shows you are the file as it *was*.\n\n"
        "So treat every result as a pointer: take the path, then `read_file` "
        "it to see what is there now. Never quote a snippet from here into an "
        "`edit` — it is the single most reliable way to have that edit "
        "refused, because you will be quoting bytes that have since changed or "
        "that you yourself have already replaced.\n\n"
        "It is also not an existence check. Asked about something with no "
        "matches it returns the nearest things it has, which look like answers. "
        "`list_files` and `search` answer existence; this does not."
    ),
    "input_schema": SEMANTIC_TOOL["input_schema"],
}


def tool_schemas(
    semantic: SemanticSearch | None, project_tools=None
) -> list[dict[str, Any]]:
    """Everything the executor may call. Semantic search only when configured.

    Its description is the executor's own rather than the planner's, and the
    difference is one paragraph that matters: the index lags. That never
    arose for the other two roles because both read a tree nobody is editing —
    the planner before a stage starts, the reviewer after it has committed. The
    executor is the first caller whose own uncommitted work is missing from
    what it is being shown.

    Operator-declared tools go last and are otherwise undecorated. Nothing
    marks them as project-supplied, because a tool the model reads as
    second-class is one it reaches for last — and the whole point is that
    `bundle install` should be as ordinary to it as `read_file`.
    """
    from orchestrator.projecttools import tool_schema

    read = [*READ_TOOLS, SEMANTIC_TOOL_FOR_EDITING] if semantic else list(READ_TOOLS)
    declared = [tool_schema(t) for t in (project_tools or [])]
    return [*read, *EDIT_TOOLS, *declared]


def openai_tool_schemas(
    semantic: SemanticSearch | None, project_tools=None
) -> list[dict[str, Any]]:
    """The same tools in the Responses API's shape.

    Strict mode is not a preference: the SDK refuses to auto-parse otherwise,
    and strict in turn requires every property in `required` and
    `additionalProperties: false`. Optional properties are therefore made
    nullable and required, which is the shape strict mode provides for "may be
    omitted" — `dispatch` reads them with `.get`, so a null arrives as a
    missing argument and nothing downstream can tell the difference.

    Nested object properties are rewritten too, which the planner's version
    never had to do because none of its schemas nest. `edit` carries a list of
    objects with an optional `replace_all`.
    """
    from orchestrator.plannertools import as_strict_tool

    return [as_strict_tool(tool) for tool in tool_schemas(semantic, project_tools)]


def dispatch(
    name: str,
    args: dict,
    reader: RepoReader,
    editor: FileEditor,
    semantic: SemanticSearch | None,
    project_tools=None,
    runner=None,
) -> str:
    """Run one tool call and render its result as text.

    Every failure becomes a readable string rather than an exception, and every
    refusal is recorded against the object that refused it. A loop that stopped
    because it was finished and one that stopped because every edit was refused
    produce the same artifact otherwise, and only one of them is a working
    executor.

    Declared tools are checked before the built-ins fall through to the
    planner's dispatch, and a name collision is impossible by then: config
    refuses a declared tool named after a built-in, because two tools with one
    name is whichever the provider picks and the model cannot tell.
    """
    from orchestrator import plannertools

    declared = {t.name: t for t in (project_tools or [])}
    if name in declared:
        from orchestrator.projecttools import invoke

        if runner is None:  # pragma: no cover - defensive
            return (
                f"cannot do that: {name} is declared but this executor was "
                "built without a command runner"
            )
        try:
            return invoke(declared[name], args, runner)
        except ToolError as e:
            # Recorded on the editor's ledger for the same reason its own
            # refusals are: an attempt that achieved nothing because every call
            # was refused must not read like one that finished.
            if editor is not None:
                editor.record_refusal(name, call_detail(args), str(e), "declared")
            return f"cannot do that: {e}"

    if name in {"edit", "create_file", "delete_file"}:
        try:
            if name == "edit":
                edits = [
                    Edit(
                        old_string=e.get("old_string") or "",
                        new_string=e.get("new_string") or "",
                        replace_all=bool(e.get("replace_all")),
                    )
                    for e in (args.get("edits") or [])
                ]
                if not edits:
                    raise ToolError("no edits given")
                return editor.edit(args.get("path", ""), edits)
            if name == "create_file":
                return editor.create_file(
                    args.get("path", ""), args.get("content") or ""
                )
            return editor.delete_file(args.get("path", ""))
        except ToolError as e:
            editor.record_refusal(
                name, call_detail(args), str(e), getattr(e, "kind", "")
            )
            return f"cannot do that: {e}"

    return plannertools.dispatch(name, args, reader, semantic)
