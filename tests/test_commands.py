"""The declared-command runner.

Every command CodeGantry executes goes through this: setup, tests,
checks, preconditions, context commands, script-stage transforms. It is the
foundation the rest of the system sits on, so it is tested hard.
"""

import os
import sys

from code_gantry.commands import CommandRunner, truncate_middle


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
        # A per-call timeout overrides the runner's general one.
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

    def test_a_multi_line_command_stays_one_event_in_the_log(self, tmp_path):
        # The run log is one line per event and is read by skimming it. An argv
        # element may now legitimately contain newlines — an operator declares a
        # tool as `sh -c '<script>'` with the model's values arriving as
        # positional parameters, which is the shape that keeps the argv safety
        # property while still resolving a path before reading under it. Joined
        # naively, one such call put five lines into the timeline, the last of
        # them the `exit 0` that belongs to the first.
        #
        # `run_argv`'s docstring said the joined label and the list "can only
        # disagree by whitespace in an element", which was true until an element
        # could hold a newline.
        lines = []
        runner = CommandRunner(cwd=tmp_path, timeout=30, log=lines.append)
        runner.run_argv(["sh", "-c", "x=1\nif [ $x = 1 ]; then\n  echo hi\nfi"])
        entry = "\n".join(lines)
        assert "echo hi" in entry, "the command is no longer legible"
        # One line for the command, one for the outcome. Not five.
        assert len(entry.splitlines()) == 2, entry

    def test_the_result_keeps_the_command_whole(self, tmp_path):
        # Collapsed for the log only. What the model is shown, and what the
        # denylist scanned, is the command as written — a rendering choice must
        # not become a change to the record.
        runner = CommandRunner(cwd=tmp_path, timeout=30)
        result = runner.run_argv(["sh", "-c", "echo a\necho b"])
        assert "echo a\necho b" in result.command


