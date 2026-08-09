"""The in-process edit cycle.

The ordering assertions are the substance here. Lint before the gates because
it rewrites; commit before the tests because squash-merge is what makes "every
commit on the project branch is green" and "the executor commits before it
tests" both true; the cheap regex gates before the suite. Each of those is
pinned by observation rather than by reading the code, because the code is what
these tests exist to catch changing.
"""

import json
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


def build(repo, stage_overrides=None, executor=None, **cfg_overrides):
    from orchestrator.config import Stage

    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "test_command": "true",
        "executor": executor or {"model": "m"},
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
        """Two commits, and the second is the rewrite alone.

        This test passed for a year on one commit, because the single commit
        the cycle made was unconditionally labelled `after checks` whether the
        checks had touched anything or not. It asserted the message and the
        message was a constant. What it names — that a rewrite is separable —
        needs the *contents* of the second commit.
        """
        cfg, stage = build(
            repo, {"checks": ["printf 'class C\\nend\\n' > app/a.rb"]}
        )
        drive(repo, cfg, stage, ScriptedModel([[edit_file("app/a.rb", "class A", "class B")]]))

        import subprocess

        def out(*args):
            return subprocess.run(
                args, cwd=repo, capture_output=True, text=True
            ).stdout

        subjects = out("git", "log", "--format=%s").splitlines()
        assert "after checks" in subjects[0]
        assert "after checks" not in subjects[1]

        # The model's edit is in the parent; the rewrite of it is on its own.
        rewrite = out("git", "show", "--format=", "HEAD")
        assert "+class C" in rewrite and "-class B" in rewrite
        assert "+class B" not in rewrite

        authored = out("git", "show", "--format=", "HEAD~1")
        assert "+class B" in authored and "class C" not in authored

    def test_checks_that_rewrite_nothing_make_no_second_commit(self, repo):
        # The log line and the commit both claimed a rewrite on every cycle
        # that committed anything, which is every cycle the model edited in.
        # Twenty such lines were read as twenty rewrites; none of them were.
        cfg, stage = build(repo, {"checks": ["true"]})
        drive(repo, cfg, stage, ScriptedModel([[edit_file("app/a.rb", "class A", "class B")]]))

        import subprocess

        subjects = subprocess.run(
            ["git", "log", "--format=%s"], cwd=repo, capture_output=True, text=True
        ).stdout
        assert "after checks" not in subjects

    def test_the_transcript_records_every_item_in_order(self, tmp_path):
        from types import SimpleNamespace

        from orchestrator.executor import Transcript

        t = Transcript(
            [{"role": "system", "content": [{"type": "input_text", "text": "SYS"}]}],
            tmp_path,
        )
        t.append(SimpleNamespace(type="function_call", name="read_file", arguments="{}"))
        t.extend([{"type": "function_call_output", "call_id": "c", "output": []}])

        rows = [
            json.loads(line)
            for line in (tmp_path / "executor-conversation.jsonl").read_text().splitlines()
        ]
        assert [r.get("role") or r.get("type") for r in rows] == [
            "system", "function_call", "function_call_output",
        ]
        assert rows[1]["name"] == "read_file"
        assert len(t) == 3

    def test_a_directory_that_cannot_be_written_does_not_fail_the_attempt(self, tmp_path):
        # Best effort, like every other artifact here: an attempt that worked
        # must not be failed by a record of it that could not be kept.
        from orchestrator.executor import Transcript

        t = Transcript([{"role": "user", "content": "x"}], tmp_path / "nope" / "deeper")
        t.append({"role": "user", "content": "y"})
        assert len(t) == 2

    def test_an_item_that_will_not_serialise_is_still_recorded(self, tmp_path):
        from orchestrator.executor import Transcript

        class Odd:
            type = "reasoning"

        t = Transcript([], tmp_path)
        t.append(Odd())
        rows = (tmp_path / "executor-conversation.jsonl").read_text().splitlines()
        assert len(rows) == 1
        assert json.loads(rows[0])["type"] == "reasoning"

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


