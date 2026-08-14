"""One test selection, asked two ways.

The executor's inner loop and the verify gate both need "which tests does this
stage need". They asked separately for most of this project's life and drifted
in five places, each drift correct for its own caller and invisible to the
other. These tests pin the differences *against each other*, in the same file
and where possible in the same test, so that changing one and not the other
fails here rather than in a run.
"""

from orchestrator.config import Stage, parse_config
from orchestrator.gates import resolve_test_command, resolve_test_paths
from orchestrator.gitops import Git


def build(repo, stage_overrides=None, **cfg_overrides):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "full-suite",
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(cfg_overrides)
    cfg = parse_config(data)

    fields = {"id": "s1", "instruction": "do it", "edit_files": ["app.py", "src/**"]}
    fields.update(stage_overrides or {})
    return cfg, Stage(**fields)


class TestTheTwoCallersDifferOnPurpose:
    def test_a_path_the_stage_may_create_is_kept_by_the_loop_and_dropped_by_the_gate(
        self, repo
    ):
        """The single most important difference, asserted in one place.

        The loop runs *after* the edits, so a spec the stage was told to write
        will be there by the time the command runs. The gate runs after too,
        but it has the diff to tell it what actually appeared, and a declared
        path that never materialised is not a test — running the full suite
        instead is slow but true.

        Kept unconditionally on the gate's side, a path the stage cannot create
        is a command that can never pass. Observed live: a planner declared two
        spec paths for a repository containing neither, and the attempt hung on
        a 77,000-token fix while the gate dropped the same two paths and ran
        the whole suite green.
        """
        cfg, stage = build(
            repo,
            {
                "test_paths": ["spec/not_yet_spec.rb"],
                "edit_files": ["spec/not_yet_spec.rb"],
            },
            scoped_test_command="rspec {paths}",
        )
        sha = Git(repo).head_sha()

        loop = resolve_test_paths(stage, cfg, for_loop=True)
        gate = resolve_test_paths(stage, cfg, Git(repo), sha, for_loop=False)

        assert loop == ["spec/not_yet_spec.rb"]
        assert gate == []

    def test_a_path_the_stage_could_not_create_is_dropped_by_both(self, repo):
        # The keep rule is evidence-based, not permissive. A declared path
        # outside `edit_files` on a stage with no test requirement is one the
        # planner guessed at, and neither caller should run it.
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/phantom_spec.rb"], "edit_files": ["app.py"]},
            scoped_test_command="rspec {paths}",
        )
        sha = Git(repo).head_sha()

        assert resolve_test_paths(stage, cfg, for_loop=True) == []
        assert resolve_test_paths(stage, cfg, Git(repo), sha, for_loop=False) == []

    def test_only_the_loop_adds_the_tests_the_stage_may_edit(self, repo):
        # The gate does not need them: if the stage touched a test file, it is
        # in the diff and arrives that way.
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "thing_spec.rb").write_text("describe\n")
        Git(repo).commit_all("spec")
        sha = Git(repo).head_sha()

        cfg, stage = build(
            repo,
            {"edit_files": ["spec/thing_spec.rb"]},
            scoped_test_command="rspec {paths}",
        )

        assert resolve_test_paths(stage, cfg, for_loop=True) == ["spec/thing_spec.rb"]
        assert resolve_test_paths(stage, cfg, Git(repo), sha, for_loop=False) == []

    def test_nothing_to_run_means_no_loop_but_the_full_suite_at_the_gate(self, repo):
        """The fifth difference, and the one that reads backwards.

        A command that can never pass is worse than no command, because the
        attempt ends believing it succeeded. The gate has the opposite problem
        and the opposite answer: running nothing and calling it green is the
        failure there, so it falls back to the whole suite.
        """
        cfg, stage = build(repo, scoped_test_command="rspec {paths}")
        sha = Git(repo).head_sha()

        assert resolve_test_command(stage, cfg, for_loop=True) is None
        assert (
            resolve_test_command(stage, cfg, Git(repo), sha, for_loop=False)
            == "full-suite"
        )


