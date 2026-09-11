"""Markdown in, plan tree out, and back again.

A plan document is a title, a preamble, and sections holding prose and
checkbox items. `parse_markdown` reads that shape; `import_documents` writes
it into a ledger as nodes, turning closed items into `landed` or `struck`
events; `export_markdown` renders a document back from the views for a human
to edit and re-import.

Keys are `{#prefix.nnn}` right after the checkbox or at the end of a heading.
An item's title is its leading bold span; what follows is its body, or for a
closed item its mark and evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from code_gantry.ledger import LANDED, STRUCK, Ledger, Views

KEY_MARK = re.compile(r"\{#([A-Za-z0-9][A-Za-z0-9._-]*)\}")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ITEM = re.compile(r"^- \[([ xX])\]\s+(.*)$")
_FENCE = re.compile(r"^\s*```")
_TITLE = re.compile(r"^(~~)?\*\*(.+?)\*\*(~~)?\s*(.*)$", re.S)
_SHA = re.compile(r"`([0-9a-f]{7,40})`")
_STRUCK = re.compile(r"\*\*STRUCK:?\s*(.*?)\*\*", re.S)
_CLOSED = re.compile(r"\*\*CLOSED\.?\*\*")
_LEADING_DASH = re.compile(r"^\s*[—–-]+\s*")


@dataclass
class Mark:
    sha: str | None = None
    struck: str | None = None  # the reason, when the item was struck
    evidence: str = ""


@dataclass
class ParsedItem:
    key: str | None
    checked: bool
    title: str
    body: str
    mark: Mark = field(default_factory=Mark)


@dataclass
class ParsedSection:
    key: str | None
    level: int
    title: str
    body: str = ""
    items: list[ParsedItem] = field(default_factory=list)
    trailing: str = ""
    children: list[ParsedSection] = field(default_factory=list)


@dataclass
class ParsedDocument:
    key: str | None
    title: str
    preamble: str = ""
    items: list[ParsedItem] = field(default_factory=list)  # before any section
    sections: list[ParsedSection] = field(default_factory=list)
    path: str | None = None

    def all_sections(self) -> list[ParsedSection]:
        out: list[ParsedSection] = []

        def walk(sections):
            for s in sections:
                out.append(s)
                walk(s.children)

        walk(self.sections)
        return out

    def all_items(self) -> list[ParsedItem]:
        return list(self.items) + [i for s in self.all_sections() for i in s.items]


def _split_key(text: str) -> tuple[str | None, str]:
    """A key marker anywhere in `text`, and the text without it."""
    m = KEY_MARK.search(text)
    if not m:
        return None, text.strip()
    return m.group(1), (text[: m.start()] + text[m.end():]).strip()


def _parse_item(first: str, continuation: list[str], checked: bool) -> ParsedItem:
    key, head = _split_key(first)
    joined = " ".join([head, *(line.strip() for line in continuation)]).strip()
    joined = re.sub(r"\s+", " ", joined)
    m = _TITLE.match(joined)
    if m:
        title, rest = m.group(2).strip(), m.group(4).strip()
    else:
        title, _, rest = joined.partition(". ")
        title = title.strip()
        rest = rest.strip()
    mark = Mark()
    if checked:
        sha = _SHA.search(rest)
        struck = _STRUCK.search(rest)
        mark.sha = sha.group(1) if sha else None
        if struck:
            mark.struck = struck.group(1).strip().rstrip(".")
        evidence = rest
        for pattern in (_STRUCK, _CLOSED):
            evidence = pattern.sub("", evidence)
        evidence = _LEADING_DASH.sub("", evidence)
        evidence = re.sub(r"^\.\s*", "", evidence).strip()
        evidence = re.sub(r"\s*[,—]\s*(?=[,—.])", "", evidence)
        evidence = re.sub(r"^[,.]\s*", "", evidence)
        evidence = re.sub(r"\s+", " ", evidence).strip()
        mark.evidence = evidence
        body = ""
    else:
        body = rest
    return ParsedItem(key=key, checked=checked, title=title, body=body, mark=mark)


def parse_markdown(text: str, *, path: str | None = None) -> ParsedDocument:
    """The document's tree. Fenced code is prose wherever it sits."""
    doc = ParsedDocument(key=None, title="", path=path)
    stack: list[ParsedSection] = []
    prose: list[str] = []
    item_first: str | None = None
    item_checked = False
    item_lines: list[str] = []
    in_fence = False
    seen_heading = False

    def flush_prose():
        chunk = "\n".join(prose).strip()
        prose.clear()
        if not chunk:
            return
        if not stack:
            doc.preamble = (doc.preamble + "\n\n" + chunk).strip() if doc.preamble else chunk
        elif stack[-1].items:
            s = stack[-1]
            s.trailing = (s.trailing + "\n\n" + chunk).strip() if s.trailing else chunk
        else:
            s = stack[-1]
            s.body = (s.body + "\n\n" + chunk).strip() if s.body else chunk

    def flush_item():
        nonlocal item_first, item_lines
        if item_first is None:
            return
        item = _parse_item(item_first, item_lines, item_checked)
        (stack[-1].items if stack else doc.items).append(item)
        item_first, item_lines = None, []

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if _FENCE.match(line):
            in_fence = not in_fence
            if item_first is not None:
                item_lines.append(line)
            else:
                prose.append(line)
            continue
        if in_fence:
            (item_lines if item_first is not None else prose).append(line)
            continue

        heading = _HEADING.match(line)
        if heading:
            flush_item()
            flush_prose()
            level = len(heading.group(1))
            key, title = _split_key(heading.group(2))
            if level == 1 and not seen_heading:
                doc.key, doc.title = key, title
                seen_heading = True
                continue
            seen_heading = True
            section = ParsedSection(key=key, level=level, title=title)
            while stack and stack[-1].level >= level:
                stack.pop()
            (stack[-1].children if stack else doc.sections).append(section)
            stack.append(section)
            continue

        item = _ITEM.match(line)
        if item:
            flush_item()
            flush_prose()
            item_checked = item.group(1).lower() == "x"
            item_first = item.group(2)
            continue

        if item_first is not None:
            if line.strip() == "" or line[:1].isspace():
                item_lines.append(line)
                continue
            flush_item()
        prose.append(line)

    flush_item()
    flush_prose()
    return doc


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def _number(key: str, prefix: str) -> int:
    stem, _, number = key.rpartition(".")
    return int(number) if stem == prefix and number.isdigit() else 0


