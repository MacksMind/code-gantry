"""Path glob matching for the scope guard.

`fnmatch` is not usable here: its `*` happily crosses `/`, so `app/*.rb`
would authorise `app/admin/deeply/nested.rb`. `pathlib.PurePath.full_match`
has the right semantics but only from 3.13, and the spec targets 3.11+.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to an anchored regex.

    - `**/` matches any number of leading directories, including none
    - `**`  matches anything, crossing separators
    - `*`   matches anything except a separator
    - `?`   matches one character except a separator
    - everything else is literal
    """
    pattern = _normalise(pattern)
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            # Optional, so a root-level file still matches `**/*.py`.
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def matches_any(path: str, patterns: list[str]) -> bool:
    """True if `path` matches at least one pattern.

    An empty pattern list matches nothing — a stage that declares no scope
    authorises no edits.
    """
    candidate = _normalise(path)
    return any(glob_to_regex(p).match(candidate) for p in patterns)


def _normalise(value: str) -> str:
    return value[2:] if value.startswith("./") else value
