"""Which paths may be named on the runner's command line.

Two defects from one run, both visible in a single command:

    bin/parallel_rspec spec/config spec/controllers spec/factories/user_factory.rb
      spec/features … spec/models spec/models/user_spec.rb spec/requests …

`spec/models/user_spec.rb` is already covered by `spec/models`, and
`spec/factories/user_factory.rb` is not a spec at all — it is a factory
definition that the suite loads once through its own helper. Naming it on the
command line loaded it a second time, the suite broke, and the executor was
told its diff had failed. It then began editing the factory to defend against
a double load that our path selection had caused: a repair for a defect the
machinery introduced, landing permanently in the project.

The cause is that `test_file_patterns` answers two different questions with one
list. `**/*_spec.rb` describes a file the runner can be pointed at; `spec/**`
describes a *tree*, and exists so the new-tests gate can ask whether a change
touched tests at all. Only the first kind may reach a command line.
"""

from __future__ import annotations

from code_gantry.gates import prune_contained, runnable_test_patterns

PATTERNS = [
    "**/test_*.py",
    "**/*_test.rb",
    "**/*_spec.rb",
    "**/*.test.ts",
    "test/**",
    "tests/**",
    "spec/**",
]


class TestRunnableTestPatterns:
    def test_tree_globs_are_dropped(self):
        assert runnable_test_patterns(PATTERNS) == [
            "**/test_*.py",
            "**/*_test.rb",
            "**/*_spec.rb",
            "**/*.test.ts",
        ]

    def test_a_spec_still_matches(self):
        from code_gantry.globs import matches_any

        kept = runnable_test_patterns(PATTERNS)
        assert matches_any("spec/models/user_spec.rb", kept)

    def test_a_factory_under_spec_does_not(self):
        """The incident. It matches `spec/**` and nothing else."""
        from code_gantry.globs import matches_any

        assert matches_any("spec/factories/user_factory.rb", PATTERNS)
        assert not matches_any(
            "spec/factories/user_factory.rb", runnable_test_patterns(PATTERNS)
        )

    def test_a_bare_star_segment_is_a_tree_too(self):
        """`spec/**/*` names every file under a tree as surely as `spec/**`.

        The rule is not "ends in `**`" — it is that the last segment carries no
        literal text, so the pattern constrains the directory and says nothing
        about the file.
        """
        assert runnable_test_patterns(["spec/**/*", "spec/**/*_spec.rb"]) == [
            "spec/**/*_spec.rb"
        ]

    def test_a_project_with_only_tree_patterns_keeps_none(self):
        """A straight answer, and the policy about it lives elsewhere.

        What the caller does with an empty result is `_diff_test_patterns`'
        decision and is tested there — it falls back to the whole list, because
        a project that describes only trees has said nothing about which files
        are specs and must not be quietly narrowed on that account.
        """
        assert runnable_test_patterns(["spec/**", "test/**"]) == []


class TestPruneContained:
    def test_a_file_under_a_named_directory_is_dropped(self):
        assert prune_contained(["spec/models", "spec/models/user_spec.rb"]) == [
            "spec/models"
        ]

    def test_a_sibling_is_kept(self):
        assert prune_contained(["spec/models", "spec/factories/x_spec.rb"]) == [
            "spec/factories/x_spec.rb",
            "spec/models",
        ]

    def test_a_prefix_that_is_not_a_path_boundary_is_not_containment(self):
        """`spec/model` does not contain `spec/models/user_spec.rb`.

        String prefixes are how this goes wrong; the comparison is on path
        segments.
        """
        assert prune_contained(["spec/model", "spec/models/user_spec.rb"]) == [
            "spec/model",
            "spec/models/user_spec.rb",
        ]

    def test_deep_nesting_collapses_to_the_outermost(self):
        assert prune_contained(
            ["spec", "spec/models", "spec/models/user_spec.rb"]
        ) == ["spec"]

    def test_an_exact_duplicate_survives_once(self):
        assert prune_contained(["spec/models", "spec/models"]) == ["spec/models"]

    def test_the_result_is_sorted(self):
        """The memo compares the command as text, so spelling is the contract."""
        assert prune_contained(["spec/z", "spec/a"]) == ["spec/a", "spec/z"]


class TestNoFileShapedPatternsAtAll:
    """A project that describes only trees keeps what it had.

    Selecting nothing would be narrower than today's behaviour, decided by a
    change made for a different project — the shape of a ceiling that starts
    deciding outcomes. Two existing fixtures configure exactly this and caught
    it.
    """

    def test_the_fallback_is_the_whole_list(self):
        from code_gantry.gates import _diff_test_patterns

        assert _diff_test_patterns(["spec/**", "test/**"]) == [
            "spec/**",
            "test/**",
        ]

    def test_a_mixed_list_still_narrows(self):
        from code_gantry.gates import _diff_test_patterns

        assert _diff_test_patterns(["spec/**", "**/*_spec.rb"]) == ["**/*_spec.rb"]
