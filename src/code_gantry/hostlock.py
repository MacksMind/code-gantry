"""Host locks: one name, one holder at a time, across every process on the
host. An advisory file lock under a per-user directory outside every
checkout, released by the kernel when its holder exits, so a crashed run
leaves nothing to clean up. The wait has no ceiling of its own; whatever
bounds the holder bounds it.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
import time
from pathlib import Path


def lock_dir() -> Path:
    """Where this host's locks live. `CODE_GANTRY_LOCK_DIR` overrides it."""
    override = os.environ.get("CODE_GANTRY_LOCK_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / f"code-gantry-{os.getuid()}" / "locks"


@contextlib.contextmanager
def hold(name: str, label: str, log=None, directory: Path | None = None):
    """Hold the lock `name` for the block. Yields a one-element list carrying
    the seconds spent waiting; `log`, if given, is told who held it."""
    waited = [0.0]
    where = directory or lock_dir()
    where.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = where / f"{name}.lock"
    with open(path, "a+") as handle:
        started = time.monotonic()
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            holder = handle.read().strip() or "another process"
            if log:
                log(f"waiting for the {name!r} lock, held by {holder}")
            fcntl.flock(handle, fcntl.LOCK_EX)
            waited[0] = time.monotonic() - started
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()}: {' '.join(label.split())}")
        handle.flush()
        try:
            yield waited
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
