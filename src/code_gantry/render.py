"""What the planner and reviewer are sent, rendered from the ledger.

Two renderers, both pure: the same views give the same bytes.

`render_plan` is the stable half — the tree with its keys, bodies and the marks
a fold has written. It changes only when a node changes or a fold runs, which
is what lets it sit in the cached block.

`render_projection` is the churning half — every key whose state the plan text
does not yet show, and the open findings. Bounded by open keys times the note
cap; never truncated as a whole.
"""

from __future__ import annotations

from code_gantry.commands import clip_for_model
from code_gantry.ledger import Finding, KeyState, Node, Views


def _order(views: Views) -> dict[str, int]:
    return {node.key: i for i, node in enumerate(views.walk())}


def _flags(node: Node) -> str:
    flags = []
    if node.owner == "human":
        flags.append("human")
    if node.blocking:
        flags.append("blocks cutover")
    return f" ({', '.join(flags)})" if flags else ""


def render_plan(views: Views) -> str:
    """The tree as text, as it stood at the last fold: this is the
    cacheable block, and only a fold may rewrite it. Live before the
    first fold."""
    views = views.folded_views()
    out: list[str] = []

    def item_line(node: Node) -> str:
        key = f"{{#{node.key}}}"
        if node.marks:
            return f"- [x] {key}{_flags(node)} ~~**{node.title}**~~ — " + "; ".join(node.marks)
        line = f"- [ ] {key}{_flags(node)} **{node.title}**"
        return f"{line} {node.body}" if node.body else line

    def section(node: Node, depth: int) -> None:
        out.append("")
        out.append(f"{'#' * (depth + 1)} {node.title} {{#{node.key}}}{_flags(node)}")
        if node.body:
            out.extend(["", node.body])
        children = views.children(node.key)
        items = [c for c in children if c.kind == "item"]
        if items:
            out.append("")
            out.extend(item_line(i) for i in items)
        for child in children:
            if child.kind == "section":
                section(child, depth + 1)

    for doc in views.documents():
        if out:
            out.append("")
        out.append(f"# {doc.title} {{#{doc.key}}}{_flags(doc)}")
        if doc.body:
            out.extend(["", doc.body])
        for child in views.children(doc.key):
            if child.kind == "section":
                section(child, 1)
            elif child.kind == "item":
                out.append(item_line(child))
    return "\n".join(out).strip() + ("\n" if out else "")


def render_projection(views: Views, *, note_chars: int) -> str:
    """Everything the plan text does not yet show. Empty when there is nothing."""
    order = _order(views)
    position = lambda key: (order.get(key, len(order)), key)  # noqa: E731

    landed, struck, claimed, blocked, answered = [], [], [], [], []
    for key, state in sorted(views.key_states.items(), key=lambda kv: position(kv[0])):
        node = views.nodes.get(key)
        if node is None or node.retired:
            continue
        shown = (views.folded or {}).get(key, node) if views.folded is not None else node
        if state.state in ("landed", "struck") and shown.marks:
            continue
        line = f"- {{#{key}}} **{node.title}**"
        if state.state == "landed":
            sha = f" `{state.sha}`" if state.sha else ""
            stage = f" ({state.stage_id})" if state.stage_id else ""
            landed.append(f"{line} —{sha}{stage}")
        elif state.state == "struck":
            struck.append(f"{line} — {state.reason or state.evidence or 'struck'}")
        elif state.state == "claimed":
            who = state.actor or state.run_id or "a run"
            stage = f", stage `{state.stage_id}`" if state.stage_id else ""
            claimed.append(f"{line} — claimed by {who}{stage}, since {state.since}")
        elif state.state == "blocked":
            blocked.append(f"{line} — {state.question or 'blocked'}")
        elif state.state == "open" and state.answer:
            answered.append(f"{line} — answered: {state.answer}")

    open_findings = sorted(
        views.open_findings(),
        key=lambda f: (position(f.keys[0]) if f.keys else (len(order), ""), f.id),
    )
    answered_findings = sorted(
        (f for f in views.findings.values() if f.status == "answered"),
        key=lambda f: f.id,
    )

    waiting = [
        f"- `{d.id}` **{d.stage_id}** on "
        + (", ".join(f"{{#{k}}}" for k in d.keys) or "no key")
        + (f", settling {', '.join(f'`{f}`' for f in d.findings)}" if d.findings else "")
        + f" — drawn by {d.by_run or 'a run'}"
        for d in views.derived_waiting()
        ]

    def node_line(node: Node) -> str:
        line = f"- {{#{node.key}}}{_flags(node)} **{node.title}**"
        return f"{line} {node.body}" if node.body else line

    added_nodes, changed_nodes, retired_nodes = views.unfolded_nodes()
    added = [f"{node_line(n)} — under {{#{n.parent}}}" if n.parent else node_line(n) for n in added_nodes]
    changed = [f"{node_line(now)} — was:{_flags(then)} **{then.title}**" + (f" {then.body}" if then.body else "") for now, then in changed_nodes]
    retired = [f"- {{#{n.key}}} **{n.title}**" for n in retired_nodes]

    sections: list[tuple[str, list[str]]] = [
        ("Added since the plan text was last folded", added),
        ("Changed since the plan text was last folded", changed),
        ("Retired since the plan text was last folded", retired),
        ("Landed since the plan text was last folded", landed),
        ("Struck", struck),
        ("Claimed", claimed),
        ("Drawn and waiting for a run to take them", waiting),
        ("Blocked, waiting on a person", blocked),
        ("Answered", answered),
        ("Open findings", [_finding_line(f, views, note_chars) for f in open_findings]),
        ("Answered findings not yet folded",
         [_finding_line(f, views, note_chars, answered=True) for f in answered_findings]),
    ]
    out: list[str] = []
    for title, lines in sections:
        if not lines:
            continue
        out.extend(["", f"### {title}", "", *lines])
    return "\n".join(out).strip() + ("\n" if out else "")


def _finding_line(finding: Finding, views: Views, note_chars: int, *, answered=False) -> str:
    keys = ", ".join(f"{{#{k}}}" for k in finding.keys) or "no key"
    needs = " (needs a person)" if finding.needs == "human" else ""
    claim = clip_for_model(finding.claim, note_chars).strip()
    total = f" [{finding.total}]" if finding.total else ""
    held = f" (held by {finding.claimed_run})" if finding.claimed_run else ""
    line = f"- `{finding.id}` on {keys} — by {finding.by}{needs}{held}: {claim}{total}"
    if answered:
        line += f" → {finding.disposition}"
        if finding.answer_text:
            line += f": {clip_for_model(finding.answer_text, note_chars).strip()}"
    return line
