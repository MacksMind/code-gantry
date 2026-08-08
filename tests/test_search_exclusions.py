"""Paths a search must not read, declared by the operator.

Which files are vendored, minified or generated is project knowledge, so it
lives in config rather than in code — the same rule that keeps a framework's
vocabulary out of every tool description.

The cost of not having this was measured. `vendor/assets/javascripts/fckeditor/
editor/js/fckeditorcode_gecko.js` holds 240,181 characters in 108 lines, and
`jquery.min.js` averages 23,157 characters per line: a search matching three
such lines returns more text than an entire planner prompt. Ranking every
tracked file by characters-per-line separates them cleanly from source, and the
`max_chars_per_call` ceiling then truncates mid-token in a file nobody can act
on anyway.

Because a hit in a vendored or minified file is never actionable — the stage
cannot edit it — excluding it removes a cost with no matching benefit, which is
a different argument from the ceiling's. The ceiling stays underneath as the
backstop for whatever the globs do not anticipate.
"""

import subprocess

import pytest

from orchestrator.gitops import Git
from orchestrator.repotools import ReadBudget, RepoReader


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app").mkdir(parents=True)
    (r / "vendor" / "assets").mkdir(parents=True)
    (r / "public").mkdir()
    (r / "app" / "order.rb").write_text("class Order\n  TARGET = 1\nend\n")
    (r / "vendor" / "assets" / "big.js").write_text("var TARGET=" + "a" * 500 + ";\n")
    (r / "app" / "jquery.min.js").write_text("TARGET=" + "b" * 500 + ";\n")
    (r / "public" / "legacy.htm").write_text("TARGET here\n")
    for args in (
        ["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"],
        ["add", "-A"], ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def _reader(repo, exclude=()):
    return RepoReader(
        Git(repo), repo, ReadBudget(), search_exclude_globs=list(exclude)
    )


class TestExclusionsKeepUnreadablePathsOutOfSearch:
    def test_without_them_everything_matches(self, repo):
        hits = _reader(repo).search("TARGET")
        assert any("vendor/" in h for h in hits)
        assert any("jquery.min.js" in h for h in hits)
        assert any("public/" in h for h in hits)

    def test_declared_globs_are_excluded(self, repo):
        hits = _reader(repo, ["vendor/**", "*.min.js", "public/**"]).search("TARGET")
        assert [h.split(":")[0] for h in hits] == ["app/order.rb"]

    def test_an_exclusion_beats_a_models_own_glob(self, repo):
        # ripgrep resolves overlapping globs in order and the last wins, so a
        # model asking for `**/*` must not be able to re-include what the
        # operator excluded. Same ordering hazard as `!.git`.
        hits = _reader(repo, ["vendor/**"]).search("TARGET", "**/*")
        assert not any("vendor/" in h for h in hits)

    def test_it_does_not_stop_a_deliberate_read(self, repo):
        # `read_file` is the planner naming a path on purpose. The exclusion is
        # about what a search sweeps up by accident, and conflating the two
        # would make a file the operator can see unreadable to the pipeline.
        out = _reader(repo, ["vendor/**"]).read_file("vendor/assets/big.js")
        assert "TARGET" in out

    def test_no_exclusions_configured_changes_nothing(self, repo):
        assert len(_reader(repo, []).search("TARGET")) == 4


class TestItIsProjectKnowledge:
    def test_the_config_field_exists_and_defaults_to_empty(self):
        from orchestrator.config import parse_config

        cfg = parse_config({
            "target_repo": ".", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        assert cfg.search_exclude_globs == []

    def test_no_default_ships_one_projects_layout(self):
        # A default naming `vendor/` or `public/` would be this repository's
        # shape shipped to every other project's planner.
        from orchestrator.config import ProjectConfig

        default = ProjectConfig.model_fields["search_exclude_globs"].default_factory()
        assert default == []