class _KeyMint:
    """Hands out keys under one prefix, never one already in use."""

    def __init__(self, views: Views, prefix: str):
        self._prefix = prefix
        self._used = set(views.nodes)
        self._highest = max((_number(k, prefix) for k in self._used), default=0)

    def take(self, wanted: str | None) -> str:
        if wanted:
            self._used.add(wanted)
            self._highest = max(self._highest, _number(wanted, self._prefix))
            return wanted
        self._highest += 1
        key = f"{self._prefix}.{self._highest:03d}"
        self._used.add(key)
        return key


def import_documents(
    ledger: Ledger,
    documents: list[ParsedDocument],
    *,
    prefix: str,
    owner: str = "pipeline",
    blocking: bool = False,
    at_sha: str | None = None,
    actor: str | None = None,
) -> dict[str, int]:
    """Write parsed documents into the ledger. Returns counts by node kind.

    Existing keys are kept and re-upserted; new nodes take the next key under
    `prefix`. A checked item becomes `landed` (or `struck`) with its sha and
    evidence, unless the ledger already holds a terminal state for that key.
    """
    views = ledger.views()
    mint = _KeyMint(views, prefix)
    counts = {"document": 0, "section": 0, "item": 0, "landed": 0, "struck": 0}
    next_doc_position = len(views.documents())

    def upsert(key, *, parent, position, kind, title, body, path=None):
        ledger.upsert_node(
            key, parent=parent, position=position, kind=kind, title=title,
            body=body, owner=owner, blocking=blocking, actor=actor,
        )
        counts[kind] += 1

    for doc in documents:
        doc_key = mint.take(doc.key)
        existing = views.nodes.get(doc_key)
        position = existing.position if existing and not existing.retired else next_doc_position
        if not (existing and not existing.retired):
            next_doc_position += 1
        ledger.upsert_node(
            doc_key, parent=None, position=position, kind="document",
            title=doc.title, body=doc.preamble, owner=owner, blocking=blocking,
            path=doc.path, actor=actor,
        )
        counts["document"] += 1
        top = 0
        for item in doc.items:
            item_key = mint.take(item.key)
            item.key = item_key
            upsert(item_key, parent=doc_key, position=top, kind="item",
                   title=item.title, body=item.body)
            top += 1
            _record_mark(ledger, item_key, item, at_sha, actor, counts)

        def walk(sections, parent_key, start):
            pos = start
            for section in sections:
                key = mint.take(section.key)
                section.key = key
                body = section.body
                if section.trailing:
                    body = (body + "\n\n" + section.trailing).strip()
                upsert(key, parent=parent_key, position=pos, kind="section",
                       title=section.title, body=body)
                pos += 1
                item_pos = 0
                for item in section.items:
                    item_key = mint.take(item.key)
                    item.key = item_key
                    upsert(item_key, parent=key, position=item_pos, kind="item",
                           title=item.title, body=item.body)
                    item_pos += 1
                    _record_mark(ledger, item_key, item, at_sha, actor, counts)
                walk(section.children, key, item_pos)

        walk(doc.sections, doc_key, top)
        doc.key = doc_key
    return counts


