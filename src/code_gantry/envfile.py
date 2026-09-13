"""Credentials the config points at, rather than credentials the config holds.

`api_key_env` names an environment *variable*, which keeps secrets out of a
file that is tracked in the repository it describes. That leaves the operator
to get the variable into the process, and until now the only answer was the
launching shell — which is a property of how a run was started, appears in no
config, no log and no artifact, and does not survive being moved to another
machine.

`env_file` closes that: the config names a path, the run reads it. It is the
`hold the path, not the copy` rule applied to a secret, and the path belongs
under `work_dir`, which preflight refuses to run without having proved is
git-ignored.

**Parsed, never sourced.** `KEY=value` and nothing else — no expansion, no
substitution, no shell. Handing a credentials file to a shell is arbitrary
execution by another name, and argv-never-a-shell is the whole of this
project's safety story; a file that can run a command can do anything the run
can.

**The file wins, for the variables it names.** The config decides what runs,
and the credentials it points at are the project's; a person's login shell
carries their own, which have nothing to do with it. It was the other way
round once, so an `export` for a one-off test could override the file, and
the ledger's reads then went out signed with a shell's own AWS key to an
account with no table — a credential that is not the one you think you are
using, which is the failure the old rule was written to prevent. What the
file overrode is named, never valued, so the override is visible in the log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from code_gantry.config import ConfigError


def parse_env_file(text: str) -> dict[str, str]:
    """`KEY=value` lines, as data.

    A line that is not an assignment raises rather than being skipped. Skipping
    is how a typo becomes a missing credential reported three checks later as
    something else entirely — the shape this codebase keeps paying for, where
    an empty answer reads as a fact about the world.
    """
    out: dict[str, str] = {}
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            # The file an operator already has is one they were sourcing.
            line = line[len("export "):].strip()
        name, sep, value = line.partition("=")
        # `partition`, not `split`: a value may contain `=`, and base64 and
        # URLs both routinely do.
        name = name.strip()
        if not sep or not name:
            problems.append(f"line {number} is not a KEY=value assignment: {raw!r}")
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[name] = value
    if problems:
        raise ConfigError(problems)
    return out


@dataclass
class Applied:
    """What the file did to the environment: every name it set, and the
    ones whose shell value it replaced. Names only, never values, and no
    caller should log one."""

    set: list[str]
    overrode: list[str]


def apply_env_file(path: Path | str, environ=None) -> Applied:
    """Load `path` into the environment, and say what it did."""
    environ = os.environ if environ is None else environ
    path = Path(path)
    try:
        text = path.read_text(encoding="utf8")
    except OSError as e:
        raise ConfigError(
            [f"env_file {path} could not be read: {e}. The config names it, so "
             "a run cannot authenticate without it."]
        ) from e

    applied = Applied(set=[], overrode=[])
    for name, value in parse_env_file(text).items():
        if name in environ and environ[name] != value:
            applied.overrode.append(name)
        environ[name] = value
        applied.set.append(name)
    return applied