class TestTheTranscriptIsReadableWhileTheAttemptRuns:
    """The record has to exist while the attempt runs, not after it.

    `executor-conversation.json` was a JSON array written once, on the way out
    — so a stage that spent forty minutes had nothing to read for thirty-nine
    of them, and an attempt that never returned left nothing at all. That is
    the shape of gap the monitor rules elsewhere are about: the only instrument
    on a live attempt was the run log, which deliberately reports counts after
    the fact because the loop makes sixty calls a cycle.

    Driven through `run_agent_stage` rather than against `Transcript` directly.
    A unit test can only show that appending writes; what is being claimed is
    that the file is *there while the model is still working*, and the only
    place that can be observed is inside the model's own turn.
    """

    def _drive(self, repo, tmp_path, monkeypatch, inspect, cycles=1):
        from orchestrator import executorclient
        from orchestrator.edittools import Edit
        from orchestrator.executor import Executor
        from orchestrator.executorclient import ExecutorTurn

        history = tmp_path / "attempt"
        history.mkdir()
        counter = {"n": 0}

        class Scripted:
            def __init__(self, *a, **kw):
                pass

            def run(self, conversation, reader, editor, semantic=None, cache_key=None):
                counter["n"] += 1
                inspect(history, counter["n"])
                out = ExecutorTurn()
                out.turns = 1
                out.stopped = True
                editor.edit(
                    "app/a.rb", [Edit(f"class {chr(64 + counter['n'])}",
                                      f"class {chr(65 + counter['n'])}")]
                )
                return out

        monkeypatch.setattr(executorclient, "OpenAIExecutorModel", Scripted)
        cfg, stage = build(
            repo, {"must_not_remain": ["class"]} if cycles > 1 else None
        )
        git = Git(repo)
        Executor(cfg, CommandRunner(cwd=repo, timeout=60), git).run_agent_stage(
            stage, "STAGE INSTRUCTION", history_dir=history,
            since_sha=git.head_sha(),
        )
        return history

    def test_the_prompt_is_on_disk_before_the_model_has_said_anything(
        self, repo, tmp_path, monkeypatch
    ):
        seen = {}

        def inspect(history, n):
            path = history / "executor-conversation.jsonl"
            seen["transcript"] = path.read_text() if path.exists() else ""
            seen["prompt"] = (history / "sent-prompt.md").exists()

        self._drive(repo, tmp_path, monkeypatch, inspect)

        assert "STAGE INSTRUCTION" in seen["transcript"], (
            "the opening messages must be readable before the first turn"
        )
        assert seen["prompt"] is True

    def test_each_cycle_lands_before_the_next_one_starts(
        self, repo, tmp_path, monkeypatch
    ):
        sizes = []

        def inspect(history, n):
            path = history / "executor-conversation.jsonl"
            sizes.append(len(path.read_text().splitlines()) if path.exists() else 0)

        self._drive(repo, tmp_path, monkeypatch, inspect, cycles=2)

        assert len(sizes) > 1, "the gate must have fed back and run another cycle"
        assert sizes[1] > sizes[0], (
            "the feedback turn from cycle 1 should be on disk before cycle 2 runs"
        )

    def test_the_finished_file_parses_line_by_line(
        self, repo, tmp_path, monkeypatch
    ):
        history = self._drive(repo, tmp_path, monkeypatch, lambda h, n: None)
        lines = (history / "executor-conversation.jsonl").read_text().splitlines()
        assert lines and all(isinstance(json.loads(line), dict) for line in lines)


