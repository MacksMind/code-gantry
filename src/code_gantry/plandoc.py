"""Markdown links a plan document makes, for `plan import --follow-links`."""

from __future__ import annotations

import re

# Markdown inline links. Deliberately only markdown: a plan document is prose,
# and anything cleverer becomes a way to pull in files nobody reviewed.
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# Only these are followed. A link to a .rb file is a code reference, not a plan
# child, and inlining it would be both wrong and expensive.
_FOLLOWED_SUFFIXES = (".md", ".markdown")


def extract_links(content: str) -> list[str]:
    """Markdown links worth following, in document order, deduplicated."""
    found: list[str] = []
    for target in _LINK.findall(content):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        # Strip an anchor: docs/plan.md#section refers to the same document.
        target = target.split("#", 1)[0]
        if not target:
            continue
        if not target.lower().endswith(_FOLLOWED_SUFFIXES):
            continue
        if target not in found:
            found.append(target)
    return found
