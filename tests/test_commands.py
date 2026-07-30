"""The declared-command runner.

Every command the orchestrator executes goes through this: setup, tests,
checks, preconditions, context commands, script-stage transforms. It is the
foundation the rest of the system sits on, so it is tested hard.
"""

import os
import sys

from orchestrator.commands import CommandRunner, truncate_middle


class TestBasics:
    def test_captures_stdout_and_exit_zero(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=30).run("echo hello")
        assert r.ok
        assert r.exit_code == 0
        assert "hello" in r.stdout

    def test_captures_stderr_and_nonzero_exit(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=30).run("echo oops >&2; exit 3")
        assert not r.ok
        assert r.exit_code == 3
        assert "oops" in r.stderr

    def test_output_combines_streams(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=30).run("echo out; echo err >&2")
        assert "out" in r.output
        assert "err" in r.output

    def test_runs_in_the_configured_cwd(self, tmp_path):
        (tmp_path / "marker.txt").write_text("x")
        r = CommandRunner(cwd=tmp_path, timeout=30).run("ls")
        assert "marker.txt" in r.stdout

    def test_shell_operators_work(self, tmp_path):
        # Config commands are shell strings: `a && b`, `! grep -q x`.
        r = CommandRunner(cwd=tmp_path, timeout=30).run("true && echo chained")
        assert r.ok
        assert "chained" in r.stdout

    def test_negation_operator_works(self, tmp_path):
        # The preconditions in PLAN.md's example use this form.
        r = CommandRunner(cwd=tmp_path, timeout=30).run("! grep -q nope /dev/null")
        assert r.ok

    def test_records_duration(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=30).run("true")
        assert r.duration_seconds >= 0

    def test_records_the_command_it_ran(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=30).run("echo x")
        assert r.command == "echo x"


class TestTimeout:
    def test_timeout_is_reported_not_raised(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=1).run("sleep 30")
        assert r.timed_out
        assert not r.ok

    def test_per_call_timeout_overrides_default(self, tmp_path):
        # Aider gets aider_timeout_seconds; everything else gets the general
        # command timeout.
        r = CommandRunner(cwd=tmp_path, timeout=300).run("sleep 30", timeout=1)
        assert r.timed_out

    def test_timeout_kills_child_processes(self, tmp_path):
        # docker compose, bundler, and pytest all spawn children. Killing only
        # the shell leaves orphans holding the test database.
        marker = tmp_path / "child-still-running"
        script = f"( sleep 5; touch {marker} ) & sleep 30"
        r = CommandRunner(cwd=tmp_path, timeout=1).run(script)
        assert r.timed_out
        CommandRunner(cwd=tmp_path, timeout=30).run("sleep 7")
        assert not marker.exists(), "child survived the process-group kill"

    def test_timeout_output_is_still_captured(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=1).run("echo before; sleep 30")
        assert r.timed_out
        assert "before" in r.output


class TestEnvironment:
    def test_inherits_environment(self, tmp_path):
        os.environ["ORCH_TEST_INHERITED"] = "yes"
        try:
            r = CommandRunner(cwd=tmp_path, timeout=30).run("echo $ORCH_TEST_INHERITED")
            assert "yes" in r.stdout
        finally:
            del os.environ["ORCH_TEST_INHERITED"]

    def test_extra_env_is_added(self, tmp_path):
        r = CommandRunner(cwd=tmp_path, timeout=30, env={"ORCH_EXTRA": "42"}).run(
            "echo $ORCH_EXTRA"
        )
        assert "42" in r.stdout


