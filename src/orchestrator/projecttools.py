"""Operator-declared tools: their schemas, and running one.

The orchestrator ships eight tools and knows nothing about any project's
toolchain. What a Rails migration needs next — resolve the manifest, precompile
assets — is project knowledge, and the rule is that project knowledge lives in
config rather than in code. A `dependencies:` block in Python would be that rule
broken with the ecosystem's name filed off, and the command after it would need
another block.

So config declares a menu. This module turns one entry into a schema the model
sees and, when the model picks it, into argv and a result.

**Argv, never a shell.** `ProjectTool.command` is a list and `run_argv` spawns
it with `shell=False`. That single property is what makes it safe for the model
to supply argument values: `rails; rm -rf /` arrives as one element that makes
the underlying program error, because nothing is present to interpret it. The
config layer refuses a shell as `argv[0]` for the same reason.

**A refusal runs nothing.** An argument that is missing, empty, or not a string
raises `ToolError` before any process starts — `invoke` builds the whole argv
first and spawns last. An empty repeated argument is the case worth naming: it
must never widen to the unscoped command, because "update these gems" with the
list dropped is "update everything", and that reads identically in a log.
"""

from __future__ import annotations

from typing import Any

from orchestrator.config import ProjectTool
from orchestrator.repotools import ToolError

# What a declared tool may not be called. Imported lazily by `config` to keep
# the module cycle one-directional.
BUILTIN_TOOL_NAMES = frozenset(
    {
        "read_file",
        "list_files",
        "search",
        "git_show",
        "git_diff",
        "semantic_search",
        "edit",
        "create_file",
        "delete_file",
    }
)


def for_role(role: str, tools) -> list[ProjectTool]:
    """The declared tools one role may call, in declaration order.

    One selector, used by every caller that offers or runs a declared tool.
    The alternative — each role filtering the list where it happens to need it
    — is how a boundary ends up enforced in the place that advertises and not
    in the place that runs, which is the shape of the `search` glob leak: a
    filter over what is offered is not a constraint on what is reachable.
    """
    return [t for t in (tools or []) if role in (t.roles or [])]


def tool_schema(tool: ProjectTool) -> dict[str, Any]:
    """One declared tool, in the same shape as a built-in.

    The description is the operator's verbatim. Nothing is prepended about the
    tool being project-declared: the model should not be able to tell, because
    a tool it treats as second-class is one it reaches for last.
    """
    properties: dict[str, Any] = {}
    for argument in tool.arguments:
        if argument.repeated:
            properties[argument.name] = {
                "type": "array",
                "description": argument.description,
                "items": {"type": "string"},
            }
        else:
            properties[argument.name] = {
                "type": "string",
                "description": argument.description,
            }
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": [a.name for a in tool.arguments],
        },
    }


def build_argv(tool: ProjectTool, args: dict) -> list[str]:
    """Substitute the model's values into the operator's command.

    Raises before anything runs. A repeated argument expands in place into as
    many elements as it has values — the only expansion with no quoting rule,
    which is why the config layer requires a placeholder to be a whole element.
    """
    repeated = {a.name for a in tool.arguments if a.repeated}
    values: dict[str, Any] = {}
    for argument in tool.arguments:
        supplied = (args or {}).get(argument.name)
        if argument.name in repeated:
            if not isinstance(supplied, list) or not supplied:
                raise ToolError(
                    f"{tool.name} needs at least one value for "
                    f"{argument.name!r}. Nothing was run: an omitted scope is "
                    "not the same as no scope, and running the command without "
                    "it would be a wider action than you asked for."
                )
            if not all(isinstance(v, str) and v for v in supplied):
                raise ToolError(
                    f"{tool.name}: every value in {argument.name!r} must be a "
                    "non-empty string"
                )
            values[argument.name] = list(supplied)
        else:
            if not isinstance(supplied, str) or not supplied:
                raise ToolError(
                    f"{tool.name} needs a value for {argument.name!r}. Nothing "
                    "was run."
                )
            values[argument.name] = supplied

    from orchestrator.config import _PLACEHOLDER

    argv: list[str] = []
    for element in tool.command:
        match = _PLACEHOLDER.match(element)
        if not match:
            argv.append(element)
            continue
        value = values[match.group(1)]
        argv.extend(value) if isinstance(value, list) else argv.append(value)
    return argv


def invoke(tool: ProjectTool, args: dict, runner) -> str:
    """Run one declared tool and render the result for the model.

    A non-zero exit is *returned*, not raised. The whole reason a project tool
    exists is that the model learns immediately whether its manifest edit is
    coherent; an exception here would end the cycle instead of informing it,
    which is the outcome the tool was added to avoid.
    """
    argv = build_argv(tool, args)
    result = runner.run_argv(argv, timeout=tool.timeout_seconds)
    return render(result)


def render(result) -> str:
    """Exit code first, then whatever the command said.

    Both streams, and stderr even when stdout is empty: a resolver puts its
    errors on stderr, and an empty answer reads to a model as "nothing
    happened" — the same wrong belief an empty search result produces.
    """
    from orchestrator.commands import collapse_progress_runs

    head = f"$ {result.command}\nexit {result.exit_code}"
    if getattr(result, "timed_out", False):
        head += " (timed out)"
    body = "\n".join(
        part.strip()
        for part in (result.stdout or "", result.stderr or "")
        if part.strip()
    )
    if not body:
        return head + "\n(no output)"
    # Already bounded: `CommandRunner` truncates both streams to
    # `max_output_chars` on the way out, so this only collapses the progress
    # runs that would otherwise be most of what the model reads.
    return head + "\n" + collapse_progress_runs(body)
