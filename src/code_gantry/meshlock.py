"""The semaphore a run takes: one holder at a time for a name, across every
host in the mesh.

The holder is decided by the daemon, and the run's connection to it *is*
the hold: a Unix socket under the daemon's state directory, opened to ask
and closed to let go. So a run that is killed, or a machine that sleeps or
drops off the link, releases the name at once with nothing to expire and
nothing to clean up — the same property `hostlock` has from `flock`, which
is why this is not in the ledger's table.

The name is the thing serialised, not the machine. The planner semaphore is
named for the ledger, so every bay of every host working one project queues
behind the others, and a second project waits for none of them.

**With no daemon to ask, a run goes ahead unheld and says so.** Nothing
else about a run needs a daemon and this must not be the exception: a run
started by hand on a machine with nothing running behaves as it always
did. A lock of this machine only would be worse than none — it would
read as a hold while excluding nobody the semaphore is about, and the bay
it did exclude is the one bay that could have seen the request.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from pathlib import Path

# Names this thread already holds, with a depth: a hold inside a hold on the
# same name re-enters rather than asking the daemon for a name it is already
# holding, which would wait on itself.
_local = threading.local()


def _held() -> dict[str, int]:
    if not hasattr(_local, "held"):
        _local.held = {}
    return _local.held


def held(name: str) -> bool:
    return _held().get(name, 0) > 0


def state_dir() -> Path:
    """Where the daemon keeps its state. Must agree with `Host.state_dir/0`
    in the daemon, which is the one that creates it."""
    override = os.environ.get("CODE_GANTRY_DAEMON_STATE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "code_gantry" / "daemon"


def socket_path() -> Path:
    """The door the daemon listens at. Finding the daemon is finding this."""
    return state_dir() / "semaphore.sock"


@contextlib.contextmanager
def hold(name: str, label: str, log=None):
    """Hold `name` for the block, yielding a one-element list carrying the
    seconds spent waiting. `label` says who is asking, and is what a bay
    waiting behind this one is told it is behind.

    Yields immediately, holding nothing, when there is no daemon to ask.
    """
    label = " ".join(label.split())
    if held(name):
        _held()[name] += 1
        try:
            yield [0.0]
        finally:
            _held()[name] -= 1
        return

    answer = _ask(name, label, log)
    if answer is None:
        yield [0.0]
        return

    waited, conn = answer
    _held()[name] = 1
    try:
        yield waited
    finally:
        _held().pop(name, None)
        # A daemon that restarted while this was held gave the name away
        # without anyone here noticing. Worth a line: the derivation this
        # protected may have had company.
        if log and _gone(conn):
            log(f"the daemon holding {name!r} went away while it was held")
        conn.close()


def _ask(name: str, label: str, log=None):
    """Ask the daemon for `name` and wait until it says the name is ours.
    Answers the wait and the open connection, or None when there is no
    daemon to ask or it cannot answer — in which case the caller goes
    ahead holding nothing."""
    path = socket_path()
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(str(path))
    except OSError as e:
        if log:
            log(f"no daemon at {path} ({e.__class__.__name__}); "
                f"going ahead without {name!r}")
        return None

    started = time.monotonic()
    try:
        conn.sendall(f"acquire {name} {label}\n".encode())
        lines = conn.makefile("r")
        while True:
            line = lines.readline()
            if not line:
                raise OSError("the daemon closed the connection before granting")
            answer = line.strip()
            if answer.startswith("held"):
                waited = [time.monotonic() - started]
                if waited[0] >= 1 and log:
                    log(f"waited {waited[0]:.0f}s for {name!r}")
                return waited, conn
            if answer.startswith("waiting") and log:
                log(f"waiting for {name!r}, held by {answer[len('waiting'):].strip()}")
            if answer.startswith("error"):
                raise OSError(answer)
    except OSError as e:
        conn.close()
        if log:
            log(f"the daemon could not grant {name!r} ({e}); going ahead without it")
        return None


def _gone(conn: socket.socket) -> bool:
    """Whether the connection carrying a hold has been closed under us."""
    conn.settimeout(0)
    try:
        return conn.recv(1) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True