class TestTruncation:
    def test_large_output_is_truncated(self, tmp_path):
        # A full test suite's output must not be carried whole into graph
        # state — the checkpointer would balloon and prompts would blow up.
        runner = CommandRunner(cwd=tmp_path, timeout=30, max_output_chars=500)
        r = runner.run(f"{sys.executable} -c \"print('x' * 100000)\"")
        assert len(r.stdout) <= 600
        assert "truncated" in r.stdout

    def test_truncation_keeps_head_and_tail(self, tmp_path):
        # Test failures are usually summarised at the end; the command being
        # run is usually at the start. Both matter.
        runner = CommandRunner(cwd=tmp_path, timeout=30, max_output_chars=200)
        script = "echo FIRSTLINE; for i in $(seq 1 500); do echo filler; done; echo LASTLINE"
        r = runner.run(script)
        assert "FIRSTLINE" in r.stdout
        assert "LASTLINE" in r.stdout

    def test_small_output_is_untouched(self, tmp_path):
        runner = CommandRunner(cwd=tmp_path, timeout=30, max_output_chars=500)
        r = runner.run("echo small")
        assert r.stdout.strip() == "small"


class TestTruncateMiddle:
    def test_short_text_unchanged(self):
        assert truncate_middle("hello", 100) == "hello"

    def test_long_text_keeps_both_ends(self):
        text = "START" + ("m" * 1000) + "END"
        out = truncate_middle(text, 100)
        assert out.startswith("START")
        assert out.endswith("END")
        assert len(out) < len(text)

    def test_marker_reports_how_much_was_dropped(self):
        out = truncate_middle("a" * 1000, 100)
        assert "truncated" in out
        assert "900" in out or "characters" in out


class TestRunAll:
    def test_stops_at_first_failure(self, tmp_path):
        # Checks are a gate, not a report: there is no value in running the
        # rest once one has failed.
        runner = CommandRunner(cwd=tmp_path, timeout=30)
        results = runner.run_all(["true", "false", "echo never"])
        assert len(results) == 2
        assert not results[-1].ok

    def test_all_pass_returns_all(self, tmp_path):
        runner = CommandRunner(cwd=tmp_path, timeout=30)
        results = runner.run_all(["true", "true"])
        assert len(results) == 2
        assert all(r.ok for r in results)

    def test_empty_list_is_vacuously_fine(self, tmp_path):
        runner = CommandRunner(cwd=tmp_path, timeout=30)
        assert runner.run_all([]) == []


class TestLogging:
    def test_each_command_is_logged(self, tmp_path):
        lines = []
        runner = CommandRunner(cwd=tmp_path, timeout=30, log=lines.append)
        runner.run("echo logged")
        joined = "\n".join(lines)
        assert "echo logged" in joined
        assert "exit 0" in joined

    def test_timeout_is_logged(self, tmp_path):
        lines = []
        runner = CommandRunner(cwd=tmp_path, timeout=1, log=lines.append)
        runner.run("sleep 30")
        assert any("timed out" in line for line in lines)


class TestStdinIsClosed:
    """Nothing the orchestrator runs may read from the terminal.

    A command that waits on stdin in an unattended run does not fail — it
    hangs, silently, until the timeout kills it an hour later. Worse, if the
    operator happens to be at the terminal, it eats their keystrokes.

    Found on the first real project: the target repo's test scripts branch on
    whether stdin is a tty and read it when it is not.
    """

    def test_a_command_reading_stdin_gets_eof_not_a_hang(self, tmp_path):
        runner = CommandRunner(cwd=tmp_path, timeout=10)
        result = runner.run("read line; echo \"got:[$line]\"")
        assert "got:[]" in result.output

    def test_it_does_not_consume_the_parents_stdin(self, tmp_path):
        # `cat` with an inherited stdin would block; with DEVNULL it is instant.
        runner = CommandRunner(cwd=tmp_path, timeout=10)
        result = runner.run("cat")
        assert not result.timed_out
        assert result.ok

    def test_stdin_is_explicitly_closed(self, tmp_path, monkeypatch):
        # The two tests above pass under pytest even without the fix, because
        # pytest redirects stdin itself. This one cannot: it checks what is
        # actually asked for, which is what matters under nohup or cron.
        import subprocess as sp

        seen = {}
        real = sp.Popen

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(sp, "Popen", spy)
        CommandRunner(cwd=tmp_path, timeout=10).run("true")
        assert seen.get("stdin") is sp.DEVNULL
