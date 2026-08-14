"""`code-gantry reconcile` — checking the plan against what the branch did.

This file exists because of how its subject was written. The command called
`Git.commits_between`, which had been deleted an hour earlier when its only
caller was removed. The full suite passed: the call sits inside a function
body, so a missing method is a runtime `AttributeError`, and nothing exercised
the command. It failed the first time a human ran it.

The lesson is narrow and worth encoding: a CLI command with no test is
untested however green the suite is. These drive the real `click` entry point
so the code path actually executes.
"""

import subprocess

from pathlib import Path

import pytest
from click.testing import CliRunner

from code_gantry import cli
from code_gantry.planner import PlannerOutcome


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A target repo with a plan, and a project config pointing at it."""
    repo = tmp_path / "target"
    (repo / "docs" / "addendum").mkdir(parents=True)
    (repo / "app").mkdir()
    (repo / "docs" / "plan.md").write_text("# Plan\n\n1. Convert 24 call sites.\n")
    (repo / "app" / "thing.rb").write_text("render text: 'x'\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        ["commit", "-q", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)
    (repo / "app" / "thing.rb").write_text("render plain: 'x'\n")
    subprocess.run(["git", "commit", "-qam", "convert one site"], cwd=repo, check=True)

    projects = tmp_path / "projects"
    (projects / "demo").mkdir(parents=True)
    (projects / "demo" / "config.yaml").write_text(
        f"""
target_repo: {repo}
base_ref: main
project_branch: work
plan_root: docs/plan.md
plan_addendum_path: docs/addendum
test_command: "true"
executor:
  model: m
planner:
  model: claude-opus-5
  repo_access: true
reviewer:
  model: gpt-5.5
