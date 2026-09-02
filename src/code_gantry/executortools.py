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

from code_gantry.edittools import Edit, FileEditor
from code_gantry.plannertools import SEMANTIC_TOOL, call_detail, read_tools
from code_gantry.repotools import RepoReader, ToolError
from code_gantry.semantic import SemanticSearch

REPLAN_TOOL: dict[str, Any] = {
    "name": "request_replan",
    "description": (
        "Hand this stage back to the planner, ending your attempt now.\n\n"
        "Two uses, and the second is ordinary rather than exceptional:\n\n"
        "**`unsatisfiable`** — the stage as written cannot be completed. Its "
        "requirements contradict each other, or contradict something the "
        "project's own checks enforce, or ask for a change in a file you may "
        "not touch, or ask for something none of your tools can do. Say "
        "which, and name the file, the two requirements, or the thing you "
        "have no tool for. Two outcomes must not happen: working around it "
        "inside the files you are allowed to touch, and answering in prose as "
        "though your reply were the missing channel — it becomes no part of "
        "the stage, so a requirement met only there is not met.\n\n"
        "**`incomplete`** — you made the change you were asked for, and doing "
        "it revealed work the plan did not anticipate. A version bump whose "
        "consequences only appear once it is applied is the ordinary case: "
        "nobody could have listed them in advance, and the planner needs the "
        "list you now have. Report what the change revealed.\n\n"
        "Neither is a failure of nerve and neither wastes the attempt. What "
        "you have already committed stays on the branch for the planner to "
        "build on. It does not land: nothing here approves anything, skips a "
        "gate, or ends a stage successfully — a redrawn stage still has to "
        "pass every check and a review.\n\n"
        "Do not use this because the work is hard, only because the *stage* "
        "is wrong or too small. If you can finish it, finish it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["unsatisfiable", "incomplete"],
                "description": (
                    "Which of the two above. The planner does different things "
                    "with them: it rewrites the stage's requirements for the "
                    "first and widens or splits its scope for the second."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you found, in enough detail to act on: the "
                    "requirements that conflict, the file that is out of "
                    "scope, or what the change broke. This is the whole of "
                    "what the planner gets."
                ),
            },
        },
        "required": ["kind", "reason"],
        "additionalProperties": False,
    },
}

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
            "`old_string` is **literal text, never a pattern.** Nothing in it "
            "is interpreted: a backslash, a bracket or a dot matches that "
            "character and no other. This is the opposite of `search`, whose "
            "`pattern` is a regular expression — so text copied out of a "
            "`search` result goes into `old_string` exactly as it appears, "
            "with no escaping added or removed. A file that contains regex "
            "source is quoted the same way as any other file.\n\n"
            "Read before you edit. You have a read tool, the file is in front "
            "of you, and quoting from memory is what makes an edit fail.\n\n"
            "**When to reach for `apply_patch` instead.** This tool identifies "
            "a span by quoting the whole of it, so a long or repeated block is "
            "where it struggles: quote too little and the edit is ambiguous, "
            "and get the far end wrong and you replace part of a construct and "
            "leave the rest. `apply_patch` names every removed line "
            "individually, so neither can happen."
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
        "name": "apply_patch",
        "description": (
            "Change a file by describing the change as a patch, rather than by "
            "quoting the span it replaces.\n\n"
            "`diff` is a V4A hunk body. Every line begins with one character: "
            "a space for a line that must already be there and stays, `-` for "
            "a line that must already be there and goes, `+` for a line to "
            "add. A `@@` line above a hunk names an enclosing line — a `def`, "
            "a `class`, a block opener — and scopes the search to below it, "
            "which is how you point at one of several identical blocks. "
            "Every `@@` line starts a new hunk — it is not a way to skip "
            "lines inside one. To change two places, write two hunks that "
            "each have `-` or `+` lines; to reach past a few lines, quote "
            "them as context.\n\n"
            "Example, changing the second of two identical guards:\n\n"
            "```\n"
            "@@ def update\n"
            "   return unless owner?\n"
            "-  record.save\n"
            "+  record.save!\n"
            "```\n\n"
            "**Every context and `-` line must match the file byte for byte, "
            "indentation included.** Nothing is fuzzy-matched and nothing is "
            "interpreted as a pattern; a hunk that does not match exactly once "
            "is refused and nothing is written. Read the range first and build "
            "the hunk from what comes back.\n\n"
            "**Prefer this to `edit` when the change is long, when the block "
            "appears more than once, or when you are replacing part of a "
            "nested construct.** `edit` describes a span by its content, so "
            "the far end can land in the wrong place and strand what follows "
            "it; here every removed line is named, so that cannot happen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Repository-relative path."},
                "type": {
                    "type": "string",
                    "enum": ["create_file", "update_file", "delete_file"],
                    "description": (
                        "The operation. `delete_file` ignores `diff`; "
                        "`create_file` takes a patch of `+` lines only."
                    ),
                },
                "diff": {
                    "type": "string",
                    "description": (
                        "The hunks, and nothing else. No `***` envelope "
                        "lines of any kind — not `*** Begin Patch`, not "
                        "`*** Update File:`, not `*** End Patch` — because "
                        "the path and the operation are their own arguments. "
                        "They are ignored if you send them anyway."
                    ),
                },
            },
            "required": ["path", "type", "diff"],
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
    # The planner's description, plus one paragraph that is true only here.
    #
    # It used to be a second description, opening "good for locating the place
    # to work on" and spending the rest on the lag. That has the emphasis
    # backwards. An executor with a complete instruction has no reason to ask
    # the index anything; the cases where it does are a gap the instruction did
    # not cover and interpreting reviewer feedback, and both are *how does this
    # work* questions — the same ones the other two roles ask. Observed: a
    # rework issue naming a chain across three files, handed to an executor
    # that had been told the tool finds a place to work.
    #
    # Concatenated rather than restated for the reason this module's docstring
    # already gives about the read tools: two descriptions of one tool drift,
    # and then a planner and an executor reason from different contracts about
    # the same repository.
    "description": (
        SEMANTIC_TOOL["description"]
        + "\n\nOne thing more, which is true for you and not for the roles "
        "that drew this stage. **The index does not contain your own work.** "
        "It is rebuilt from commits, so nothing you have edited in this "
        "session is in it, and it may be several commits behind the working "
        "tree besides. Never quote a snippet from here into an `edit`: it is "
        "the most reliable way to have that edit refused, because you will be "
        "quoting bytes that have since changed or that you yourself have "
        "already replaced. Take the path, then `read_file` it to see what is "
        "there now."
    ),
    "input_schema": SEMANTIC_TOOL["input_schema"],
}


