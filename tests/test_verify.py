"""The layered verify gate.

Ordering is the whole point: free deterministic checks run before anything
that costs minutes of compute or a paid API call. Routing matters too —
containment and environment failures escalate, work-quality failures retry.
"""

from orchestrator.commands import CommandRunner
from orchestrator.config import parse_config
from orchestrator.gitops import Git
from orchestrator.verify import Layer, run_verify


def build(repo, stage_overrides=None, **cfg_overrides):
    """A config whose single stage can be adjusted per test."""
    stage = {
        "id": "s1",
        "instruction": "do it",
        "edit_files": ["app.py", "src/**"],
    }
    stage.update(stage_overrides or {})
    data = {
        "target_repo": str(repo),
        "branch": "work",
        "test_command": "true",
        "executor": {"model": "m"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": [stage],
    }
    data.update(cfg_overrides)
    return parse_config(data)


def verify(repo, cfg, sha):
    return run_verify(
        stage=cfg.stages[0],
        cfg=cfg,
        git=Git(repo),
        runner=CommandRunner(cwd=repo, timeout=60),
        stage_start_sha=sha,
    )


def edit(repo, name="app.py", text="changed\n"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestHappyPath:
    def test_passes_when_everything_is_green(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        out = verify(repo, build(repo), sha)
        assert out.passed
        assert out.failed_layer is None

    def test_reports_no_feedback_on_success(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        assert verify(repo, build(repo), sha).feedback == ""


class TestSetupLayer:
    def test_setup_failure_fails_the_stage(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        out = verify(repo, build(repo, setup_command="false"), sha)
        assert not out.passed
        assert out.failed_layer is Layer.SETUP

    def test_setup_failure_is_not_retryable(self, repo):
        # A broken environment is not a defect in the diff, and rework will
        # not fix it.
        sha = Git(repo).head_sha()
        edit(repo)
        out = verify(repo, build(repo, setup_command="false"), sha)
        assert not out.retryable

    def test_setup_runs_before_the_tests(self, repo):
        # The tests may depend on what setup builds.
        sha = Git(repo).head_sha()
        edit(repo)
        marker = repo.parent / "setup-ran"  # outside the repo: not stage scope
        cfg = build(
            repo,
            setup_command=f"touch {marker}",
            test_command=f"test -f {marker}",
        )
        assert verify(repo, cfg, sha).passed

    def test_stage_setup_overrides_global(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, stage_overrides={"setup_command": "true"}, setup_command="false")
        assert verify(repo, cfg, sha).passed


class TestScopeGuard:
    def test_in_scope_edit_passes(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "x\n")
        assert verify(repo, build(repo), sha).passed

    def test_out_of_scope_edit_fails(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "unrelated/other.py", "x\n")
        out = verify(repo, build(repo), sha)
        assert not out.passed
        assert out.failed_layer is Layer.SCOPE

    def test_out_of_scope_edit_is_not_retryable(self, repo):
        # An executor editing outside its declared scope is a containment
        # failure, not a quality problem.
        sha = Git(repo).head_sha()
        edit(repo, "unrelated/other.py", "x\n")
        assert not verify(repo, build(repo), sha).retryable

    def test_feedback_names_the_offending_paths(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "unrelated/other.py", "x\n")
        out = verify(repo, build(repo), sha)
        assert "unrelated/other.py" in out.feedback

    def test_scope_guard_is_skipped_for_manual_stages(self, repo):
        # A human bump legitimately touches Dockerfile, lockfiles, and CI.
        sha = Git(repo).head_sha()
        edit(repo, "Dockerfile", "FROM ruby:2.4\n")
        cfg = build(
            repo,
            stage_overrides={
                "id": "bump",
                "kind": "manual",
                "human_steps": "bump it",
                "instruction": None,
                "edit_files": [],
            },
        )
        assert verify(repo, cfg, sha).passed

    def test_scope_guard_runs_before_the_tests(self, repo):
        # A containment failure should not wait behind a slow suite.
        sha = Git(repo).head_sha()
        edit(repo, "unrelated/other.py", "x\n")
        cfg = build(repo, test_command="exit 1")
        assert verify(repo, cfg, sha).failed_layer is Layer.SCOPE


class TestEmptyDiff:
    def test_empty_diff_fails(self, repo):
        # An executor that returned without editing anything has not done the
        # stage, and a green suite proves nothing about that.
        sha = Git(repo).head_sha()
        out = verify(repo, build(repo), sha)
        assert not out.passed
        assert out.failed_layer is Layer.SCOPE
        assert "no changes" in out.feedback.lower()

    def test_empty_diff_is_retryable_for_an_agent_stage(self, repo):
        sha = Git(repo).head_sha()
        assert verify(repo, build(repo), sha).retryable

    def test_empty_diff_is_not_retryable_for_a_manual_stage(self, repo):
        # Retrying a human is not a thing the orchestrator can do.
        sha = Git(repo).head_sha()
        cfg = build(
            repo,
            stage_overrides={
                "id": "bump",
                "kind": "manual",
                "human_steps": "bump it",
                "instruction": None,
                "edit_files": [],
            },
        )
        out = verify(repo, cfg, sha)
        assert not out.passed
        assert not out.retryable


class TestForbiddenPatterns:
    def test_added_line_matching_a_pattern_fails(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "belongs_to :thing, optional: true\n")
        cfg = build(repo, stage_overrides={"forbidden_patterns": [r"optional:\s*true"]})
        out = verify(repo, cfg, sha)
        assert not out.passed
        assert out.failed_layer is Layer.PATTERNS

    def test_pattern_failure_is_retryable(self, repo):
        # Feedback of the form "you used an API this stage forbids" is exactly
        # what a rework attempt can act on.
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "optional: true\n")
        cfg = build(repo, stage_overrides={"forbidden_patterns": ["optional: true"]})
        assert verify(repo, cfg, sha).retryable

    def test_feedback_quotes_the_offending_line_and_file(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "optional: true\n")
        cfg = build(repo, stage_overrides={"forbidden_patterns": ["optional: true"]})
        out = verify(repo, cfg, sha)
        assert "app.py" in out.feedback
        assert "optional: true" in out.feedback

    def test_removed_line_matching_a_pattern_passes(self, repo):
        # The stage whose purpose is deleting a construct must be able to
        # forbid it without flagging its own success.
        (repo / "app.py").write_text("render nothing: true\n")
        Git(repo).commit_all("seed")
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "head :ok\n")
        cfg = build(repo, stage_overrides={"forbidden_patterns": ["render nothing"]})
        assert verify(repo, cfg, sha).passed

    def test_patterns_run_before_the_tests(self, repo):
        # Deterministic and free; it must not wait behind a suite.
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "optional: true\n")
        cfg = build(
            repo,
            stage_overrides={"forbidden_patterns": ["optional: true"]},
            test_command="exit 1",
        )
        assert verify(repo, cfg, sha).failed_layer is Layer.PATTERNS


class TestTests:
    def test_failing_tests_fail_the_stage(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        out = verify(repo, build(repo, test_command="exit 1"), sha)
        assert not out.passed
        assert out.failed_layer is Layer.TESTS

    def test_failing_tests_are_retryable(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        assert verify(repo, build(repo, test_command="exit 1"), sha).retryable

    def test_feedback_includes_test_output(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, test_command="echo DISTINCTIVE_FAILURE; exit 1")
        assert "DISTINCTIVE_FAILURE" in verify(repo, cfg, sha).feedback

    def test_stage_test_command_overrides_global(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, stage_overrides={"test_command": "true"}, test_command="exit 1")
        assert verify(repo, cfg, sha).passed

    def test_absent_test_command_skips_the_layer(self, repo):
        # Greenfield: nothing exists to run yet, checks carry verification.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(
            repo,
            stage_overrides={"checks": ["true"]},
            test_command=None,
        )
        assert verify(repo, cfg, sha).passed

    def test_records_test_runtime(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        assert verify(repo, build(repo), sha).test_seconds >= 0


class TestFlakeRerun:
    def test_a_test_that_passes_on_rerun_does_not_fail_the_stage(self, repo):
        # Browser-driven suites otherwise burn the whole retry budget on noise
        # and escalate falsely.
        sha = Git(repo).head_sha()
        edit(repo)
        flag = repo.parent / "flake-flag"
        cfg = build(
            repo,
            test_command=f"if [ -f {flag} ]; then exit 0; else touch {flag}; exit 1; fi",
        )
        out = verify(repo, cfg, sha)
        assert out.passed
        assert out.flake_reruns == 1

    def test_a_consistently_failing_test_still_fails(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        out = verify(repo, build(repo, test_command="exit 1"), sha)
        assert not out.passed
        assert out.flake_reruns == 0

    def test_a_test_that_passes_first_time_is_not_rerun(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        counter = repo.parent / "runs.txt"
        cfg = build(repo, test_command=f"echo run >> {counter}")
        out = verify(repo, cfg, sha)
        assert out.passed
        assert counter.read_text().count("run") == 1


class TestChecks:
    def test_failing_check_fails_the_stage(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, stage_overrides={"checks": ["true", "false"]})
        out = verify(repo, cfg, sha)
        assert not out.passed
        assert out.failed_layer is Layer.CHECKS

    def test_failing_check_is_retryable(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, stage_overrides={"checks": ["false"]})
        assert verify(repo, cfg, sha).retryable

    def test_feedback_names_the_failing_check(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, stage_overrides={"checks": ["echo BAD_ROUTES >&2; exit 2"]})
        out = verify(repo, cfg, sha)
        assert "BAD_ROUTES" in out.feedback

    def test_checks_run_after_the_tests(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, stage_overrides={"checks": ["false"]}, test_command="exit 1")
        assert verify(repo, cfg, sha).failed_layer is Layer.TESTS


class TestRequireNewTests:
    def test_diff_without_tests_fails(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "def f(): pass\n")
        cfg = build(repo, stage_overrides={"require_new_tests": True})
        out = verify(repo, cfg, sha)
        assert not out.passed
        assert out.failed_layer is Layer.NEW_TESTS

    def test_diff_with_a_new_test_file_passes(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "def f(): pass\n")
        edit(repo, "src/test_thing.py", "def test_f(): pass\n")
        cfg = build(repo, stage_overrides={"require_new_tests": True})
        assert verify(repo, cfg, sha).passed

    def test_modifying_an_existing_test_file_counts(self, repo):
        # Adding cases to an existing spec is legitimate test-first work.
        (repo / "src").mkdir()
        (repo / "src" / "test_thing.py").write_text("def test_a(): pass\n")
        Git(repo).commit_all("seed tests")
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "def f(): pass\n")
        edit(repo, "src/test_thing.py", "def test_a(): pass\ndef test_b(): pass\n")
        cfg = build(repo, stage_overrides={"require_new_tests": True})
        assert verify(repo, cfg, sha).passed

    def test_ruby_spec_naming_is_recognised(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing_spec.rb", "describe Thing do; end\n")
        cfg = build(repo, stage_overrides={"require_new_tests": True})
        assert verify(repo, cfg, sha).passed

    def test_patterns_are_configurable(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/checks/thing.py", "assert True\n")
        cfg = build(
            repo,
            stage_overrides={"require_new_tests": True, "edit_files": ["src/**"]},
            test_file_patterns=["src/checks/**"],
        )
        assert verify(repo, cfg, sha).passed

    def test_off_by_default(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "def f(): pass\n")
        assert verify(repo, build(repo), sha).passed

    def test_failure_is_retryable(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "x\n")
        cfg = build(repo, stage_overrides={"require_new_tests": True})
        assert verify(repo, cfg, sha).retryable


class TestResultsRecorded:
    def test_command_results_are_returned_for_logging(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg = build(repo, setup_command="echo setting-up", stage_overrides={"checks": ["echo checking"]})
        out = verify(repo, cfg, sha)
        joined = "\n".join(r.output for r in out.results)
        assert "setting-up" in joined
        assert "checking" in joined
