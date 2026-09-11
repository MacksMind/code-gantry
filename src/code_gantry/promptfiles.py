"""The prompts, read from the repository's top-level `prompts/` directory.

Every sentence a model is sent as standing instruction lives in a Markdown
file there, one file per block, so it can be edited by hand. The code owns
what is generated from config or state — lists, counts, names, diffs — and
hands it to the file through `$name` placeholders. A file may not name a
placeholder the code does not supply; a literal dollar sign is written `$$`.

`CODE_GANTRY_PROMPTS` names another directory, for trying an edit without
touching the checkout.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS_ENV = "CODE_GANTRY_PROMPTS"


def prompts_dir() -> Path:
    override = os.environ.get(PROMPTS_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "prompts"


def names() -> list[str]:
    """Every prompt file, as the name `text` takes, in path order."""
    root = prompts_dir()
    return sorted(
        p.relative_to(root).with_suffix("").as_posix() for p in root.rglob("*.md")
    )


@lru_cache(maxsize=None)
def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def text(name: str) -> str:
    """The file's text, without its final newline."""
    path = prompts_dir() / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no prompt file {name!r} under {prompts_dir()}")
    return _read(str(path)).rstrip("\n")


def placeholders(name: str) -> set[str]:
    return set(Template(text(name)).get_identifiers())


def render(name: str, **fields) -> str:
    """The file with every placeholder filled. A placeholder the caller does
    not supply is an error naming the file, so an edit that invents one is
    caught at the first render rather than shipped to a model."""
    try:
        return Template(text(name)).substitute(fields)
    except KeyError as e:
        raise KeyError(
            f"prompt file {name}.md names ${e.args[0]}, which the code does not "
            f"supply; it supplies {', '.join('$' + k for k in sorted(fields)) or 'nothing'}"
        ) from None
    except ValueError as e:
        raise ValueError(f"prompt file {name}.md: {e}; write a literal dollar sign as $$") from None


def forget() -> None:
    """Drop the read cache, for a test that rewrites a file."""
    _read.cache_clear()
