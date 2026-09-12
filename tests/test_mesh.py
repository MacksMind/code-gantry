"""The planner semaphore as a run sees it: a hold that reaches every host,
taken from the daemon over its socket, falling back to this machine's own
lock when there is no daemon to ask."""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from code_gantry import hostlock, mesh


class FakeDaemon:
    """The daemon's side of the protocol, enough to answer one name. Records
    every connection and when each one ended, because the release is the
    close and nothing else."""

    def __init__(self, path: Path, *, grant_after: float = 0.0, refuse: bool = False, runs=None, busy: str = ""):
        self.busy = busy
        self.path = path
        self.grant_after = grant_after
        self.refuse = refuse
        self.runs = runs or []
        self.announced: list[str] = []
        self.gone = threading.Event()
        self.asked: list[str] = []
        self.closed = threading.Event()
        self.granted = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(path))
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        with conn:
            line = conn.makefile("r").readline().strip()
            self.asked.append(line)
            if line.startswith("runs"):
                conn.sendall(("".join(f"{r}\n" for r in self.runs) + "end\n").encode())
                return
            if line.startswith("try"):
                if self.busy:
                    conn.sendall(f"busy {self.busy}\n".encode())
                    return
                # Set before the answer goes out, not after: the client is
                # released by the answer and can reach its assertions
                # before this thread runs another line.
                self.granted.set()
                conn.sendall(b"held ref-2\n")
                conn.recv(1)
                self.closed.set()
                return
            if line.startswith("presence"):
                self.announced.append(line)
                conn.sendall(b"alive ref-9\n")
                conn.recv(1)
                self.gone.set()
                return
            if self.refuse:
                conn.sendall(b"error expected: acquire <name> <label>\n")
                return
            if self.grant_after:
                conn.sendall(b"waiting another bay\n")
                time.sleep(self.grant_after)
            self.granted.set()
            conn.sendall(b"held ref-1\n")
            # The hold lasts exactly as long as the connection.
            conn.recv(1)
            self.closed.set()

    def stop(self):
        self._sock.close()


@pytest.fixture
def daemon_state(monkeypatch):
    # Short, deliberately: a Unix socket path is capped near 100 bytes and
    # pytest's own temporary directory is most of that on its own. The
    # daemon's real state directory is well inside the limit.
    state = Path(tempfile.mkdtemp(prefix="cg-sem-", dir="/tmp"))
    monkeypatch.setenv("CODE_GANTRY_DAEMON_STATE", str(state))
    yield state
    shutil.rmtree(state, ignore_errors=True)


class TestTheDaemonHoldsIt:
    def test_a_run_asks_the_daemon_and_is_told_it_holds_it(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path())
        try:
            with mesh.hold("planner-abc", "bay1 run-7") as waited:
                assert daemon.granted.wait(2), "the run went on without being granted anything"
                assert waited[0] == pytest.approx(0.0, abs=0.5)
            assert daemon.closed.wait(2), "the hold outlived the block"
        finally:
            daemon.stop()
        assert daemon.asked == ["acquire planner-abc bay1 run-7"]

    def test_the_name_and_who_is_asking_both_reach_the_daemon(self, daemon_state):
        # The label is what a waiting bay is told it is behind, so it must
        # survive the wire whole.
        daemon = FakeDaemon(mesh.socket_path())
        try:
            with mesh.hold("planner-abc", "host-a bay2 run-9"):
                pass
        finally:
            daemon.stop()
        assert daemon.asked == ["acquire planner-abc host-a bay2 run-9"]

    def test_waiting_for_another_host_is_reported_as_time_waited(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path(), grant_after=0.4)
        said: list[str] = []
        try:
            with mesh.hold("planner-abc", "bay1", said.append) as waited:
                assert waited[0] >= 0.3, "a wait nothing measures cannot be reported"
            assert any("another bay" in line for line in said), said
        finally:
            daemon.stop()

    def test_the_block_runs_only_once_it_holds_it(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path(), grant_after=0.5)
        entered = []
        try:
            with mesh.hold("planner-abc", "bay1"):
                entered.append(time.monotonic())
                assert daemon.granted.is_set()
        finally:
            daemon.stop()
        assert entered