class TestStdinIsClosed:
    """Nothing CodeGantry runs may read from the terminal.

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


class TestSignalledCommands:
    """A killed process is not a failing test.

    Observed live: the operator stopped the Docker containers mid-run, believing
    the run had paused for review. The suite came back `exit 137` — 128+9,
    SIGKILL — and verify read it as a test failure, routed it to the executor,
    and spent one of three attempts on it. The same shape covers an OOM kill and
    a Ctrl-C.

    Exit codes above 128 can in principle be a program's own choice, so this is
    a convention rather than a certainty. It is the right convention: no test
    runner returns 137, and misreading a real failure as an environment problem
    stops the run for a human, which is the safe direction.
    """

    def test_a_killed_command_reports_its_signal(self, repo):
        runner = CommandRunner(cwd=repo, timeout=30)
        result = runner.run("kill -9 $$")
        assert result.signal == 9
        assert not result.ok

    def test_sigterm_is_recognised(self, repo):
        runner = CommandRunner(cwd=repo, timeout=30)
        result = runner.run("kill -15 $$")
        assert result.signal == 15

    def test_an_ordinary_failure_has_no_signal(self, repo):
        runner = CommandRunner(cwd=repo, timeout=30)
        assert runner.run("exit 1").signal is None

    def test_a_clean_exit_has_no_signal(self, repo):
        runner = CommandRunner(cwd=repo, timeout=30)
        assert runner.run("true").signal is None

    def test_our_own_timeout_is_not_reported_as_a_signal(self, repo):
        # We kill the process group with SIGKILL on timeout, which would
        # otherwise look identical to the environment dying underneath us.
        # A timeout is the stage's problem; a stranger's SIGKILL is not.
        runner = CommandRunner(cwd=repo, timeout=1)
        result = runner.run("sleep 5")
        assert result.timed_out
        assert result.signal is None

    def test_the_summary_names_the_signal(self, repo):
        runner = CommandRunner(cwd=repo, timeout=30)
        assert "signal 9" in runner.run("kill -9 $$").summary()


class TestOutputIsKeptWholeForParsing:
    """One cap was serving two different needs, and the parse lost.

    Output feeds two kinds of consumer: things that *parse* it — which need all
    of it — and things that put it in a prompt or a log line, which must be
    bounded. A single 20,000-character cap in the runner served the second and
    silently broke the first.

    Live consequence: the merge gate's suite emitted 334,143 characters with
    the `Failed examples:` block 146,285 characters from the end, under
    per-worker summaries, a coverage report and deprecation tallies.
    truncate_middle keeps the head and tail, so the block landed squarely in
    the dropped middle. The flake gate then found no failing files, fell back
    to re-running everything, tripped a second order-dependent spec, and reset
    a stage the reviewer had already approved.

    Truncation now happens where text is *used*, not where it is captured.
    """

    def test_a_large_output_survives_capture(self, repo):
        runner = CommandRunner(cwd=repo, timeout=60)
        result = runner.run("for i in $(seq 1 40000); do echo 'padding line'; done")
        assert len(result.output) > 400_000

    def test_a_marker_after_the_middle_is_still_findable(self, repo):
        # The shape that actually bit us: the interesting line is neither at the
        # head nor at the very tail.
        runner = CommandRunner(cwd=repo, timeout=60)
        result = runner.run(
            "for i in $(seq 1 20000); do echo head; done; "
            "echo 'rspec ./spec/a_spec.rb:12'; "
            "for i in $(seq 1 20000); do echo tail; done"
        )
        assert "rspec ./spec/a_spec.rb:12" in result.output

    def test_there_is_still_a_ceiling(self, repo):
        # A runaway command must not be held in memory without limit; the cap
        # is a memory guard now rather than a display limit.
        runner = CommandRunner(cwd=repo, timeout=60, max_output_chars=5_000)
        result = runner.run("for i in $(seq 1 5000); do echo 'x'; done")
        assert len(result.output) <= 5_200


class TestOutputPreparedForAModel:
    """Progress reporters put the noise first and the finding after.

    Measured on a live stage: the feedback handed to the executor was 2,393
    characters, of which a single unbroken run of 1,575 dots was 66%. The two
    lines that said what to fix — a RuboCop offence and its source line — sat
    behind it. That stage then survived two executor attempts, a progress
    failure, a planner revision and another attempt without the offence being
    touched.

    The latent half is worse than the noise. `truncate_middle` keeps the head
    and tail because "command output is informative at both ends", which is
    true of most commands and false of a progress reporter: RuboCop and RSpec
    both emit their dots first and their failures after. This output happened
    to fit under the cap; one with more offences would have kept the dots as
    head and dropped the offences as middle. So collapsing has to happen
    *before* truncation, not after, and that ordering is the point of having
    one function rather than two calls at each site.
    """

    def test_a_long_run_is_collapsed(self):
        from code_gantry.commands import collapse_progress_runs

        out = collapse_progress_runs("Inspecting\n" + "." * 1575 + "\nOffenses:")
        assert "." * 1575 not in out
        assert "Inspecting" in out and "Offenses:" in out

    def test_it_says_how_much_it_dropped(self):
        # Lossy about the characters, honest about the quantity: a reader can
        # still tell a 1,575-dot run from a 40-dot one.
        from code_gantry.commands import collapse_progress_runs

        assert "1575" in collapse_progress_runs("." * 1575).replace(",", "")

    def test_short_runs_are_left_alone(self):
        # `...F...` is the whole result of a small suite. Collapsing that would
        # destroy the signal rather than the noise.
        from code_gantry.commands import collapse_progress_runs

        text = "....F....\n1 failure"
        assert collapse_progress_runs(text) == text

    def test_it_names_no_tool_and_no_language(self):
        # This ships to every project. A rule written around dots, `F`/`E`, or
        # RuboCop is a Ruby hint in a framework string.
        import inspect
        from code_gantry.commands import collapse_progress_runs

        source = inspect.getsource(collapse_progress_runs).lower()
        for word in ("rubocop", "rspec", "ruby", "pytest", "eslint"):
            assert word not in source, f"{word!r} is project knowledge"

    def test_collapsing_happens_before_truncation(self):
        """The ordering bug, pinned.

        With the run intact the dots are the head and survive truncation while
        the finding, sitting after them, is dropped as the middle.
        """
        from code_gantry.commands import clip_for_model

        text = "start\n" + "." * 5000 + "\nOFFENCE_HERE\n" + "tail\n"
        out = clip_for_model(text, 400)
        assert "OFFENCE_HERE" in out

    def test_it_still_bounds_the_result(self):
        from code_gantry.commands import clip_for_model

        assert len(clip_for_model("x " * 50_000, 500)) <= 600



class TestTheHostLock:
    """A command mapped to a lock name waits for every other holder of that
    name on the host, and records the wait apart from its own duration."""

    def test_two_holders_of_one_name_run_one_after_the_other(self, tmp_path):
        import threading

        locks = tmp_path / "locks"
        first = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"sleep 0.8": "suite"}, lock_dir=locks
        )
        second = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"true": "suite"}, lock_dir=locks
        )
        t = threading.Thread(target=first.run, args=("sleep 0.8",))
        t.start()
        import time

        time.sleep(0.2)
        r = second.run("true")
        t.join()
        assert r.ok
        assert r.waited_seconds >= 0.4
        assert r.duration_seconds < 0.4, "the wait is not the command's duration"

    def test_a_different_name_does_not_wait(self, tmp_path):
        import threading

        locks = tmp_path / "locks"
        first = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"sleep 0.8": "suite"}, lock_dir=locks
        )
        other = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"true": "lint"}, lock_dir=locks
        )
        t = threading.Thread(target=first.run, args=("sleep 0.8",))
        t.start()
        import time

        time.sleep(0.2)
        r = other.run("true")
        t.join()
        assert r.waited_seconds == 0

    def test_an_unmapped_command_takes_no_lock(self, tmp_path):
        locks = tmp_path / "locks"
        runner = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"sleep 1": "suite"}, lock_dir=locks
        )
        r = runner.run("true")
        assert r.waited_seconds == 0
        assert not locks.exists()

    def test_the_log_names_the_holder_and_the_wait(self, tmp_path):
        import threading
        import time

        locks = tmp_path / "locks"
        lines = []
        first = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"sleep 0.8": "suite"}, lock_dir=locks
        )
        second = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"true": "suite"}, lock_dir=locks,
            log=lines.append,
        )
        t = threading.Thread(target=first.run, args=("sleep 0.8",))
        t.start()
        time.sleep(0.2)
        second.run("true")
        t.join()
        assert any(
            "waiting for the 'suite' lock" in line and f"pid {os.getpid()}: sleep 0.8" in line
            for line in lines
        ), lines
        assert any("after waiting" in line for line in lines), lines

    def test_the_lock_survives_the_holder_only_as_long_as_the_holder(self, tmp_path):
        """The lock is released by the kernel with the process, so a run that
        died holding it leaves nothing for the next one to clean up."""
        import subprocess
        import time

        locks = tmp_path / "locks"
        locks.mkdir()
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                "import fcntl,time,sys; h=open(sys.argv[1],'a+'); "
                "fcntl.flock(h, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
                str(locks / "suite.lock"),
            ],
            stdout=subprocess.PIPE, text=True,
        )
        assert holder.stdout.readline().strip() == "held"
        holder.kill()
        holder.wait()
        runner = CommandRunner(
            cwd=tmp_path, timeout=30, exclusive={"true": "suite"}, lock_dir=locks
        )
        started = time.monotonic()
        r = runner.run("true")
        assert r.ok and time.monotonic() - started < 5

    def test_the_default_lock_dir_is_per_user_and_outside_any_checkout(self, monkeypatch, tmp_path):
        from code_gantry.commands import host_lock_dir

        monkeypatch.delenv("CODE_GANTRY_LOCK_DIR", raising=False)
        d = host_lock_dir()
        assert d.name == "locks" and d.parent.name == f"code-gantry-{os.getuid()}"
        monkeypatch.setenv("CODE_GANTRY_LOCK_DIR", str(tmp_path / "elsewhere"))
        assert host_lock_dir() == tmp_path / "elsewhere"


class TestTheLockIsReentrant:
    def test_a_hold_inside_a_hold_on_the_same_name_does_not_block(self, tmp_path):
        from code_gantry import hostlock

        locks = tmp_path / "locks"
        with hostlock.hold("suite", "outer", directory=locks) as outer:
            assert hostlock.held("suite")
            with hostlock.hold("suite", "inner", directory=locks) as inner:
                assert inner[0] == 0
                assert hostlock.held("suite")
            assert hostlock.held("suite"), "the inner exit did not release the outer"
        assert not hostlock.held("suite")
        assert outer[0] == 0

    def test_a_different_name_inside_still_locks(self, tmp_path):
        from code_gantry import hostlock

        locks = tmp_path / "locks"
        with hostlock.hold("suite", "outer", directory=locks):
            with hostlock.hold("planner", "inner", directory=locks):
                assert hostlock.held("planner") and hostlock.held("suite")
            assert not hostlock.held("planner")
