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
from orchestrator.flake import DEFAULT_FAILED_EXAMPLE_PATTERN, adjudicate, failed_examples


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
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(over)
    return parse_config(data)


def judge(repo, output, cfg=None, owned=(), command="full-suite"):
    return adjudicate(
        output=output,
        command=command,
        cfg=cfg or config(repo),
        runner=CommandRunner(cwd=repo, timeout=60),
        owned_paths=set(owned),
    )


class TestParsingWhatFailed:
    def test_it_finds_the_locator_in_real_rspec_output(self):
        assert failed_examples(RSPEC_OUTPUT, DEFAULT_FAILED_EXAMPLE_PATTERN) == [
            "./spec/requests/checkout_spec.rb[1:1:1:1]"
        ]

    def test_the_locator_keeps_its_example_id(self):
        # Re-running the whole file would run its siblings too, which is most of
        # what we are trying to avoid.
        [only] = failed_examples(RSPEC_OUTPUT, DEFAULT_FAILED_EXAMPLE_PATTERN)
        assert only.endswith("[1:1:1:1]")

    def test_unquoted_line_and_colon_form_also_parse(self):
        text = "Failed examples:\n\nrspec ./spec/a_spec.rb:12 # something\n"
        assert failed_examples(text, DEFAULT_FAILED_EXAMPLE_PATTERN) == [
            "./spec/a_spec.rb:12"
        ]

    def test_repeated_locators_across_workers_collapse(self):
        # parallel_rspec concatenates each worker's block; a retrying worker can
        # print the same locator twice.
        doubled = RSPEC_OUTPUT + RSPEC_OUTPUT
        assert len(failed_examples(doubled, DEFAULT_FAILED_EXAMPLE_PATTERN)) == 1

    def test_output_from_another_framework_yields_nothing(self):
        # Silence is the correct answer: it routes to the old whole-suite re-run
        # rather than to a wrong guess about what to run.
        pytest_output = "FAILED tests/test_a.py::test_b - AssertionError\n"
        assert failed_examples(pytest_output, DEFAULT_FAILED_EXAMPLE_PATTERN) == []

    def test_the_pattern_is_a_valid_regex_with_one_group(self):
        compiled = re.compile(DEFAULT_FAILED_EXAMPLE_PATTERN, re.MULTILINE)
        assert compiled.groups == 1


class TestTheFlakeVerdict:
    def test_examples_that_pass_alone_are_a_flake(self, repo):
        out = judge(repo, RSPEC_OUTPUT, config(repo, scoped_test_command="true {paths}"))
        assert out.flaked
        assert out.examples == ["./spec/requests/checkout_spec.rb[1:1:1:1]"]

    def test_examples_that_fail_alone_are_real(self, repo):
        out = judge(repo, RSPEC_OUTPUT, config(repo, scoped_test_command="false {paths}"))
        assert not out.flaked

    def test_only_the_failed_examples_are_re_run(self, repo):
        log = repo.parent / "reran.txt"
        cfg = config(repo, scoped_test_command=f"echo {{paths}} >> {log}")
        judge(repo, RSPEC_OUTPUT, cfg)
        assert log.read_text().strip() == "./spec/requests/checkout_spec.rb[1:1:1:1]"

    def test_the_locator_is_quoted_for_the_shell(self, repo):
        # `[1:1:1:1]` is a glob bracket expression. Unquoted, zsh fails the whole
        # command with "no matches found" and bash silently passes a literal that
        # may or may not be what rspec wanted.
        log = repo.parent / "quoted.txt"
        cfg = config(repo, scoped_test_command=f"printf '%s' {{paths}} > {log}")
        out = judge(repo, RSPEC_OUTPUT, cfg)
        assert out.flaked, "an unquoted bracket would have failed the command"
        assert log.read_text() == "./spec/requests/checkout_spec.rb[1:1:1:1]"

    def test_the_re_run_time_is_accounted_for(self, repo):
        out = judge(repo, RSPEC_OUTPUT, config(repo, scoped_test_command="true {paths}"))
        assert out.seconds >= 0.0
        assert out.results, "the re-run should be recorded for the verify log"