class TestWhatBothDoIdentically:
    def test_an_unmatched_glob_is_dropped_by_both(self, repo):
        # Left in, it reaches the runner as a literal asterisk and dies in
        # three seconds — which the gate then reads as failing tests and
        # charges to the executor's retry budget.
        cfg, stage = build(
            repo,
            {"test_paths": ["spec/**/*nothing*_spec.rb"]},
            scoped_test_command="rspec {paths}",
        )
        sha = Git(repo).head_sha()

        assert resolve_test_paths(stage, cfg, for_loop=True) == []
        assert resolve_test_paths(stage, cfg, Git(repo), sha, for_loop=False) == []

    def test_both_use_the_scoped_command_on_the_same_input(self, repo):
        """A directory selection is not a different command any more."""
        (repo / "spec" / "models").mkdir(parents=True, exist_ok=True)
        (repo / "spec" / "models" / "keep_spec.rb").write_text("x\n")
        Git(repo).commit_all("specs")
        sha = Git(repo).head_sha()

        cfg, stage = build(
            repo,
            {"test_paths": ["spec/models"], "edit_files": ["spec/models"]},
            scoped_test_command="rspec {paths}",
        )

        assert resolve_test_command(stage, cfg, for_loop=True) == "rspec spec/models"
        assert (
            resolve_test_command(stage, cfg, Git(repo), sha, for_loop=False)
            == "rspec spec/models"
        )

    def test_an_operator_named_loop_command_is_used(self, repo):
        # An operator who named the loop's command meant that command.
        (repo / "spec" / "models").mkdir(parents=True, exist_ok=True)
        (repo / "spec" / "models" / "keep_spec.rb").write_text("x\n")
        Git(repo).commit_all("specs")

        cfg, stage = build(
            repo,
            {"test_paths": ["spec/models"], "edit_files": ["spec/models"]},
            auto_test_command="loop-cmd {paths}",
            scoped_test_command="rspec {paths}",
        )

        assert resolve_test_command(stage, cfg, for_loop=True) == "loop-cmd spec/models"


class TestTheMovedLayersAgreeWithTheGate:
    """Each moved layer, asked directly and through `run_verify`.

    The move is only safe if the two spellings of the same question return the
    same verdict for the same tree. Asserted per layer rather than once, because
    the failure this guards against is one layer drifting while the others hold
    — which is exactly how the two test selectors came to disagree.
    """

    def _verify(self, repo, cfg, stage, sha):
        from orchestrator.commands import CommandRunner
        from orchestrator.verify import run_verify

        return run_verify(
            stage=stage,
            cfg=cfg,
            git=Git(repo),
            runner=CommandRunner(cwd=repo, timeout=60),
            stage_start_sha=sha,
        )

    def test_patterns_agrees(self, repo):
        from orchestrator.gates import check_patterns
        from orchestrator.verify import Layer

        sha = Git(repo).head_sha()
        (repo / "app.py").write_text("import pdb; pdb.set_trace()\n")
        cfg, stage = build(repo, {"forbidden_patterns": ["pdb"]}, test_command="true")

        found = check_patterns(stage, cfg, Git(repo), sha)
        outcome = self._verify(repo, cfg, stage, sha)

        assert not found.ok
        assert not outcome.passed
        assert outcome.failed_layer is Layer.PATTERNS
        assert outcome.summary == found.summary

    def test_residue_agrees(self, repo):
        from orchestrator.gates import check_residue
        from orchestrator.verify import Layer

        sha = Git(repo).head_sha()
        (repo / "app.py").write_text("before_filter :x\nchanged\n")
        cfg, stage = build(repo, {"must_not_remain": ["before_filter"]}, test_command="true")

        found = check_residue(stage, cfg, Git(repo))
        outcome = self._verify(repo, cfg, stage, sha)

        assert not found.ok
        assert not outcome.passed
        assert outcome.failed_layer is Layer.RESIDUE
        assert outcome.summary == found.summary

    def test_new_tests_agrees(self, repo):
        from orchestrator.gates import check_new_tests
        from orchestrator.verify import Layer

        sha = Git(repo).head_sha()
        (repo / "app.py").write_text("changed\n")
        cfg, stage = build(repo, {"require_new_tests": True}, test_command="true")

        found = check_new_tests(stage, cfg, Git(repo), sha)
        outcome = self._verify(repo, cfg, stage, sha)

        assert not found.ok
        assert not outcome.passed
        assert outcome.failed_layer is Layer.NEW_TESTS
        assert outcome.summary == found.summary

    def test_checks_agrees(self, repo):
        from orchestrator.commands import CommandRunner
        from orchestrator.gates import run_checks
        from orchestrator.verify import Layer

        sha = Git(repo).head_sha()
        (repo / "app.py").write_text("changed\n")
        cfg, stage = build(repo, {"checks": ["false"]}, test_command="true")

        found = run_checks(stage, CommandRunner(cwd=repo, timeout=60))
        outcome = self._verify(repo, cfg, stage, sha)

        assert not found.ok
        assert not outcome.passed
        assert outcome.failed_layer is Layer.CHECKS
        assert outcome.summary == found.summary

    def test_tests_agrees_and_carries_the_failing_paths(self, repo):
        # `failing_paths` is what the planner reads at an intervention, and it
        # is produced by a regex that was nearly rewritten during this move.
        from orchestrator.commands import CommandRunner
        from orchestrator.gates import run_tests
        from orchestrator.verify import Layer

        sha = Git(repo).head_sha()
        (repo / "app.py").write_text("changed\n")
        cfg, stage = build(repo, test_command="echo 'app/models/order.rb:12 failed'; false")

        found = run_tests(
            stage, cfg, Git(repo), CommandRunner(cwd=repo, timeout=60), sha,
            for_loop=False,
        )
        outcome = self._verify(repo, cfg, stage, sha)

        assert not found.ok
        assert not outcome.passed
        assert outcome.failed_layer is Layer.TESTS
        assert "app/models/order.rb" in found.failing_paths
        assert outcome.failing_paths == found.failing_paths


