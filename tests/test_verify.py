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

RSPEC_PATTERN = r"^\s*rspec\s+'?\.?/?([^'\s\[:]+_spec\.rb)"


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

    def test_a_broad_suite_re_runs_only_the_failing_files(self, repo):
        # Iteration falls back to the whole suite when a stage cannot be scoped.
        # Re-running all of it to test one file's order dependence is the same
        # waste as at the merge gate, and here it repeats every attempt.
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
            failed_file_pattern=RSPEC_PATTERN,
        )
        out = verify(repo, cfg, stage, sha)
        assert out.passed
        assert out.flake_reruns == 1
        assert log.read_text().strip() == "spec/other_spec.rb"

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
        g = Git(repo)
        (repo / "spec" / "models").mkdir(parents=True, exist_ok=True)
        (repo / "spec" / "models" / "order_spec.rb").write_text("x\n")
        g.commit_all("spec")
        sha = g.head_sha()
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


class TestDirectoryScopedRunsGoParallel:
    """A directory is not a file, and should not be run like one.

    The first real stage declared `test_paths: ["spec/controllers",
    "spec/requests"]`. Serially that is `bin/rspec spec/controllers
    spec/requests` — 449 seconds, repeated on every attempt and again on every
    re-run. The same selection under the project's parallel runner is a
    fraction of that.

    Which command that is stays the operator's: this only picks between two
    strings they wrote, on a fact about the filesystem.
    """

    def test_a_declared_directory_uses_the_parallel_command(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        (repo / "spec" / "controllers").mkdir(parents=True, exist_ok=True)
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/controllers"]},
            scoped_test_command="rspec {paths}",
            directory_test_command="parallel_rspec {paths}",
        )
        command = resolve_test_command(stage, cfg, Git(repo), sha)
        assert command == "parallel_rspec spec/controllers"

    def test_individual_files_stay_serial(self, repo):
        # Starting N workers to run one file is slower than running it.
        sha = Git(repo).head_sha()
        edit(repo, "spec/thing_spec.rb", "describe\n")
        cfg, stage = build(
            repo,
            scoped_test_command="rspec {paths}",
            directory_test_command="parallel_rspec {paths}",
        )
        command = resolve_test_command(stage, cfg, Git(repo), sha)
        assert command == "rspec spec/thing_spec.rb"

    def test_one_directory_among_files_is_enough(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "spec/thing_spec.rb", "describe\n")
        (repo / "spec" / "requests").mkdir(parents=True, exist_ok=True)
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/requests"]},
            scoped_test_command="rspec {paths}",
            directory_test_command="parallel_rspec {paths}",
        )
        assert resolve_test_command(stage, cfg, Git(repo), sha).startswith("parallel_rspec")

    def test_without_the_parallel_command_nothing_changes(self, repo):
        # Unconfigured, a directory runs under the serial command as before.
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        (repo / "spec" / "controllers").mkdir(parents=True, exist_ok=True)
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/controllers"]},
            scoped_test_command="rspec {paths}",
        )
        assert resolve_test_command(stage, cfg, Git(repo), sha) == "rspec spec/controllers"

    def test_a_declared_path_that_does_not_exist_never_reaches_a_command(self, repo):
        # It is dropped before the directory question is asked, so neither
        # command runs against a path that is not there.
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/nope"]},
            scoped_test_command="rspec {paths}",
            directory_test_command="parallel_rspec {paths}",
            test_command="rspec-all",
        )
        assert resolve_test_command(stage, cfg, Git(repo), sha) == "rspec-all"


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
        g = Git(repo)
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "thing_spec.rb").write_text("x\n")
        g.commit_all("spec")  # it has to exist to be declarable
        sha = g.head_sha()
        edit(repo)
        cfg, stage = build(
            repo,
            {"require_scoped_tests": True, "test_paths": ["spec/thing_spec.rb"]},
            scoped_test_command="true {paths}",
        )
        assert verify(repo, cfg, stage, sha).passed

    def test_a_declared_path_that_does_not_exist_does_not_satisfy_it(self, repo):
        # Naming a spec that is not there is not identifying the specs that
        # prove the stage; it is the unscoped case wearing a disguise.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(
            repo,
            {"require_scoped_tests": True, "test_paths": ["spec/imagined_spec.rb"]},
            scoped_test_command="true {paths}",
        )
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.route is Route.PLANNER

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


