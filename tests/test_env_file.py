"""Credentials named by the config and read from a file it points at.

The config is a tracked file in the repository it describes, so it may name a
path and must never carry a value. The path it names lands in `work_dir`,
which preflight already refuses to run without having proved is git-ignored —
so the one place the tool can be sure is untracked is the one place the file
goes.

Parsed rather than sourced. `KEY=value` and nothing else: no shell, no
expansion, no command substitution. A sourced credentials file is arbitrary
execution by another name, and argv-never-a-shell is the whole of this
project's safety story.

The shell wins over the file. An `export` for a one-off test still takes
effect, and a stale file cannot silently shadow a deliberate override.
"""

import os

import pytest

from code_gantry.config import ConfigError, parse_config
from code_gantry.envfile import apply_env_file, parse_env_file


class TestParsing:
    def test_plain_assignments(self):
        assert parse_env_file("A=1\nB=two\n") == {"A": "1", "B": "two"}

    def test_comments_and_blank_lines_are_ignored(self):
        text = "# a comment\n\nA=1\n   # indented comment\nB=2\n"
        assert parse_env_file(text) == {"A": "1", "B": "2"}

    def test_an_export_prefix_is_accepted(self):
        # The file people already have is one they were sourcing.
        assert parse_env_file("export A=1\n") == {"A": "1"}

    def test_surrounding_quotes_are_stripped(self):
        assert parse_env_file("A='1'\nB=\"2\"\n") == {"A": "1", "B": "2"}

    def test_a_value_may_contain_equals(self):
        # Base64 and URLs both do. Split once, not on every separator.
        assert parse_env_file("A=a=b=c\n") == {"A": "a=b=c"}

    def test_whitespace_around_the_name_and_value_goes(self):
        assert parse_env_file("  A = 1  \n") == {"A": "1"}

    def test_nothing_is_expanded(self):
        # `$HOME` and `$(id)` are values here, not instructions. A file that
        # can run a command is a file that can do anything the run can.
        got = parse_env_file("A=$HOME\nB=$(id)\nC=`id`\n")
        assert got == {"A": "$HOME", "B": "$(id)", "C": "`id`"}

    def test_a_line_that_is_not_an_assignment_is_an_error(self):
        # Silently skipping it is how a typo becomes a missing credential
        # three checks later, reported as something else entirely.
        with pytest.raises(ConfigError) as e:
            parse_env_file("A=1\nthis is not an assignment\n")
        assert "line 2" in str(e.value)

    def test_an_empty_name_is_an_error(self):
        with pytest.raises(ConfigError):
            parse_env_file("=1\n")

    def test_an_empty_value_is_allowed(self):
        # Distinct from absent, and a provider that wants a placeholder key
        # is a real case.
        assert parse_env_file("A=\n") == {"A": ""}


class TestPrecedence:
    def test_the_file_fills_in_what_is_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CG_TEST_KEY", raising=False)
        env = tmp_path / "env"
        env.write_text("CG_TEST_KEY=from-file\n")
        applied = apply_env_file(env)
        assert os.environ["CG_TEST_KEY"] == "from-file"
        assert applied == ["CG_TEST_KEY"]

    def test_the_shell_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CG_TEST_KEY", "from-shell")
        env = tmp_path / "env"
        env.write_text("CG_TEST_KEY=from-file\n")
        applied = apply_env_file(env)
        assert os.environ["CG_TEST_KEY"] == "from-shell"
        # Reported as not applied, so a log line can say what the file
        # actually contributed rather than what it contained.
        assert applied == []

    def test_a_missing_file_is_a_config_problem(self, tmp_path):
        with pytest.raises(ConfigError) as e:
            apply_env_file(tmp_path / "nope")
        assert "nope" in str(e.value)


class TestTheConfigField:
    def _data(self, repo, **over):
        data = {
            "base_ref": "main",
            "project_branch": "proj",
            "plan_root": "PLAN.md",
            "full_test_command": "true",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.5"},
        }
        data.update(over)
        return data

    def test_it_resolves_against_the_config_directory(self, tmp_path):
        # The same rule `target_repo` and `config_rel_path` already follow, so
        # the answer does not depend on the cwd a run was launched from — the
        # property that made a 1.76MB rate table land in a tracked directory
        # when it was missing.
        repo = tmp_path / "r"
        (repo / "docs").mkdir(parents=True)
        (repo / ".git").mkdir()
        source = repo / "docs" / "cg.yaml"
        source.write_text("")
        cfg = parse_config(
            self._data(repo, target_repo=str(repo), env_file=".code_gantry/env"),
            source=source,
        )
        assert cfg.env_file == repo / "docs" / ".code_gantry" / "env"

    def test_it_is_optional(self, tmp_path):
        repo = tmp_path / "r"
        (repo / ".git").mkdir(parents=True)
        cfg = parse_config(self._data(repo, target_repo=str(repo)))
        assert cfg.env_file is None

    def test_parsing_the_config_does_not_touch_the_environment(
        self, tmp_path, monkeypatch
    ):
        """A reader must not write.

        `load_state` created its table before reading and destroyed the
        evidence it was about to look for. Parsing a config that *names* a
        credentials file must not load it: the config is data, and applying it
        is an action the caller takes deliberately.
        """
        monkeypatch.delenv("CG_TEST_KEY", raising=False)
        repo = tmp_path / "r"
        (repo / ".git").mkdir(parents=True)
        (repo / "env").write_text("CG_TEST_KEY=from-file\n")
        source = repo / "cg.yaml"
        source.write_text("")
        parse_config(
            self._data(repo, target_repo=str(repo), env_file="env"), source=source
        )
        assert "CG_TEST_KEY" not in os.environ


class TestItReachesTheProcess:
    """The journey, because the endpoints were never the problem.

    A value computed correctly and written correctly, then lost between two
    schemas, is this codebase's most repeated defect. Here the value has to
    travel from a path in a YAML file, through config parsing, into the
    process environment, before any client is built — so the test drives the
    CLI seam every command goes through rather than the loader on its own.
    """

    def test_the_cli_seam_applies_it(self, tmp_path, monkeypatch):
        import subprocess

        from code_gantry.cli import _project_for

        monkeypatch.delenv("CG_TEST_KEY", raising=False)
        repo = tmp_path / "r"
        (repo / "docs").mkdir(parents=True)
        (repo / ".gitignore").write_text(".code_gantry/\n")
        (repo / "docs" / "PLAN.md").write_text("# plan\n")
        for args in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@example.com"],
            ["git", "config", "user.name", "T"],
            ["git", "add", "-A"],
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "first"],
        ):
            subprocess.run(args, cwd=repo, check=True)

        (repo / "docs" / ".code_gantry").mkdir()
        (repo / "docs" / ".code_gantry" / "env").write_text("CG_TEST_KEY=from-file\n")
        source = repo / "docs" / "cg.yaml"
        source.write_text(
            "base_ref: main\n"
            "project_branch: proj\n"
            "plan_root: docs/PLAN.md\n"
            "full_test_command: 'true'\n"
            "env_file: .code_gantry/env\n"
            "executor: {model: m}\n"
            "planner: {model: claude-opus-5}\n"
            "reviewer: {model: gpt-5.5}\n"
        )

        _project_for(source)
        assert os.environ["CG_TEST_KEY"] == "from-file"