class TestWithNoDaemonToAsk:
    def test_a_run_goes_ahead_holding_nothing_and_says_so(self, daemon_state):
        # No socket at all: nothing is running on this machine. Nothing
        # else about a run needs a daemon and this is not the exception.
        said: list[str] = []
        started = time.monotonic()
        with mesh.hold("planner-abc", "bay1", said.append) as waited:
            assert waited[0] == 0.0
        assert time.monotonic() - started < 1, "a missing daemon must not be waited for"
        assert any("no daemon" in line for line in said), said

    def test_it_holds_nothing_rather_than_holding_this_machine_only(self, daemon_state):
        # A lock of this machine would read as a hold while excluding
        # nobody the semaphore is about — and the one bay it did exclude is
        # the only other bay that could have seen the request.
        order: list[str] = []
        first_in = threading.Event()
        release = threading.Event()

        def first():
            with mesh.hold("planner-abc", "bay1"):
                order.append("first in")
                first_in.set()
                release.wait(3)

        def second():
            with mesh.hold("planner-abc", "bay2"):
                order.append("second in")

        a = threading.Thread(target=first)
        a.start()
        assert first_in.wait(2)
        b = threading.Thread(target=second)
        b.start()
        b.join(2)
        assert order == ["first in", "second in"], "a run was held up by a lock nothing else can see"
        release.set()
        a.join(3)

    def test_a_daemon_that_refuses_the_request_does_not_stop_the_run(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path(), refuse=True)
        said: list[str] = []
        try:
            with mesh.hold("planner-abc", "bay1", said.append) as waited:
                assert waited[0] == 0.0
        finally:
            daemon.stop()
        assert any("could not grant" in line for line in said), said


class TestTheContractTheLockAlreadyHad:
    def test_holding_a_name_inside_itself_does_not_wait_on_itself(self, daemon_state, tmp_path):
        daemon = FakeDaemon(mesh.socket_path())
        try:
            with mesh.hold("planner-abc", "outer"):
                with mesh.hold("planner-abc", "inner") as inner:
                    assert inner[0] == 0.0
        finally:
            daemon.stop()
        assert len(daemon.asked) == 1, "re-entering asked the daemon a second time"

    def test_the_suite_lock_is_untouched_by_any_of_this(self, tmp_path):
        # One suite per host is a fact about the host, so the suite keeps
        # the lock this machine can see and never asks the mesh.
        locks = tmp_path / "locks"
        with hostlock.hold("full-suite", "a", directory=locks):
            assert hostlock.held("full-suite")


class TestWhereTheSocketIs:
    def test_it_is_the_daemon_state_dir_the_daemon_itself_writes_to(self, daemon_state):
        assert mesh.socket_path() == daemon_state / "semaphore.sock"

    def test_without_the_override_it_is_the_daemon_s_default_state_dir(self, monkeypatch):
        monkeypatch.delenv("CODE_GANTRY_DAEMON_STATE", raising=False)
        assert mesh.socket_path() == Path.home() / ".local/state/code_gantry/daemon/semaphore.sock"


class TestWhichLockEachCallerTakes:
    """Two locks now, and the difference is what they are about. The suite
    is one machine's resource and stays on `hostlock`; the planner is one
    project's and goes to the mesh. Parsed rather than grepped, because the
    question is which function is called and a string is not that."""

    def _calls(self, module: str) -> dict[str, list[str]]:
        import ast

        tree = ast.parse(Path("src/code_gantry") .joinpath(module).read_text())
        found: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "hold":
                if isinstance(func.value, ast.Name):
                    found.setdefault(func.value.id, []).append(ast.unparse(node.args[0]) if node.args else "")
        return found

    def test_the_planner_semaphore_is_taken_through_the_mesh(self):
        calls = self._calls("nodes.py")
        assert "_planner_lock(rt)" in calls.get("mesh", []), (
            "the planner semaphore must reach every host; a host lock cannot see one"
        )
        assert "_planner_lock(rt)" not in calls.get("hostlock", [])

    def test_the_suite_lock_stays_on_this_machine(self):
        calls = self._calls("commands.py")
        assert calls.get("hostlock"), "one suite per host is a fact about the host"
        assert "mesh" not in calls, (
            "a suite is a machine's own resource: serialising it across the mesh "
            "would idle every other host for the duration"
        )


class TestTheSuiteNeverAsksTheRealDaemon:
    def test_the_daemon_state_dir_is_isolated_for_every_test(self, tmp_path):
        # Without this the suite queues behind a real derivation on another
        # machine and waits there for as long as that derivation takes —
        # which is minutes, and looks like a hung test.
        assert mesh.socket_path().parent == tmp_path / "no-daemon"
        assert not mesh.socket_path().exists()


