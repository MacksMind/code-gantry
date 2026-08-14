"""The config lives in the repository it describes, and names its own work dir.

Two things move together, and each is the reason the other is safe.

**The config is a property of the target repo.** Every command in it —
`bin/parallel_rspec`, `bin/rubocop -A`, the docker invocation — is that
repository's, and drifts when that repository does. Versioned beside the code
it describes, a change to the suite and the change to how the suite is invoked
land in one commit and are reviewed together. Versioned in CodeGantry,
they are two commits in two repositories and only one of them gets read.

**Everything CodeGantry writes moves out of its own tree.** `work_dir` is
the single place runs, logs, the flake ledger, the cost ledger and the approval
record live, so CodeGantry repository holds code and nothing else.

The relocation creates one hazard and closes it twice. Arbitrary shell —
`checks`, `test_command`, `setup_command` — is now a file inside the tree the
executor edits, where before it was in a repository the executor could not
reach. `_is_plan_document` covers the config for the reason its own docstring
gives about agent-context documents: "a stage able to edit one could retire its
own constraints". And approval covers the other direction: a config that
arrives by `git pull` or by anyone's branch fails its hash and refuses to run,
which is the case `.gitignore` already keeps `approval.json` uncommitted for.

Reading it at the run's sha would be a third guard and is not needed: the CLI
loads the config once at process start and holds the object, so a mid-run edit
cannot take effect on the run that is going. Only the next resume can see it,
and that is the one approval already checks.
"""

import os

import pytest

from code_gantry.config import ConfigError, parse_config


def _data(**over):
    base = {
        "project_branch": "p",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.6-sol"},
    }
    base.update(over)
    return base


def _ctx(cfg):
    from code_gantry.verify import _Context

    return _Context(
        stage=None, cfg=cfg, git=None, runner=None, stage_start_sha="",
        stage_branch=None, project_branch=None, base_ref=None, base_sha=None,
    )


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".code_gantry").mkdir(parents=True)
    (tmp_path / "PLAN.md").write_text("# plan\n")
    return tmp_path


class TestTheWorkDir:
    def test_it_is_expanded(self, repo, monkeypatch):
        # A tracked file in a shared repository cannot carry one machine's home
        # directory. Two people with the same checkout need different work
        # dirs, and the config is now the same file for both.
        monkeypatch.setenv("ORCH_TEST_WORK", str(repo / "elsewhere"))
        cfg = parse_config(
            _data(target_repo=str(repo), work_dir="${ORCH_TEST_WORK}/proj")
        )
        assert cfg.work_dir == repo / "elsewhere" / "proj"

    def test_a_home_relative_path_is_expanded(self, repo):
        cfg = parse_config(_data(target_repo=str(repo), work_dir="~/o/proj"))
        assert str(cfg.work_dir).startswith(os.path.expanduser("~"))
        assert "~" not in str(cfg.work_dir)

    def test_it_defaults_beside_the_plan(self, repo):
        # Nothing to configure for the ordinary case. The plan documents and
        # the record of what was done to them are one project, and a later
        # reader wants them together — so the data goes under the plan's own
        # directory rather than at the repository root.
        cfg = parse_config(
            _data(target_repo=str(repo), plan_root="docs/migration/PLAN.md"),
            source=repo / "docs" / "migration" / "code_gantry.yaml",
        )
        assert cfg.work_dir == repo / "docs" / "migration" / ".code_gantry"

    def test_the_repo_is_found_by_walking_up(self, repo):
        # Not a fixed depth: the config sits beside the plan, and a plan root
        # is commonly several directories down.
        (repo / "docs" / "migration").mkdir(parents=True)
        cfg = parse_config(
            _data(plan_root="docs/migration/PLAN.md"),
            source=repo / "docs" / "migration" / "code_gantry.yaml",
        )
        assert cfg.target_repo == repo
        assert cfg.config_rel_path == "docs/migration/code_gantry.yaml"

    def test_an_unset_variable_is_a_config_error(self, repo):
        # Not a directory literally named `${NOPE}`. A run that writes its
        # entire record somewhere unintended is worse than one that refuses.
        with pytest.raises(ConfigError) as exc:
            parse_config(_data(target_repo=str(repo), work_dir="${ORCH_NOT_SET}/x"))
        assert "ORCH_NOT_SET" in str(exc.value)