class TestPathHintsCannotStallTheGate:
    """A regex that backtracks is a hang, not a slow function.

    `_PATH_HINT`'s leading class includes `.`, so against an unbroken run of
    dots it matched greedily from every start position and backtracked the
    whole run looking for an extension that was not there. Quadratic: 0.29s at
    8,000 dots, and a real progress reporter emits 200,000.

    This is production behaviour, not a test artifact. `run_tests` calls
    `path_hints` on *raw* command output, so a suite drawing a long progress
    bar stalled the gate for minutes while the planner waited. The suite was
    paying 500 seconds in one test to demonstrate it and nobody had read the
    durations.
    """

    def test_a_long_progress_run_is_answered_promptly(self):
        import time

        from orchestrator.gates import path_hints

        started = time.time()
        hints = path_hints("." * 200_000 + "\nfailed at app/models/order.rb:12")
        elapsed = time.time() - started

        # Generous by three orders of magnitude against the 180s this took
        # before, so the assertion is about complexity rather than about the
        # machine it runs on.
        assert elapsed < 2.0, f"path_hints took {elapsed:.1f}s on a progress run"
        assert hints == ["app/models/order.rb"]

    def test_a_path_after_a_collapsed_run_still_survives(self):
        # Collapsing must not eat the finding. A progress reporter puts its
        # dots first and what it found afterwards, which is the whole reason
        # `clip_for_model` collapses before it truncates.
        from orchestrator.gates import path_hints

        assert path_hints("." * 500 + "\nspec/a_spec.rb:4 failed") == [
            "spec/a_spec.rb"
        ]


class TestTheLoopDoesNotDoubleTheSuite:
    """One run per cycle, which is what the editor this replaces did.

    The executor's loop runs its test command once per cycle, and cycles are
    capped. Sharing `run_tests` with the gate quietly doubles that: six runs an
    attempt where there had been three, and nothing says so.

    The loop does not need the re-run. A failing cycle feeds the output back
    and the next cycle runs the same specs again, so iteration is the re-run.
    The gate keeps it, because there a flake costs a whole executor round trip
    rather than one more pass.
    """

    def test_the_loop_runs_the_command_once(self, repo):
        from orchestrator.commands import CommandRunner
        from orchestrator.gates import run_tests

        counter = repo / "runs.txt"
        cfg, stage = build(
            repo,
            {"test_paths": ["app.py"]},
            scoped_test_command=f"printf x >> {counter}; false # {{paths}}",
        )
        (repo / "app.py").write_text("x\n")
        Git(repo).commit_all("app")

        run_tests(
            stage, cfg, Git(repo), CommandRunner(cwd=repo, timeout=60),
            Git(repo).head_sha(), for_loop=True,
        )
        assert counter.read_text() == "x", "the loop re-ran a failing command"

    def test_the_gate_still_re_runs_once(self, repo):
        from orchestrator.commands import CommandRunner
        from orchestrator.gates import run_tests

        counter = repo / "runs.txt"
        cfg, stage = build(
            repo,
            {"test_paths": ["app.py"]},
            scoped_test_command=f"printf x >> {counter}; false # {{paths}}",
        )
        (repo / "app.py").write_text("x\n")
        Git(repo).commit_all("app")

        run_tests(
            stage, cfg, Git(repo), CommandRunner(cwd=repo, timeout=60),
            Git(repo).head_sha(), for_loop=False,
        )
        assert counter.read_text() == "xx", "the gate stopped adjudicating flakes"