"""
    )
    # PROJECTS_ROOT is a relative path bound as a default argument at import
    # time, so patching the module attribute does nothing. Chdir instead.
    monkeypatch.chdir(tmp_path)
    return repo, projects


def stub_planner(monkeypatch, notes, *, reader=object()):
    class Stub:
        def __init__(self):
            self.reader = reader
            self.messages = None

        def plan(self, messages):
            self.messages = messages
            return PlannerOutcome(
                verdict="project_complete",
                reasoning="checked",
                status_entry="e",
                plan_notes=notes,
                tool_calls=["search(render text: in .) -> 1 line(s)"],
                reads_answered=1,
            )

    stub = Stub()
    monkeypatch.setattr(cli, "make_planner", lambda *a, **k: stub)
    return stub


A_NOTE = {
    "plan_path": "PLAN.md",
    "anchor": "1. Convert 24 call sites",
    "observation": "search finds 0 remaining in app/",
}


class TestItActuallyRuns:
    def test_the_command_executes_end_to_end(self, project, monkeypatch):
        # The test that would have caught the deleted method: it calls through
        # to real git rather than stubbing the repository.
        stub_planner(monkeypatch, [A_NOTE])
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code == 0, result.output
        assert "1 commit(s)" in result.output

    def test_the_observation_is_written_to_the_addendum(self, project, monkeypatch):
        repo, _ = project
        stub_planner(monkeypatch, [A_NOTE])
        CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        written = (repo / "docs" / "addendum" / "plan-addendum.md").read_text()
        assert "search finds 0 remaining" in written
        assert "1. Convert 24 call sites" in written

    def test_dry_run_writes_nothing(self, project, monkeypatch):
        repo, _ = project
        stub_planner(monkeypatch, [A_NOTE])
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml", "--dry-run"])
        assert "search finds 0 remaining" in result.output
        assert not (repo / "docs" / "addendum" / "plan-addendum.md").exists()

    def test_nothing_to_add_says_so_and_writes_nothing(self, project, monkeypatch):
        repo, _ = project
        stub_planner(monkeypatch, [])
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert "nothing to add" in result.output
        assert not (repo / "docs" / "addendum" / "plan-addendum.md").exists()


class TestItRefusesWhenItCannotWork:
    def test_without_repo_access_it_explains_why(self, project, monkeypatch):
        # Reconciling means checking the plan against the repository. Without
        # the read tools it would be the planner guessing, which is the failure
        # this whole mechanism exists to correct.
        stub_planner(monkeypatch, [A_NOTE], reader=None)
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code != 0
        assert "repo_access" in result.output


class TestTheDiffIsAgainstBaseRef:
    def test_the_prompt_names_the_configured_base_not_main(self, tmp_path, monkeypatch):
        # `base_ref` is config. A project whose baseline is `develop` or a
        # release branch must be reconciled against that, not against whatever
        # this project happens to call it.
        repo = tmp_path / "t"
        (repo / "docs" / "addendum").mkdir(parents=True)
        (repo / "docs" / "plan.md").write_text("# Plan\n")
        for args in (
            ["init", "-q", "-b", "develop"],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
            ["config", "commit.gpgsign", "false"],
            ["add", "-A"],
            ["commit", "-q", "-m", "initial"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True)
        subprocess.run(["git", "checkout", "-q", "-b", "work"], cwd=repo, check=True)

        projects = tmp_path / "projects"
        (projects / "demo").mkdir(parents=True)
        (projects / "demo" / "config.yaml").write_text(
            f"target_repo: {repo}\nbase_ref: develop\nproject_branch: work\n"
            "plan_root: docs/plan.md\nplan_addendum_path: docs/addendum\n"
            'test_command: "true"\nexecutor:\n  model: m\n'
            "planner:\n  model: claude-opus-5\n  repo_access: true\n"
            "reviewer:\n  model: gpt-5.5\n"
        )
        monkeypatch.chdir(tmp_path)

        stub = stub_planner(monkeypatch, [])
        CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        sent = stub.messages[0]["content"]
        assert "develop" in sent
        assert "main" not in sent


class TestAnUnverifiedVerdictIsRefused:
    """"Nothing to add" from a planner that read nothing is not a finding.

    Observed live: the same command against the same repository produced six
    cited observations on one call and "nothing to add" on the next, the second
    having made no tool calls at all. Recording that as "checked, nothing
    found" would be worse than recording nothing, because it reads as evidence
    and would stop anyone looking again.
    """

    def test_an_empty_verdict_without_reads_is_an_error(self, project, monkeypatch):
        stub = stub_planner(monkeypatch, [])
        monkeypatch.setattr(
            type(stub),
            "plan",
            lambda self, messages: PlannerOutcome(
                verdict="project_complete",
                reasoning="looks fine",
                status_entry="e",
                plan_notes=[],
                tool_calls=[],
            ),
        )
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code != 0
        assert "without reading anything" in result.output

    def test_calls_that_were_all_refused_do_not_count_as_reading(
        self, project, monkeypatch
    ):
        """A denial is a record of looking, not a record of having looked.

        Once refusals joined the ledger, `tool_calls` stopped being a proxy for
        "it read something": a planner that asked twice for paths that are not
        there now has a non-empty log and has seen nothing. The guard counts
        answered calls for exactly that reason.
        """
        stub = stub_planner(monkeypatch, [])
        monkeypatch.setattr(
            type(stub),
            "plan",
            lambda self, messages: PlannerOutcome(
                verdict="project_complete",
                reasoning="looks fine",
                status_entry="e",
                plan_notes=[],
                tool_calls=[
                    "read_file(app/ghost.rb) -> refused: does not exist",
                    "read_file(app/other_ghost.rb) -> refused: does not exist",
                ],
                reads_answered=0,
            ),
        )
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code != 0
        assert "without reading anything" in result.output

    def test_notes_without_reads_are_refused_too(self, project, monkeypatch):
        # Notes are the more dangerous direction: an unsourced claim about the
        # plan gets written down and acted on.
        stub = stub_planner(monkeypatch, [A_NOTE])
        monkeypatch.setattr(
            type(stub),
            "plan",
            lambda self, messages: PlannerOutcome(
                verdict="project_complete",
                reasoning="r",
                status_entry="e",
                plan_notes=[A_NOTE],
                tool_calls=[],
            ),
        )
        repo, _ = project
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code != 0
        assert not (repo / "docs" / "addendum" / "plan-addendum.md").exists()

    def test_a_verdict_backed_by_reads_is_accepted(self, project, monkeypatch):
        stub_planner(monkeypatch, [])
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code == 0
        assert "nothing to add" in result.output
        assert "1 read(s)" in result.output


class TestAFailedCallIsNotAVerdict:
    """An unreachable planner is not a planner with nothing to say.

    An expired API key produced `blocked` with an empty tool log. The first
    version of this command read that as "the planner chose not to look", and
    a retry and a confident commit message were built on three data points
    that were all 401s. The two states must be distinguishable here or the
    same mistake is available to anyone reading the output.
    """

    def test_a_transport_failure_says_so(self, project, monkeypatch):
        stub = stub_planner(monkeypatch, [])
        monkeypatch.setattr(
            type(stub), "plan",
            lambda self, m: PlannerOutcome(
                verdict="blocked",
                reasoning="the planner call failed: Error code: 401",
                status_entry="e",
                plan_notes=[],
                tool_calls=[],
                failed=True,
            ),
        )
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code != 0
        assert "could not be reached" in result.output
        assert "401" in result.output
        # And emphatically not the other message.
        assert "without reading anything" not in result.output

    def test_a_real_toolless_answer_still_reports_as_one(self, project, monkeypatch):
        stub = stub_planner(monkeypatch, [])
        monkeypatch.setattr(
            type(stub), "plan",
            lambda self, m: PlannerOutcome(
                verdict="project_complete", reasoning="looks fine",
                status_entry="e", plan_notes=[], tool_calls=[], failed=False,
            ),
        )
        result = CliRunner().invoke(cli.main, ["reconcile", "projects/demo/config.yaml"])
        assert result.exit_code != 0
        assert "without reading anything" in result.output


class TestThePlanDirectoryIsNotWork:
    """Editing the plan is not progress against it.

    Reconcile diffs base against the branch, and that range carries everything
    the branch ever did — including a restructure of the plan corpus itself.
    Asked what the branch accomplished, the planner dutifully reported the
    document set being relocated, the cross-refs being rewritten, and the
    citation lines in the log's own earlier entries going stale. Three of six
    entries, all true, none of them work.

    The log rides in the prompt prefix on every planning step now, so that is a
    standing cost rather than a one-off tidy-up. Cheaper to say what does not
    count than to move commits onto base so the diff stops showing them.
    """

    def _prompt_text(self):
        from code_gantry.cli import _load, _reconcile_prompt

        cfg = _load(Path("projects/demo/config.yaml"))
        return _reconcile_prompt(cfg)[0]["content"]

    def test_it_names_the_plan_directory(self, project):
        assert "docs" in self._prompt_text()

    def test_it_says_document_changes_are_not_progress(self, project):
        text = self._prompt_text().lower()
        assert "not progress" in text or "not work" in text