class TestWhatTheExecutorAskedFor:
    """The third agentic loop to report what it looked at.

    The planner and reviewer have logged this since they got tools; the
    executor makes more calls than either and logged none of them. Counts
    rather than rendered calls, because the planner's forty-call line is
    already hard to read and this one makes sixty a cycle — and the calls
    themselves sit in `executor-conversation.json` beside the log line.
    """

    def test_both_ledgers_are_merged(self, repo):
        from orchestrator.edittools import Edit
        from orchestrator.executor import _count_tool_use, ExecutionResult
        from orchestrator.repotools import ToolCall

        cfg, stage = build(repo)
        reader, editor = parts(repo, stage)
        reader.read_file("app/a.rb")
        editor.edit("app/a.rb", [Edit("class A", "class B")])

        out = ExecutionResult(ok=True)
        _count_tool_use(out, reader, editor)
        assert out.tool_counts == {"read_file": 1, "edit": 1}
        assert out.refusal_counts == {}

    def test_refusals_are_bucketed_by_what_to_do_about_them(self, repo):
        # Not by which function raised. "budget" means stop asking, "not
        # found" means read the file, "not unique" means widen the anchor —
        # a count of ToolError would say nothing an operator could act on.
        from orchestrator.executor import _count_tool_use, ExecutionResult

        cfg, stage = build(repo)
        reader, editor = parts(repo, stage)
        editor.record_refusal("edit", "a.rb", "edit 1: that text does not appear in the file.")
        editor.record_refusal("edit", "a.rb", "edit 1: that text appears 3 times, so it does not")
        reader.record_refusal("read_file", "b.rb", "too many tool calls in one step (limit 60).")

        out = ExecutionResult(ok=True)
        _count_tool_use(out, reader, editor)
        assert out.refusal_counts == {
            "edit not found": 1, "edit not unique": 1, "budget": 1
        }

    def test_the_fallback_route_travels_from_the_editor_to_the_counts(self, repo):
        """A real refused edit, through the ledger, into the bucket.

        Written as a journey rather than two unit tests because that is where
        this class of value has been lost four times: computed correctly,
        written correctly, and dropped in transit. The route is decided inside
        `nearest_text`, raised on the error, recorded on the call and bucketed
        here, and nothing between those points has a test of its own.

        It matters because three changes now separate a bad `old_string` from a
        stalled attempt and they are not interchangeable — the separator stops
        refusals, the windows make them recoverable. A blended count cannot say
        which one moved.
        """
        from orchestrator.edittools import Edit
        from orchestrator.executor import ExecutionResult, _count_tool_use
        from orchestrator.executortools import dispatch

        cfg, stage = build(repo)
        reader, editor = parts(repo, stage)
        (editor.repo / "app" / "a.rb").write_text(
            "class A\n  def total\n    sum\n  end\nend\n"
        )
        # Indentation wrong throughout, which is the shape 63% of real misses
        # took. The first line still matches exactly once, so the anchor places
        # it without the index being consulted.
        out_text = dispatch(
            "edit",
            {
                "path": "app/a.rb",
                "edits": [
                    {"old_string": "    def total\n      sum\n    end", "new_string": "x"}
                ],
            },
            reader, editor, None,
        )
        assert out_text.startswith("cannot do that:")

        out = ExecutionResult(ok=True)
        _count_tool_use(out, reader, editor)
        assert out.refusal_counts == {"edit not found (anchor)": 1}

    def test_a_loop_that_asked_for_nothing_reports_nothing(self, repo):
        from orchestrator.executor import _count_tool_use, ExecutionResult

        cfg, stage = build(repo)
        reader, editor = parts(repo, stage)
        out = ExecutionResult(ok=True)
        _count_tool_use(out, reader, editor)
        assert out.tool_counts == {}