def _record_mark(ledger, key, item: ParsedItem, at_sha, actor, counts) -> None:
    if not item.checked:
        return
    state = ledger.views().state(key).state
    if state in ("landed", "struck"):
        return
    if item.mark.struck is not None:
        ledger.append(STRUCK, key=key, sha=item.mark.sha, actor=actor,
                      reason=item.mark.struck, evidence=item.mark.evidence,
                      source="import")
        counts["struck"] += 1
    else:
        ledger.append(LANDED, key=key, sha=item.mark.sha, actor=actor,
                      evidence=item.mark.evidence, source="import")
        counts["landed"] += 1


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


def export_markdown(views: Views, doc_key: str) -> str:
    """The document as Markdown a human can edit and re-import, state included."""
    doc = views.nodes[doc_key]
    out: list[str] = [f"# {doc.title} {{#{doc.key}}}".rstrip()]
    if doc.body:
        out.extend(["", doc.body])

    def render(node, depth):
        if node.kind == "section":
            out.append("")
            out.append(f"{'#' * (depth + 1)} {node.title} {{#{node.key}}}")
            if node.body:
                out.extend(["", node.body])
            children = views.children(node.key)
            items = [c for c in children if c.kind == "item"]
            if items:
                out.append("")
                for item in items:
                    out.append(item_line(views, item))
            for child in children:
                if child.kind == "section":
                    render(child, depth + 1)

    children = views.children(doc_key)
    top_items = [c for c in children if c.kind == "item"]
    if top_items:
        out.append("")
        out.extend(item_line(views, i) for i in top_items)
    for child in children:
        render(child, 1)
    return "\n".join(out).rstrip() + "\n"


def item_line(views: Views, item) -> str:
    state = views.state(item.key)
    key = f"{{#{item.key}}}"
    if state.state == "landed":
        return f"- [x] {key} ~~**{item.title}**~~ — {with_sha(state.sha, state.evidence) or 'landed'}"
    if state.state == "struck":
        reason = f": {state.reason}" if state.reason else ""
        tail = with_sha(state.sha, state.evidence)
        return f"- [x] {key} ~~**{item.title}**~~ — **STRUCK{reason}.**" + (f" {tail}" if tail else "")
    line = f"- [ ] {key} **{item.title}**"
    return f"{line} {item.body}" if item.body else line


def with_sha(sha: str | None, evidence: str | None) -> str:
    """Evidence led by its sha, unless the evidence already names it."""
    evidence = (evidence or "").strip()
    if not sha:
        return evidence
    if sha in evidence:
        return evidence
    return f"`{sha}`. {evidence}" if evidence else f"`{sha}`"
