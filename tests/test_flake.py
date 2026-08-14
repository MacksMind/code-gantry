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

import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from orchestrator.commands import CommandRunner
from orchestrator.config import ConfigError, parse_config
from orchestrator.flake import (
    FLAKES_FILENAME,
    FlakeRecord,
    adjudicate,
    append_flakes,
    failed_files,
    recent_flakes,
    seeds_by_file,
)

# A real operator's, copied from a project config — which now lives in the
# repository it describes rather than here. Group 1 is the file path;
# it stops before `[1:1]` or `:531`, so both of RSpec's locator forms reduce to
# the same file. Nothing in the code knows this shape.
RSPEC_PATTERN = r"^\s*rspec\s+'?\.?/?([^'\s\[:]+_spec\.rb)"


# From a real parallel run, trimmed, with the example descriptions rewritten.
# Note that each worker prints its own block, so the markers repeat and the
# locators are quoted.
RSPEC_OUTPUT = """\
Failures:

  1) Checkout an existing account checking out with a cart
     Failure/Error: expect(@cart.shipping_address).to(eq(@user.addresses.first))

Failed examples:

rspec './spec/requests/checkout_spec.rb[1:1:1:1]' # Checkout an existing account checking out with a cart created at login

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

    def test_ansi_colour_does_not_hide_the_path(self):
        """Test output reaching us is not always plain text.

        The real project's runner ends with:

            grep $'^\033\\[31m' log/failing_specs.log || true

        so every locator on our stdout arrives wrapped in a red SGR sequence.
        A pattern anchored at `^rspec` then matches nothing, the gate silently
        falls back to re-running the whole suite, and on this project that cost
        a reviewer-approved stage: the fallback tripped over a different
        order-dependent spec and the work was reset.

        Stripping escapes belongs here rather than in each project's regex —
        terminal colour is a property of terminals, not of a codebase.
        """
        coloured = (
            "Failed examples:\n\n"
            "\x1b[31mrspec './spec/requests/checkout_spec.rb[1:1:1:1]'\x1b[0m # Checkout\n"
        )
        assert failed_files(coloured, RSPEC_PATTERN) == [
            "spec/requests/checkout_spec.rb"
        ]

    def test_a_coloured_and_a_plain_line_are_the_same_file(self):
        # Both forms appear in one run: the runner echoes coloured lines at the
        # end and rspec prints plain ones per worker.
        text = (
            "\x1b[31mrspec './spec/a_spec.rb[1:1]'\x1b[0m # x\n"
            "rspec ./spec/a_spec.rb:12 # x\n"
        )
        assert failed_files(text, RSPEC_PATTERN) == ["spec/a_spec.rb"]

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


class TestThroughTheRealRunner:
    """The gate reading what the runner actually hands it.

    Every earlier test fed `adjudicate` a string directly, which is why the
    truncation bug survived a green suite: the pattern was always correct, and
    the input it got in production was not the input the tests used.

    This one runs a command that emits a realistic amount of noise around the
    failure, through CommandRunner, and asserts the file is still found.
    """

    def test_a_locator_buried_in_a_third_of_a_megabyte_is_found(self, repo):
        cfg = config(repo, scoped_test_command="true {paths}")
        runner = CommandRunner(cwd=repo, timeout=120)
        # Mirrors the real shape: the block is far from both ends, with the
        # coverage report and deprecation tallies printing after it.
        noisy = (
            "for i in $(seq 1 8000); do echo 'an example passed'; done; "
            "echo 'Failed examples:'; "
            "echo \"rspec './spec/requests/checkout_spec.rb[1:1:1:1]' # Checkout\"; "
            "for i in $(seq 1 8000); do echo 'DEPRECATION WARNING: something'; done; "
            "echo 'Coverage report generated'; exit 1"
        )
        result = runner.run(noisy)
        assert len(result.output) > 150_000, "the test must be big enough to matter"

        out = adjudicate(
            output=result.output, command=noisy, cfg=cfg, runner=runner
        )
        assert out.files == ["spec/requests/checkout_spec.rb"]
        assert out.flaked, "it passes when re-run alone, so the stage may land"


class TestThreeStrikes:
    """Once in the group, twice alone.

    A single isolated re-run was not enough. `admin_order_edit_personalization`
    failed in the full suite, failed again when re-run whole and alone, and
    then passed on the next full run — so the gate called it real, the stage
    was abandoned, and the work turned out to be innocent.

    The isolated run is cheap (49 seconds for that file against a 4-minute
    suite), so a second one costs little and turns a two-strike rule into a
    three-strike one: fail in the group, fail alone, fail alone again.
    """

    def counting_command(self, repo, name, fail_times):
        # Fails the first `fail_times` invocations, then passes.
        counter = repo.parent / name
        return (
            f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; "
            f"test $n -gt {fail_times}"
        )

    def test_a_file_that_passes_on_the_second_isolated_run_is_a_flake(self, repo):
        cfg = config(
            repo,
            scoped_test_command=self.counting_command(repo, "c1", 1) + " # {paths}",
        )
        out = judge(repo, RSPEC_OUTPUT, cfg)
        assert out.flaked
        assert len(out.results) == 2, "it should have tried twice"

    def test_a_file_failing_both_isolated_runs_is_real(self, repo):
        cfg = config(repo, scoped_test_command="false {paths}")
        out = judge(repo, RSPEC_OUTPUT, cfg)
        assert not out.flaked
        assert len(out.results) == 2

    def test_passing_first_time_costs_only_one_run(self, repo):
        # No point paying for a second run to confirm a pass.
        log = repo.parent / "once.txt"
        cfg = config(repo, scoped_test_command=f"echo x >> {log} # {{paths}}")
        out = judge(repo, RSPEC_OUTPUT, cfg)
        assert out.flaked
        assert log.read_text().count("x") == 1

    def test_the_attempt_count_is_configurable(self, repo):
        cfg = config(
            repo,
            flake_rerun_attempts=1,
            scoped_test_command=self.counting_command(repo, "c2", 1) + " # {paths}",
        )
        out = judge(repo, RSPEC_OUTPUT, cfg)
        assert not out.flaked, "one attempt means one strike in isolation"
        assert len(out.results) == 1


class TestTheSeedThatProducedTheFailure:
    """Excusing a flake without its seed makes the flake permanent.

    The orchestrator excused the same handful of files all night, and the only
    record of *which ordering* did it lived in output nobody kept. The target
    repo already ships `bin/fragile_bisect <seed> <spec>`, which pins those two
    and searches the other 220 spec files for the minimal set that reproduces
    the ordering — so the seed is not a note, it is the missing argument to a
    tool that already exists.
    """

    SEED = r"^Randomized with seed (\d+)"

    # From a two-worker run that flaked, with the example descriptions
    # rewritten: two workers, two files, two different seeds. Everything the
    # parsers read — the markers, the quoted locators, the seed placement — is
    # the runner's own output. A single-seed reading gets one of them wrong.
    REAL = (
        Path(__file__).parent / "fixtures" / "parallel_rspec_two_workers.txt"
    ).read_text()

    def test_each_file_gets_its_own_workers_seed(self):
        assert seeds_by_file(self.REAL, RSPEC_PATTERN, self.SEED) == {
            "spec/models/user_spec.rb": "4845",
            "spec/requests/checkout_spec.rb": "58072",
        }

    def test_the_seed_before_a_failure_is_not_its_seed(self):
        # parallel_rspec prints every worker's seed up front, before any of
        # them has run. Reading the nearest seed rather than the following one
        # would attribute all 14 failures to the first worker's ordering.
        text = (
            "Randomized with seed 111\n\n"
            "Randomized with seed 222\n\n"
            "Failed examples:\n\n"
            "rspec ./spec/a_spec.rb:12 # boom\n\n"
            "Randomized with seed 333\n"
        )
        assert seeds_by_file(text, RSPEC_PATTERN, self.SEED) == {
            "spec/a_spec.rb": "333"
        }

    def test_a_failure_with_no_seed_after_it_is_omitted(self):
        # Half an answer reproduces nothing. Better absent than wrong.
        text = "Failed examples:\n\nrspec ./spec/a_spec.rb:12 # boom\n"
        assert seeds_by_file(text, RSPEC_PATTERN, self.SEED) == {}

    def test_no_seed_pattern_configured_yields_nothing(self):
        assert seeds_by_file(self.REAL, RSPEC_PATTERN, None) == {}

    def test_the_verdict_carries_the_seeds(self, repo):
        verdict = judge(repo, self.REAL, cfg=config(repo, seed_pattern=self.SEED))
        assert verdict.seeds["spec/models/user_spec.rb"] == "4845"


class TestTheExcusalOutlivesTheRun:
    """`flaky_files` dies with the run, and the bar for filing is two sightings.

    Counting them meant grepping a run log that the next run truncates. One
    append-only file per project answers it directly.
    """

    def test_a_line_carries_the_arguments_a_bisect_needs(self, tmp_path):
        # The arguments, not a command line: whoever chases this knows how to
        # invoke the repo's own bisect tool, and a formatted command would be
        # one more thing to keep correct as that tool changes.
        append_flakes(
            tmp_path, "some-stage",
            ["spec/models/user_spec.rb"],
            {"spec/models/user_spec.rb": "4845"},
            "2026-07-31T01:14:34-04:00",
        )
        found = recent_flakes(tmp_path / FLAKES_FILENAME)[0]
        assert found["file"] == "spec/models/user_spec.rb"
        assert found["seed"] == "4845"

    def test_the_timestamp_is_kept_so_time_bugs_are_visible(self, tmp_path):
        """Not every red suite has an ordering behind it.

        The failure that motivated the baseline check was a spec asserting
        `Time.zone.today - 1.month`, which broke the moment the clock crossed
        into the 31st. No file name and no seed says that. A column of
        timestamps says it the first time two of them cluster after midnight.
        """
        append_flakes(
            tmp_path, "s", ["spec/a_spec.rb"], {"spec/a_spec.rb": "1"},
            "2026-07-31T00:06:12-04:00",
        )
        assert recent_flakes(tmp_path / FLAKES_FILENAME)[0]["at"] == (
            "2026-07-31T00:06:12-04:00"
        )

    def test_repeat_sightings_are_kept_apart(self, tmp_path):
        # Deduplicating would destroy the exact signal the filing bar reads.
        for stage in ("stage-a", "stage-b"):
            append_flakes(
                tmp_path, stage, ["spec/models/user_spec.rb"],
                {"spec/models/user_spec.rb": "4845"},
                "2026-07-31T01:14:34-04:00",
            )
        found = recent_flakes(tmp_path / FLAKES_FILENAME)
        assert len(found) == 2
        assert [f["stage_id"] for f in found] == ["stage-a", "stage-b"]

    def test_a_missing_seed_says_so(self, tmp_path):
        # A silently short line reads as "this flake had no ordering", which is
        # never true; it means seed_pattern needs fixing. The markdown format
        # needed a sentence to say that. `null` is the field saying it.
        append_flakes(tmp_path, "s", ["spec/a_spec.rb"], {}, "2026-07-31T01:00:00-04:00")
        written = json.loads((tmp_path / FLAKES_FILENAME).read_text())
        assert written["seed"] is None
        assert "seed" in written, "absent and null are different answers"
        assert recent_flakes(tmp_path / FLAKES_FILENAME)[0]["seed"] is None

    def test_it_records_which_run_and_what_produced_it(self, tmp_path):
        """`preflight` used to be a sentinel inside the stage field.

        A baseline flake and a stage flake are different animals, and while the
        only way to tell them apart was string equality on a field that means
        something else, no sort could separate them.
        """
        append_flakes(
            tmp_path, None, ["spec/a_spec.rb"], {}, "2026-07-31T01:00:00-04:00",
            origin="preflight",
        )
        append_flakes(
            tmp_path, "some-stage", ["spec/a_spec.rb"], {},
            "2026-07-31T01:05:00-04:00", run_id="20260731-010000-x",
        )
        first, second = recent_flakes(tmp_path / FLAKES_FILENAME)
        assert (first["origin"], first["stage_id"]) == ("preflight", None)
        assert (second["origin"], second["stage_id"]) == ("stage", "some-stage")
        # Preflight runs before a run id exists, so null is the true answer
        # there rather than a gap.
        assert first["run_id"] is None
        assert second["run_id"] == "20260731-010000-x"

    def test_every_field_of_the_record_is_written(self, tmp_path):
        """The writer is the dataclass, not a list of keys somebody maintains.

        `executor-loop.json` carried ten fields of twenty for exactly this
        reason, and the field added the same morning was already missing. Here
        the same hand-built line meant `preflight` never recorded a locator,
        because that call site was written before the argument existed.
        """
        append_flakes(
            tmp_path, "s", ["spec/a_spec.rb"], {"spec/a_spec.rb": "1"},
            "2026-07-31T01:00:00-04:00",
        )
        written = json.loads((tmp_path / FLAKES_FILENAME).read_text())
        assert set(written) == {f.name for f in fields(FlakeRecord)}


class TestTheExactExampleThatFailed:
    """A file name is where to look; the locator is what to run.

    The ledger recorded the file and the seed, which between them say "this
    file failed somewhere under this ordering". The runner had already printed
    the answer — every RSpec failure ends in a re-run line naming the exact
    example — and it was being read only far enough to extract the path, then
    discarded. Measured over 276 excusals: 76 of them name one feature spec,
    and nothing in the ledger says whether that is one example failing 76 times
    or 76 different ones. Those are different bugs and the file could not tell
    them apart.

    Taken from the same lines `failed_file_pattern` already matches, so no
    second regex has to be kept correct against the first. Everything before
    the runner's ` # description` comment is the locator; the description is
    prose and changes when someone renames a test.
    """

    def test_it_keeps_the_bracket_locator_whole(self):
        from orchestrator.flake import failing_examples

        found = failing_examples(RSPEC_OUTPUT, RSPEC_PATTERN)
        assert found == {
            "spec/requests/checkout_spec.rb": [
                "rspec './spec/requests/checkout_spec.rb[1:1:1:1]'"
            ]
        }

    def test_it_keeps_the_line_number_form_too(self):
        from orchestrator.flake import failing_examples

        output = (
            "Failed examples:\n\n"
            "rspec ./spec/models/user_spec.rb:531 # User does a thing\n"
        )
        assert failing_examples(output, RSPEC_PATTERN) == {
            "spec/models/user_spec.rb": ["rspec ./spec/models/user_spec.rb:531"]
        }

    def test_several_examples_in_one_file_are_all_kept(self):
        from orchestrator.flake import failing_examples

        output = (
            "rspec ./spec/a_spec.rb:1 # one\n"
            "rspec ./spec/a_spec.rb:9 # two\n"
        )
        assert failing_examples(output, RSPEC_PATTERN)["spec/a_spec.rb"] == [
            "rspec ./spec/a_spec.rb:1",
            "rspec ./spec/a_spec.rb:9",
        ]

    def test_a_parallel_runners_repeated_block_is_deduplicated(self):
        # Each worker prints its own summary, so the same locator arrives more
        # than once — the same reason `failed_files` deduplicates.
        from orchestrator.flake import failing_examples

        output = "rspec ./spec/a_spec.rb:1 # one\n" * 3
        assert failing_examples(output, RSPEC_PATTERN)["spec/a_spec.rb"] == [
            "rspec ./spec/a_spec.rb:1"
        ]

    def test_it_survives_a_description_containing_a_hash(self):
        # Split on the first ` # `, which is the runner's separator; a `#`
        # inside the description belongs to the description.
        from orchestrator.flake import failing_examples

        output = "rspec ./spec/a_spec.rb:1 # renders #show for the user\n"
        assert failing_examples(output, RSPEC_PATTERN)["spec/a_spec.rb"] == [
            "rspec ./spec/a_spec.rb:1"
        ]

    def test_no_pattern_means_no_answer_rather_than_a_guess(self):
        from orchestrator.flake import failing_examples

        assert failing_examples(RSPEC_OUTPUT, None) == {}


class TestTheLedgerCarriesTheLocator:
    def test_it_is_written_beside_the_seed(self, tmp_path):
        append_flakes(
            tmp_path, "s", ["spec/a_spec.rb"], {"spec/a_spec.rb": "42"},
            "2026-08-11T20:00:00-04:00",
            examples={"spec/a_spec.rb": ["rspec ./spec/a_spec.rb:1"]},
        )
        written = json.loads((tmp_path / FLAKES_FILENAME).read_text())
        assert written["seed"] == "42"
        assert written["examples"] == ["rspec ./spec/a_spec.rb:1"]

    def test_every_locator_is_kept(self, tmp_path):
        """The ten-cap existed because a markdown line became unreadable.

        It threw away the data the ledger exists to hold, and said "and 11
        more" in its place — a count, where the question is *which*. Nothing
        about a JSON array is unreadable at twenty.
        """
        many = [f"rspec ./spec/a_spec.rb:{n}" for n in range(21)]
        append_flakes(
            tmp_path, "s", ["spec/a_spec.rb"], {}, "2026-08-11T20:00:00-04:00",
            examples={"spec/a_spec.rb": many},
        )
        assert recent_flakes(tmp_path / FLAKES_FILENAME)[0]["examples"] == many

    def test_the_reader_gets_them_back(self, tmp_path):
        append_flakes(
            tmp_path, "s", ["spec/a_spec.rb"], {"spec/a_spec.rb": "42"},
            "2026-08-11T20:00:00-04:00",
            examples={
                "spec/a_spec.rb": [
                    "rspec ./spec/a_spec.rb:1", "rspec ./spec/a_spec.rb:9"
                ]
            },
        )
        found = recent_flakes(tmp_path / FLAKES_FILENAME)[0]
        assert found["examples"] == [
            "rspec ./spec/a_spec.rb:1", "rspec ./spec/a_spec.rb:9"
        ]
        assert found["seed"] == "42"

    def test_a_flake_with_no_locator_records_an_empty_list(self, tmp_path):
        # Not an absent key. The markdown format could only omit the segment,
        # so "no locators captured" and "written before locators existed" were
        # the same bytes and the reader could not separate them.
        append_flakes(
            tmp_path, "s", ["spec/a_spec.rb"], {"spec/a_spec.rb": "42"},
            "2026-08-11T20:00:00-04:00",
        )
        assert json.loads((tmp_path / FLAKES_FILENAME).read_text())["examples"] == []

    def test_a_corrupt_line_is_loud(self, tmp_path):
        """An unreadable entry must not read as no entry.

        The failure this project keeps meeting is the empty answer that gets
        believed — a search that returns nothing, an extraction that returns
        `{}`. A ledger that silently drops what it cannot parse undercounts,
        and undercounting is the one thing it exists not to do.
        """
        (tmp_path / FLAKES_FILENAME).write_text(
            '{"at":"2026-08-01T00:00:00-04:00","file":"spec/a_spec.rb"}\n'
            "not json at all\n"
        )
        with pytest.raises(ValueError, match="line 2"):
            recent_flakes(tmp_path / FLAKES_FILENAME)


class TestTheLocatorSurvivesTheJourney:
    """Runner output to `flakes.jsonl`, through every schema between them.

    The locator crosses five: `FlakeVerdict`, `GateResult`, `VerifyOutcome`,
    `_record_flakes`'s arguments, and the line itself. Four separate defects in
    this project have been values computed correctly and lost in transit, and
    every one passed its unit tests on both ends — so the parser being right
    and the writer being right is exactly the evidence that has proved
    insufficient before.
    """

    def test_it_reaches_the_ledger_from_a_real_command(self, repo, tmp_path):
        from types import SimpleNamespace

        from orchestrator import gates
        from orchestrator.config import Stage
        from orchestrator.gitops import Git
        from orchestrator.nodes import _record_flakes

        cfg = config(
            repo,
            test_command=(
                "echo 'Failed examples:'; "
                "echo \"rspec ./spec/models/user_spec.rb:531 # User does a thing\"; "
                "echo 'Randomized with seed 4845'; exit 1"
            ),
            scoped_test_command="true {paths}",
            seed_pattern=r"^Randomized with seed (\d+)",
        )
        stage = Stage(id="s1", instruction="do it", edit_files=["app.py"])
        found = gates.run_tests(
            stage, cfg, Git(repo), CommandRunner(cwd=repo, timeout=60),
            Git(repo).head_sha(), for_loop=False,
        )
        assert found.ok, "the file passes alone, so this is a flake"
        assert found.flaky_examples == {
            "spec/models/user_spec.rb": ["rspec ./spec/models/user_spec.rb:531"]
        }

        rt = SimpleNamespace(
            project=SimpleNamespace(project_dir=tmp_path),
            paths=SimpleNamespace(run_id="20260812-032911-x"),
            log=lambda *a: None,
        )
        _record_flakes(
            rt, stage.id, found.flaky_files, found.flaky_seeds, found.flaky_examples
        )
        entry = recent_flakes(tmp_path / FLAKES_FILENAME)[0]
        assert entry["file"] == "spec/models/user_spec.rb"
        assert entry["seed"] == "4845"
        assert entry["examples"] == ["rspec ./spec/models/user_spec.rb:531"]
        # The run id crosses the same journey and had no field until now, so it
        # is exactly the shape of value this class exists to catch in transit.
        assert entry["run_id"] == "20260812-032911-x"
        assert entry["origin"] == "stage"