class TestTheLocatorIsWiredButIsNotATool:
    """Consulted when an edit misses; never offered to the model.

    The index lags the working tree by however many edits and commits have
    happened since it was built. That is survivable for something choosing
    where to look and not for something quoting bytes exactly, which is why it
    reaches the editor and not the tool list.
    """

    def test_no_semantic_tool_is_offered_even_when_configured(self):
        from orchestrator.executortools import tool_schemas

        names = {t["name"] for t in tool_schemas(None)}
        assert "semantic_search" not in names

    def test_an_unconfigured_project_gets_no_locator(self, repo):
        from orchestrator.executorloop import build_loop_parts

        cfg, stage = build(repo)
        _, editor, _sem = build_loop_parts(stage, cfg, repo)
        assert editor.locator is None

    def test_a_configured_project_gets_one(self, repo, monkeypatch):
        # Endpoints arrive by environment variable name, never as literals: a
        # tailnet host is an identifiable infrastructure value and the config
        # file is tracked and hashed for approval.
        from orchestrator.executorloop import build_loop_parts

        monkeypatch.setenv("TEST_EMBED_BASE", "http://embed")
        monkeypatch.setenv("TEST_QDRANT", "http://qdrant")
        cfg, stage = build(
            repo,
            executor={
                "model": "m",
                "semantic_search": {
                    "api_base_env": "TEST_EMBED_BASE",
                    "qdrant_url_env": "TEST_QDRANT",
                    "embedding_model": "e", "collection": "c",
                },
            },
        )
        _, editor, _sem = build_loop_parts(stage, cfg, repo)
        assert editor.locator is not None

    def test_the_lookup_lands_in_the_ledger_under_its_own_name(self):
        # It costs an embedding and a query, so it belongs in the attempt's
        # record — but under a name that does not imply the model asked.
        from orchestrator.semantic import SemanticSearch, SemanticSearchConfig

        calls = []
        s = SemanticSearch(
            SemanticSearchConfig(
                api_base="http://x", qdrant_url="http://y",
                embedding_model="e", collection="c",
            ),
            calls=calls,
        )
        s._search = lambda q: [{"payload": {"path": "a.rb", "content": "TEXT"}}]
        s.chunks_for("something", "a.rb")
        assert [c.tool for c in calls] == ["locate"]


