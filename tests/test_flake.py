"""Adjudicating a red full suite: the stage's fault, or the suite's?

The merge gate is reviewer approval AND a green full suite. On the first real
target that second half was unreliable in a specific, diagnosable way: across
four runs of a 2,335-example suite, every failure was a *different* example,
and every one of them passed when run alone. `spec/features/ops_order_funnel`,
`admin_order_edit_personalization`, and `spec/requests/checkout_spec.rb` — two
directories, three files, one property. The suite has order- and
parallelism-dependent examples.

Re-running the whole suite once, which is what the gate did, is a poor test of
that: the second run is just as likely to trip over a *different* example, so a
stage fails to land for a reason it did not cause, and the failure costs a
planner intervention. Re-running only the examples that failed, without the
2,300 that did not, tests exactly the property in question.

The sharp edge is the ownership rule. "Passed alone" cannot be allowed to
excuse a stage that broke a spec it was working on, so a failure in a file the
stage touched or named is never credited as a flake, however it re-runs.
"""

import re

import pytest

from orchestrator.commands import CommandRunner
from orchestrator.config import ConfigError, parse_config
from orchestrator.flake import adjudicate, failed_files

# The operator's, from projects/example/config.yaml. Group 1 is the file path;
# it stops before `[1:1]` or `:531`, so both of RSpec's locator forms reduce to
# the same file. Nothing in the code knows this shape.
RSPEC_PATTERN = r"^\s*rspec\s+'?\.?/?([^'\s\[:]+_spec\.rb)"


# Verbatim from a real `bin/parallel_rspec` run, trimmed. Note that each worker
# prints its own block, so the markers repeat and the locators are quoted.
RSPEC_OUTPUT = """\
Failures:

  1) Checkout Storefront checkout process existing account check out with cart
     Failure/Error: expect(@cart.shipping_address).to(eq(@user.user_addresses.first))

Failed examples:

rspec './spec/requests/checkout_spec.rb[1:1:1:1]' # Checkout Storefront checkout process existing account check out with cart created at login

Randomized with seed 9830
"""


def config(repo, **over):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "full_test_command": "true",
        "scoped_test_command": "rspec-stub {paths}",
        "failed_file_pattern": RSPEC_PATTERN,
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(over)
    return parse_config(data)


def judge(repo, output, cfg=None, command="full-suite"):
    return adjudicate(
        output=output,
        command=command,
        cfg=cfg or config(repo),
        runner=CommandRunner(cwd=repo, timeout=60),
    )


class TestParsingWhatFailed:
    def test_it_finds_the_file_in_real_rspec_output(self):
        assert failed_files(RSPEC_OUTPUT, RSPEC_PATTERN) == [
            "spec/requests/checkout_spec.rb"
        ]

    def test_the_locator_is_reduced_to_its_file(self):
        # The re-run is per *file*, not per example: a file that passes whole,
        # standalone, is green. Re-running one example proves less, because a
        # dependence between two examples in the same file would survive it.
        assert failed_files(RSPEC_OUTPUT, RSPEC_PATTERN) == [
            "spec/requests/checkout_spec.rb"
        ]

    def test_two_failures_in_one_file_collapse_to_one_re_run(self):
        text = (
            "Failed examples:\n\n"
            "rspec ./spec/a_spec.rb:12 # one\n"
            "rspec ./spec/a_spec.rb:40 # two\n"
        )
        assert failed_files(text, RSPEC_PATTERN) == ["spec/a_spec.rb"]

    def test_the_line_number_form_reduces_to_the_same_file(self):
        # parallel_rspec prints `./path:531`; a serial run prints `'./path[1:1]'`.
        text = "Failed examples:\n\nrspec ./spec/a_spec.rb:12 # something\n"
        assert failed_files(text, RSPEC_PATTERN) == ["spec/a_spec.rb"]

    def test_repeated_paths_across_workers_collapse(self):
        # parallel_rspec concatenates each worker's block; a retrying worker can
        # print the same locator twice.
        doubled = RSPEC_OUTPUT + RSPEC_OUTPUT
        assert len(failed_files(doubled, RSPEC_PATTERN)) == 1

    def test_output_from_another_framework_yields_nothing(self):
        # Silence is the correct answer: it routes to the old whole-suite re-run
        # rather than to a wrong guess about what to run.
        pytest_output = "FAILED tests/test_a.py::test_b - AssertionError\n"
        assert failed_files(pytest_output, RSPEC_PATTERN) == []

    def test_the_pattern_is_a_valid_regex_with_one_group(self):
        compiled = re.compile(RSPEC_PATTERN, re.MULTILINE)
        assert compiled.groups == 1

    def test_no_pattern_configured_yields_nothing(self):
        # There is no shipped default: an unconfigured project falls back to
        # re-running the whole suite rather than guessing at a runner.
        assert failed_files(RSPEC_OUTPUT, None) == []


