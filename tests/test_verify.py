"""The layered pre-review gate.

Ordering is economic: free deterministic checks run before anything costing
minutes of compute or a paid API call. Routing is three-way — executor, planner,
or human — and which failure goes where is the substance of the escalation
tiers.
"""

from orchestrator.commands import CommandRunner
from orchestrator.config import parse_config
from orchestrator.gitops import Git
from orchestrator.verify import Layer, Route, resolve_test_command, run_verify


def build(repo, stage_overrides=None, **cfg_overrides):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(cfg_overrides)
    cfg = parse_config(data)

    fields = {"id": "s1", "instruction": "do it", "edit_files": ["app.py", "src/**"]}
    fields.update(stage_overrides or {})
    from orchestrator.config import Stage

    return cfg, Stage(**fields)


def verify(repo, cfg, stage, sha, **kw):
    return run_verify(
        stage=stage,
        cfg=cfg,
        git=Git(repo),
        runner=CommandRunner(cwd=repo, timeout=60),
        stage_start_sha=sha,
        **kw,
    )


def edit(repo, name="app.py", text="changed\n"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestHappyPath:
    def test_passes_when_everything_is_green(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.failed_layer is None
        assert out.route is None


class TestSetupLayer:
    def test_failure_goes_to_a_human(self, repo):
        # A broken environment is not a planning defect, and rework will not
        # fix it.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, setup_command="false")
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.SETUP
        assert out.route is Route.HUMAN

    def test_runs_before_the_tests(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        marker = repo.parent / "setup-ran"
        cfg, stage = build(
            repo, setup_command=f"touch {marker}", test_command=f"test -f {marker}"
        )
        assert verify(repo, cfg, stage, sha).passed


class TestBranchIdentityLayer:
    def test_passes_on_a_healthy_stage(self, repo):
        g = Git(repo)
        base_sha = g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-s1", "proj")
        sha = g.head_sha()
        edit(repo)
        cfg, stage = build(repo)
        out = verify(
            repo, cfg, stage, sha,
            stage_branch="proj-stage/001-s1", project_branch="proj",
            base_ref="main", base_sha=base_sha,
        )
        assert out.passed

    def test_a_stray_checkout_goes_to_a_human(self, repo):
        # A containment breach does not negotiate.
        g = Git(repo)
        base_sha = g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-s1", "proj")
        sha = g.head_sha()
        edit(repo)
        g.commit_all("work")
        g.checkout("proj")
        cfg, stage = build(repo)
        out = verify(
            repo, cfg, stage, sha,
            stage_branch="proj-stage/001-s1", project_branch="proj",
            base_ref="main", base_sha=base_sha,
        )
        assert out.failed_layer is Layer.BRANCH
        assert out.route is Route.HUMAN

    def test_runs_before_the_scope_guard(self, repo):
        # Free and deterministic, and a containment breach outranks a scope
        # violation.
        g = Git(repo)
        base_sha = g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-s1", "proj")
        sha = g.head_sha()
        edit(repo, "wandered.py")
        g.commit_all("work")
        g.checkout("proj")
        cfg, stage = build(repo)
        out = verify(
            repo, cfg, stage, sha,
            stage_branch="proj-stage/001-s1", project_branch="proj",
            base_ref="main", base_sha=base_sha,
        )
        assert out.failed_layer is Layer.BRANCH

    def test_skipped_when_branch_context_is_absent(self, repo):
        # Unit-level callers need not supply it.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        assert verify(repo, cfg, stage, sha).passed


class TestScopeGuard:
    def test_in_scope_edit_passes(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "x\n")
        cfg, stage = build(repo)
        assert verify(repo, cfg, stage, sha).passed

    def test_out_of_scope_edit_goes_to_the_planner(self, repo):
        # Two indistinguishable causes — the executor wandered, or the fix
        # genuinely lies outside the box — and the planner can tell them apart
        # by widening or not.
        sha = Git(repo).head_sha()
        edit(repo, "unrelated/other.py", "x\n")
        cfg, stage = build(repo)
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.SCOPE
        assert out.route is Route.PLANNER

    def test_reports_the_out_of_scope_paths_for_the_planner(self, repo):
        # The planner decides whether to adopt them; nothing else is discarded.
        sha = Git(repo).head_sha()
        edit(repo, "unrelated/a.py", "x\n")
        edit(repo, "unrelated/b.py", "x\n")
        cfg, stage = build(repo)
        out = verify(repo, cfg, stage, sha)
        assert out.out_of_scope_paths == ["unrelated/a.py", "unrelated/b.py"]

    def test_feedback_explains_both_options(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "unrelated/other.py", "x\n")
        cfg, stage = build(repo)
        feedback = verify(repo, cfg, stage, sha).feedback
        assert "widen edit_files" in feedback
        assert "reverted" in feedback

    def test_an_empty_diff_goes_back_to_the_executor(self, repo):
        sha = Git(repo).head_sha()
        cfg, stage = build(repo)
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.SCOPE
        assert out.route is Route.EXECUTOR
        assert "no changes" in out.feedback.lower()


class TestForbiddenPatterns:
    def test_added_line_goes_back_to_the_executor(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "belongs_to :thing, optional: true\n")
        cfg, stage = build(repo, {"forbidden_patterns": [r"optional:\s*true"]})
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.PATTERNS
        assert out.route is Route.EXECUTOR

    def test_feedback_quotes_the_line_and_the_pattern(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "optional: true\n")
        cfg, stage = build(repo, {"forbidden_patterns": ["optional: true"]})
        feedback = verify(repo, cfg, stage, sha).feedback
        assert "app.py" in feedback and "optional: true" in feedback

    def test_a_removed_line_matching_a_pattern_passes(self, repo):
        # A stage whose purpose is deleting a construct must be able to forbid
        # it without flagging its own success.
        (repo / "app.py").write_text("render nothing: true\n")
        Git(repo).commit_all("seed")
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "head :ok\n")
        cfg, stage = build(repo, {"forbidden_patterns": ["render nothing"]})
        assert verify(repo, cfg, stage, sha).passed

    def test_runs_before_the_tests(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py", "optional: true\n")
        cfg, stage = build(
            repo, {"forbidden_patterns": ["optional: true"]}, test_command="exit 1"
        )
        assert verify(repo, cfg, stage, sha).failed_layer is Layer.PATTERNS


class TestTests:
    def test_failure_goes_back_to_the_executor(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, test_command="exit 1")
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.TESTS
        assert out.route is Route.EXECUTOR

    def test_feedback_includes_output(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, test_command="echo DISTINCTIVE; exit 1")
        assert "DISTINCTIVE" in verify(repo, cfg, stage, sha).feedback

    def test_extracts_failing_paths_for_the_planner(self, repo):
        # Where the damage is, is what tells the planner whether to widen scope
        # or insert a predecessor stage.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(
            repo, test_command="echo 'spec/models/order_spec.rb:14 failed'; exit 1"
        )
        out = verify(repo, cfg, stage, sha)
        assert "spec/models/order_spec.rb" in out.failing_paths

    def test_absent_test_command_skips_the_layer(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, {"checks": ["true"]}, test_command=None,
                           stage_defaults={"checks": ["true"]})
        assert verify(repo, cfg, stage, sha).passed


class TestFlakeRerun:
    def test_passing_on_rerun_does_not_fail_the_stage(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        flag = repo.parent / "flake-flag"
        cfg, stage = build(
            repo,
            test_command=f"if [ -f {flag} ]; then exit 0; else touch {flag}; exit 1; fi",
        )
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.flake_reruns == 1

    def test_a_consistent_failure_still_fails(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, test_command="exit 1")
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.flake_reruns == 0

    def test_a_broad_suite_re_runs_only_what_failed(self, repo):
        # Iteration falls back to the whole suite when a stage cannot be scoped.
        # Re-running all of it to test one example's order dependence is the
        # same waste as at the merge gate, and here it repeats every attempt.
        log = repo.parent / "iteration-reran.txt"
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(
            repo,
            test_command=(
                "echo \"Failed examples:\"; "
                "echo \"rspec './spec/other_spec.rb[1:1]' # x\"; exit 1"
            ),
            scoped_test_command=f"echo {{paths}} >> {log}",
        )
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.flake_reruns == 1
        assert log.read_text().strip() == "./spec/other_spec.rb[1:1]"

    def test_a_scoped_run_keeps_the_plain_re_run(self, repo):
        # When the command is already narrowed to the stage's own specs there is
        # nothing broader to blame, and every failing example is one the stage
        # owns — so the honest re-run is the same command again.
        flag = repo.parent / "scoped-flake-flag"
        sha = Git(repo).head_sha()
        edit(repo, "spec/app_spec.rb", "describe\n")
        cfg, stage = build(
            repo,
            {"edit_files": ["spec/**"], "test_paths": ["spec/app_spec.rb"]},
            scoped_test_command=(
                f"if [ -f {flag} ]; then exit 0; else touch {flag}; exit 1; fi # {{paths}}"
            ),
        )
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.flake_reruns == 1

    def test_a_first_time_pass_is_not_rerun(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        counter = repo.parent / "runs.txt"
        cfg, stage = build(repo, test_command=f"echo run >> {counter}")
        assert verify(repo, cfg, stage, sha).passed
        assert counter.read_text().count("run") == 1


class TestNoProgressGuard:
    """An attempt that reproduces the previous diff exactly.

    Observed live: three rework attempts produced byte-identical diffs and drew
    three byte-identical reviewer verdicts. Each rework costs a paid review and,
    with a real local model, minutes of inference. Retrying an executor that
    just demonstrated it cannot move is spending money to learn nothing.
    """

    def test_an_identical_diff_stops_the_retry_loop(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)

        first = verify(repo, cfg, stage, sha)
        assert first.passed
        assert first.diff_digest

        again = verify(repo, cfg, stage, sha, previous_diff_digest=first.diff_digest)
        assert not again.passed
        assert again.failed_layer is Layer.PROGRESS

    def test_it_routes_to_the_planner_not_another_retry(self, repo):
        # The executor has shown it cannot do this. Only a redrawn stage helps.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        first = verify(repo, cfg, stage, sha)
        again = verify(repo, cfg, stage, sha, previous_diff_digest=first.diff_digest)
        assert again.route is Route.PLANNER

    def test_a_changed_diff_passes_through(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        first = verify(repo, cfg, stage, sha)

        edit(repo, text="changed again\n")
        second = verify(repo, cfg, stage, sha, previous_diff_digest=first.diff_digest)
        assert second.passed
        assert second.diff_digest != first.diff_digest

    def test_the_first_attempt_has_nothing_to_compare(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        assert verify(repo, cfg, stage, sha, previous_diff_digest=None).passed

    def test_an_empty_diff_is_reported_as_no_changes_not_no_progress(self, repo):
        # Two different failures. "You changed nothing" is the executor's
        # problem; "you changed the same thing twice" is the stage's.
        sha = Git(repo).head_sha()
        cfg, stage = build(repo)
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.failed_layer is Layer.SCOPE


class TestScopedTestCommand:
    def test_uses_the_paths_from_the_diff(self, repo):
        # The planner supplies paths; the operator supplies the command.
        sha = Git(repo).head_sha()
        edit(repo, "spec/thing_spec.rb", "describe\n")
        cfg, stage = build(repo, scoped_test_command="rspec {paths}")
        assert resolve_test_command(stage, cfg, Git(repo), sha) == "rspec spec/thing_spec.rb"

    def test_includes_planner_declared_paths(self, repo):
        # Changing a model should run specs that exercise it without touching
        # them, and the planner knows which those are.
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/models/order_spec.rb"]},
            scoped_test_command="rspec {paths}",
        )
        command = resolve_test_command(stage, cfg, Git(repo), sha)
        assert "spec/models/order_spec.rb" in command

    def test_deduplicates_paths(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "spec/thing_spec.rb", "x\n")
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/thing_spec.rb"]},
            scoped_test_command="rspec {paths}",
        )
        command = resolve_test_command(stage, cfg, Git(repo), sha)
        assert command.count("spec/thing_spec.rb") == 1

    def test_falls_back_to_the_full_command_when_nothing_is_identifiable(self, repo):
        # Running an empty selection and calling it green would be worse than
        # running everything.
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        cfg, stage = build(repo, scoped_test_command="rspec {paths}", test_command="rspec")
        assert resolve_test_command(stage, cfg, Git(repo), sha) == "rspec"

    def test_an_explicit_stage_command_wins(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "spec/a_spec.rb", "x\n")
        cfg, stage = build(
            repo, {"test_command": "rspec spec/only"}, scoped_test_command="rspec {paths}"
        )
        assert resolve_test_command(stage, cfg, Git(repo), sha) == "rspec spec/only"


class TestChecks:
    def test_failure_goes_back_to_the_executor(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, {"checks": ["true", "false"]})
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.CHECKS
        assert out.route is Route.EXECUTOR

    def test_feedback_names_the_failing_check(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, {"checks": ["echo BAD_ROUTES >&2; exit 2"]})
        assert "BAD_ROUTES" in verify(repo, cfg, stage, sha).feedback

    def test_runs_after_the_tests(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, {"checks": ["false"]}, test_command="exit 1")
        assert verify(repo, cfg, stage, sha).failed_layer is Layer.TESTS


class TestRequireNewTests:
    def test_a_diff_without_tests_fails(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "def f(): pass\n")
        cfg, stage = build(repo, {"require_new_tests": True})
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.NEW_TESTS
        assert out.route is Route.EXECUTOR

    def test_a_new_test_file_passes(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "def f(): pass\n")
        edit(repo, "src/test_thing.py", "def test_f(): pass\n")
        cfg, stage = build(repo, {"require_new_tests": True})
        assert verify(repo, cfg, stage, sha).passed

    def test_touching_an_existing_test_file_counts(self, repo):
        # Adding cases to an existing spec is legitimate test-first work;
        # demanding a brand-new file pushes the executor into redundant ones.
        (repo / "src").mkdir()
        (repo / "src" / "test_thing.py").write_text("def test_a(): pass\n")
        Git(repo).commit_all("seed tests")
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "def f(): pass\n")
        edit(repo, "src/test_thing.py", "def test_a(): pass\ndef test_b(): pass\n")
        cfg, stage = build(repo, {"require_new_tests": True})
        assert verify(repo, cfg, stage, sha).passed

    def test_off_by_default(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "src/thing.py", "x\n")
        cfg, stage = build(repo)
        assert verify(repo, cfg, stage, sha).passed


class TestUnscopedFallbackIsVisible:
    """Falling back to the whole suite during iteration is worth knowing about.

    `scoped_test_command` exists so an attempt runs only the specs it affects.
    When a stage's diff touches no spec files and the planner declared no
    test_paths, there is nothing to scope to and the full command runs instead
    — correct, but on a real project that is eleven minutes, repeated for every
    retry. Silent, it looks like the scoped path simply being slow.
    """

    def test_the_outcome_records_that_it_was_unscoped(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)  # app.py only: nothing matching a test file pattern
        cfg, stage = build(repo, scoped_test_command="true {paths}")
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.unscoped_tests is True

    def test_a_scoped_run_is_not_flagged(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, name="spec/thing_spec.rb", text="x\n")
        cfg, stage = build(
            repo,
            {"edit_files": ["spec/**"]},
            scoped_test_command="true {paths}",
            test_file_patterns=["spec/**"],
        )
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.unscoped_tests is False


class TestRequireScopedTests:
    """Policy: a stage must identify which specs prove it.

    A prompt asking the planner for test_paths is a hope. With this on, a stage
    that identifies no specs fails to the planner *before* the suite runs, so
    the cost is one redraw rather than a full suite on this attempt and on
    every reviewer round trip after it.

    Off by default: some projects genuinely have stages nothing covers, and
    failing those would be worse than running the suite.
    """

    def test_an_unscoped_stage_fails_to_the_planner(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)  # app.py only — nothing matching a test file pattern
        cfg, stage = build(
            repo,
            {"require_scoped_tests": True},
            scoped_test_command="true {paths}",
        )
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.failed_layer is Layer.TESTS
        assert out.route is Route.PLANNER
        assert "test_paths" in out.feedback

    def test_declared_test_paths_satisfy_it(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(
            repo,
            {"require_scoped_tests": True, "test_paths": ["spec/thing_spec.rb"]},
            scoped_test_command="true {paths}",
        )
        assert verify(repo, cfg, stage, sha).passed

    def test_editing_a_spec_satisfies_it(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, name="spec/thing_spec.rb", text="x\n")
        cfg, stage = build(
            repo,
            {"require_scoped_tests": True, "edit_files": ["spec/**"]},
            scoped_test_command="true {paths}",
            test_file_patterns=["spec/**"],
        )
        assert verify(repo, cfg, stage, sha).passed

    def test_off_by_default_it_just_runs_the_suite(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, scoped_test_command="true {paths}")
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.unscoped_tests is True