class TestADeletedSpecIsNotPutInItsOwnTestCommand:
    """A command that cannot pass by construction is worse than no command.

    The gate builds its selection from the diff, and a deleted file is in the
    diff. A stage whose job is to fold one spec into another and delete it
    therefore produced a command naming the file it had just removed.

    What makes this worth a test rather than a one-line guard is how it
    surfaced: the executor was handed the impossible command, worked out that
    no code change could satisfy it, said so and stopped — exactly what it is
    asked to do — and the run spent two planner revisions redrawing a stage
    that was correct all along.
    """

    def test_a_deleted_spec_is_dropped_from_the_gates_selection(self, repo):
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "old_spec.rb").write_text("describe\n")
        (repo / "spec" / "kept_spec.rb").write_text("describe\n")
        g = Git(repo)
        g.commit_all("specs")
        sha = g.head_sha()

        (repo / "spec" / "old_spec.rb").unlink()
        (repo / "spec" / "kept_spec.rb").write_text("describe :more\n")
        g.commit_all("fold one spec into the other")

        cfg, stage = build(repo, scoped_test_command="rspec {paths}")
        paths = resolve_test_paths(stage, cfg, g, sha, for_loop=False)

        assert "spec/kept_spec.rb" in paths
        assert "spec/old_spec.rb" not in paths, (
            "a deleted spec in the command makes it unable to pass"
        )

    def test_the_surviving_spec_still_reaches_the_command(self, repo):
        # The guard must not be so eager that a consolidation runs nothing.
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "kept_spec.rb").write_text("describe\n")
        g = Git(repo)
        g.commit_all("spec")
        sha = g.head_sha()
        (repo / "spec" / "kept_spec.rb").write_text("describe :more\n")
        g.commit_all("edit")

        cfg, stage = build(repo, scoped_test_command="rspec {paths}")
        assert resolve_test_command(stage, cfg, g, sha, for_loop=False) == (
            "rspec spec/kept_spec.rb"
        )


class TestBothSidesSpellTheSameSetTheSameWay:
    """The same set of specs, and therefore the same string.

    `verify._recorded_answer` skips the gate's run when the loop already ran
    *this command* on *this HEAD* — two facts compared, not trust. It compares
    the command as a string, and the two sides build their path list in
    different orders by construction: the gate leads with what the diff says
    was touched, the loop with what the stage declared. Same set, different
    spelling, and the record misses in silence.

    Measured over one run's log: **18 adjacent pairs where the two commands
    named an identical set of files in a different order, and 18 of 18 differed
    only in the spelling** — 790 seconds, 13 minutes, of re-running specs on a
    tree nothing had touched. The saving was unavailable to that project even
    if it had granted the layer, which is the part worth pinning: the config
    switch reads as the whole story and would have been a no-op.

    Sorting is at the selection rather than at the comparison because the same
    two facts should be *visible* as the same in the log. It is also the rule
    `CLAUDE.md` already states for a command run in two places, applied to the
    argument list rather than to the flags.
    """

    def _both(self, repo):
        (repo / "spec").mkdir(exist_ok=True)
        for name in ("a_spec.rb", "b_spec.rb", "c_spec.rb"):
            (repo / "spec" / name).write_text("describe :x\n")
        g = Git(repo)
        g.commit_all("specs")
        sha = g.head_sha()
        # Edited, so the gate finds them in the diff — and in an order that is
        # git's rather than the stage's.
        for name in ("a_spec.rb", "c_spec.rb"):
            (repo / "spec" / name).write_text("describe :y\n")
        g.commit_all("edit two")

        cfg, stage = build(
            repo,
            {
                # Declared last-first, which is what makes the two orders
                # disagree without changing the set.
                "test_paths": ["spec/c_spec.rb", "spec/b_spec.rb"],
                "edit_files": ["spec/a_spec.rb", "spec/b_spec.rb"],
            },
            scoped_test_command="rspec {paths}",
        )
        return (
            resolve_test_command(stage, cfg, g, sha, for_loop=True),
            resolve_test_command(stage, cfg, g, sha, for_loop=False),
        )

    def test_the_set_is_the_same(self, repo):
        loop, gate = self._both(repo)
        assert set(loop.split()[1:]) == set(gate.split()[1:])

    def test_and_so_is_the_string(self, repo):
        loop, gate = self._both(repo)
        assert loop == gate
