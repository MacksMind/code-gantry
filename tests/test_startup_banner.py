"""The line that says a run has begun, before anything slow happens.

Two problems, and the second is the one that has already cost time.

**An operator watching the console sees nothing.** `run_preflight` is the
first thing either command does, and on a fresh run it starts docker, prepares
the test database and runs the whole suite — minutes — before the first
`click.echo`. The `run_id` is not even generated until after it, so during
that window there is no id to look up and no run directory to list. Alive,
hung and dead all look identical.

**And `last-run.out` is appended across every invocation**, with nothing in it
marking where one starts. That is the append-only-log trap already recorded
in the project instructions: a monitor grepping the file for "Paused at your
request" matched 24 historical pauses and reported a stop that had not
happened. Anchoring correctly currently means taking `wc -l` out of band
before launching and remembering to. A banner carrying the wall clock and the
pid gives every later reader an in-band anchor it can find by itself.

The banner is a pure string so it can be tested without starting anything, and
it names the pid rather than only the time because two invocations in the same
second are exactly the case a resume loop produces.
"""

import re
import subprocess

import pytest
from click.testing import CliRunner

from orchestrator import cli
from orchestrator.cli import _startup_banner
from orchestrator.runtime import ProjectPaths


def _cfg(tmp_path):
    from orchestrator.config import parse_config

    return parse_config({
        "target_repo": str(tmp_path),
        "base_ref": "main",
        "project_branch": "upgrade/thing",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.6-sol"},
    })


class TestItSaysWhatIsStarting:
    def test_it_names_the_command_and_what_it_is_working_on(self, tmp_path):
        text = _startup_banner("run", "demo", _cfg(tmp_path), pid=4242)
        assert "run" in text and "demo" in text

    def test_it_names_the_repository_and_the_branch(self, tmp_path):
        text = _startup_banner("run", "demo", _cfg(tmp_path), pid=4242)
        assert str(tmp_path) in text
        assert "upgrade/thing" in text

    def test_it_says_preflight_is_what_the_wait_is(self, tmp_path):
        # The whole point: the silence has a cause and the operator should be
        # told what it is rather than inferring it.
        text = _startup_banner("run", "demo", _cfg(tmp_path), pid=4242)
        assert "preflight" in text.lower()

    def test_a_run_says_the_suites_are_included(self, tmp_path):
        text = _startup_banner("run", "demo", _cfg(tmp_path), pid=4242, run_tests=True)
        assert "suite" in text.lower()

    def test_a_resume_says_they_are_not(self, tmp_path):
        # A resume skips them, so it is quick, and promising minutes of waiting
        # would be its own kind of wrong.
        text = _startup_banner(
            "resume", "20260807-x", _cfg(tmp_path), pid=4242, run_tests=False
        )
        assert "suite" in text.lower()


class TestItIsAnAnchorForAMonitor:
    def test_it_carries_the_pid_and_a_utc_timestamp(self, tmp_path):
        text = _startup_banner("run", "demo", _cfg(tmp_path), pid=4242)
        assert "4242" in text
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text)

    def test_it_is_findable_by_a_fixed_marker(self, tmp_path):
        # A monitor has to be able to find the last one without knowing the
        # slug, the branch or the time it started.
        text = _startup_banner("run", "demo", _cfg(tmp_path), pid=4242)
        assert text.splitlines()[0].startswith("=== orchestrator ")

    def test_two_invocations_in_one_second_are_still_distinct(self, tmp_path):
        a = _startup_banner("run", "demo", _cfg(tmp_path), pid=1, now="2026-01-01T00:00:00Z")
        b = _startup_banner("run", "demo", _cfg(tmp_path), pid=2, now="2026-01-01T00:00:00Z")
        assert a != b


class TestBothCommandsEmitItBeforePreflight:
    """Pinned by making preflight fail, not by reading the source.

    The defect is entirely that one call comes after another, so a test that
    merely checked the banner exists would have passed before this change.
    Here preflight raises: if the banner is emitted after it, it is never
    emitted at all, and the assertion fails for the right reason.
    """

    @pytest.fixture
    def demo(self, tmp_path, monkeypatch):
        repo = tmp_path / "target"
        repo.mkdir()
        (repo / "PLAN.md").write_text("# Plan\n\n1. Do it.\n")
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
            ["add", "-A"],
            ["commit", "-q", "-m", "initial"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True)

        projects = tmp_path / "projects" / "demo"
        projects.mkdir(parents=True)
        (projects / "config.yaml").write_text(
            f"""
target_repo: {repo}
base_ref: main
project_branch: work
plan_root: PLAN.md
test_command: "true"
executor:
  model: m
planner:
  model: claude-opus-5
reviewer:
  model: gpt-5.5
"""
        )
        # Nothing to approve any more: a config is identified by its git
        # sha, and `run` reaches preflight without a separate gate.
        monkeypatch.chdir(tmp_path)
        return projects

    def _preflight_explodes(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("preflight reached")

        monkeypatch.setattr(cli, "run_preflight", boom)

    def test_run_banners_before_preflight(self, demo, monkeypatch):
        self._preflight_explodes(monkeypatch)
        result = CliRunner().invoke(cli.main, ["run", "demo"], catch_exceptions=True)
        assert "=== orchestrator run demo" in result.output
        assert isinstance(result.exception, RuntimeError)

    def test_resume_banners_before_preflight(self, demo, monkeypatch):
        self._preflight_explodes(monkeypatch)
        from orchestrator.config import parse_config

        cfg = parse_config(
            {
                "target_repo": str(demo.parent.parent / "target"),
                "base_ref": "main", "project_branch": "work",
                "plan_root": "PLAN.md", "test_command": "true",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.5"},
            }
        )
        monkeypatch.setattr(
            cli, "_locate_run", lambda rid: (ProjectPaths("demo"), cfg)
        )
        monkeypatch.setattr(cli, "_load_state", lambda *a, **k: {"failure_layer": None})
        result = CliRunner().invoke(
            cli.main, ["resume", "20260807-x"], catch_exceptions=True
        )
        assert "=== orchestrator resume 20260807-x" in result.output
        assert isinstance(result.exception, RuntimeError)