class TestSignalledTestRuns:
    """The environment died; that is not a stage failure.

    Observed live: the operator stopped the container stack mid-run, thinking
    the run had paused for review. The suite returned exit 137 and verify
    charged it to the executor's retry budget — one of three attempts spent on
    something no attempt could fix, and the next attempt would have run against
    the same dead environment.

    Routed to a human, like `setup`, and for the same reason: a broken
    environment is not a planning defect and rework will not repair it.
    """

    def test_a_signalled_suite_goes_to_a_human(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, test_command="kill -9 $$")
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.TESTS
        assert out.route is Route.HUMAN
        assert "signal" in out.feedback.lower()

    def test_it_does_not_spend_a_re_run(self, repo):
        # Re-running a suite against an environment that just died learns
        # nothing and costs whatever the suite costs.
        counter = repo.parent / "signalled-runs.txt"
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(
            repo, test_command=f"echo run >> {counter}; kill -9 $$"
        )
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert counter.read_text().count("run") == 1

    def test_an_ordinary_failure_still_goes_to_the_executor(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, test_command="exit 1")
        out = verify(repo, cfg, stage, sha)
        assert out.route is Route.EXECUTOR

    def test_a_signalled_check_also_goes_to_a_human(self, repo):
        # Same reasoning as the suite: `checks` are operator commands running in
        # the same environment, and they die with it.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo, {"checks": ["kill -9 $$"]})
        out = verify(repo, cfg, stage, sha)
        assert out.failed_layer is Layer.CHECKS
        assert out.route is Route.HUMAN