class TestWhoIsAlive:
    def test_a_run_announces_itself_for_as_long_as_it_lives(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path())
        try:
            with mesh.presence("20260912-1-bay1", "host-a/target"):
                assert daemon.announced == ["presence 20260912-1-bay1 host-a/target"]
                assert not daemon.gone.is_set()
            assert daemon.gone.wait(2), "the announcement outlived the run"
        finally:
            daemon.stop()

    def test_a_run_with_no_daemon_announces_nothing_and_goes_on(self, daemon_state):
        said: list[str] = []
        with mesh.presence("r1", "bay", said.append):
            pass
        assert any("no daemon" in line for line in said), said

    def test_the_live_runs_of_every_host_come_back_with_who_answered(self, daemon_state):
        daemon = FakeDaemon(
            mesh.socket_path(),
            runs=["host-a 20260912-1-bay1", "host-b 20260912-2-bay1", "unreachable dgx-2"],
        )
        try:
            answered, live = mesh.live_runs()
        finally:
            daemon.stop()
        assert live == {("host-a", "20260912-1-bay1"), ("host-b", "20260912-2-bay1")}
        assert answered == {"host-a", "host-b"}
        assert "dgx-2" not in answered, (
            "a host that could not be asked must not be reported as having no runs"
        )

    def test_with_no_daemon_nobody_answered(self, daemon_state):
        # Not "no runs are alive". The difference is the whole point: a
        # caller that cannot ask must not conclude anything is dead.
        answered, live = mesh.live_runs()
        assert answered == set() and live == set()


class TestTheTwoLocksAreNeverNested:
    """The planner semaphore is held for the length of a planner call; the
    ledger's writer is held for a read and a few appends. A bay that blocked
    on the first while holding the second would be waiting for a deriver
    that is waiting for it."""

    def test_nothing_reaches_for_the_semaphore_holding_the_ledger_writer(self):
        import ast

        tree = ast.parse(Path("src/code_gantry/nodes.py").read_text())
        inside: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            opens_a_transaction = any(
                isinstance(i.context_expr, ast.Call)
                and isinstance(i.context_expr.func, ast.Attribute)
                and i.context_expr.func.attr == "transaction"
                for i in node.items
            )
            if not opens_a_transaction:
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "hold"
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "mesh"
                ):
                    inside.append(child.lineno)
        assert inside == [], (
            f"nodes.py reaches for the mesh semaphore while holding the ledger's "
            f"writer at line(s) {inside}; the two must never nest that way"
        )


class TestAnAttemptThatWillNotWait:
    """A caller with something better to do: the lander asks whether it may
    compose, and a bay that cannot works a stage instead. Waiting would cost
    it a whole suite for a job somebody else is already doing."""

    def test_a_free_name_is_held_for_the_block(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path())
        try:
            with mesh.attempt("landing-abc", "bay1") as got:
                assert got is True
                assert daemon.granted.is_set()
            assert daemon.closed.wait(2), "the hold outlived the block"
        finally:
            daemon.stop()
        assert daemon.asked == ["try landing-abc bay1"]

    def test_a_busy_name_answers_at_once_and_says_who(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path(), busy="host-b bay1")
        said: list[str] = []
        started = time.monotonic()
        try:
            with mesh.attempt("landing-abc", "bay2", said.append) as got:
                assert got is False
        finally:
            daemon.stop()
        assert time.monotonic() - started < 1, "an attempt waited"
        assert any("host-b bay1" in line for line in said), said

    def test_with_no_daemon_nobody_may_have_it(self, daemon_state):
        # Not "it is free". One host cannot decide alone that it is the
        # only lander, and a compose is not work that must happen now.
        said: list[str] = []
        with mesh.attempt("landing-abc", "bay1", said.append) as got:
            assert got is False
        assert any("no daemon" in line for line in said), said

    def test_a_name_this_thread_already_holds_is_still_held(self, daemon_state):
        daemon = FakeDaemon(mesh.socket_path())
        try:
            with mesh.hold("landing-abc", "outer"):
                with mesh.attempt("landing-abc", "inner") as got:
                    assert got is True
        finally:
            daemon.stop()
        assert len(daemon.asked) == 1