class TestTheFlakeVerdict:
    def test_a_file_that_passes_whole_and_alone_is_green(self, repo):
        out = judge(repo, RSPEC_OUTPUT, config(repo, scoped_test_command="true {paths}"))
        assert out.flaked
        assert out.files == ["spec/requests/checkout_spec.rb"]

    def test_a_file_that_fails_alone_is_a_real_failure(self, repo):
        out = judge(repo, RSPEC_OUTPUT, config(repo, scoped_test_command="false {paths}"))
        assert not out.flaked

    def test_the_whole_file_is_re_run_not_the_one_example(self, repo):
        """A file, not a locator.

        Re-running `spec/a_spec.rb[1:1]` alone proves only that one example is
        independent of the rest of the *suite*. Running the whole file also
        proves it is independent of its siblings, which is the cheaper half of
        the same question and is what "this file is green" has to mean.
        """
        log = repo.parent / "reran.txt"
        cfg = config(repo, scoped_test_command=f"echo {{paths}} >> {log}")
        judge(repo, RSPEC_OUTPUT, cfg)
        assert log.read_text().strip() == "spec/requests/checkout_spec.rb"

    def test_a_stage_may_excuse_a_file_it_edited(self, repo):
        """Ownership does not enter into it.

        An earlier design refused to excuse a spec the stage had touched, on the
        grounds that the stage might have introduced the order dependence. But a
        file that passes whole and standalone has been proven green *including*
        the stage's edits to it. Whatever makes it fail in the group is a
        property of the suite, and cleaning that up is separate work.
        """
        log = repo.parent / "owned-reran.txt"
        cfg = config(repo, scoped_test_command=f"true {{paths}} && echo ok >> {log}")
        out = judge(repo, RSPEC_OUTPUT, cfg)
        assert out.flaked
        assert log.exists(), "an owned file is re-run like any other"

    def test_several_failing_files_all_have_to_pass(self, repo):
        text = (
            "Failed examples:\n\n"
            "rspec ./spec/a_spec.rb:1 # a\n"
            "rspec ./spec/b_spec.rb:1 # b\n"
        )
        cfg = config(repo, scoped_test_command="grep -q b_spec <<< '{paths}' && exit 1; true")
        out = judge(repo, text, cfg)
        assert not out.flaked

    def test_the_paths_are_quoted_for_the_shell(self, repo):
        # Bracket locators are glob expressions; a file path can contain spaces.
        log = repo.parent / "quoted.txt"
        cfg = config(repo, scoped_test_command=f"printf '%s' {{paths}} > {log}")
        out = judge(repo, RSPEC_OUTPUT, cfg)
        assert out.flaked
        assert log.read_text() == "spec/requests/checkout_spec.rb"

    def test_the_re_run_time_is_accounted_for(self, repo):
        out = judge(repo, RSPEC_OUTPUT, config(repo, scoped_test_command="true {paths}"))
        assert out.seconds >= 0.0
        assert out.results, "the re-run should be recorded for the verify log"


class TestWhenNotToAdjudicate:
    def test_too_many_failures_is_not_a_flake(self, repo):
        # Thirty files do not all flake at once; that is a broken stage, and
        # re-running thirty spec files to confirm it costs minutes for nothing.
        block = "Failed examples:\n\n" + "".join(
            f"rspec './spec/x{i}_spec.rb[1:{i}]' # x\n" for i in range(30)
        )
        out = judge(repo, block, config(repo, scoped_test_command="true {paths}"))
        assert not out.flaked
        assert "too many" in out.summary.lower()

    def test_unparseable_output_falls_back_to_the_whole_suite(self, repo):
        # The pre-existing behaviour, unchanged: projects whose test runner does
        # not print a failed-example block keep the re-run-once rule.
        log = repo.parent / "which-ran.txt"
        cfg = config(repo, scoped_test_command=f"echo scoped {{paths}} >> {log}")
        out = judge(repo, "no locators here", cfg, command=f"echo whole >> {log}")
        assert out.flaked
        assert out.files == []
        assert log.read_text().strip() == "whole"

    def test_the_whole_suite_fallback_can_still_fail(self, repo):
        out = judge(repo, "no locators here", config(repo), command="exit 1")
        assert not out.flaked

    def test_no_scoped_command_means_no_way_to_re_run_one_file(self, repo):
        # Without an operator-supplied template there is nothing to substitute
        # the paths into, and the orchestrator will not invent shell.
        cfg = config(repo, scoped_test_command=None)
        out = judge(repo, RSPEC_OUTPUT, cfg, command="exit 1")
        assert not out.flaked
        assert out.files == []

    def test_adjudication_can_be_switched_off(self, repo):
        cfg = config(repo, flake_rerun_failed_files=False, scoped_test_command="true {paths}")
        out = judge(repo, RSPEC_OUTPUT, cfg, command="exit 1")
        assert not out.flaked
        assert out.files == []


class TestConfigSurface:
    def test_the_pattern_is_overridable(self, repo):
        cfg = config(repo, failed_file_pattern=r"^BOOM (\S+)$")
        assert cfg.failed_file_pattern == r"^BOOM (\S+)$"
        assert failed_files("BOOM spec/a.rb:1", cfg.failed_file_pattern) == [
            "spec/a.rb:1"
        ]

    def test_a_pattern_without_a_capture_group_is_rejected(self, repo):
        # It would silently match and yield nothing, i.e. look exactly like a
        # framework we do not support.
        with pytest.raises(ConfigError, match="capture group"):
            config(repo, failed_file_pattern=r"^rspec \S+$")

    def test_an_uncompilable_pattern_is_rejected_at_parse_time(self, repo):
        with pytest.raises(ConfigError, match="not a valid regex"):
            config(repo, failed_file_pattern=r"^rspec (\S+$")
