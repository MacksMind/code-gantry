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

from code_gantry import hostlock, meshlock


class FakeDaemon:
    """The daemon's side of the protocol, enough to answer one name. Records
    every connection and when each one ended, because the release is the
    close and nothing else."""

    def __init__(self, path: Path, *, grant_after: float = 0.0, refuse: bool = False):
        self.path = path
        self.grant_after = grant_after
        self.refuse = refuse
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
            if self.refuse:
                conn.sendall(b"error expected: acquire <name> <label>\n")
                return
            if self.grant_after:
                conn.sendall(b"waiting another bay\n")
                time.sleep(self.grant_after)
            conn.sendall(b"held ref-1\n")
            self.granted.set()
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
        daemon = FakeDaemon(meshlock.socket_path())
        try:
            with meshlock.hold("planner-abc", "bay1 run-7") as waited:
                assert daemon.granted.wait(2), "the run went on without being granted anything"
                assert waited[0] == pytest.approx(0.0, abs=0.5)
            assert daemon.closed.wait(2), "the hold outlived the block"
        finally:
            daemon.stop()
        assert daemon.asked == ["acquire planner-abc bay1 run-7"]

    def test_the_name_and_who_is_asking_both_reach_the_daemon(self, daemon_state):
        # The label is what a waiting bay is told it is behind, so it must
        # survive the wire whole.
        daemon = FakeDaemon(meshlock.socket_path())
        try:
            with meshlock.hold("planner-abc", "host-a bay2 run-9"):
                pass
        finally:
            daemon.stop()
        assert daemon.asked == ["acquire planner-abc host-a bay2 run-9"]

    def test_waiting_for_another_host_is_reported_as_time_waited(self, daemon_state):
        daemon = FakeDaemon(meshlock.socket_path(), grant_after=0.4)
        said: list[str] = []
        try:
            with meshlock.hold("planner-abc", "bay1", said.append) as waited:
                assert waited[0] >= 0.3, "a wait nothing measures cannot be reported"
            assert any("another bay" in line for line in said), said
        finally:
            daemon.stop()

    def test_the_block_runs_only_once_it_holds_it(self, daemon_state):
        daemon = FakeDaemon(meshlock.socket_path(), grant_after=0.5)
        entered = []
        try:
            with meshlock.hold("planner-abc", "bay1"):
                entered.append(time.monotonic())
                assert daemon.granted.is_set()
        finally:
            daemon.stop()
        assert entered


class TestWithNoDaemonToAsk:
    def test_it_falls_back_to_the_lock_this_machine_can_see(self, daemon_state, tmp_path):
        # No socket at all: the daemon is not running. A run must still be
        # excluded from its neighbour on this host rather than proceeding
        # unheld, which is what the lock did before any of this.
        locks = tmp_path / "locks"
        said: list[str] = []
        order: list[str] = []
        first_held = threading.Event()
        release = threading.Event()

        def outer():
            with meshlock.hold("planner-abc", "bay1", said.append, directory=locks):
                order.append("first in")
                first_held.set()
                release.wait(3)
                order.append("first out")

        thread = threading.Thread(target=outer)
        thread.start()
        assert first_held.wait(2)

        def inner():
            with meshlock.hold("planner-abc", "bay2", directory=locks):
                order.append("second in")

        waiter = threading.Thread(target=inner)
        waiter.start()
        time.sleep(0.3)
        assert order == ["first in"], "the second bay was not excluded"
        release.set()
        thread.join(3)
        waiter.join(3)
        assert order == ["first in", "first out", "second in"]
        assert any("daemon" in line for line in said), said

    def test_a_daemon_that_refuses_the_request_does_not_stop_the_run(self, daemon_state, tmp_path):
        daemon = FakeDaemon(meshlock.socket_path(), refuse=True)
        said: list[str] = []
        try:
            with meshlock.hold("planner-abc", "bay1", said.append, directory=tmp_path / "locks"):
                pass
        finally:
            daemon.stop()
        assert any("daemon" in line for line in said), said


class TestTheContractTheLockAlreadyHad:
    def test_holding_a_name_inside_itself_does_not_wait_on_itself(self, daemon_state, tmp_path):
        daemon = FakeDaemon(meshlock.socket_path())
        try:
            with meshlock.hold("planner-abc", "outer"):
                with meshlock.hold("planner-abc", "inner") as inner:
                    assert inner[0] == 0.0
        finally:
            daemon.stop()
        assert len(daemon.asked) == 1, "re-entering asked the daemon a second time"

    def test_the_host_lock_is_untouched_by_any_of_this(self, tmp_path):
        # The suite lock is genuinely one machine's: one suite per host is
        # the rule, and it must not start asking the mesh.
        locks = tmp_path / "locks"
        with hostlock.hold("full-suite", "a", directory=locks):
            assert hostlock.held("full-suite")


class TestWhereTheSocketIs:
    def test_it_is_the_daemon_state_dir_the_daemon_itself_writes_to(self, daemon_state):
        assert meshlock.socket_path() == daemon_state / "semaphore.sock"

    def test_without_the_override_it_is_the_daemon_s_default_state_dir(self, monkeypatch):
        monkeypatch.delenv("CODE_GANTRY_DAEMON_STATE", raising=False)
        assert meshlock.socket_path() == Path.home() / ".local/state/code_gantry/daemon/semaphore.sock"


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
        assert "_planner_lock(rt)" in calls.get("meshlock", []), (
            "the planner semaphore must reach every host; a host lock cannot see one"
        )
        assert "_planner_lock(rt)" not in calls.get("hostlock", [])

    def test_the_suite_lock_stays_on_this_machine(self):
        calls = self._calls("commands.py")
        assert calls.get("hostlock"), "one suite per host is a fact about the host"
        assert "meshlock" not in calls, (
            "a suite is a machine's own resource: serialising it across the mesh "
            "would idle every other host for the duration"
        )
