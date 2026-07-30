"""Glob matching for the scope guard.

`edit_files` globs decide both what the executor may edit and what the scope
guard permits. Getting `**` wrong means either escalating on legitimate edits
or letting the executor roam.
"""

from orchestrator.globs import matches_any


class TestSingleStar:
    def test_matches_direct_children(self):
        assert matches_any("app/thing.rb", ["app/*.rb"])

    def test_does_not_cross_a_slash(self):
        assert not matches_any("app/admin/thing.rb", ["app/*.rb"])

    def test_matches_bare_extension_at_root(self):
        assert matches_any("README.md", ["*.md"])

    def test_bare_extension_does_not_match_nested(self):
        assert not matches_any("docs/guide.md", ["*.md"])


class TestDoubleStar:
    def test_matches_direct_child(self):
        assert matches_any("src/main.py", ["src/**"])

    def test_matches_deeply_nested(self):
        assert matches_any("src/a/b/c/deep.py", ["src/**"])

    def test_does_not_escape_the_prefix(self):
        assert not matches_any("other/main.py", ["src/**"])

    def test_leading_double_star_matches_any_depth(self):
        assert matches_any("a/b/thing.py", ["**/*.py"])

    def test_leading_double_star_also_matches_root(self):
        # `**/` must be optional, or a root-level file fails a pattern the
        # operator reasonably expects to cover everything.
        assert matches_any("thing.py", ["**/*.py"])

    def test_double_star_in_the_middle(self):
        assert matches_any("app/views/order/edit.haml", ["app/**/*.haml"])


class TestExactPaths:
    def test_exact_match(self):
        assert matches_any("config/routes.rb", ["config/routes.rb"])

    def test_non_match(self):
        assert not matches_any("config/routes.rb", ["config/application.rb"])

    def test_prefix_is_not_a_match(self):
        # "app/models" must not authorise "app/models_backup.rb".
        assert not matches_any("app/models_backup.rb", ["app/models"])


class TestMultiplePatterns:
    def test_any_pattern_may_match(self):
        patterns = ["app/services/**", "spec/services/**"]
        assert matches_any("spec/services/thing_spec.rb", patterns)

    def test_no_pattern_matches(self):
        patterns = ["app/services/**", "spec/services/**"]
        assert not matches_any("app/controllers/orders_controller.rb", patterns)

    def test_empty_pattern_list_matches_nothing(self):
        # A stage with no declared scope authorises nothing, which is why
        # config validation requires edit_files on scope-guarded stages.
        assert not matches_any("anything.rb", [])


class TestRegexSafety:
    def test_dots_are_literal(self):
        # A regex-naive translation would let "app/aXrb" match "app/a.rb".
        assert not matches_any("app/aXrb", ["app/a.rb"])

    def test_regex_metacharacters_are_literal(self):
        assert matches_any("weird+name.rb", ["weird+name.rb"])
        assert not matches_any("weirddname.rb", ["weird+name.rb"])


class TestQuestionMark:
    def test_matches_one_character(self):
        assert matches_any("a1.rb", ["a?.rb"])

    def test_does_not_match_two(self):
        assert not matches_any("a12.rb", ["a?.rb"])

    def test_does_not_cross_a_slash(self):
        assert not matches_any("a/b.rb", ["a?b.rb"])


class TestNormalisation:
    def test_leading_dot_slash_is_ignored(self):
        assert matches_any("./src/main.py", ["src/**"])

    def test_pattern_leading_dot_slash_is_ignored(self):
        assert matches_any("src/main.py", ["./src/**"])