class TestOwnershipEndsTheArgument:
    """A stage cannot excuse a spec it was working on.

    Passing alone is evidence of order dependence, and order dependence in a
    file the stage just edited is at least as likely to be something the stage
    introduced as something that was already there.
    """

    def test_a_file_the_stage_touched_is_never_a_flake(self, repo):
        out = judge(
            repo,
            RSPEC_OUTPUT,
            config(repo, scoped_test_command="true {paths}"),
            owned=["spec/requests/checkout_spec.rb"],
        )
        assert not out.flaked
        assert "checkout_spec.rb" in out.summary

    def test_an_owned_failure_is_not_even_re_run(self, repo):
        # The verdict cannot change, so the suite time is pure waste.
        marker = repo.parent / "should-not-run"
        out = judge(
            repo,
            RSPEC_OUTPUT,
            config(repo, scoped_test_command=f"touch {marker} # {{paths}}"),
            owned=["spec/requests/checkout_spec.rb"],
        )
        assert not out.flaked
        assert not marker.exists()

    def test_ownership_matching_ignores_the_leading_dot_slash(self, repo):
        # rspec prints `./spec/...`; git prints `spec/...`.
        out = judge(
            repo,
            RSPEC_OUTPUT,
            config(repo, scoped_test_command="true {paths}"),
            owned=["./spec/requests/checkout_spec.rb"],
        )
        assert not out.flaked

    def test_a_declared_directory_covers_the_specs_beneath_it(self, repo):
        """`test_paths` are paths, and a planner will name directories.

        The first real stage declared `test_paths: ["spec/controllers",
        "spec/requests"]` and the merge gate then failed on
        `spec/controllers/admin/orders_controller_spec.rb`. Exact matching made
        that a flake: the stage had named the directory as the thing that
        proves it, and would still have been excused a failure inside it.
        """
        out = judge(
            repo,
            "Failed examples:\n\nrspec ./spec/controllers/admin/orders_spec.rb:531 # x\n",
            config(repo, scoped_test_command="true {paths}"),
            owned=["spec/controllers"],
        )
        assert not out.flaked
        assert "spec/controllers" in out.summary

    def test_a_directory_does_not_own_a_merely_similar_sibling(self, repo):
        # `spec/controllers` must not swallow `spec/controllers_helper`.
        out = judge(
            repo,
            "Failed examples:\n\nrspec ./spec/controllers_helper/a_spec.rb:1 # x\n",
            config(repo, scoped_test_command="true {paths}"),
            owned=["spec/controllers"],
        )
        assert out.flaked

    def test_an_unrelated_owned_file_does_not_block_the_flake(self, repo):
        out = judge(
            repo,
            RSPEC_OUTPUT,
            config(repo, scoped_test_command="true {paths}"),
            owned=["app/models/order.rb", "spec/models/order_spec.rb"],
        )
        assert out.flaked


class TestWhenNotToAdjudicate:
    def test_too_many_failures_is_not_a_flake(self, repo):
        # Thirty examples do not all flake at once; that is a broken stage, and
        # re-running thirty specs to confirm it costs minutes for nothing.
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
        assert out.examples == []
        assert log.read_text().strip() == "whole"

    def test_the_whole_suite_fallback_can_still_fail(self, repo):
        out = judge(repo, "no locators here", config(repo), command="exit 1")
        assert not out.flaked

    def test_no_scoped_command_means_no_way_to_re_run_one_example(self, repo):
        # Without an operator-supplied template there is nothing to substitute
        # the locators into, and the orchestrator will not invent shell.
        cfg = config(repo, scoped_test_command=None)
        out = judge(repo, RSPEC_OUTPUT, cfg, command="exit 1")
        assert not out.flaked
        assert out.examples == []

    def test_adjudication_can_be_switched_off(self, repo):
        cfg = config(repo, flake_rerun_examples=False, scoped_test_command="true {paths}")
        out = judge(repo, RSPEC_OUTPUT, cfg, command="exit 1")
        assert not out.flaked
        assert out.examples == []


class TestConfigSurface:
    def test_the_pattern_is_overridable(self, repo):
        cfg = config(repo, failed_example_pattern=r"^BOOM (\S+)$")
        assert cfg.failed_example_pattern == r"^BOOM (\S+)$"
        assert failed_examples("BOOM spec/a.rb:1", cfg.failed_example_pattern) == [
            "spec/a.rb:1"
        ]

    def test_a_pattern_without_a_capture_group_is_rejected(self, repo):
        # It would silently match and yield nothing, i.e. look exactly like a
        # framework we do not support.
        with pytest.raises(ConfigError, match="capture group"):
            config(repo, failed_example_pattern=r"^rspec \S+$")

    def test_an_uncompilable_pattern_is_rejected_at_parse_time(self, repo):
        with pytest.raises(ConfigError, match="not a valid regex"):
            config(repo, failed_example_pattern=r"^rspec (\S+$")
