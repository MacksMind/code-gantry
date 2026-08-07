"""The in-process edit cycle.

The ordering assertions are the substance here. Lint before the gates because
it rewrites; commit before the tests because squash-merge is what makes "every
commit on the project branch is green" and "the executor commits before it
tests" both true; the cheap regex gates before the suite. Each of those is
pinned by observation rather than by reading the code, because the code is what
these tests exist to catch changing.
"""

import subprocess

import pytest

from orchestrator.commands import CommandRunner
from orchestrator.config import parse_config
from orchestrator.edittools import FileEditor
from orchestrator.executorloop import run_loop
from orchestrator.gitops import Git
from orchestrator.repotools import ReadBudget, RepoReader


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app").mkdir(parents=True)
    (r / "app" / "a.rb").write_text("class A\nend\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "first"],
    ):
        subprocess.run(args, cwd=r, check=True)
    return r


def build(repo, stage_overrides=None, **cfg_overrides):
    from orchestrator.config import Stage

    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "executor": {"model": "m", "provider": "openai"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    data.update(cfg_overrides)
    cfg = parse_config(data)
    fields = {"id": "s1", "instruction": "do it", "edit_files": ["app/**"]}
    fields.update(stage_overrides or {})
    return cfg, Stage(**fields)


class ScriptedModel:
    """Applies canned edits, then stops. Stands in for the Responses client.

    Deliberately not a mock of the client: what these tests are about is the
    loop's ordering and budgets, and a real client would put an API contract in
    the middle of that. The client has its own tests.
    """

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0

    def run(self, conversation, reader, editor, semantic=None, cache_key=None):
        from orchestrator.executorclient import ExecutorTurn

        out = ExecutorTurn()
        out.turns = 1
        self.calls += 1
        if self.scripts:
            for fn in self.scripts.pop(0):
                fn(editor)
        out.stopped = True
        return out


def edit_file(rel, old, new):
    from orchestrator.edittools import Edit

    return lambda editor: editor.edit(rel, [Edit(old, new)])


def parts(repo, stage):
    reader = RepoReader(Git(repo), repo, ReadBudget())
    reader.writable_globs = list(stage.edit_files)
    editor = FileEditor(repo=repo, edit_files=list(stage.edit_files))
    return reader, editor


def drive(repo, cfg, stage, model, **kw):
    reader, editor = parts(repo, stage)
    git = Git(repo)
    return run_loop(
        stage, cfg, git, CommandRunner(cwd=repo, timeout=60), model, reader, editor,
        since_sha=git.head_sha(), **kw,
    )


class TestTheHappyPath:
    def test_one_edit_then_stop_commits_and_passes(self, repo):
        cfg, stage = build(repo)
        out = drive(repo, cfg, stage, ScriptedModel([[edit_file("app/a.rb", "class A", "class B")]]))

        assert out.ok is True
        assert out.cycles == 1
        assert out.commits
        assert Git(repo).is_clean()
        assert "class B" in (repo / "app" / "a.rb").read_text()


class TestOrdering:
    def test_the_commit_precedes_the_test_run(self, repo):
        """Pinned by observation, not by reading the loop.

        A test command that records whether the tree was clean when it ran is
        the only way to assert this from outside. Committing after the tests
        would make "every commit on the project branch is green" false the
        moment a stage landed on a squash of an untested tree.
        """
        # Outside the repository: a redirect into the repo creates the file
        # before `git status` runs, so the marker would report its own
        # untracked self and the test would measure nothing.
        marker = repo.parent / "state.txt"
        cfg, stage = build(
            repo,
            test_command=f"git status --porcelain > {marker}",
        )
        drive(repo, cfg, stage, ScriptedModel([[edit_file("app/a.rb", "class A", "class B")]]))

        assert marker.exists(), "the test command never ran"
        assert marker.read_text().strip() == "", (
            "the tree was dirty when the tests ran, so the commit came after them"
        )

    def test_a_check_that_rewrites_is_committed_before_the_gates_read(self, repo):
        # `rubocop -A` and its kin exit zero *after* changing files. Run after
        # the gates, the rewrite is swept up silently on landing and orphaned
        # when the stage fails.
        cfg, stage = build(
            repo,
            {"checks": ["printf 'class C\\nend\\n' > app/a.rb"]},
        )
        out = drive(repo, cfg, stage, ScriptedModel([[edit_file("app/a.rb", "class A", "class B")]]))

        assert out.ok is True
        assert Git(repo).is_clean()
        assert "class C" in (repo / "app" / "a.rb").read_text()


class TestBudgets:
    def test_a_failing_gate_feeds_back_and_runs_another_cycle(self, repo):
        cfg, stage = build(repo, {"must_not_remain": ["class B"]})
        model = ScriptedModel([
            [edit_file("app/a.rb", "class A", "class B")],
            [edit_file("app/a.rb", "class B", "class D")],
        ])
        out = drive(repo, cfg, stage, model)

        assert model.calls == 2
        assert out.cycles == 2
        assert out.ok is True
        assert out.in_loop_failures and "residue" not in out.in_loop_failures[0].lower()

    def test_exhausting_the_cycles_still_leaves_the_work_committed(self, repo):
        # The loop is cooperative, so there is no mid-write kill. That turns
        # "the executor committed before verify" from an inference into a
        # guarantee, and the graph reads a committed tree.
        cfg, stage = build(repo, {"must_not_remain": ["class"]}, )
        model = ScriptedModel([
            [edit_file("app/a.rb", "class A", "class B")],
            [edit_file("app/a.rb", "class B", "class C")],
            [edit_file("app/a.rb", "class C", "class D")],
        ])
        out = drive(repo, cfg, stage, model)

        assert out.cycles == 3
        assert Git(repo).is_clean()
        assert out.commits
        assert len(out.in_loop_failures) == 3

    def test_two_identical_trees_in_a_row_end_the_loop_early(self, repo):
        # `_layer_progress`'s reasoning one level down: spending another cycle
        # to learn nothing is the same waste at either altitude.
        cfg, stage = build(repo, {"must_not_remain": ["class"]})
        model = ScriptedModel([
            [edit_file("app/a.rb", "class A", "class B")],
            [],
            [],
        ])
        out = drive(repo, cfg, stage, model)

        assert out.cycles == 2, "a second cycle that changed nothing should stop it"

    def test_a_model_that_changes_nothing_at_all_ends_the_loop(self, repo):
        # Not adjudicated here: the scope gate already owns the sentence "the
        # attempt produced no changes", and two places saying it is how they
        # drift.
        cfg, stage = build(repo)
        out = drive(repo, cfg, stage, ScriptedModel([[]]))

        assert out.cycles == 1
        assert not out.commits


class TestFailures:
    def test_a_client_failure_is_the_one_thing_that_reports_not_ok(self, repo):
        class Broken:
            def run(self, conversation, reader, editor, semantic=None, cache_key=None):
                from orchestrator.executorclient import ExecutorTurn

                out = ExecutorTurn()
                out.failure = "the executor call failed: connection reset"
                return out

        cfg, stage = build(repo)
        out = drive(repo, cfg, stage, Broken())

        assert out.ok is False
        assert "connection reset" in out.log

    def test_a_refused_edit_is_counted_not_swallowed(self, repo):
        # The instrument for the claim this design rests on and has not yet
        # earned: that exact matching plus a read tool beats fuzzy matching.
        def refuse(editor):
            from orchestrator.edittools import Edit
            from orchestrator.repotools import ToolError

            try:
                editor.edit("app/a.rb", [Edit("nope", "x")])
            except ToolError as e:
                editor.record_refusal("edit", "app/a.rb", str(e))

        cfg, stage = build(repo)
        out = drive(repo, cfg, stage, ScriptedModel([[refuse]]))

        assert out.edit_refusals
        assert "does not appear" in out.edit_refusals[0]


class TestTheRecordOfAnAttempt:
    def test_commit_messages_distinguish_the_cycles(self, repo):
        # Squashed on landing, so this changes nothing about the project
        # branch. A stage branch is what you read when a stage misbehaves, and
        # five commits all saying the same thing cannot tell cycle 1 from
        # cycle 3, or the model's edits from a formatter's rewrite of them.
        cfg, stage = build(repo, {"must_not_remain": ["class"]})
        model = ScriptedModel([
            [edit_file("app/a.rb", "class A", "class B")],
            [edit_file("app/a.rb", "class B", "class C")],
        ])
        drive(repo, cfg, stage, model)

        import subprocess

        log = subprocess.run(
            ["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "cycle 1" in log and "cycle 2" in log

    def test_a_checks_rewrite_says_so_in_its_own_commit(self, repo):
        cfg, stage = build(
            repo, {"checks": ["printf 'class C\\nend\\n' > app/a.rb"]}
        )
        drive(repo, cfg, stage, ScriptedModel([[edit_file("app/a.rb", "class A", "class B")]]))

        import subprocess

        log = subprocess.run(
            ["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "after checks" in log

    def test_the_sent_prompt_is_written_whole(self, tmp_path):
        # `prompt.md` carries the stage half only on this path, so it stopped
        # explaining why an attempt existed. A reader opening the directory
        # after a rework saw a prompt identical to the previous attempt's.
        from orchestrator.executor import _write_sent_prompt

        conversation = [
            {"role": "system", "content": [{"type": "input_text", "text": "SYS"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "CONV"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "STAGE"}]},
            {"role": "user", "content": [{"type": "input_text", "text": "REWORK"}]},
        ]
        _write_sent_prompt(tmp_path, conversation)
        text = (tmp_path / "sent-prompt.md").read_text()
        assert text.index("SYS") < text.index("CONV") < text.index("STAGE")
        assert "REWORK" in text

    def test_it_stops_at_the_first_thing_the_model_said(self, tmp_path):
        # The exchange belongs in the transcript; this is the record of what
        # was asked.
        from types import SimpleNamespace

        from orchestrator.executor import _write_sent_prompt

        conversation = [
            {"role": "user", "content": [{"type": "input_text", "text": "ASKED"}]},
            SimpleNamespace(type="reasoning"),
            {"type": "function_call_output", "call_id": "c", "output": []},
        ]
        _write_sent_prompt(tmp_path, conversation)
        text = (tmp_path / "sent-prompt.md").read_text()
        assert "ASKED" in text
        assert "function_call_output" not in text
