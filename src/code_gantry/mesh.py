"""What a run asks the daemon, over one socket in its state directory.

Two questions, and both are answered from Mnesia across every host that has
joined: **hold this name for me**, and **which runs are alive**. Neither can
be answered by a run on its own, and neither may be answered wrongly when
the daemon is absent.

The semaphore: one holder at a time for a name, across every host in the
mesh.

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


@contextlib.contextmanager
def presence(run_id: str, bay: str, log=None):
    """Announce this run as alive for the length of the block.

    Held the way a semaphore is held, and for the same reason: the open
    connection *is* the claim that the process is there, so a run that is
    killed, or a machine that sleeps, stops being alive with nothing to
    expire. It is announced by the run rather than by the daemon's record
    of its bays, so a run started by hand in a terminal is as visible as
    one the daemon started.

    With no daemon, nothing is announced and the run goes on. Every reader
    of this treats "could not ask" and "not alive" as different answers,
    so an unannounced run is never taken for a dead one.
    """
    conn = _say(f"presence {run_id} {' '.join(bay.split()) or '-'}", "alive", log)
    try:
        yield
    finally:
        if conn is not None:
            conn.close()


def live_runs(log=None) -> tuple[set[str], set[tuple[str, str]]]:
    """Which runs are alive, and which hosts answered.

    Two sets, never one: the origins that answered, and the `(origin,
    run_id)` pairs alive on them. A host that could not be asked appears
    in neither, and that is the distinction the caller must keep — a host
    off the link is not a host whose runs have stopped, and nothing this
    answers may be read as "that run is dead" for a host that is absent
    from the first set.
    """
    answered: set[str] = set()
    live: set[tuple[str, str]] = set()
    conn = _say("runs", None, log)
    if conn is None:
        return answered, live
    try:
        for line in conn.makefile("r"):
            line = line.strip()
            if line == "end":
                break
            origin, _, rest = line.partition(" ")
            if origin == "unreachable":
                continue
            answered.add(origin)
            if rest:
                live.add((origin, rest))
    except OSError as e:
        if log:
            log(f"the daemon stopped answering which runs are alive ({e})")
        return set(), set()
    finally:
        conn.close()
    return answered, live


def _say(request: str, expect: str | None, log=None):
    """Open the daemon's door and send one line. Answers the connection, or
    None when there is no daemon to ask or it would not answer."""
    path = socket_path()
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(str(path))
        conn.sendall(f"{request}\n".encode())
    except OSError as e:
        if log:
            log(f"no daemon at {path} ({e.__class__.__name__}); nothing was asked")
        return None
    if expect is None:
        return conn
    try:
        answer = conn.makefile("r").readline().strip()
        if not answer.startswith(expect):
            raise OSError(answer or "the daemon closed the connection")
    except OSError as e:
        conn.close()
        if log:
            log(f"the daemon would not answer {request.split()[0]!r} ({e})")
        return None
    return conn