class TestProgressAfterAFlakyMergeGate:
    """Reproducing an approved diff is compliance, not stalling.

    The guard's premise is that a byte-identical diff means the feedback
    changed nothing, so the stage cannot be drawn as asked. That holds when the
    rework followed a reviewer rejection or a failure the stage caused.

    It does not hold after a merge-gate failure. There the reviewer had already
    approved the diff and only the full suite was red — so the correct response
    to "do it again" is the same diff. Observed live: one order-dependent spec
    elsewhere in the suite cost a reset, a rework, and then a planner
    intervention whose reasoning was "the executor is convinced its output is
    correct and re-emits it" — which was true, and correct, and not a defect.
    """

    def test_an_identical_diff_after_a_full_suite_failure_is_allowed(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        digest = verify(repo, cfg, stage, sha).diff_digest
        out = verify(
            repo, cfg, stage, sha,
            previous_diff_digest=digest,
            previous_failure_layer="full_suite",
        )
        assert out.passed

    def test_an_identical_diff_after_a_review_rejection_still_stalls(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        digest = verify(repo, cfg, stage, sha).diff_digest
        out = verify(
            repo, cfg, stage, sha,
            previous_diff_digest=digest,
            previous_failure_layer="review",
        )
        assert out.failed_layer is Layer.PROGRESS

    def test_an_identical_diff_with_no_recorded_layer_still_stalls(self, repo):
        # The original behaviour, unchanged, for every other path in.
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        digest = verify(repo, cfg, stage, sha).diff_digest
        out = verify(repo, cfg, stage, sha, previous_diff_digest=digest)
        assert out.failed_layer is Layer.PROGRESS


class TestDeclaredTestPathsAreResolved:
    """The planner writes globs, and an unmatched glob is not a test failure.

    Live: the planner declared

        spec/controllers/**/*schedule*  spec/controllers/**/*billing*  ...

    which we substituted verbatim. The shell passed the unmatched literals to
    rspec, which died in 3.1 seconds — and verify read that as the stage's
    tests failing and charged it to the executor's retry budget. Three seconds
    is not a test run on a 2,335-example suite, and nothing noticed.

    Globs are legitimate declarative input; the planner cannot know which paths
    exist. Resolving them against the filesystem is our job.
    """

    def test_a_glob_is_expanded_to_real_files(self, repo):
        g = Git(repo)
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "a_schedule_spec.rb").write_text("x\n")
        (repo / "spec" / "b_billing_spec.rb").write_text("x\n")
        g.commit_all("specs")  # committed, so they arrive by declaration only
        sha = g.head_sha()
        edit(repo, "app.py")
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/**/*schedule*"]},
            scoped_test_command="rspec {paths}",
        )
        command = resolve_test_command(stage, cfg, Git(repo), sha)
        assert "spec/a_schedule_spec.rb" in command
        assert "b_billing_spec" not in command

    def test_a_glob_matching_nothing_is_dropped(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/**/*nothing_here*"]},
            scoped_test_command="rspec {paths}",
            test_command="rspec-all",
        )
        # Nothing identifiable to scope to, so the full command rather than a
        # command that will die on a literal asterisk.
        assert resolve_test_command(stage, cfg, Git(repo), sha) == "rspec-all"

    def test_a_plain_path_that_does_not_exist_is_dropped(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/imagined_spec.rb"]},
            scoped_test_command="rspec {paths}",
            test_command="rspec-all",
        )
        assert resolve_test_command(stage, cfg, Git(repo), sha) == "rspec-all"

    def test_a_real_directory_survives(self, repo):
        sha = Git(repo).head_sha()
        edit(repo, "app.py")
        (repo / "spec" / "controllers").mkdir(parents=True, exist_ok=True)
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/controllers"]},
            scoped_test_command="rspec {paths}",
        )
        assert "spec/controllers" in resolve_test_command(stage, cfg, Git(repo), sha)

    def test_paths_from_the_diff_are_trusted_unchanged(self, repo):
        # git named them, so they exist; a deleted spec is the scope guard's
        # business rather than something to silently drop here.
        sha = Git(repo).head_sha()
        edit(repo, "spec/touched_spec.rb", "x\n")
        cfg, stage = build(repo, scoped_test_command="rspec {paths}")
        assert "spec/touched_spec.rb" in resolve_test_command(stage, cfg, Git(repo), sha)