class TestTheContextHighWaterMarkAndCostReachTheResult:
    """Two values the subprocess executor used to supply, on the path that
    replaced it.

    Both were scraped from a console — `context_tokens_from_log` and
    `cost_from_log` — and the in-process loop set neither. `advance` guards on
    `if executor_context_tokens or executor_cost_usd` before calling
    `append_stage_cost`, so the guard went permanently false and
    `stage-costs.md` stopped being written the hour the executor switched,
    while `executor-loop.json` carried correct usage throughout. The planner
    reads `stage-costs.md` on every call.

    The context figure is the *peak* single-turn prompt, not the first and not
    the sum. That is what the scraper reported and what the docstring on
    `context_tokens_from_log` argues for: "what bounds the next stage is the
    high-water mark, not the last thing it happened to say." Keeping the same
    quantity is what lets the series continue across the cutover instead of
    silently changing instrument. The opening turn understates it badly — tool
    results accumulate as the loop runs — and the sum is every turn added
    together, which answers no question anyone has.
    """

    def _model(self, peaks, edits=None):
        """Reports a peak per cycle, and edits so the loop keeps going.

        Without an edit the loop breaks on `if not editor.touched`, so a
        multi-cycle assertion silently measures one cycle. That is the shape of
        fake that makes a max() look like a first().
        """
        scripted = list(edits or [])

        class Peaked:
            def __init__(self):
                self.left = list(peaks)

            def run(self, conversation, reader, editor, semantic=None, cache_key=None):
                from orchestrator.executorclient import ExecutorTurn
                from orchestrator.openaiclient import TokenUsage

                out = ExecutorTurn()
                out.turns = 1
                out.stopped = True
                if scripted:
                    scripted.pop(0)(editor)
                peak = self.left.pop(0)
                out.peak_prompt_tokens = peak
                out.first_prompt_tokens = peak // 2
                out.usage = TokenUsage(
                    prompt_tokens=peak, cached_tokens=0,
                    cache_write_tokens=0, completion_tokens=100,
                )
                return out

        return Peaked()

    def test_the_peak_is_carried_not_the_first_or_the_sum(self, repo):
        cfg, stage = build(repo)
        out = drive(repo, cfg, stage, self._model([9000]))
        assert out.context_tokens == 9000, "the turn's peak did not reach the result"
        assert out.first_prompt_tokens == 4500, "still recorded, and still not the peak"

    def test_the_peak_is_the_largest_across_cycles(self, repo):
        # A rework cycle can load more than the first one did, and a later
        # cycle can load less. Neither the last nor the first is the bound.
        cfg, stage = build(
            repo, {"must_not_remain": ["class"]},
            executor={"model": "m", "max_cycles": 3},
        )
        out = drive(repo, cfg, stage, self._model(
            [4000, 21000, 7000],
            edits=[
                edit_file("app/a.rb", "class A", "class B"),
                edit_file("app/a.rb", "class B", "class C"),
                edit_file("app/a.rb", "class C", "class D"),
            ],
        ))
        assert out.cycles == 3, "the fixture did not actually run three cycles"
        assert out.context_tokens == 21000

    def test_an_unpriced_model_reports_none_rather_than_zero(self, repo, monkeypatch, tmp_path):
        # `load_price_map` fetches before it falls back, so without
        # blocking the fetch this reads the live table and the pinned
        # one is never consulted. And the loop memoises, so the cache
        # has to be cleared between tests.
        import orchestrator.pricing as pricing
        from orchestrator import executorloop
        monkeypatch.setattr(pricing, "_fetch", lambda url: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(executorloop, "_PRICES", None)
        # `price_usage` returns None for "no rate", and the distinction is the
        # whole reason to compute this rather than scrape it: a zero has meant
        # "not priced" as often as "free".
        empty = tmp_path / "prices.json"
        empty.write_text("{}")
        monkeypatch.setenv("ORCHESTRATOR_PRICE_MAP", str(empty))
        cfg, stage = build(repo)
        out = drive(repo, cfg, stage, self._model([9000]))
        assert out.cost_usd is None

    def test_a_priced_model_is_billed_from_real_counts(self, repo, monkeypatch, tmp_path):
        # `load_price_map` fetches before it falls back, so without
        # blocking the fetch this reads the live table and the pinned
        # one is never consulted. And the loop memoises, so the cache
        # has to be cleared between tests.
        import orchestrator.pricing as pricing
        from orchestrator import executorloop
        monkeypatch.setattr(pricing, "_fetch", lambda url: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(executorloop, "_PRICES", None)
        table = tmp_path / "prices.json"
        table.write_text(json.dumps({
            "m": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
        }))
        monkeypatch.setenv("ORCHESTRATOR_PRICE_MAP", str(table))
        cfg, stage = build(repo)
        out = drive(repo, cfg, stage, self._model([9000]))
        # 9000 prompt @ 1e-6 + 100 completion @ 2e-6
        assert out.cost_usd == pytest.approx(0.0092)

    def test_an_attempt_whose_gate_passes_is_priced_too(
        self, repo, monkeypatch, tmp_path
    ):
        """The success path skipped pricing entirely, and nothing here saw it.

        `run_loop` returned early the moment `_gate_cycle` came back clean —
        before `out.cost_usd = _price(...)` at the bottom. So every attempt
        that worked first time was billed at zero, and only attempts that
        failed a gate or edited nothing were priced at all.

        Invisible to the two tests above because `_model` defaults to no edits,
        which breaks the loop on `not editor.touched` and falls out of the
        bottom — the same shape as `CLAUDE.md`'s "an earlier branch can eat
        every fixture", one branch later. Both were green throughout.

        Measured across one run's 57 recorded attempts: 41 carried real usage
        and `cost_usd == 0`, together 17,943,722 prompt tokens against
        77,402,051 billed, the largest single unbilled attempt 2,319,957.
        """
        import orchestrator.pricing as pricing
        from orchestrator import executorloop
        monkeypatch.setattr(pricing, "_fetch", lambda url: (_ for _ in ()).throw(OSError()))
        monkeypatch.setattr(executorloop, "_PRICES", None)
        table = tmp_path / "prices.json"
        table.write_text(json.dumps({
            "m": {"input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}
        }))
        monkeypatch.setenv("ORCHESTRATOR_PRICE_MAP", str(table))
        cfg, stage = build(repo)
        out = drive(
            repo, cfg, stage,
            self._model(
                [9000],
                edits=[lambda ed: edit_file("app/a.rb", "class A", "class B")(ed)],
            ),
        )
        assert out.commits, "the fixture must reach the gate, not break before it"
        assert out.cost_usd == pytest.approx(0.0092)


class TestSetupRunsInsideTheLoop:
    """The environment, re-prepared where a stage's own edits can break it.

    `setup_command` already ran twice — precheck before the attempt, verify
    after it — and never in between. So a stage that edits a migration or a
    Gemfile ran its in-session tests against an environment its own work had
    made stale, could not see why, and would start repairing code that was
    not broken. Same shape as the linter deadlock: the model cannot see or
    affect the thing that is actually wrong.

    The failure goes back to the model rather than to a human, and that needs
    no heuristic. Precheck ran this same command on the pre-edit tree and it
    passed — a precondition of the attempt existing — so a failure here is the
    stage's doing by construction.
    """

    def test_a_failing_setup_becomes_feedback_not_an_abort(self, repo):
        cfg, stage = build(repo, {"setup_command": "exit 3"})
        model = ScriptedModel([
            [edit_file("app/a.rb", "class A", "class B")],
            [edit_file("app/a.rb", "class B", "class C")],
        ])
        out = drive(repo, cfg, stage, model)

        assert out.in_loop_failures, "the failure was not recorded"
        assert "environment" in out.in_loop_failures[0]
        # And the model got another turn with it, rather than the attempt
        # ending on a problem it could fix.
        assert model.calls > 1

    def test_the_feedback_says_the_stage_caused_it(self, repo):
        # Without this the model reads an environment failure as something to
        # wait out or report, which is what the gate's wording means and is
        # the wrong reading from inside the loop.
        from orchestrator.gates import run_setup

        cfg, stage = build(repo, {"setup_command": "echo 'Could not find gem' && exit 1"})
        found = run_setup(stage, cfg, CommandRunner(cwd=repo, timeout=60))
        assert not found.ok
        assert "before you started" in found.feedback
        assert "Could not find gem" in found.feedback

    def test_no_setup_command_is_not_a_gate(self, repo):
        from orchestrator.commands import CommandRunner
        from orchestrator.gates import run_setup

        cfg, stage = build(repo)
        assert run_setup(stage, cfg, CommandRunner(cwd=repo, timeout=60)).ok


class TestTheGatesAreEvaluatedLazily:
    """Cheapest-first ordering that never took the saving.

    The gates were the elements of a tuple literal, and a tuple literal
    evaluates every element before the loop body sees the first — so the suite
    ran even when `patterns` had already failed, on every cycle, for as long as
    the ordering has been documented as cheapest-first.

    Harmless while every entry was a pure question. Not harmless once one of
    them prepares an environment: building a tuple would restart containers.
    """

    def test_the_suite_does_not_run_when_a_cheap_gate_fails(self, repo):
        # `must_not_remain` fails on the tree the model leaves, and the test
        # command writes a file if it runs. It must not exist afterwards.
        marker = repo / "ran-the-suite"
        cfg, stage = build(
            repo,
            {"must_not_remain": ["class B"]},
            test_command=f"touch {marker} && true",
        )
        drive(repo, cfg, stage, ScriptedModel([
            [edit_file("app/a.rb", "class A", "class B")],
        ]))
        assert not marker.exists(), "the suite ran behind a failed regex gate"