def tool_schemas(
    semantic: SemanticSearch | None, project_tools=None, budget=None
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

    Scoped to the executor's own share of the menu. Easy to miss, because this
    role had the menu to itself and takes the whole list by history rather than
    by decision — but a declaration the operator wrote for the planner alone is
    not an executor tool, and the ones most likely to be planner-only are
    read-only, so nothing would fail loudly if this leaked.
    """
    from code_gantry.projecttools import for_role, tool_schema

    reads = read_tools(budget)
    read = [*reads, SEMANTIC_TOOL_FOR_EDITING] if semantic else reads
    declared = [tool_schema(t) for t in for_role("executor", project_tools)]
    return [*read, *EDIT_TOOLS, REPLAN_TOOL, *declared]


def openai_tool_schemas(
    semantic: SemanticSearch | None, project_tools=None, budget=None
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
    from code_gantry.plannertools import as_strict_tool

    return [
        as_strict_tool(tool)
        for tool in tool_schemas(semantic, project_tools, budget)
    ]


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
    from code_gantry import plannertools
    from code_gantry.projecttools import for_role

    # The client reads the arguments off the call itself and ends the attempt;
    # this only supplies the reply that keeps the conversation well formed. A
    # tool call the provider sees no result for is a malformed exchange, and
    # the next request fails for a reason that has nothing to do with replans.
    if name == REPLAN_TOOL["name"]:
        return (
            "Recorded. This attempt ends here and the stage goes back to the "
            "planner with your reason. What you committed stays on the branch."
        )

    # Scoped here as well as where the schema is built, and for the reason the
    # planner's dispatch is: a model can name a tool it was never offered, so
    # advertising and permission have to be two checks over one selector.
    declared = {t.name: t for t in for_role("executor", project_tools)}
    if name in declared:
        from code_gantry.projecttools import invoke

        if runner is None:  # pragma: no cover - defensive
            return (
                f"cannot do that: {name} is declared but this executor was "
                "built without a command runner"
            )
        from code_gantry.projecttools import call_detail as declared_detail

        try:
            answer = invoke(declared[name], args, runner)
            # On the reader's ledger, where every other answered call goes. A
            # refusal was already recorded and a success was not, so the ledger
            # listed the failures and nothing else — and the output is context
            # the attempt is paying for either way.
            if reader is not None:
                from code_gantry.plannertools import _exit_code

                return reader.record_answer(
                    name,
                    declared_detail(declared[name], args),
                    answer,
                    exit_code=_exit_code(answer),
                )
            return answer
        except ToolError as e:
            # Recorded on the editor's ledger for the same reason its own
            # refusals are: an attempt that achieved nothing because every call
            # was refused must not read like one that finished.
            if editor is not None:
                editor.record_refusal(
                    name, declared_detail(declared[name], args), str(e), "declared"
                )
            return f"cannot do that: {e}"

    if name in {"edit", "apply_patch", "create_file", "delete_file"}:
        try:
            if name == "apply_patch":
                return editor.apply_patch(
                    args.get("path", ""),
                    args.get("type") or "update_file",
                    args.get("diff") or "",
                )
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
