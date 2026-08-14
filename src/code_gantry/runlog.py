"""The run log: a human-readable timeline of node transitions and decisions.

Written to `runs/<run_id>/run.log` and echoed to the terminal. Deliberately
plain text rather than structured logging — its reader is a person
reconstructing why a run stopped.

**Echoed to stdout, not stderr.** The timeline is what this command produces
while it works; stderr is for what went wrong, and `cli.py` uses
`click.echo(..., err=True)` for that in fourteen places. Echoing here to stderr
meant `code-gantry run <project> > out.txt` captured the closing report and
lost everything before it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TextIO


_TERMINAL = object()
"""Default for `echo`, so `None` can mean "nowhere" rather than "the default".

`echo=None` previously selected the terminal, which left no way to ask for a
log that writes only to its file. The tool log needs exactly that: the terminal
is where the timeline goes, and a role's reads are what the timeline is being
kept free of.
"""


class RunLog:
    def __init__(self, path: Path | str, echo: TextIO | None = _TERMINAL):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Append, so a resumed run continues the same timeline rather than
        # truncating the history of why it paused.
        self._handle = self.path.open("a", encoding="utf-8")
        self._echo = sys.stdout if echo is _TERMINAL else echo

    def __call__(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        line = f"{stamp} {message}"
        self._handle.write(line + "\n")
        self._handle.flush()
        if self._echo:
            print(line, file=self._echo, flush=True)

    def record(self, text: str) -> None:
        """Into the file, unstamped, and nowhere else.

        For the run header, which is the one thing written here that is not an
        event: it is the banner the clocked lines follow, and a timestamp on it
        would be a clock on a title.

        The closing report used to come through here too. It is written to
        `report.md` now and the timeline gets a stamped line naming the path —
        two copies of a document in one directory being a synchronisation
        problem rather than a safeguard.
        """
        self._handle.write(text.rstrip("\n") + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> Callable[[str], None]:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