class TestTheTargetRepoIsDerived:
    def test_the_config_names_the_repo_it_sits_in(self, repo):
        # One fewer absolute path in a tracked file, and it cannot disagree
        # with reality: the repo is wherever the config was read from.
        cfg = parse_config(_data(), source=repo / ".code_gantry" / "config.yaml")
        assert cfg.target_repo == repo

    def test_an_explicit_value_still_wins(self, repo, tmp_path):
        # The tests build configs without a file, and an operator may point at
        # a worktree. Derivation is the default, not a rule.
        other = tmp_path / "other"
        other.mkdir()
        cfg = parse_config(
            _data(target_repo=str(other)), source=repo / ".code_gantry" / "config.yaml"
        )
        assert cfg.target_repo == other

    def test_neither_is_a_config_error(self):
        with pytest.raises(ConfigError) as exc:
            parse_config(_data())
        assert "target_repo" in str(exc.value)


class TestTheExecutorCannotEditIt:
    def test_the_config_is_a_plan_document(self, repo):
        """Arbitrary shell is now inside the tree the executor edits.

        `checks`, `test_command` and `setup_command` are operator-authored
        commands that run unattended. Before the move they sat in a repository
        no stage could reach; after it they are one `edit_files` glob away.
        """
        from code_gantry.verify import _is_plan_document

        cfg = parse_config(_data(), source=repo / ".code_gantry" / "config.yaml")
        ctx = _ctx(cfg)
        assert _is_plan_document(".code_gantry/config.yaml", ctx)

    def test_an_ordinary_file_still_is_not(self, repo):
        from code_gantry.verify import _is_plan_document

        cfg = parse_config(_data(), source=repo / ".code_gantry" / "config.yaml")
        ctx = _ctx(cfg)
        assert not _is_plan_document("app/models/order.rb", ctx)


class TestTheCharBudgetTracksTheLineBudget:
    """A second dimension whose default ignored the first is a tightening.

    `max_chars_per_call` bounds one call and `max_total_chars` bounds the step,
    for the reason `ReadBudget` records: a line is not a unit of size. But the
    step ceiling shipped with a default computed from the *class* default line
    budget, while every real config overrides the line budget three to seven
    times higher.

    Measured on this project's config before it was caught: the reviewer had
    `max_read_lines_total: 20000` and an effective char budget of 240,000 —
    a seventh of what the line budget implies. It would have bound long before
    the ceiling it was added underneath, and silently, because a read budget
    refusal reads the same whichever ceiling raised it.

    So it is derived at parse time from the configured value. Eighty characters
    a line is a generous average for source, which is the point: the ceiling
    exists for the minified bundle and the one-row fixture, and should never be
    what stops ordinary reading.
    """

    def test_it_follows_a_raised_line_budget(self):
        from code_gantry.config import ReviewerConfig

        cfg = ReviewerConfig(model="m", max_read_lines_total=30_000)
        assert cfg.max_read_chars_total == 30_000 * 80

    def test_an_explicit_value_still_wins(self):
        from code_gantry.config import ReviewerConfig

        cfg = ReviewerConfig(
            model="m", max_read_lines_total=30_000, max_read_chars_total=1_000
        )
        assert cfg.max_read_chars_total == 1_000

    def test_every_role_derives_it(self):
        from code_gantry.config import (
            ExecutorConfig,
            PlannerConfig,
            ReviewerConfig,
        )

        for kind in (ExecutorConfig, PlannerConfig, ReviewerConfig):
            cfg = kind(model="m", max_read_lines_total=7_000)
            assert cfg.max_read_chars_total == 7_000 * 80, kind.__name__