class TestResumeDoesNotLookLikeAStall:
    """Re-entering at verify is re-checking, not repeating.

    A repository-state failure resumes at verify precisely so a human's fix is
    checked rather than discarded — which means the tree may legitimately be
    byte-identical to what the last verify saw. The progress guard read that as
    the executor failing to move and sent the stage to the planner, costing an
    intervention for the act of resuming.

    Observed live: a run stopped mid-attempt, resumed, and went straight to
    "failed at progress" without an executor pass in between.
    """

    def test_a_resumed_verify_ignores_the_previous_digest(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        digest = verify(repo, cfg, stage, sha).diff_digest
        out = verify(repo, cfg, stage, sha, previous_diff_digest=digest, resuming=True)
        assert out.passed

    def test_an_ordinary_verify_still_catches_a_stall(self, repo):
        sha = Git(repo).head_sha()
        edit(repo)
        cfg, stage = build(repo)
        digest = verify(repo, cfg, stage, sha).diff_digest
        out = verify(repo, cfg, stage, sha, previous_diff_digest=digest)
        assert out.failed_layer is Layer.PROGRESS


class TestThePlanIsNotEditable:
    """A stage may not rewrite the plan it is being drawn from.

    Harmless while the planner was blind. Now it reads the plan tree and also
    chooses `edit_files`, so nothing structural stops it declaring the plan in
    scope and having the executor amend the instructions it will be judged
    against next cycle. Over an unattended run that is goalpost drift with a
    green suite behind it.

    Plan maintenance belongs to a separate pass driven by git history — a human
    decision about what the work has become, not a side effect of doing it.
    """

    def _fixture(self, repo, edit_files):
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "plan.md").write_text("# Plan\n\nstep one\n")
        g = Git(repo)
        g.commit_all("plan")
        cfg, stage = build(
            repo,
            stage_overrides={"edit_files": edit_files},
            plan_root="docs/plan.md",
        )
        return cfg, stage, g.head_sha()

    def test_a_stage_may_not_edit_the_plan_root(self, repo):
        cfg, stage, sha = self._fixture(repo, ["docs/**"])
        (repo / "docs" / "plan.md").write_text("# Plan\n\nrewritten\n")
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.failed_layer is Layer.SCOPE
        assert "plan" in out.summary.lower()

    def test_declaring_the_plan_in_scope_does_not_help(self, repo):
        # The point: this is not enforced by the planner's restraint.
        cfg, stage, sha = self._fixture(repo, ["docs/plan.md"])
        (repo / "docs" / "plan.md").write_text("# Plan\n\nrewritten\n")
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.failed_layer is Layer.SCOPE

    def test_ordinary_files_are_unaffected(self, repo):
        cfg, stage, sha = self._fixture(repo, ["app.py"])
        (repo / "app.py").write_text("changed\n")
        out = verify(repo, cfg, stage, sha)
        assert out.passed or out.failed_layer is not Layer.SCOPE


class TestTheAddendumIsNotTheExecutorsToWrite:
    """The addendum records what a run did. A stage must not edit it.

    It lives inside the plan directory and the orchestrator does append to it —
    from the planner's structured output, at advance time, outside any stage's
    diff. So it never appears in a scope check legally, and an executor edit to
    it is the executor reaching into the record of its own work.
    """

    def _fixture(self, repo):
        (repo / "docs" / "addendum").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "plan.md").write_text("# Plan\n\nstep one\n")
        (repo / "docs" / "addendum" / "log.md").write_text("# Done so far\n")
        g = Git(repo)
        g.commit_all("plan and addendum")
        cfg, stage = build(
            repo,
            stage_overrides={"edit_files": ["docs/**"]},
            plan_root="docs/plan.md",
            plan_addendum_path="docs/addendum",
        )
        return cfg, stage, g.head_sha()

    def test_a_stage_may_not_edit_the_addendum(self, repo):
        cfg, stage, sha = self._fixture(repo)
        (repo / "docs" / "addendum" / "log.md").write_text("# Done\n\nfabricated\n")
        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.failed_layer is Layer.SCOPE


class TestAnUncommittedAddendumWouldPoisonTheNextStage:
    """Why the addendum is committed at once rather than at the end of a run.

    Batching the commits would be tidier — one commit per landed stage is a
    stated property, and a note-heavy run doubles the commit count. But the
    file is written into the working tree, and the next stage's scope guard
    diffs the working tree against its start sha. An uncommitted addendum
    therefore shows up as a plan document modified by a stage that never
    touched it, and the stage fails for something the orchestrator did.
    """

    def test_an_uncommitted_addendum_fails_the_next_stage(self, repo):
        (repo / "docs" / "addendum").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "plan.md").write_text("# Plan\n")
        g = Git(repo)
        g.commit_all("plan")
        sha = g.head_sha()

        cfg, stage = build(
            repo,
            stage_overrides={"edit_files": ["app.py"]},
            plan_root="docs/plan.md",
            plan_addendum_path="docs/addendum",
        )
        # The stage does its own work...
        edit(repo)
        # ...and the orchestrator left an addendum behind, uncommitted.
        (repo / "docs" / "addendum" / "plan-addendum.md").write_text("# Notes\n")

        out = verify(repo, cfg, stage, sha)
        assert not out.passed
        assert out.failed_layer is Layer.SCOPE
