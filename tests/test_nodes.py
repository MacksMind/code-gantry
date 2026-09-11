"""Node logic with stubbed planner, executor, and reviewer.

These are the decisions that make the loop behave: which failures route to the
executor, which to the planner, and which to a human; whether a scope violation
throws work away; and whether the expensive half of the merge gate runs only
when the cheap half passed.
"""

import json
import subprocess
import time

import pytest

from test_config import runner_script, as_test_tools
from dataclasses import dataclass, field

from code_gantry import nodes
from code_gantry.commands import CommandRunner
from code_gantry.config import Stage, parse_config
from code_gantry.executor import ExecutionResult
from code_gantry.gitops import Git, GitError
from code_gantry.ledger import CLAIMED, FINDING_CLAIMED, LANDED, open_ledger, read_ledger
from code_gantry.planmodel import import_documents, parse_markdown
from code_gantry.planner import PlannerOutcome, PlannerUsage
from code_gantry.reviewer import Issue, ReviewOutcome, TokenUsage
from code_gantry.runtime import ProjectPaths, RunPaths, Runtime
from code_gantry.state import RunState, fresh_stage_fields, new_state

# The operator's pattern, as a real project would configure it.
RSPEC_PATTERN = r"^\s*rspec\s+'?\.?/?([^'\s\[:]+_spec\.rb)"


@dataclass
class StubPlanner:
    outcomes: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def plan(self, messages):
        self.calls.append(messages)
        if self.outcomes:
            return self.outcomes.pop(0)
        return PlannerOutcome(
            verdict="project_complete", reasoning="done", status_entry="e"
        )


@dataclass
class StubReviewer:
    outcomes: list = field(default_factory=list)
    calls: int = 0
    cache_keys: list = field(default_factory=list)

    messages: list = field(default_factory=list)

    def review(self, messages, cache_key=None):
        self.cache_keys.append(cache_key)
        self.messages.append(messages)
        self.calls += 1
        if self.outcomes:
            return self.outcomes.pop(0)
        return ReviewOutcome(
            verdict="approved", summary="fine", usage=TokenUsage(1000, 40, 900)
        )


@dataclass
class StubExecutor:
    repo: object = None
    edits: list = field(default_factory=list)
    ok: bool = True
    timed_out: bool = False
    prompts: list = field(default_factory=list)
    history_dirs: list = field(default_factory=list)
    feedback: list = field(default_factory=list)
    failure_layers: list = field(default_factory=list)

    def gather_context(self, stage):
        return [], []

    def _apply(self):
        if self.edits and self.repo is not None:
            name, text = self.edits.pop(0)
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    log: str = "executor log"
    edits_applied: int = 0
    dropped_reads: list = field(default_factory=list)
    unproductive_stop: str = ""

    def run_agent_stage(
        self, stage, prompt, history_dir=None, since_sha="",
        agent_context=None, feedback=None, failure_layer=None, model="",
    ):
        self.prompts.append(prompt)
        self.history_dirs.append(history_dir)
        self.feedback.append(feedback)
        self.failure_layers.append(failure_layer)
        self._apply()
        # The measured fields are set by whoever needs them — subclasses in
        # the tests that care. There is no log to parse any more: the loop
        # reports usage from the provider's own counts, so a stub that scraped
        # text would be pinning a journey that no longer exists.
        return ExecutionResult(
            ok=self.ok, log=self.log, timed_out=self.timed_out,
            edits_applied=self.edits_applied,
            dropped_reads=list(self.dropped_reads),
            unproductive_stop=self.unproductive_stop,
        )

    def run_script_stage(self, stage):
        self._apply()
        return ExecutionResult(ok=self.ok, log="script log")


PLAN_TEXT = "# The plan\n\n- [ ] **do the thing**\n- [ ] **do the other thing**\n"
# Keys the fixture plan imports to: p.001 the document, p.002 and p.003 the items.
THE_ITEM = "p.002"
THE_OTHER_ITEM = "p.003"


def planned_stage(**over):
    fields = {
        "id": "extract",
        "instruction": "Extract the thing.",
        "edit_files": ["app.py", "src/**"],
        "plan_keys": [THE_ITEM],
    }
    fields.update(over)
    return fields


def make(repo, tmp_path, planner=None, reviewer=None, executor=None, **cfg_over):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "PLAN.md",
        "full_test_command": "true",
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
        "ledger": {"key_prefix": "p", "fold_ratio": 1000.0},
    }
    data.update(cfg_over)
    cfg = parse_config(as_test_tools(data))

    project = ProjectPaths(tmp_path / "projects" / "proj-slug")
    project.ensure()
    paths = RunPaths(project, "r1")
    paths.ensure()
    ledger = open_ledger(project.ledger, origin="test-host", actor="run:r1")
    if not ledger.views().documents():
        import_documents(ledger, [parse_markdown(PLAN_TEXT)], prefix="p")

    ex = executor or StubExecutor(repo=repo, edits=[("app.py", "changed\n")])
    ex.repo = repo
    rt = Runtime(
        cfg=cfg,
        project=project,
        paths=paths,
        git=Git(repo),
        runner=CommandRunner(cwd=repo, timeout=60, exclusive=cfg.exclusive_commands()),
        executor=ex,
        planner=planner or StubPlanner(),
        reviewer=reviewer or StubReviewer(),
        ledger=ledger,
    )

    base_sha = rt.git.ensure_project_branch("proj", "main")
    state = new_state(
        run_id="r1",
        project_slug="proj-slug",
        config_hash="hash",
        target_repo=str(repo),
        base_ref="main",
        base_sha=base_sha,
        # As `cli.run` does it: measured against the base, read from the branch.
        plan_sha=rt.git.rev_parse("proj"),
        project_branch="proj",
        started_at=time.time(),
    )
    return cfg, rt, state


def with_stage(state, rt, **over):
    """Put a stage in flight with a cut branch, as precheck would."""
    stage = Stage(**planned_stage(**over))
    state["current"] = stage.model_dump()
    branch = rt.cfg.stage_branch(state["stage_index"], stage.id)
    state["stage_branch"] = branch
    state["stage_start_sha"] = rt.git.cut_stage_branch(branch, "proj")
    state["stage_started_at"] = time.time()
    return state


def _text_of(messages):
    """Every text block of a built prompt, as the model receives it."""
    out = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            out.extend(block.get("text", "") for block in content)
        else:
            out.append(content)
    return "\n\n".join(out)


def _digest(rt, state):
    """The fingerprint verify records when the full suite passes."""
    from code_gantry.verify import diff_digest

    return diff_digest(rt.git, state["stage_start_sha"])


class TestWhatADerivationProduced:
    """Named on every derivation, including the ones that produced one stage.

    `additional_stages` exists to amortise a planner call — seven minutes and
    several dollars — over more than one stage, so the question that decides
    whether it earns its place is the *distribution* of batch sizes. Two lines
    reported this before: the stage about to run, and a count of the rest only
    when there were any. A batch of one was therefore indistinguishable from a
    batch that was never offered, and the run where the question was asked had
    to have its distribution recovered by a script — 17 derivations, 26 stages,
    8 singletons and 9 pairs against a cap of five.

    The absence was the finding. A log that speaks up only when the answer is
    greater than one cannot be read for how often the answer is one.
    """

    def _derive(self, repo, tmp_path, extras):
        planner = StubPlanner([
            PlannerOutcome(
                "next_stage", "first", "e",
                stage_fields=planned_stage(),
                additional_stage_fields=extras,
            )
        ])
        # `make`'s `planner` argument is the stub, and the config key of the
        # same name is the endpoint — so the cap is raised after the fact.
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.cfg.planner.max_batch_stages = 5
        seen: list[str] = []
        rt.log = seen.append
        nodes.plan(state, rt)
        return [line for line in seen if line.startswith("[plan] derived:")]

    def test_a_single_stage_derivation_still_names_what_it_produced(
        self, repo, tmp_path
    ):
        assert self._derive(repo, tmp_path, []) == ["[plan] derived: extract"]

    def test_a_batch_names_every_stage_in_order(self, repo, tmp_path):
        extra = {**planned_stage(), "id": "second", "edit_files": ["other.py"]}
        assert self._derive(repo, tmp_path, [extra]) == [
            "[plan] derived: extract, second"
        ]


class TestPlanDerivation:
    def test_a_new_stage_goes_to_precheck(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "first", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "precheck"
        assert out["current"]["id"] == "extract"

    def test_the_derivation_time_survives_the_stage_reset(self, repo, tmp_path):
        """The journey, not the endpoints — which is how this shipped broken.

        `plan_seconds` was measured correctly in this node and recorded
        correctly by `advance`, and read zero in production for four hours.
        Deriving a stage returns `**base` and then `**fresh_stage_fields()`,
        and the reset zeroed the value `base` had just set. The unit tests on
        both halves passed throughout.

        `fresh_stage_fields`' own docstring warns about exactly this, for
        `pending_plan_notes`, and says it cost two stages to find. It is the
        same trap one field along, so the same rule applies: cleared by
        `advance`, which is the node that ends the stage the time belongs to.
        """
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "first", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan({**state, "plan_seconds": 90.0}, rt)
        assert out["plan_seconds"] >= 90.0, "the reset must not swallow it"

    def test_landing_clears_it_for_the_next_stage(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        out = nodes.advance({**state, "plan_seconds": 90.0}, rt)
        assert out["completed"][-1]["plan_seconds"] == 90.0, "recorded on the stage"
        assert out["plan_seconds"] == 0.0, "and not carried to the next"

    def test_project_complete_goes_to_finalize(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.plan(state, rt)["next_hop"] == "finalize"

    def test_blocked_escalates(self, repo, tmp_path):
        planner = StubPlanner([PlannerOutcome("blocked", "plan contradicts itself", "e")])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "contradicts" in out["escalation_reason"]

    def test_an_invalid_spec_goes_back_to_the_planner(self, repo, tmp_path):
        # It used to escalate on the first occurrence, which the code's own
        # comment argued against — "a malformed spec is the planner's error to
        # fix". A malformed spec is the definition of a stage drawn wrongly,
        # which is the planner's tier, not a human's. It became urgent when
        # `validate_stage` started rejecting a fenced code block in the
        # instruction: that rule asks a model to break a strong habit, and one
        # slip stopping an unattended overnight run is a bad trade.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"})]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "plan"
        assert "edit_files" in json.dumps(out["last_failure"])

    def test_it_counts_against_the_no_landing_budget(self, repo, tmp_path):
        # What bounds the loop. Without this the run would redraw an invalid
        # stage forever, which is worse than the escalation it replaces.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"})]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["interventions_since_landing"] == 1

    def test_it_escalates_once_the_budget_is_gone(self, repo, tmp_path):
        # The guard that bounds it only fired while a stage was in flight, so
        # on the derive path — where a rejected spec leaves no stage — it was
        # unreachable and the loop had nothing stopping it.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"})]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state["interventions_since_landing"] = 3
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"

    def test_the_problems_reach_the_next_planner_call(self, repo, tmp_path):
        # The journey. A rejection recorded in state but not rendered into the
        # prompt would buy three identical redraws and then escalate anyway,
        # for three times the cost of escalating immediately.
        planner = StubPlanner(
            [
                PlannerOutcome("next_stage", "r", "e", stage_fields={"id": "x", "instruction": "i"}),
                PlannerOutcome("project_complete", "r", "e"),
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state.update(nodes.plan(state, rt))
        nodes.plan(state, rt)
        assert "edit_files" in _text_of(planner.calls[1])

    def test_operator_stage_defaults_are_applied(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(
            repo, tmp_path, planner=planner,
            stage_defaults={"checks": ["true"], "require_new_tests": True},
        )
        out = nodes.plan(state, rt)
        assert out["current"]["checks"] == ["true"]
        assert out["current"]["require_new_tests"] is True

    def test_writes_a_status_entry_every_invocation(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        nodes.plan(state, rt)
        assert "done" in rt.project.status.read_text()

    def test_records_planner_usage_on_the_run(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("project_complete", "r", "e", usage=PlannerUsage(500, 400, 90))]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["run_usage"]["planner_prompt_tokens"] == 500


class TestTheBranchWorkReachesThePlanner:
    """A value computed by git, carried through the node, into a paid prompt.

    The endpoints both worked in isolation: `Git.diff` returns the stage's
    cumulative diff and the prompt renders whatever it is handed. What was
    missing was the wire between them, and a revision written without it
    described the branch as baseline and was blocked for contradicting the
    diff the reviewer judged. That is the journey this pins, not either end.
    """

    def _plan_with_a_stage_in_flight(self, repo, tmp_path, run_git):
        planner = StubPlanner([PlannerOutcome("blocked", "r", "e")])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        with_stage(state, rt)
        (repo / "app.py").write_text("def hello():\n    return SENTINEL\n")
        run_git(repo, "commit", "-aqm", "the failed attempt")
        state["last_failure"] = {"layer": "review", "summary": "rework", "detail": "d"}
        nodes.plan(state, rt)
        return planner.calls[0]

    def test_the_stage_s_own_diff_reaches_the_prompt(self, repo, tmp_path, run_git):
        messages = self._plan_with_a_stage_in_flight(repo, tmp_path, run_git)
        assert "SENTINEL" in _text_of(messages)

    def test_a_derivation_with_no_stage_in_flight_carries_none(self, repo, tmp_path):
        planner = StubPlanner([PlannerOutcome("project_complete", "r", "e")])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        (repo / "app.py").write_text("def hello():\n    return SENTINEL\n")
        nodes.plan(state, rt)
        assert "SENTINEL" not in _text_of(planner.calls[0])


class TestAnUnresolvableExcerptReachesThePlanner:
    """The journey, not the endpoints — the rule this file exists for.

    `resolve_excerpts` raises and `_planner_failure` routes; both are covered
    on their own. What is not covered by either is that `execute` reads the
    range at the stage's start sha and turns a failure into a redraw rather
    than a traceback. With the instruction carrying no code, an excerpt that
    does not resolve is a stage that cannot be attempted, and the participant
    that can fix it is the one that chose the range.
    """

    def test_it_routes_to_the_planner_rather_than_raising(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        with_stage(state, rt, read_excerpts=[{"path": "gone.rb", "start": 1, "end": 5}])
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "plan"
        assert "gone.rb" in json.dumps(out.get("last_failure") or {})

    def test_a_range_is_read_at_the_stage_start_sha(self, repo, tmp_path, run_git):
        # The executor's own prior attempt moves lines. A range chosen against
        # the stage's start and read against the tree is silently the wrong
        # code — and the prompt is where that would land, so assert there.
        executor = StubExecutor(repo=repo, edits=[("app.py", "changed\n")])
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        with_stage(state, rt, read_excerpts=[{"path": "app.py", "start": 1, "end": 1}])
        (repo / "app.py").write_text("MOVED_BY_A_PRIOR_ATTEMPT\n")
        run_git(repo, "commit", "-aqm", "prior attempt")

        nodes.execute(state, rt)
        # The excerpt section only. The cumulative-diff section of the same
        # prompt carries the prior attempt on purpose, so asserting over the
        # whole prompt would fail for a correct reason.
        prompt = executor.prompts[-1]
        after = prompt.split("## Existing lines, quoted from the repository")[1]
        section = after.split("\n## ")[0]
        assert "def hello" in section  # the line as it stood when the stage began
        assert "MOVED_BY_A_PRIOR_ATTEMPT" not in section


class TestTheConventionsReachTheReviewer:
    """Runtime → node → prompt, for the two that never had it.

    Both ends worked already: `agent_context` reads the documents at the run's
    commit, and the prompt renders whatever it is handed. The wire between them
    is what was missing. The executor reaches the same documents as `--read`
    file arguments rather than through state, so its coverage is in the
    executor's own tests; this is the hop that crosses a schema boundary.
    """

    def _cfg_files(self, repo, run_git):
        (repo / "AGENTS.md").write_text("# How this repo works\n\nSCOPE_BY_TENANT\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "conventions")

    def test_only_the_planner_gets_the_operational_half(self, repo, tmp_path, run_git):
        """The split, enforced where it matters.

        The conventions half binds anything that writes or judges code. The
        operational half — build, test, deploy — is the planner's alone: it
        decides what is possible, and it is the participant that reads a plan
        item as blocked when it does not know the container reinstalls the
        bundle on a Gemfile edit. Handing the same pages to an executor that
        runs no commands is what invites it to narrate one it never ran.
        """
        (repo / "AGENTS.md").write_text("SCOPE_BY_TENANT\n")
        (repo / "OPERATIONS.md").write_text("RUN_BIN_RSPEC\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "split docs")
        executor = StubExecutor(repo=repo, edits=[("app.py", "changed\n")])
        cfg, rt, state = make(
            repo, tmp_path, executor=executor, operations_context=["OPERATIONS.md"]
        )
        state["plan_sha"] = rt.git.rev_parse("proj")
        with_stage(state, rt)

        nodes.execute(state, rt)
        assert "RUN_BIN_RSPEC" not in executor.prompts[-1]

        planner = rt.planner
        nodes.plan(state, rt)
        text = _text_of(planner.calls[-1])
        assert "SCOPE_BY_TENANT" in text
        assert "RUN_BIN_RSPEC" in text

    def test_the_reviewer_prompt_gets_them(self, repo, tmp_path, run_git):
        self._cfg_files(repo, run_git)
        reviewer = StubReviewer()
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state["plan_sha"] = rt.git.rev_parse("proj")
        with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        run_git(repo, "commit", "-aqm", "stage work")
        nodes.review(state, rt)
        assert "SCOPE_BY_TENANT" in _text_of(reviewer.messages[-1])


class TestTheExecutorsCostSurvivesTheJourney:
    """The loop's usage → ExecutionResult → state → the durable record.

    Four defects in this project have been values computed correctly and lost
    in transit, every one of them passing its unit tests on both ends. This is
    a new value crossing three boundaries, and the last of them writes a file
    that outlives the run and is fed back to the planner.
    """

    def test_it_accumulates_across_a_stage_s_attempts(self, repo, tmp_path):
        # Each attempt prices its own turns, so replacing rather than adding
        # would report a four-attempt stage at the price of one.
        class Priced(StubExecutor):
            def run_agent_stage(self, stage, prompt, history_dir=None,
                                since_sha="", agent_context=None, feedback=None,
                                failure_layer=None, model=""):
                self._apply()
                return ExecutionResult(ok=True, log="", cost_usd=0.05)

        executor = Priced(repo=repo, edits=[("app.py", "a\n"), ("app.py", "b\n")])
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        with_stage(state, rt)
        state.update(nodes.execute(state, rt))
        state.update(nodes.execute(state, rt))
        assert state["executor_cost_usd"] == 0.10

    def test_a_free_executor_records_nothing(self, repo, tmp_path):
        executor = StubExecutor(repo=repo, edits=[("app.py", "a\n")])
        executor.log = "Tokens: 12k sent, 1.1k received."
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert out.get("executor_cost_usd", 0.0) == 0.0


class TestPlannerBudgets:
    def test_exhausted_interventions_escalate(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, limits={"max_planner_interventions": 2})
        state = with_stage(state, rt)
        state["planner_interventions"] = 2
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "budget is exhausted" in out["escalation_reason"]

    def test_the_budget_is_global_not_per_stage(self, repo, tmp_path):
        # Per-stage caps let a pathological project consume unbounded paid
        # inference one stage at a time.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="restart")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        state["planner_interventions"] = 5
        out = nodes.plan(state, rt)
        assert out["planner_interventions"] == 6

    def test_max_stages_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, limits={"max_stages": 2})
        state["completed"] = [{"id": "a"}, {"id": "b"}]
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "max_stages" in out["escalation_reason"]

    def test_executor_history_lands_in_the_attempt_directory(self, repo, tmp_path):
        # Not in the repository under test: the executor's scratch files fail the
        # scope gate there, and they are worth keeping as artifacts anyway.
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "first", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        destination = rt.executor.history_dirs[-1]
        assert destination is not None
        assert repo not in destination.parents
        assert destination.is_dir()

    def test_planner_cached_tokens_are_recorded(self, repo, tmp_path):
        # The client extracts cache_read_input_tokens and it was being thrown
        # away, so the report could not show whether the planner's cacheable
        # prefix was working — which is the design's economic premise.
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "r", "e",
                    stage_fields=planned_stage(),
                    usage=PlannerUsage(12_000, 11_200, 300),
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        out = nodes.plan(state, rt)
        assert out["run_usage"]["planner_cached_tokens"] == 11_200

    def test_the_wall_clock_budget_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, limits={"wall_clock_hours": 2})
        state["session_started_at"] = time.time() - 3 * 3600
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "wall_clock_hours" in out["escalation_reason"]

    def test_inside_the_wall_clock_budget_proceeds(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "first", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(
            repo, tmp_path, planner=planner, limits={"wall_clock_hours": 8}
        )
        state["session_started_at"] = time.time() - 3600
        assert nodes.plan(state, rt)["next_hop"] == "precheck"

    def test_the_deadline_reports_the_underlying_failure(self, repo, tmp_path):
        # Escalating for time must not lose why the stage was being revised.
        cfg, rt, state = make(repo, tmp_path, limits={"wall_clock_hours": 1})
        state = with_stage(state, rt)
        state["session_started_at"] = time.time() - 2 * 3600
        state["last_failure"] = {"layer": "tests", "summary": "3 specs red"}
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "3 specs red" in out["escalation_reason"]

    def test_a_missing_session_start_falls_back_to_the_run_start(self, repo, tmp_path):
        # A checkpoint written before this field existed must not read as a
        # session that began at the epoch and blow the budget instantly.
        cfg, rt, state = make(repo, tmp_path, limits={"wall_clock_hours": 8})
        state.pop("session_started_at", None)
        state["started_at"] = time.time() - 60
        assert nodes.plan(state, rt)["next_hop"] == "finalize"

    def test_deriving_a_first_stage_costs_no_intervention(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("next_stage", "r", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        assert nodes.plan(state, rt)["planner_interventions"] == 0


class TestRevision:
    def test_revise_keeps_the_stage_identity(self, repo, tmp_path):
        # So the report reads "stage 14 revision 2" rather than pretending
        # stages 14-16 were different work.
        planner = StubPlanner(
            [PlannerOutcome("revise", "too narrow", "e",
                            stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert out["revision"] == 1
        assert out["current"]["id"] == "extract"
        # stage_index is untouched — a revision is the same stage, not the next.
        assert "stage_index" not in out

    def test_extend_keeps_the_branch(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert "stage_branch" not in out

    def test_extend_carries_the_suite_failures_onto_the_revised_stage(
        self, repo, tmp_path
    ):
        """The branch survives a revision; the record of what is red on it must.

        `extend` routes to `verify` rather than to the executor, and the only
        thing making that safe is that the gates read state — so if the gate is
        handed a narrower selection than the failure, it ratifies. The revised
        stage is rebuilt from the planner's fields and the planner may not
        write this one, so without carrying it the rebuild silently drops the
        evidence at exactly the moment the branch is kept.

        Observed live on `remove-non-admin-catch-all-retry` revision 1: ~30
        specs attributed to the stage, dropped by the rebuild, one path
        declared, verify green in 5.2s, approved, and a 228.5s full suite spent
        rediscovering it.
        """
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(),
                            revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        state["current"]["suite_failing_paths"] = ["spec/red_spec.rb"]
        out = nodes.plan(state, rt)
        assert out["current"]["suite_failing_paths"] == ["spec/red_spec.rb"]

    def test_restart_drops_the_suite_failures_with_the_branch(
        self, repo, tmp_path
    ):
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(),
                            revision_mode="restart")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        state["current"]["suite_failing_paths"] = ["spec/red_spec.rb"]
        out = nodes.plan(state, rt)
        assert out["current"]["suite_failing_paths"] == []

    def test_restart_discards_the_branch(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="restart")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert out["stage_branch"] is None
        assert out["stage_start_sha"] == ""

    def test_extend_re_enters_at_verify(self, repo, tmp_path):
        # `extend` means the approach was right and only the scope was too
        # narrow, so the work on the branch stands — that is the mode's whole
        # premise, and `_revert_unadopted` is careful to preserve it. Routing
        # onward to the executor denies it: the diff is already written, and the
        # instruction still describes it as undone. A stage once deleted the
        # line next to its target to produce a change that was already made.
        # Ask the gates whether it passes instead.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        assert nodes.plan(state, rt)["next_hop"] == "verify"

    def test_restart_re_enters_at_precheck(self, repo, tmp_path):
        # Nothing survives a restart, so there is nothing for the gates to read
        # and the branch has to be re-cut before the executor runs.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="restart")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        assert nodes.plan(state, rt)["next_hop"] == "precheck"

    def test_revision_clears_stale_feedback(self, repo, tmp_path):
        # It was written against an instruction that no longer applies.
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        state["review_feedback"] = ["old rejection"]
        assert nodes.plan(state, rt)["review_feedback"] == []


class TestScopeQuarantine:
    """A scope violation must not throw away hours of correct work."""

    def test_adopted_paths_keep_their_work(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "widening", "e",
                            stage_fields=planned_stage(edit_files=["app.py", "wandered.py"]),
                            revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("in scope work\n")
        (repo / "wandered.py").write_text("adopted work\n")
        state["last_failure"] = {
            "layer": "scope", "summary": "s", "detail": "d",
            "out_of_scope_paths": ["wandered.py"], "failing_paths": [],
        }
        nodes.plan(state, rt)
        assert (repo / "wandered.py").read_text() == "adopted work\n"
        assert (repo / "app.py").read_text() == "in scope work\n"

    def test_unadopted_paths_are_reverted_but_the_rest_survives(self, repo, tmp_path):
        # The whole point: one unexpected spec file must not cost the stage.
        planner = StubPlanner(
            [PlannerOutcome("revise", "declining", "e",
                            stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("hours of good work\n")
        (repo / "wandered.py").write_text("should go\n")
        state["last_failure"] = {
            "layer": "scope", "summary": "s", "detail": "d",
            "out_of_scope_paths": ["wandered.py"], "failing_paths": [],
        }
        nodes.plan(state, rt)
        assert not (repo / "wandered.py").exists()
        assert (repo / "app.py").read_text() == "hours of good work\n"

    def test_no_reverting_when_there_was_no_scope_violation(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "r", "e", stage_fields=planned_stage(), revision_mode="extend")]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        state["last_failure"] = {"layer": "tests", "summary": "s", "detail": "d"}
        nodes.plan(state, rt)
        assert (repo / "app.py").read_text() == "work\n"


class TestPrecheck:
    def test_cuts_the_child_branch_from_the_project_tip(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state["current"] = planned_stage()
        out = nodes.precheck(state, rt)
        assert out["stage_branch"] == "proj-stage/000-extract"
        assert out["next_hop"] == "execute"

    def test_does_not_recut_an_existing_branch(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        pinned = state["stage_start_sha"]
        out = nodes.precheck(state, rt)
        assert "stage_start_sha" not in out or out["stage_start_sha"] == pinned

    def test_a_stale_excerpt_says_why_in_the_log(self, repo, tmp_path, monkeypatch):
        """The reason has to reach the person watching, not only the planner.

        Observed live: a stage landed cleanly, the next one printed
        `[precheck] stage <id> revision 0` and then `[plan] revising <id>` one
        second later, and nothing said why. The cause — a batch-mate had edited
        a file this stage quoted — was in the checkpoint and in the planner's
        prompt, and nowhere a human would look. Every other gate names its
        failure in the log; this one routed silently, so a correct rejection
        read as something going wrong.
        """
        cfg, rt, state = make(repo, tmp_path)
        state["current"] = planned_stage()
        monkeypatch.setattr(nodes, "stale_excerpts", lambda *_: ["spec/a_spec.rb"])
        lines = []
        monkeypatch.setattr(rt, "log", lines.append)

        out = nodes.precheck(state, rt)

        assert out["next_hop"] == "plan"
        said = [x for x in lines if "spec/a_spec.rb" in x]
        assert said, f"no log line named the stale file: {lines}"
        assert "[precheck]" in said[0]

    def test_a_failed_precondition_goes_to_the_planner(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state["current"] = planned_stage(preconditions=["false"])
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["layer"] == "precondition"

    def test_the_precondition_failure_names_the_command(self, repo, tmp_path):
        # A mis-written precondition is unfixable by the planner, so the
        # escalation must eventually say which one never passed.
        cfg, rt, state = make(repo, tmp_path)
        state["current"] = planned_stage(preconditions=["test -f nope"])
        out = nodes.precheck(state, rt)
        assert "test -f nope" in out["last_failure"]["summary"]

    def test_failed_setup_escalates_to_a_human(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, setup_command="false")
        state["current"] = planned_stage()
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "setup"


class TestExecute:
    def test_a_successful_attempt_goes_to_verify(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        assert nodes.execute(state, rt)["next_hop"] == "verify"

    def _prior_work(self, repo):
        """A commit on the stage branch, so `cumulative_diff` is non-empty."""
        (repo / "app.py").write_text("prior\n")
        for args in (["add", "-A"], ["commit", "-qm", "prior attempt"]):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    def test_a_rework_that_changes_nothing_records_what_it_said(
        self, repo, tmp_path
    ):
        """`result.log` is otherwise read by nothing on this path.

        Observed on `remove-non-admin-catch-all-retry` attempt 1: 73 tool calls,
        zero edits, and a closing paragraph naming the cause precisely — the
        files it needed were outside its permitted list. Its only consumers are
        the timeout and turns-exhausted branches, so it went to `executor.log`
        and nowhere else. Twenty minutes, a reviewer call, a 246s suite and a
        106s baseline later the planner re-derived it unaided.
        """
        ex = StubExecutor(repo=repo, log="those files are outside my list")
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        self._prior_work(repo)
        out = nodes.execute(state, rt)
        # Routing is unchanged: the gates decide, not the model's own stop.
        assert out["next_hop"] == "verify"
        assert "outside my list" in out["executor_note"]

    def test_a_first_attempt_with_no_edits_is_left_to_the_scope_gate(
        self, repo, tmp_path
    ):
        # Nothing on the branch to have left alone, so "the attempt produced no
        # changes" is the true sentence and it belongs to one place.
        ex = StubExecutor(repo=repo, log="I did nothing")
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert out["next_hop"] == "verify"
        assert "executor_note" not in out

    def test_a_repeat_abort_is_reported_even_on_a_first_attempt(
        self, repo, tmp_path
    ):
        """The one stop the scope gate's sentence describes wrongly.

        `cumulative_diff` is the right question for a *voluntary* stop: with
        nothing on the branch to have left alone, "the attempt produced no
        changes" is true and belongs to one place. A model stopped for asking
        the same question ten times has not decided anything, and that sentence
        sends the planner to redraw a stage that was never the problem —
        measured on one run, three attempts ended exactly this way, one of them
        a first attempt with 590 calls and a single edit.
        """
        ex = StubExecutor(
            repo=repo,
            log="the model called `read_file` 10 times in a row",
            unproductive_stop="the model called `read_file` 10 times in a row",
        )
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        # Routing is still the gates'. What changes is what they are told.
        assert out["next_hop"] == "verify"
        assert "10 times in a row" in out["executor_note"]

    def test_an_attempt_that_edited_does_not_record_one(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, edits=[("app.py", "new\n")], edits_applied=3,
                          log="done")
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        self._prior_work(repo)
        assert "executor_note" not in nodes.execute(state, rt)


class TestTheExecutorsAccountReachesTheNextFailure:
    """It rides the next `FailureDetail`, and both destinations get it.

    Not `opening_failure`: that is write-once, and the full-suite failure which
    *caused* the rework claims it one node earlier in `_rework_or_plan`. The
    first implementation put it there and was dead code by construction — it
    could never fire in the case it was written for.

    Not `review_feedback` either: both handoffs compose their detail from
    `feedback[-2:]`, so a third kind of entry evicts the failure that actually
    ended the stage. On the observed sequence the note would have been pushed
    out by the reviewer rework that followed it.
    """

    def _state(self):
        return {
            "executor_note": "those files are outside my list",
            "verify_attempt": 0,
            "rework_attempt": 0,
            "stage_start_sha": "abc",
            "review_feedback": ["the suite went red"],
        }

    def test_it_reaches_the_planner(self, repo, tmp_path):
        cfg, rt, _ = make(repo, tmp_path)
        out = nodes._planner_failure(self._state(), "tests", "red", "one spec")
        assert "outside my list" in out["last_failure"]["detail"]
        assert "one spec" in out["last_failure"]["detail"]
        assert out["executor_note"] is None

    def test_it_reaches_another_executor_attempt(self, repo, tmp_path):
        cfg, rt, _ = make(repo, tmp_path)
        out = nodes._retry_or_plan(
            self._state(), rt, "tests", "red", "feedback", "one spec"
        )
        assert out["next_hop"] == "execute"
        assert "outside my list" in out["opening_failure"]["detail"]
        assert out["executor_note"] is None

    def test_it_reaches_a_rework(self, repo, tmp_path):
        cfg, rt, _ = make(repo, tmp_path)
        out = nodes._rework_or_plan(
            self._state(), rt, ["the reviewer said no"], "blocked"
        )
        assert out["next_hop"] == "execute"
        assert "outside my list" in out["opening_failure"]["detail"]
        assert out["executor_note"] is None

    def test_nothing_recorded_changes_nothing(self, repo, tmp_path):
        cfg, rt, _ = make(repo, tmp_path)
        state = self._state() | {"executor_note": None}
        out = nodes._planner_failure(state, "tests", "red", "one spec")
        assert out["last_failure"]["detail"] == "one spec"
        assert "executor_note" not in out

    def test_it_is_cleared_so_a_later_failure_does_not_reuse_it(
        self, repo, tmp_path
    ):
        # It describes one attempt. Carried twice it would be attributed to
        # work it never saw.
        cfg, rt, _ = make(repo, tmp_path)
        state = self._state()
        state.update(nodes._planner_failure(state, "tests", "red", "one spec"))
        again = nodes._planner_failure(state, "tests", "red", "another spec")
        assert "outside my list" not in again["last_failure"]["detail"]

    def test_writes_the_prompt_under_a_revision_scoped_path(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state["revision"] = 2
        nodes.execute(state, rt)
        assert (rt.paths.attempt_dir(0, "extract", 2, 0) / "prompt.md").exists()

    def test_feedback_reaches_the_executor_beside_the_prompt(self, repo, tmp_path):
        # Beside, not inside. `build_executor_prompt` puts a retry opening at
        # the head of the string, so folding feedback in would make an attempt
        # with it differ from one without at character zero — and that string
        # is the cached prefix. It arrives as its own conversation turn.
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state["review_feedback"] = ["Wrong verb on the route."]
        nodes.execute(state, rt)
        assert "Wrong verb on the route." in "\n".join(ex.feedback[0] or [])
        assert "Wrong verb on the route." not in ex.prompts[0]

    def test_a_review_rejection_asks_for_a_replacement_not_an_addition(
        self, repo, tmp_path
    ):
        # This said "do not repeat the rejected approach", which made sense when
        # `rework_reset` was on and the branch went back to the baseline first.
        # With the work now left in place and shown to the executor, that told
        # it to discard the very diff it was handed as the thing to amend — and
        # the observed result was a reviewer rejecting a rework for leaving the
        # original assertion in place and adding the new form beside it.
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state["review_feedback"] = ["Wrong verb on the route."]
        state["failure_layer"] = "review"
        nodes.execute(state, rt)
        # The node's job is to say *which* failure this was; choosing the
        # wording is `build_executor_messages`', and is pinned there. Split
        # because the two get it wrong in different ways: this one by losing
        # the layer, that one by framing a gate failure as a rejection.
        assert ex.failure_layers[0] == "review"
        assert "Wrong verb on the route." in "\n".join(ex.feedback[0] or [])

    def test_a_gate_failure_does_not_call_the_work_rejected(self, repo, tmp_path):
        # Measured over one run of 35 stages: this opening fired about a dozen
        # times and was wrong every one of them, because the reviewer rejected
        # nothing all run. Seven of those were `residue`, where the work is
        # incomplete rather than wrong and repeating the approach on the sites
        # that were missed is exactly the fix — so "do not repeat the rejected
        # approach" sat fifty lines above feedback saying the opposite.
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state["review_feedback"] = ["Two occurrences were never edited."]
        state["failure_layer"] = "residue"
        nodes.execute(state, rt)
        opening = "\n".join(ex.feedback[0] or [])
        assert "rejected" not in opening
        assert "Two occurrences were never edited." in opening

    def test_a_retry_is_shown_what_the_stage_has_changed_so_far(
        self, repo, tmp_path
    ):
        # Editing forward is only reasonable if the executor can see what it is
        # editing forward from. Reviewers leave comments on work; they do not
        # ask for the work again, and an author who cannot see their own diff
        # is not in a position to amend it.
        ex = StubExecutor(repo=repo, edits=[("app.py", "second pass\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work from the first attempt\n")
        state["review_feedback"] = ["The comment contradicts the code."]
        state["failure_layer"] = "review"
        nodes.execute(state, rt)
        assert "work from the first attempt" in ex.prompts[0]

    def test_a_first_attempt_is_shown_no_diff(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        assert "changed so far" not in ex.prompts[0]

    def test_a_first_attempt_has_no_opening_at_all(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, edits=[("app.py", "x\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        assert "previous attempt" not in ex.prompts[0]

    def test_executor_failure_retries_then_goes_to_the_planner(self, repo, tmp_path):
        ex = StubExecutor(repo=repo, ok=False)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        assert nodes.execute(state, rt)["next_hop"] == "execute"
        state["verify_attempt"] = 3
        assert nodes.execute(state, rt)["next_hop"] == "plan"


class TestVerifyRouting:
    def test_pass_goes_to_review(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "review"

    def test_scope_violation_goes_to_the_planner_with_the_paths(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "wandered.py").write_text("x\n")
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["out_of_scope_paths"] == ["wandered.py"]

    def test_branch_identity_failure_escalates_to_a_human(self, repo, tmp_path):
        # A containment breach does not negotiate.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("x\n")
        rt.git.commit_all("work")
        rt.git.checkout("proj")
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "branch"

    def test_failing_tests_retry_then_go_to_the_planner(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, full_test_command="exit 1")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "execute"
        state["verify_attempt"] = 3
        out = nodes.verify(state, rt)
        assert out["next_hop"] == "plan"
        assert "max_test_retries" in out["last_failure"]["summary"]

    def test_skips_review_when_the_stage_opts_out(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt, review=False)
        (repo / "app.py").write_text("changed\n")
        assert nodes.verify(state, rt)["next_hop"] == "advance"


class TestReviewGate:
    """Reviewer approval AND a green full suite, cheapest first."""

    def test_approval_plus_green_suite_advances(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        assert nodes.review(state, rt)["next_hop"] == "advance"

    def test_a_rejected_stage_never_pays_for_the_full_suite(self, repo, tmp_path):
        # The economic point: the reviewer is seconds and pennies, the suite is
        # minutes.
        marker = tmp_path / "suite-ran"
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(
            repo, tmp_path, reviewer=reviewer,
            full_test_command=f"touch {marker}",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        assert not marker.exists()

    def test_approved_but_red_suite_does_not_advance(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, full_test_command="exit 1")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "execute"

    def test_verify_hands_the_gate_what_it_needs_to_skip(self, repo, tmp_path):
        # End to end, because the isolated version of this test passed while the
        # feature did nothing: verify wrote `full_suite_digest`, the graph's
        # state schema did not declare it, the key was dropped in transit, and
        # the gate never saw it. Seeding the key by hand tests the gate's logic
        # and not the wiring, so this drives the real node and asserts on what
        # verify actually returns.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=f"touch {marker}",
            scoped_test_command=None,
        )
        state = with_stage(state, rt, test_paths=[])
        (repo / "app.py").write_text("changed\n")

        out = nodes.verify(state, rt)
        assert out["next_hop"] == "review"
        assert out["full_suite_digest"], (
            "verify ran the full suite and must fingerprint the tree it passed on"
        )
        from code_gantry.state import RunState

        assert "full_suite_digest" in RunState.__annotations__, (
            "the key must be declared in the state schema or the graph drops it"
        )

        marker.unlink()
        out = nodes.review({**state, **out}, rt)
        assert out["next_hop"] == "advance"
        assert not marker.exists(), "the gate re-ran a suite verify had just run"


class TestPlanDocumentsFollowTheProjectBranch:
    """Plan maintenance happens on the branch, and must not need a merge first.

    A migration branch lives for months. Its plan documents get restructured,
    folded and corrected on that branch the whole time, and `main` sees none of
    it until the end. Reading plan documents at `base_ref` meant the run could
    not see the plan it was executing.
    """

    def test_the_conventions_document_is_still_protected(
        self, repo, tmp_path, run_git
    ):
        (repo / "AGENTS.md").write_text("# Conventions\n\nrule one\n")
        Git(repo).commit_all("conventions, on main")

        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt, edit_files=["*.md"])
        (repo / "AGENTS.md").write_text("# Conventions\n\nrewritten\n")

        out = nodes.verify(state, rt)
        assert out["failure_layer"] == "scope"

        from code_gantry.state import RunState

        assert "plan_sha" in RunState.__annotations__, (
            "the key must be declared in the state schema or the graph drops it"
        )

    def test_a_suite_already_green_at_verify_is_not_run_again(self, repo, tmp_path):
        # Nothing mutates the tree between verify and this gate — the reviewer
        # reads a diff, it does not edit — so re-running the identical suite on
        # an identical tree buys no information. Measured on the first real
        # project: three stages, 584 seconds, and because the second run
        # re-rolls every order-dependent example it also produced two of the
        # night's flakes at the gate, where a flake is most expensive.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        state = {**state, "full_suite_digest": _digest(rt, state)}
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert not marker.exists()

    def test_a_changed_tree_still_pays_for_the_suite(self, repo, tmp_path):
        # The skip is keyed to what was actually tested. If anything moved, the
        # recorded pass proves nothing about the tree being merged.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        state = {**state, "full_suite_digest": "stale" * 8}
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert marker.exists()

    def test_a_scoped_verify_leaves_the_gate_to_do_its_job(self, repo, tmp_path):
        # The common case. Verify ran only the stage's own specs, so the full
        # suite has never been run on this tree and the gate is the only thing
        # standing between a scoped pass and a merge.
        marker = tmp_path / "suite-ran"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert marker.exists()

    def test_a_flaky_full_suite_does_not_block_a_good_stage(self, repo, tmp_path):
        # The full suite has far more surface for ordering flakes, and a flake
        # here wastes a planner intervention rather than an executor attempt.
        flag = tmp_path / "suite-flag"
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=f"if [ -f {flag} ]; then exit 0; else touch {flag}; exit 1; fi",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert out["flake_reruns_review_gate"] == 1

    def test_only_the_failing_files_are_re_run_at_the_gate(self, repo, tmp_path):
        # The whole point: on a 2,335-example suite the re-run was as likely to
        # trip over a different order-dependent example as to clear the first
        # one, so the stage was blamed for a property of the repository.
        log = tmp_path / "gate-reran.txt"
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=(
                "echo \"Failed examples:\"; "
                "echo \"rspec './spec/requests/checkout_spec.rb[1:1]' # c\"; exit 1"
            ),
            scoped_test_command=(
                runner_script(log.parent, f'echo "$@" >> {log}', "log_runner")
                + " {paths}"
            ),
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert out["flake_reruns_review_gate"] == 1
        assert log.read_text().strip() == "spec/requests/checkout_spec.rb"

    def test_the_flaky_files_are_named_for_the_report(self, repo, tmp_path):
        # Excusing a flake and not saying which file leaves the operator with a
        # count and nothing to fix. The list is the whole path back to a suite
        # that does not need this machinery.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=(
                "echo \"Failed examples:\"; "
                "echo \"rspec './spec/requests/checkout_spec.rb[1:1]' # c\"; exit 1"
            ),
            scoped_test_command="true {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["flaky_files"] == ["spec/requests/checkout_spec.rb"]

    def test_a_spec_the_stage_edited_can_still_be_excused(self, repo, tmp_path):
        """Ownership is deliberately not a factor.

        An earlier design refused to excuse a spec the stage had touched. But a
        file that passes whole and standalone has been proven green *including*
        the stage's edits to it, so what makes it fail in the group is a
        property of the suite — separate work, not this stage's to answer for.
        """
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=(
                "echo \"Failed examples:\"; "
                "echo \"rspec './spec/app_spec.rb[1:1]' # c\"; exit 1"
            ),
            scoped_test_command="true {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt, edit_files=["app.py", "spec/**"])
        (repo / "app.py").write_text("changed\n")
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "app_spec.rb").write_text("describe\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "advance"
        assert out["flaky_files"] == ["spec/app_spec.rb"]

    def test_a_signalled_full_suite_stops_for_a_human(self, repo, tmp_path):
        # At the merge gate the stage has already been approved and its diff is
        # correct. Reading a killed suite as a rejection would send correct work
        # back for rework, and the rework would run against the same dead
        # environment.
        cfg, rt, state = make(repo, tmp_path, full_test_command="kill -9 $$")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "escalate"
        assert "signal" in out["escalation_reason"].lower()

    def test_the_full_suite_can_be_switched_off_per_stage(self, repo, tmp_path):
        marker = tmp_path / "suite-ran-2"
        cfg, rt, state = make(repo, tmp_path, full_test_command=f"touch {marker}")
        state = with_stage(state, rt, full_suite_on_approval=False)
        (repo / "app.py").write_text("changed\n")
        assert nodes.review(state, rt)["next_hop"] == "advance"
        assert not marker.exists()

    def test_blocked_goes_to_the_planner_not_a_human(self, repo, tmp_path):
        reviewer = StubReviewer([ReviewOutcome(verdict="blocked", summary="instruction wrong")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["layer"] == "review"

    def test_rework_loops_with_feedback(self, repo, tmp_path):
        reviewer = StubReviewer(
            [ReviewOutcome(
                verdict="rework", summary="One problem.",
                issues=[Issue(severity="major", file="app.py", description="Fix it.")],
            )]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["next_hop"] == "execute"
        assert "Fix it." in out["review_feedback"][0]

    def test_exhausted_rework_goes_to_the_planner(self, repo, tmp_path):
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="still wrong")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        state["rework_attempt"] = 2
        out = nodes.review(state, rt)
        assert out["next_hop"] == "plan"
        assert "max_rework_retries" in out["last_failure"]["summary"]

    def test_rework_keeps_the_work_by_default(self, repo, tmp_path):
        # A reviewer leaves comments on the work in front of it; it does not
        # ask for the work again. Discarding a rejected attempt was the default
        # until a rejection arrived saying the behaviour and scope were correct
        # and only an explanatory comment was wrong — resetting rebuilt a
        # correct spec from nothing to change one sentence.
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("rejected\n")
        nodes.review(state, rt)
        assert (repo / "app.py").read_text() == "rejected\n"

    def test_rework_still_resets_when_the_operator_asks(self, repo, tmp_path):
        reviewer = StubReviewer([ReviewOutcome(verdict="rework", summary="no")])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer, rework_reset=True)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("rejected\n")
        nodes.review(state, rt)
        assert (repo / "app.py").read_text() == "def hello():\n    return 1\n"

    def test_accumulates_reviewer_usage(self, repo, tmp_path):
        reviewer = StubReviewer(
            [ReviewOutcome(verdict="approved", summary="ok", usage=TokenUsage(100, 20, 80))]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.review(state, rt)
        assert out["stage_usage"]["prompt_tokens"] == 100
        assert out["stage_usage"]["cached_tokens"] == 80


class TestAdvance:
    def test_squash_merges_to_the_project_branch(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        out = nodes.advance(state, rt)
        assert rt.git.current_branch() == "proj"
        assert "[extract]" in rt.git.commit_subject()
        assert out["completed"][0]["merge_sha"]

    def test_what_it_lands_carries_no_trailing_whitespace(self, repo, tmp_path):
        # End to end because this is where it mattered: the strip is correct in
        # gitops and the commit is correct in gitops, and the run still died
        # because nothing called one before the other. A pre-commit hook
        # rejecting `git diff --cached --check` turned a landed stage into a
        # crash with a staged merge stranded on the project branch.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_bytes(b"kept   \nwork\t\n")
        nodes.advance(state, rt)
        assert rt.git.show_file("proj", "app.py") == "kept\nwork\n"

    def test_deletes_the_child_branch(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        nodes.advance(state, rt)
        assert not rt.git.branch_exists("proj-stage/000-extract")

    def test_lands_exactly_one_commit(self, repo, tmp_path):
        # The executor commits before it tests, so the child branch has red commits.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        before = rt.git.rev_parse("proj")
        for i in range(3):
            (repo / "app.py").write_text(f"attempt {i}\n")
            rt.git.commit_all(f"intermediate {i}")
        nodes.advance(state, rt)
        assert rt.git.rev_parse("proj~1") == before

    def test_records_history_and_returns_to_plan(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        state["revision"] = 1
        state["verify_attempt"] = 2
        out = nodes.advance(state, rt)
        entry = out["completed"][0]
        assert entry["revisions"] == 1
        assert entry["verify_retries"] == 2
        assert out["next_hop"] == "plan"
        assert out["current"] is None
        assert out["stage_index"] == 1

    def test_clears_per_stage_state(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("work\n")
        state["review_feedback"] = ["old"]
        out = nodes.advance(state, rt)
        assert out["review_feedback"] == []
        assert out["stage_branch"] is None


class TestFinalize:
    """It no longer runs the suite. On the path that reaches here the tree is
    the one the last landing's review gate already ran it on, so a second run
    could only resample the suite's own nondeterminism — and did it without the
    flake adjudication the review gate applies to the same command."""

    def test_the_suite_is_not_run_again(self, repo, tmp_path):
        # The whole change, asserted where it can fail: a command that would
        # fail loudly if anything still invoked it.
        cfg, rt, state = make(repo, tmp_path, full_test_command="exit 1")
        state["completed"] = [{"merge_sha": rt.git.head_sha()}]
        assert nodes.finalize(state, rt)["status"] == "complete"

    def test_an_approved_tip_completes(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state["completed"] = [{"merge_sha": rt.git.head_sha()}]
        out = nodes.finalize(state, rt)
        assert out["status"] == "complete"
        assert out["next_hop"] == "end"

    def test_a_branch_moved_under_us_escalates(self, repo, tmp_path):
        # What the suite could not have answered: it would have passed on the
        # new commit, because a suite reads the tree and not the history.
        cfg, rt, state = make(repo, tmp_path)
        state["completed"] = [{"merge_sha": "0" * 40}]
        out = nodes.finalize(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "branch_moved"
        assert rt.git.head_sha() in out["escalation_reason"]

    def test_a_dirty_tree_escalates_and_names_the_files(self, repo, tmp_path):
        # `pin_modules` pins our code for the length of a run and says nothing
        # about the target repository, so a human editing it mid-run is live.
        cfg, rt, state = make(repo, tmp_path)
        state["completed"] = [{"merge_sha": rt.git.head_sha()}]
        (repo / "app.py").write_text("edited by a human mid-run\n")
        out = nodes.finalize(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "tree_dirty"
        assert "app.py" in out["escalation_reason"]

    def test_a_run_that_landed_nothing_has_nothing_to_compare(self, repo, tmp_path):
        # The tip belongs to whatever ran before this session, so there is no
        # approved commit and no claim to make about it.
        cfg, rt, state = make(repo, tmp_path, full_test_command="exit 1")
        state["completed"] = []
        assert nodes.finalize(state, rt)["status"] == "complete"


class TestEscalate:
    def test_records_the_failed_stage(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state["escalation_reason"] = "because"
        out = nodes.escalate(state, rt)
        assert out["status"] == "escalated"
        assert out["failed_stage_id"] == "extract"

    def test_does_not_append_to_completed_history(self, repo, tmp_path):
        # completed holds completions only; a stage that escalates, gets fixed,
        # and lands must appear once.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        assert "completed" not in nodes.escalate(state, rt)


class TestOperatorPause:
    """Stopping cleanly, on request, without killing work in flight.

    Interrupting the process leaves a half-finished executor and a dirty tree.
    Checked at the same point as the budgets — before the next planner call —
    so the run stops between stages with everything landed and nothing pending.
    """

    def test_a_pause_flag_stops_the_run(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        rt.paths.pause_flag.write_text("paused by operator\n")
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "paused"

    def test_the_reason_says_nothing_is_wrong(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        rt.paths.pause_flag.write_text("")
        reason = nodes.plan(state, rt)["escalation_reason"]
        assert "resume" in reason.lower()
        assert "paused" in reason.lower()

    def test_no_flag_means_no_pause(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        assert nodes.plan(state, rt)["next_hop"] == "finalize"

    def test_the_planner_is_not_called_when_paused(self, repo, tmp_path):
        # The point is to stop before spending, not after.
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.paths.pause_flag.write_text("")
        nodes.plan(state, rt)
        assert planner.calls == []

    def test_a_red_full_suite_is_kept_on_disk(self, repo, tmp_path):
        """The most expensive failure in the loop, and nobody kept its output.

        A rejection here resets reviewer-approved work, so it is the one
        failure that most needs explaining afterwards. It was also the only
        command whose output went nowhere — when the flake gate silently
        stopped matching, the sole copy of the evidence was a file the next run
        truncated.
        """
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command="echo 'DIAGNOSTIC MARKER'; exit 1",
            scoped_test_command="true {paths}",
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        logs = list(rt.paths.run_dir.glob("stages/*/full-suite.log"))
        assert logs, "the failing full suite should leave an artifact"
        assert "DIAGNOSTIC MARKER" in logs[0].read_text()

    def test_a_green_full_suite_writes_nothing(self, repo, tmp_path):
        # Nothing to diagnose, and the output is megabytes.
        cfg, rt, state = make(repo, tmp_path, full_test_command="echo fine")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        assert not list(rt.paths.run_dir.glob("stages/*/full-suite.log"))


class TestProgressGuardAfterAFlakyMergeGate:
    """End to end: does `full_suite` actually reach the progress guard?

    The exemption was added and unit-tested against `run_verify` directly,
    which proves the guard honours the flag but not that anything ever sets it.
    This drives the real sequence — approved, full suite red, rework, identical
    diff — through the nodes, because that is where it silently would not work.
    """

    def _green_scoped_red_full(self, repo, tmp_path):
        """Scoped tests pass, the whole suite does not — the real shape.

        This used to lean on `test_command` and `full_test_command` holding
        different strings: nothing scoped, so the gate fell back to a passing
        `test_command` while the merge gate ran a failing `full_test_command`.
        With one everything-command that fixture cannot exist, and it was
        never the situation being tested. The stage now declares a spec, so
        the gate runs the scoped command and the merge gate runs the suite —
        which is what "approved, then red for reasons elsewhere" actually is.
        """
        (repo / "spec").mkdir(exist_ok=True)
        (repo / "spec" / "a_spec.rb").write_text("x\n")
        Git(repo).commit_all("a spec")
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command="exit 1",
            scoped_test_command="true {paths}",
        )
        state = with_stage(state, rt, test_paths=["spec/a_spec.rb"])
        (repo / "app.py").write_text("changed\n")
        return cfg, rt, state

    def test_a_merge_gate_failure_is_recorded_as_full_suite(self, repo, tmp_path):
        cfg, rt, state = self._green_scoped_red_full(repo, tmp_path)
        out = nodes.review(state, rt)
        assert out["failure_layer"] == "full_suite"

    def test_the_identical_redo_then_survives_verify(self, repo, tmp_path):
        # The whole point: the reviewer approved this diff, the suite was red
        # for reasons elsewhere, and doing it again is the correct answer.
        cfg, rt, state = self._green_scoped_red_full(repo, tmp_path)

        first = nodes.verify(state, rt)
        state = {**state, **first}
        rejected = nodes.review(state, rt)
        state = {**state, **rejected}
        assert state["failure_layer"] == "full_suite"

        # rework_reset threw the work away; the executor reproduces it exactly.
        (repo / "app.py").write_text("changed\n")
        again = nodes.verify(state, rt)
        assert again.get("failure_layer") != "progress"
        assert again["next_hop"] in ("review", "advance")


class TestAFailureThatPredatesTheStage:
    """Whose failure is it?

    `adjudicate` answers "flake or real", and the gate treated every real
    failure as the stage's. It is not. Observed live: a stage renamed four
    macros in one controller, was approved by the reviewer three times with a
    byte-identical diff, and was sent back three times because a
    commission-report spec in a file it never touched started failing when the
    clock crossed into the 31st. The executor was scoped to that controller and
    could not have fixed the spec under any instruction, so the whole rework
    budget bought nothing. Sixteen minutes, three executor runs, three reviews,
    three full suites.

    One scoped run against the base tree separates the two cases.
    """

    FAILED = (
        "echo 'Failed examples:'; "
        "echo \"rspec './spec/a_spec.rb[1:1]' # boom\"; exit 1"
    )

    def _land(self, run_git, repo, text="changed\n", extra=None):
        """Commit the stage's work, as the executor's own commit would."""
        (repo / "app.py").write_text(text)
        if extra:
            (repo / extra).write_text("x\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "stage work")

    def test_it_goes_to_the_planner_rather_than_the_executor(
        self, repo, tmp_path, run_git
    ):
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        self._land(run_git, repo)
        out = nodes.review(state, rt)

        assert out["next_hop"] == "plan", (
            "the executor is scoped to its own files and cannot repair a spec "
            "that was already red; reworking it spends attempts to learn nothing"
        )
        assert out["failure_layer"] == "full_suite"
        assert out["last_failure"]["failing_paths"] == ["spec/a_spec.rb"]
        assert "predates" in out["last_failure"]["detail"]

    def test_a_failure_the_stage_caused_still_goes_to_rework(
        self, repo, tmp_path, run_git
    ):
        # The guard has to stay narrow. Green at the base means the stage did
        # break it, and that is exactly what rework is for.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            # Green at the base, red at the tip: the stage committed the file.
            scoped_test_command=(
                runner_script(repo.parent, "test ! -f broke.txt", "broke_runner")
                + " {paths}"
            ),
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        self._land(run_git, repo, extra="broke.txt")
        out = nodes.review(state, rt)

        assert out["next_hop"] == "execute"
        assert out["failure_layer"] == "full_suite"

    def test_the_tree_is_left_where_it_was_found(self, repo, tmp_path, run_git):
        # The baseline run reverts to the base and back. Getting the second
        # half wrong would silently delete an approved stage's work.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
        )
        state = with_stage(state, rt)
        self._land(run_git, repo, text="the approved work\n")
        head = rt.git.head_sha()

        nodes.review(state, rt)

        assert (repo / "app.py").read_text() == "the approved work\n"
        assert rt.git.head_sha() == head

    def test_setup_runs_on_both_sides_of_the_revert(self, repo, tmp_path, run_git):
        """The environment follows the tree, or the answer is about the wrong one.

        Reverting to the base with containers still built for the tip tests the
        base tree against the stage's environment — and it fails in the
        direction that produces a false "pre-existing", which routes correct
        work to the planner. The restoring run is not optional either: leaving
        the environment at the base is the same mistake pointed the other way.
        """
        ran = tmp_path / "setup-ran"
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
            setup_command=f"echo up >> {ran}",
        )
        state = with_stage(state, rt)
        self._land(run_git, repo)
        out = nodes.review(state, rt)

        assert out["next_hop"] == "plan"
        assert ran.read_text().count("up") == 2, (
            "once at the base before running it, once after restoring the tree"
        )

    def test_a_base_that_will_not_build_answers_nothing(self, repo, tmp_path, run_git):
        # "Setup failed at the base" is not evidence that the specs were
        # already red. Blaming the tree for it would route a stage that may
        # well be at fault to the planner instead of to rework.
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
            setup_command="exit 3",
        )
        state = with_stage(state, rt)
        self._land(run_git, repo)
        out = nodes.review(state, rt)

        assert out["next_hop"] == "execute"

    def test_an_uncommitted_tree_is_never_reset(self, repo, tmp_path):
        """Restoring by sha restores what is committed.

        So on a dirty tree the check declines to run rather than destroying
        work the caller may be about to rework *forward* from. The executor
        commits, so this is the unusual case — but it is the one where being
        wrong loses work rather than time.
        """
        cfg, rt, state = make(
            repo, tmp_path,
            full_test_command=self.FAILED,
            scoped_test_command="false {paths}",
            failed_file_pattern=RSPEC_PATTERN,
            rework_reset=False,
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("uncommitted\n")
        out = nodes.review(state, rt)

        assert (repo / "app.py").read_text() == "uncommitted\n"
        assert out["next_hop"] == "execute"


class TestPlannerArtifactRecordsCacheWrites:
    """The write premium has to be visible per call, not just collected.

    `PlannerUsage` gained `cache_write_tokens` when Anthropic's orthogonal counts
    were normalised, but `planner.json` still recorded only prompt, cached and
    completion. Anthropic bills a cache write above base rate, so a run that
    writes the prefix every call and never reads it is more expensive than not
    caching — and per-call is the only place that is diagnosable, since the
    run totals average it away.
    """

    def test_the_artifact_carries_the_write_count(self, repo, tmp_path):
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "project_complete", "done", "e",
                    usage=PlannerUsage(
                        prompt_tokens=5_000,
                        cached_tokens=1_000,
                        completion_tokens=400,
                        cache_write_tokens=3_600,
                    ),
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        import json

        written = json.loads(
            next(rt.paths.run_dir.glob("stages/*/planner.json")).read_text()
        )
        assert written["usage"]["cache_write_tokens"] == 3_600


class TestPlannerArtifactRecordsWhatItLookedAtAndSaid:
    """Silence has to be distinguishable from absence.

    `planner.json` recorded the verdict and the stage but neither the reads nor
    the plan notes, so a stage that produced no observations looked exactly
    like a feature that never ran — the key was simply missing either way.
    That matters most for the case worth auditing: the planner read the
    repository, compared it to the plan, and found nothing to correct. That is
    the plan being accurate, and it is evidence, not an empty field.
    """

    def _artifact(self, rt):
        import json

        return json.loads(
            next(rt.paths.run_dir.glob("stages/*/planner.json")).read_text()
        )

    def test_reads_and_notes_are_recorded(self, repo, tmp_path):
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "project_complete", "done", "e",
                    tool_calls=["search(render text: in app) -> 7 line(s)"],
                    plan_notes=[
                        {
                            "plan_step": "item 17",
                            "observation": "7 sites remain, not 24",
                            "supersedes": "checklist says 24",
                        }
                    ],
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        written = self._artifact(rt)
        assert written["tool_calls"] == ["search(render text: in app) -> 7 line(s)"]
        assert written["plan_notes"][0]["observation"] == "7 sites remain, not 24"

    def test_both_keys_exist_when_there_was_nothing_to_say(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        nodes.plan(state, rt)
        written = self._artifact(rt)
        assert written["tool_calls"] == []
        assert written["plan_notes"] == []

    def test_the_artifact_says_how_many_reads_were_answered(self, repo, tmp_path):
        """So the question can be answered by reading, not by regex.

        The rendered log already carries "refused:", but counting a run's
        binding caps off rendered strings is the pattern that has produced a
        confident wrong answer every time it has been tried here. The number
        travels as a number.
        """
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "project_complete", "done", "e",
                    tool_calls=[
                        "search(render text: in app) -> 7 line(s)",
                        "read_file(app/ghost.rb) -> refused: does not exist",
                    ],
                    reads_answered=1,
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        written = self._artifact(rt)
        assert len(written["tool_calls"]) == 2
        assert written["reads_answered"] == 1


class TestTheLedgerRecordsTheWholeJourney:
    """Derivation opens findings, precheck claims, landing closes — end to end.

    Driven through the real nodes because every value here crosses a schema
    boundary, and the isolated halves of the old log passed while the feature
    did nothing.
    """

    A_NOTE = {
        "kind": "progress",
        "key": THE_ITEM,
        "subject": "remaining sites",
        "total": "0 remain",
        "needs": "pipeline",
        "finding": "the sweep is complete",
        "observation": "This sweep is complete; 0 sites remain in app/controllers.",
    }

    def _planner(self, **fields):
        return StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "next", "e",
                    stage_fields=planned_stage(**fields),
                    plan_notes=[self.A_NOTE],
                )
            ]
        )

    def test_the_note_opens_a_finding_at_derivation(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, planner=self._planner())
        nodes.plan(state, rt)
        (finding,) = rt.ledger.views().open_findings()
        assert finding.keys == [THE_ITEM] and finding.by == "planner"
        assert finding.stage_id == "extract" and finding.run_id == "r1"
        assert "0 sites remain" in finding.claim

    def test_precheck_claims_the_keys(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, planner=self._planner())
        state = {**state, **nodes.plan(state, rt)}
        nodes.precheck(state, rt)
        claim = rt.ledger.views().state(THE_ITEM)
        assert (claim.state, claim.run_id, claim.stage_id) == ("claimed", "r1", "extract")

    def test_landing_closes_the_key_and_its_finding(self, repo, tmp_path, run_git):
        cfg, rt, state = make(repo, tmp_path, planner=self._planner())
        state = {**state, **nodes.plan(state, rt)}
        state = {**state, **nodes.precheck(state, rt)}
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        out = nodes.advance(state, rt)

        views = rt.ledger.views()
        landed = views.state(THE_ITEM)
        assert landed.state == "landed"
        assert landed.sha == out["completed"][-1]["merge_sha"]
        assert views.findings_on(THE_ITEM)[0].status == "resolved"
        assert out["completed"][-1]["plan_keys"] == [THE_ITEM]

    def test_the_landing_commit_carries_the_trailers(self, repo, tmp_path, run_git):
        cfg, rt, state = make(repo, tmp_path, planner=self._planner())
        state = {**state, **nodes.plan(state, rt)}
        state = {**state, **nodes.precheck(state, rt)}
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)
        body = run_git(repo, "log", "-1", "--format=%B", cfg.project_branch)
        assert f"Plan-Keys: {THE_ITEM}" in body
        assert "Planner-Model: claude-opus-5" in body
        assert "Reviewer-Model: gpt-5.5" in body
        assert f"Bay: test-host/{repo.name}" in body
        assert "Stage-Base: " in body

    def test_the_next_derivation_sees_the_landing_in_the_projection(self, repo, tmp_path):
        planner = self._planner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = {**state, **nodes.plan(state, rt)}
        state = {**state, **nodes.precheck(state, rt)}
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        state = {**state, **nodes.advance(state, rt)}
        nodes.plan(state, rt)
        prompt = _text_of(planner.calls[-1])
        assert "### Landed since the plan text was last folded" in prompt
        assert f"{{#{THE_ITEM}}}" in prompt.split("### Landed", 1)[1]


class TestExecutorFeedbackIsBounded:
    """A 98KB executor log must not become the next prompt.

    CommandRunner used to cap output at 20,000 characters, which bounded
    `result.log` incidentally. Raising that cap so the flake gate could see a
    whole test run removed the bound, and a real attempt produced 97,883
    characters of executor transcript that went verbatim into the next executor
    prompt and into planner feedback.
    """

    def test_a_huge_executor_log_is_clipped_into_feedback(self, repo, tmp_path):
        # Varied text, because that is what a transcript is. It used to be one
        # character repeated 200,000 times, which the progress-run collapse now
        # reduces to a single note — bounding it, but by the wrong mechanism,
        # so the test stopped exercising truncation while still passing its
        # first assertion. The degenerate case is covered below.
        executor = StubExecutor(repo=repo, ok=False)
        executor.log = "".join(f"line {i} of transcript\n" for i in range(8_000))
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        joined = "".join(out.get("review_feedback") or [])
        assert len(joined) < 20_000, "feedback must be bounded"
        assert "truncated" in joined

    def test_a_degenerate_run_is_bounded_by_collapsing(self, repo, tmp_path):
        # The other way output gets large: a progress bar rather than prose.
        # Collapsed rather than truncated, which keeps whatever follows it.
        executor = StubExecutor(repo=repo, ok=False)
        executor.log = "." * 200_000 + "\nTHE_ACTUAL_ERROR"
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        joined = "".join(out.get("review_feedback") or [])
        assert len(joined) < 20_000, "feedback must be bounded"
        assert "THE_ACTUAL_ERROR" in joined

    def test_the_artifact_keeps_the_whole_log(self, repo, tmp_path):
        # Bounded where it is used, whole where it is read afterwards.
        executor = StubExecutor(repo=repo, ok=False)
        executor.log = "Y" * 200_000
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)
        nodes.execute(state, rt)
        logs = list(rt.paths.run_dir.glob("stages/*/executor.log"))
        assert logs and len(logs[0].read_text()) > 150_000


class TestStuckWithoutLanding:
    """Three planner passes with nothing landing, and the run stops.

    A flat global cap needs a stage count nobody has: CodeGantry's stages
    are not the plan document's stages, and the planner derives them as it
    goes. An allowance that accrues per landed stage fixes that but builds a
    reserve, which then gets spent all at once on the very stage it should have
    caught.

    Consecutive passes without a landing measures the thing directly. A run
    that keeps landing work can continue indefinitely — bounded by the wall
    clock, not by a number picked in advance. A run that has been round the
    planner three times with nothing to show for it is stuck, and more
    interventions will not unstick it.
    """

    def test_three_interventions_without_a_landing_stops_the_run(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "again", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, limits={"max_interventions_without_landing": 3})
        state = with_stage(state, rt)
        state["interventions_since_landing"] = 3
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"
        assert "without landing" in out["escalation_reason"].lower()

    def test_a_landing_resets_the_counter(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state["interventions_since_landing"] = 2
        (repo / "app.py").write_text("changed\n")
        out = nodes.advance(state, rt)
        assert out["interventions_since_landing"] == 0

    def test_an_intervention_increments_it(self, repo, tmp_path):
        planner = StubPlanner(
            [PlannerOutcome("revise", "redraw", "e", stage_fields=planned_stage())]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)
        out = nodes.plan(state, rt)
        assert out["interventions_since_landing"] == 1

    def test_a_run_that_keeps_landing_is_not_stopped(self, repo, tmp_path):
        # Two interventions, then a landing, then two more: never three in a row.
        cfg, rt, state = make(repo, tmp_path, limits={"max_interventions_without_landing": 3})
        state = with_stage(state, rt)
        state["interventions_since_landing"] = 2
        (repo / "app.py").write_text("changed\n")
        landed = nodes.advance(state, rt)
        assert landed["interventions_since_landing"] == 0


class TestAStageCostsItsPlanningToo:
    """`wall_seconds` starts at precheck, so deriving the stage is free.

    It is not. Derivations on one project ran five to seven minutes against a
    mean stage time of 10.6, so roughly 40% of the clock sat outside every
    per-stage figure — and a projection built on stage time alone understates
    by that much. `plan_seconds` is the matching half, accumulated across
    revisions because a redrawn stage is the same piece of work.
    """

    def test_planning_time_lands_on_the_stage(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "plan_seconds": 42.0}

        out = nodes.advance(state, rt)

        landed = out["completed"][-1]
        assert landed["plan_seconds"] == 42.0
        assert "wall_seconds" in landed, "the two halves travel together"

    def test_the_stage_reset_must_not_carry_it(self, repo, tmp_path):
        """It belongs to `advance`, and this is why.

        The first version of this test asserted the zero was *in*
        `fresh_stage_fields`, which is where it was and where it was wrong:
        `plan` spreads that reset over its own return after accumulating the
        time, so the value was zeroed on the way out and every landed stage
        recorded none. The test passed the whole time, because it pinned the
        location instead of the behaviour.
        """
        from code_gantry.state import fresh_stage_fields

        assert "plan_seconds" not in fresh_stage_fields()

    def test_a_stage_that_never_planned_records_zero(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        out = nodes.advance(state, rt)
        assert out["completed"][-1]["plan_seconds"] == 0.0


class TestStageCostOutlivesTheRun:
    """Driven through advance, because the halves passing proves nothing.

    Twice today a value was computed correctly, written correctly, and lost in
    transit — `full_suite_digest` to an undeclared schema key, `plan_notes` to
    a reset applied after them. The cost figure takes the same journey, so it
    gets the same end-to-end test rather than a unit test of the writer.
    """

    def test_a_landed_stage_records_what_it_cost(self, repo, tmp_path):
        from code_gantry.planner import recent_stage_costs

        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "executor_context_tokens": 13_000}

        nodes.advance(state, rt)

        costs = recent_stage_costs(rt.project.project_dir)
        assert len(costs) == 1
        assert costs[0]["context_tokens"] == 13_000
        # Keyed by something that still resolves once the branch is gone.
        assert rt.git.commit_subject().startswith("[extract]")
        assert costs[0]["merge_sha"]

    def test_a_stage_with_no_measurement_records_nothing(self, repo, tmp_path):
        from code_gantry.planner import recent_stage_costs

        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        nodes.advance(state, rt)
        assert recent_stage_costs(rt.project.project_dir) == []

    def test_the_configuration_reaches_the_file_not_just_the_writer(
        self, repo, tmp_path
    ):
        # The end-to-end half. The writer takes `roles` and the config holds
        # three of them, and the defect this class exists for is a value that
        # is correct at both ends and lost in between — so the assertion is on
        # the bytes in the file, reached through the node.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "executor_context_tokens": 13_000}

        nodes.advance(state, rt)

        line = (rt.project.project_dir / "stage-costs.md").read_text()
        assert f"exec {rt.cfg.executor.model}" in line
        assert f"plan {rt.cfg.planner.model}" in line
        assert f"review {rt.cfg.reviewer.model}" in line
        for effort in (
            rt.cfg.planner.effort,
            rt.cfg.reviewer.effort,
            rt.cfg.executor.reasoning_effort,
        ):
            if effort:
                assert f"@{effort}" in line


class TestTheReadBudgetIsToldToThePlanner:
    """The planner declares `read_files`; the tool silently cuts the tail.

    Observed live: four consecutive stages each asked for one reference file
    too many — `user.rb`, `schedule.rb`, `item.rb`, then a form template — and
    each time the executor worked without a file the instruction went on to
    reason about. Nothing failed, which is what makes it worth fixing: the
    planner was choosing blind and had no way to learn it.

    Same shape as the executor-token gap before it. A measurement the tool has
    and the planner does not is a decision made on a guess.
    """

    def test_it_survives_from_the_executor_to_the_planners_history(
        self, repo, tmp_path
    ):
        """End to end, because every part of this worked in isolation before.

        Three defects this session were values computed correctly, written
        correctly, and lost in transit — dropped by a schema that did not
        declare them, or by a reset spread over the top of them. A unit test
        on each end would have passed for all three.
        """
        executor = StubExecutor(
            repo=repo,
            edits=[("app.py", "changed\n")],
            dropped_reads=["app/models/user.rb"],
        )
        cfg, rt, state = make(repo, tmp_path, executor=executor)
        state = with_stage(state, rt)

        state = {**state, **nodes.execute(state, rt)}
        assert state["withheld_reads"] == ["app/models/user.rb"], (
            "the execute node must carry what the executor withheld"
        )

        state = {**state, **nodes.verify(state, rt)}
        state = {**state, **nodes.review(state, rt)}
        state = {**state, **nodes.advance(state, rt)}
        landed = state["completed"][-1]
        assert landed["withheld_reads"] == ["app/models/user.rb"], (
            "and the landed stage must keep it, or the planner never sees it"
        )

        nodes.plan(state, rt)
        prompt = "\n".join(
            m["content"] if isinstance(m["content"], str) else str(m["content"])
            for m in rt.planner.calls[-1]
        )
        assert "app/models/user.rb" in prompt
        assert "max_read_lines" in prompt

    def test_it_is_declared_on_the_state_schema(self, repo, tmp_path):
        # The graph drops keys the schema does not know. `full_suite_digest`
        # shipped without this line once: written every time, discarded every
        # time, and silently never used.
        assert "withheld_reads" in RunState.__annotations__

    def test_it_does_not_leak_into_the_next_stage(self, repo, tmp_path):
        # Carried forward it would report a withholding the next stage never
        # suffered, and the planner would trim a reference list that fits.
        assert fresh_stage_fields()["withheld_reads"] == []

    def test_a_stage_that_fit_says_nothing(self, repo, tmp_path):
        # Rendered as an empty list it would read as a budget problem with no
        # files, which is worse than silence.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        out = nodes.execute(state, rt)
        assert "withheld_reads" not in out


class TestTheProjectionIsSentToThePlanner:
    """What has changed since the plan text was folded reaches every derivation."""

    def test_a_landing_recorded_in_the_ledger_reaches_the_prompt(self, repo, tmp_path):
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.ledger.append(LANDED, key=THE_OTHER_ITEM, sha="abc1234def", stage_id="earlier")
        nodes.plan(state, rt)
        prompt = _text_of(planner.calls[0])
        assert "abc1234def" in prompt

    def test_the_plan_text_is_in_the_marked_block_and_the_projection_is_not(self, repo, tmp_path):
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.ledger.append(LANDED, key=THE_OTHER_ITEM, sha="abc1234def", stage_id="earlier")
        nodes.plan(state, rt)
        blocks = planner.calls[0][0]["content"]
        marked = next(b for b in blocks if "cache_control" in b)
        assert "do the thing" in marked["text"]
        assert "abc1234def" not in marked["text"]

    def test_a_fold_moves_the_landing_into_the_plan_text(self, repo, tmp_path):
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.cfg.ledger.fold_ratio = 0.0001
        rt.ledger.append(LANDED, key=THE_OTHER_ITEM, sha="abc1234def", stage_id="earlier")
        nodes.plan(state, rt)
        blocks = planner.calls[0][0]["content"]
        marked = next(b for b in blocks if "cache_control" in b)
        assert "abc1234def" in marked["text"]
        assert "### Landed" not in _text_of(planner.calls[0])


class TestTheWithoutLandingBudgetIsTerminalAndSaysSo:
    """Hitting it kills the run, and the message has to admit that.

    The check runs before the planner is called, and the counter only clears
    when a stage lands — so a run that hits it re-escalates on every resume
    with nothing having run. That terminality is deliberate: a budget an
    operator can clear by re-running is not a budget, and resume-in-a-loop is
    the exact failure it guards against.

    What was wrong was the silence. Every other escalation means "fix it and
    resume", resume works, and nothing distinguished this one — so the natural
    next move spent a preflight and a container start to print the identical
    message.
    """

    def _stuck(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        return rt, {**state, "interventions_since_landing": 3}

    def test_it_escalates(self, repo, tmp_path):
        rt, state = self._stuck(repo, tmp_path)
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "escalate"

    def test_it_says_resume_will_not_help(self, repo, tmp_path):
        rt, state = self._stuck(repo, tmp_path)
        reason = nodes.plan(state, rt)["escalation_reason"]
        assert "Resume alone will not clear this" in reason

    def test_it_names_every_way_out(self, repo, tmp_path):
        # A dead end that does not say which doors exist sends the operator to
        # the one door that is locked.
        rt, state = self._stuck(repo, tmp_path)
        reason = nodes.plan(state, rt)["escalation_reason"]
        assert "--reset-progress-budget" in reason
        assert "max_interventions_without_landing" in reason
        assert "fresh run" in reason

    def test_the_planner_is_never_called(self, repo, tmp_path):
        # The whole point: it stops before spending anything.
        rt, state = self._stuck(repo, tmp_path)
        nodes.plan(state, rt)
        assert rt.planner.calls == [], "the budget must stop the call, not judge it"
class TestResumingIsConsumedNotRemembered:
    """`resuming` means "this step", not "this run has been resumed once".

    Nothing cleared it. `cli.resume` set it true and it stayed true, so every
    later reader saw a run that was permanently mid-resume. The progress layer
    treats it as a hard bypass:

        if ctx.resuming:
            return None

    so one resume disabled "the attempt reproduced the previous diff exactly"
    for the rest of the run. Observed: a stage produced byte-identical diffs on
    two attempts, both rejected for the same reason, and the guard whose whole
    job is to redraw such a stage never fired — the repetition was put down to
    a stuck reviewer.
    """

    def test_precheck_consumes_it(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state = {**state, "resuming": True, "stage_branch": None}
        assert nodes.precheck(state, rt)["resuming"] is False

    def test_verify_consumes_it(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        out = nodes.verify({**state, "resuming": True}, rt)
        assert out["resuming"] is False

    def test_the_progress_guard_works_again_on_the_next_attempt(
        self, repo, tmp_path
    ):
        """The behaviour the leak suppressed.

        A resumed run verifies once under the exemption — re-entering at verify
        is re-checking a human's fix, not repeating an attempt — and after that
        an identical diff must route to the planner as it always did.
        """
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")

        first = nodes.verify({**state, "resuming": True}, rt)
        state = {**state, **first}
        assert state["resuming"] is False, "the exemption is spent"

        # The executor reproduces the previous diff exactly.
        again = nodes.verify(state, rt)
        assert again.get("failure_layer") == "progress", (
            "with the flag consumed, an identical redo is caught again"
        )
        assert again["next_hop"] == "plan"






class TestTheOpeningFailureOutlivesItsConsequences:
    """The first failure of a retry sequence is the diagnosis.

    Live, on stage 130 of a 129-stage run: an `assert_select` assertion failed
    in one spec, the executor reworked twice, an attempt hit its 900s timeout, and
    the no-progress guard sent the stage to the planner carrying only "the
    attempt reproduced the previous diff exactly". The planner redrew the stage
    knowing nothing about the assertion, and the redraw failed the same way.

    A previous incident was the same shape and cost thirty-five minutes: a
    legible `ArgumentError` naming a file and line became "the executor timed
    out", and the planner reasoned correctly from the wrong failure.

    So the sequence keeps its first failure as well as its last. Not a list —
    the ones in between are the same consequence repeated, and the planner
    prompt is not the place to pay for them.
    """

    def _rt(self, repo, tmp_path, **over):
        _, rt, state = make(repo, tmp_path, **over)
        return rt, state

    def test_the_first_failure_is_recorded_when_the_executor_retries(
        self, repo, tmp_path
    ):
        rt, state = self._rt(repo, tmp_path)
        out = nodes._retry_or_plan(
            state, rt, "tests", "the suite failed", "feedback", "assert_select failed"
        )
        assert out["next_hop"] == "execute", "still within the retry budget"
        assert out["opening_failure"]["summary"] == "the suite failed"
        assert out["opening_failure"]["detail"] == "assert_select failed"

    def test_a_later_failure_does_not_overwrite_it(self, repo, tmp_path):
        rt, state = self._rt(repo, tmp_path)
        first = nodes._retry_or_plan(
            state, rt, "tests", "the suite failed", "f", "assert_select failed"
        )
        state = {**state, **first}
        later = nodes._planner_failure(
            state, "progress", "the attempt reproduced the previous diff", "d"
        )
        assert "opening_failure" not in later or later["opening_failure"] == first[
            "opening_failure"
        ], "the diagnosis is not replaced by its consequence"

    def test_the_planner_receives_both(self, repo, tmp_path):
        rt, state = self._rt(repo, tmp_path)
        state = {
            **state,
            **nodes._retry_or_plan(
                state, rt, "tests", "the suite failed", "f", "assert_select failed"
            ),
        }
        out = nodes._planner_failure(
            state, "progress", "reproduced the previous diff", "no progress"
        )
        assert out["next_hop"] == "plan"
        assert out["last_failure"]["summary"] == "reproduced the previous diff"
        assert state["opening_failure"]["summary"] == "the suite failed"

    def test_a_failure_that_goes_straight_to_the_planner_opens_the_sequence(
        self, repo, tmp_path
    ):
        # Nothing preceded it, so it is both the diagnosis and the consequence.
        rt, state = self._rt(repo, tmp_path)
        out = nodes._planner_failure(state, "scope", "edited outside scope", "d")
        assert out["opening_failure"]["summary"] == "edited outside scope"

    def test_a_revision_clears_it(self, repo, tmp_path):
        # A redrawn stage is a different instruction; the old diagnosis is
        # about work that no longer exists.
        from code_gantry.state import fresh_revision_fields

        assert fresh_revision_fields()["opening_failure"] is None

    def test_a_landed_stage_clears_it(self):
        assert fresh_stage_fields()["opening_failure"] is None

    def test_the_state_schema_declares_it(self):
        # Four defects have been values written correctly and dropped by a
        # schema that did not know the key.
        from code_gantry.state import RunState as Schema

        assert "opening_failure" in Schema.__annotations__

    def test_it_reaches_the_planner_prompt(self, repo, tmp_path):
        """Node to state to prompt, not the endpoints separately.

        Four defects have been values computed correctly, written correctly,
        and lost in transit — dropped by a schema that did not declare the key,
        or zeroed by a reset spread over the top of them. Both are live risks
        here, so the journey is the test.
        """
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = with_stage(state, rt)

        # The diagnosis: a real assertion failure, which retries.
        state = {
            **state,
            **nodes._retry_or_plan(
                state,
                rt,
                "tests",
                "the suite failed",
                "feedback for the executor",
                "assert_select('a[href=...]', 'Add Personalization') failed",
                failing_paths=["spec/features/order_funnel_add_item_spec.rb"],
            ),
        }
        # The consequence: retries stop changing anything.
        state = {
            **state,
            **nodes._planner_failure(
                state,
                "progress",
                "the attempt reproduced the previous diff exactly",
                "retrying costs another review for the same result",
            ),
        }

        nodes.plan(state, rt)
        prompt = "".join(
            block["text"]
            for message in planner.calls[0]
            for block in message["content"]
            if block.get("type") == "text"
        )
        assert "Add Personalization" in prompt, "the diagnosis reached the planner"
        assert "order_funnel_add_item_spec.rb" in prompt
        assert "reproduced the previous diff exactly" in prompt


class TestPrecheckRefusesToBuildOnSomebodyElsesChanges:
    """Between stages the tree is clean, because `advance` just committed.

    A dirty tree at precheck means something wrote outside the pipeline — a
    crash between `merge --squash` and `commit`, an editor left open, a human
    mid-edit. Cutting a stage branch over it sweeps those files into the next
    stage's diff, where the scope guard reports them as the executor editing
    out of scope. The executor did nothing of the kind, and the stage pays a
    retry for it.

    Not on a resume. Preflight exempts resume from its own clean-tree check for
    a reason that applies here exactly: a run is resumed because a human just
    fixed something, and that fix is normally uncommitted. Guarding it would
    make every escalation unrecoverable.
    """

    def _dirty(self, repo):
        (repo / "app.py").write_text("someone was editing this\n")

    def test_a_dirty_tree_between_stages_escalates(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": False}
        self._dirty(repo)
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "workspace"

    def test_it_names_the_files(self, repo, tmp_path):
        # "the tree is dirty" without the paths sends the operator to run the
        # command themselves.
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": False}
        self._dirty(repo)
        assert "app.py" in nodes.precheck(state, rt)["escalation_reason"]

    def test_a_clean_tree_proceeds(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": False}
        assert nodes.precheck(state, rt)["next_hop"] == "execute"

    def test_a_resume_is_exempt(self, repo, tmp_path):
        # The human's fix is the dirt.
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": Stage(**planned_stage()).model_dump(),
                 "resuming": True}
        self._dirty(repo)
        assert nodes.precheck(state, rt)["next_hop"] == "execute"

    def test_the_resume_re_enters_at_precheck(self):
        # Not verify: the check runs before a branch is cut, so there is no
        # stage branch to diff against. Back to precheck, which re-checks.
        from code_gantry.state import resume_entry_point

        assert (
            resume_entry_point(
                {
                    "resuming": True,
                    "failure_layer": "workspace",
                    "current": {"id": "s"},
                    "stage_has_work": False,
                }
            )
            == "precheck"
        )

    def test_a_revision_is_exempt(self, repo, tmp_path):
        # A restart re-enters precheck without passing through `advance`, and
        # `advance` is what commits. An uncommitted attempt in the tree is the
        # ordinary state of a redraw, so guarding here would escalate every one.
        cfg, rt, state = make(repo, tmp_path)
        state = {
            **state,
            "current": Stage(**planned_stage()).model_dump(),
            "resuming": False,
            "revision": 2,
        }
        self._dirty(repo)
        assert nodes.precheck(state, rt)["next_hop"] == "execute"

    def test_a_stage_already_holding_a_branch_is_exempt(self, repo, tmp_path):
        # Mid-stage, for the same reason.
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        state = {**state, "resuming": False}
        self._dirty(repo)
        assert nodes.precheck(state, rt)["next_hop"] == "execute"


class TestTheRejectedAnswerReachesTheArtifact:
    """A field on the dataclass that never reaches the file is worth nothing.

    `planner.json` is what anyone actually opens afterwards. The rejected
    answer exists to be read there, so the test is the journey — planner client
    to node to file — not the dataclass.
    """

    def test_planner_json_carries_the_rejected_answer(self, repo, tmp_path):
        rejected = {"verdict": "revise", "reasoning": "narrow it", "stage": None}
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "blocked",
                    "the planner returned 'revise' without a stage spec",
                    "e",
                    failed=True,
                    raw=rejected,
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)

        written = json.loads(
            (
                rt.paths.attempt_dir(state.get("stage_index", 0), "plan", 0, 0)
                / "planner.json"
            ).read_text()
        )
        assert written["rejected_answer"] == rejected
        assert written["client_failure"] is True

    def test_an_accepted_answer_records_it_as_null(self, repo, tmp_path):
        # Recorded either way. An absent key cannot be told apart from a
        # feature that never ran — the same reason tool_calls is always
        # written, even empty.
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "r", "e", stage_fields=planned_stage()
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        written = json.loads(
            (
                rt.paths.attempt_dir(state.get("stage_index", 0), "plan", 0, 0)
                / "planner.json"
            ).read_text()
        )
        assert "rejected_answer" in written
        assert written["rejected_answer"] is None


class TestTheFindingSurvivesToTheLedger:
    """Planner response to plan note to the finding's claim."""

    def test_a_finding_reaches_the_claim(self, repo, tmp_path):
        note = {
            "kind": "progress",
            "key": THE_ITEM,
            "subject": "remaining renders",
            "total": "7 of 24",
            "needs": "pipeline",
            "finding": "7 of 24 remain, all inline `<script>` renders",
            "observation": "The mechanical half is done.",
        }
        planner = StubPlanner(
            [
                PlannerOutcome(
                    "next_stage", "next", "e",
                    stage_fields=planned_stage(),
                    plan_notes=[note],
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        (finding,) = rt.ledger.views().open_findings()
        assert "7 of 24 remain, all inline `<script>` renders" in finding.claim
        assert "The mechanical half is done." in finding.claim
        assert finding.total == "7 of 24"


class TestAFailedLandingLeavesNothingBehind:
    """Either the stage lands or the ledger is as advance found it."""

    def test_a_failed_merge_records_no_landing(self, repo, tmp_path, monkeypatch):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "review_record": "what the stage did"}

        def boom(*a, **kw):
            raise GitError("pre-commit hook rejected the commit")

        monkeypatch.setattr(rt.git, "squash_merge", boom)
        with pytest.raises(GitError):
            nodes.advance(state, rt)
        assert rt.ledger.views().state(THE_ITEM).state == "open"

    def test_a_successful_landing_keeps_the_record(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "review_record": "what the stage did", "review_summary": "fine"}
        nodes.advance(state, rt)
        landed = rt.ledger.views().state(THE_ITEM)
        assert landed.state == "landed" and landed.evidence == "fine"


class TestTheReviewerIsGivenTheProjection:
    """Runtime to node to the messages the reviewer actually receives.

    The frozen snapshot's copy of the progress log is whatever existed at run
    start — 6,680 bytes against 480,867 on the branch, measured on one long
    run — so the reviewer had no real account of what had been done. Both
    halves of the fix pass through here, and both have somewhere to be dropped
    on the way.
    """

    def _text(self, messages):
        out = []
        for message in messages:
            content = message["content"]
            if isinstance(content, str):
                out.append(content)
            else:
                out += [b.get("text", "") for b in content]
        return "\n".join(out)

    def test_the_projection_reaches_the_reviewer(self, repo, tmp_path):
        reviewer = StubReviewer()
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        rt.ledger.open_finding(keys=[THE_OTHER_ITEM], by="planner", claim="7 of 24 remain")
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")

        nodes.review(state, rt)
        assert "7 of 24 remain" in self._text(reviewer.messages[0])

    def test_the_history_cap_reaches_the_reviewer(self, repo, tmp_path):
        reviewer = StubReviewer()
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        rt.cfg.reviewer.history_stages = 2
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {
            **state,
            "completed": [
                {"index": i, "id": f"stage-{i}", "instruction": f"work {i}"}
                for i in range(5)
            ],
        }

        nodes.review(state, rt)
        text = self._text(reviewer.messages[0])
        assert "stage-4" in text
        assert "stage-0" not in text


class TestTheAgentContextReachesThePlanner:
    """Repository to runtime to prompt.

    The facts in these documents were previously hand-copied into
    `planner.guidance`, and the copy drifted: one project's `AGENTS.md`
    recorded that editing the Gemfile reinstalls the bundle, the guidance said
    nothing, and the plan asserted the opposite across five items nobody drew.
    A wiring that stops anywhere short of the prompt reproduces exactly that.
    """

    def test_conventions_reach_the_planner_prompt(self, repo, tmp_path, run_git):
        (repo / "AGENTS.md").write_text(
            "# Conventions\n\nEditing the Gemfile reinstalls the bundle.\n"
        )
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "conventions")

        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        state = {**state, "plan_sha": rt.git.rev_parse("HEAD")}

        nodes.plan(state, rt)
        prompt = "".join(
            block["text"]
            for message in planner.calls[0]
            for block in message["content"]
            if block.get("type") == "text"
        )
        assert "Editing the Gemfile reinstalls the bundle." in prompt

    def test_a_project_without_one_plans_normally(self, repo, tmp_path):
        planner = StubPlanner()
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        assert nodes.plan(state, rt)["next_hop"] in ("precheck", "finalize", "escalate")


class TestReviewerObservationsReachTheLog:
    """The whole journey, not its endpoints.

    A finding is produced in the reviewer client, carried on ReviewOutcome,
    put into state by `review`, and written by `advance`. Four hops. Every
    value this project has lost in transit passed its unit tests on both ends.

    It matters more than most because the log is the only durable home for
    something the reviewer notices outside its stage. Left in the summary it is
    printed once and gone.
    """

    def _reviewer(self, observations):
        from code_gantry.reviewer import Observation, ReviewOutcome

        class Once:
            def review(self, messages, cache_key=None):
                return ReviewOutcome(
                    verdict="approved",
                    summary="fine",
                    observations=[Observation(**o) for o in observations],
                )

        return Once()

    def test_the_run_log_says_what_was_found_not_only_where(self, repo, tmp_path):
        """The line a human actually reads while watching a run.

        It used to join the file names with semicolons and stop there, so the
        rarest thing the reviewer produces announced itself as a count and a
        path — and finding out what had been noticed meant opening an
        artifact. The paths in a real project are long enough that two of them
        filled the line on their own.

        The finding only. Detail and evidence stay in `review.json` and the
        progress log, which are the record; this is the pointer to it.
        """
        reviewer = self._reviewer([
            {
                "file": "docs/progress_log.md",
                "finding": "the entry says 46 columns; the generator has 50",
                "detail": "Field list at item_recipient.rb:1107-1156.",
            }
        ])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        seen: list[str] = []
        rt.log = seen.append

        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        nodes.review(state, rt)

        line = next(x for x in seen if "reviewer note" in x)
        assert "docs/progress_log.md" in line
        assert "the entry says 46 columns; the generator has 50" in line
        assert "Field list at" not in line, "the detail belongs in the record"

    def test_one_line_each_rather_than_one_joined_line(self, repo, tmp_path):
        reviewer = self._reviewer([
            {"file": "a.rb", "finding": "first thing", "detail": "d"},
            {"file": "b.rb", "finding": "second thing", "detail": "d"},
        ])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        seen: list[str] = []
        rt.log = seen.append

        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        nodes.review(state, rt)

        notes = [x for x in seen if "reviewer note" in x]
        assert len(notes) == 2
        assert "first thing" in notes[0] and "second thing" in notes[1]

    def _reviewer_findings(self, rt):
        return [f for f in rt.ledger.views().findings.values() if f.by == "reviewer"]

    def test_an_observation_opens_a_finding_when_the_stage_lands(self, repo, tmp_path):
        reviewer = self._reviewer([
            {
                "file": "app/views/admin/product_types/_form.html.erb",
                "finding": "mailing_service_ids is submitted but never permitted",
                "detail": "The checkbox posts it and no permit list covers it.",
            }
        ])
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)

        (finding,) = self._reviewer_findings(rt)
        assert finding.needs == "human" and finding.status == "open"
        assert "app/views/admin/product_types/_form.html.erb" in finding.claim
        assert "mailing_service_ids is submitted but never permitted" in finding.claim
        assert "The checkbox posts it and no permit list covers it." in finding.claim

    def test_nothing_is_opened_when_there_are_none(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, reviewer=self._reviewer([]))
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)
        assert self._reviewer_findings(rt) == []

    def test_a_rework_cycle_does_not_report_the_same_finding_twice(
        self, repo, tmp_path
    ):
        # Every review of a stage sees the whole cumulative diff, so the newest
        # set supersedes the last.
        observation = {
            "file": "app/models/cart.rb",
            "finding": "two attr_accessible blocks",
            "detail": "Lines 149 and 396 both declare one.",
        }
        cfg, rt, state = make(repo, tmp_path, reviewer=self._reviewer([observation]))
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)
        assert len(self._reviewer_findings(rt)) == 1

    def test_a_stage_that_never_lands_records_nothing(self, repo, tmp_path):
        # A finding from abandoned work must not enter the record.
        cfg, rt, state = make(
            repo, tmp_path,
            reviewer=self._reviewer([
                {"file": "a.rb", "finding": "something", "detail": "detail"}
            ]),
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        # No advance: the stage was abandoned.
        assert self._reviewer_findings(rt) == []
        assert state["pending_observations"][0]["file"] == "a.rb"


class TestChecksThatWrite:
    """A check may fix as well as report, and something has to commit that.

    `rubocop -A`, `eslint --fix`, `gofmt -w`. The executor commits its own work
    before verify starts, so nothing else in the loop commits what a check
    changed. Left uncommitted it survives the stage — swept up silently if the
    stage lands, orphaned if the stage is blocked or reworked away, at which
    point the next stage's precheck refuses to cut a branch over changes it
    cannot attribute. That stopped a run.
    """

    LINTER = "printf 'linted\\n' >> app.py"

    def test_a_check_that_writes_is_committed_to_the_stage_branch(
        self, repo, tmp_path
    ):
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt, checks=[self.LINTER])
        (repo / "app.py").write_text("stage work\n")
        nodes.verify(state, rt)
        assert "linted" in (repo / "app.py").read_text()
        assert not rt.git.uncommitted()

    def test_it_can_be_switched_off(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, checks_commit_changes=False)
        state = with_stage(state, rt, checks=[self.LINTER])
        (repo / "app.py").write_text("stage work\n")
        nodes.verify(state, rt)
        assert "linted" in (repo / "app.py").read_text()
        assert rt.git.uncommitted()


class TestTheRecordReachesTheCommit:
    """The reviewer's account of the change, end to end into the landing commit."""

    def _reviewer(self, record):
        return StubReviewer(
            [ReviewOutcome(verdict="approved", summary="matches the stage",
                           record=record)]
        )

    def test_it_is_what_the_commit_gets(self, repo, tmp_path, run_git):
        cfg, rt, state = make(
            repo, tmp_path,
            reviewer=self._reviewer("Widens the permit list to the two id "
                                    "columns, so the selects now save."),
        )
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)

        body = run_git(repo, "log", "-1", "--format=%B", cfg.project_branch)
        assert "so the selects now save" in body.split("Landed:", 1)[1]
        assert "matches the stage" not in body.split("Landed:", 1)[1].split("Plan-Keys", 1)[0]

    def test_a_reviewer_that_writes_none_falls_back_to_the_summary(
        self, repo, tmp_path, run_git
    ):
        cfg, rt, state = make(repo, tmp_path, reviewer=self._reviewer(""))
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)
        body = run_git(repo, "log", "-1", "--format=%B", cfg.project_branch)
        assert "matches the stage" in body


class TestTheSquashCommitIsAProperCommitMessage:
    """Subject, blank line, what was asked, what landed, trailers."""

    def _stage(self, instruction):
        from code_gantry.config import Stage

        return Stage(id="a-fairly-long-stage-id-like-real-ones", instruction=instruction)

    def test_the_subject_is_the_stage_id_alone(self):
        msg = nodes._commit_message(self._stage("Do the thing\n\nmore"), "")
        assert msg.splitlines()[0] == "[a-fairly-long-stage-id-like-real-ones]"

    def test_the_subject_does_not_carry_the_instruction(self):
        msg = nodes._commit_message(self._stage("Close the permit gap"), "did it")
        assert "Close the permit gap" not in msg.splitlines()[0]

    def test_asked_and_landed_are_kept_apart_and_in_that_order(self):
        msg = nodes._commit_message(self._stage("Close the permit gap"), "It closed it.")
        lines = msg.splitlines()
        assert lines[1] == "", "git needs a blank line after the subject"
        assert msg.index("Asked:") < msg.index("Close the permit gap") < msg.index("Landed:") < msg.index("It closed it.")

    def test_a_long_body_is_wrapped_rather_than_one_line(self):
        record = (
            "The diff removes only the unused nested package-id input and adds "
            "focused response-body assertions that guard both its absence and the "
            "retained top-level hidden input, preserving the positional request "
            "style and the existing spec structure throughout the file."
        )
        body = nodes._commit_message(self._stage("s"), record).split("Landed:\n\n", 1)[1]
        assert len(body.splitlines()) > 1
        assert max(len(line) for line in body.splitlines()) <= 72

    def test_paragraphs_survive_wrapping(self):
        msg = nodes._commit_message(self._stage("s"), "First para.\n\nSecond para.")
        body = msg.split("Landed:\n\n", 1)[1]
        assert "First para." in body and "Second para." in body
        assert "" in body.splitlines(), "the paragraph break is kept"

    def test_a_long_path_is_not_broken_across_lines(self):
        record = "It changes " + "a/very/long/path/that/goes/on/" * 4 + "file.rb here."
        body = nodes._commit_message(self._stage("s"), record).split("Landed:\n\n", 1)[1]
        assert "a/very/long/path/that/goes/on/a/very/long" in body

    def test_nothing_asked_and_nothing_landed_is_the_subject_alone(self):
        from code_gantry.config import Stage

        msg = nodes._commit_message(Stage(id="just-this"), "")
        assert msg == "[just-this]"

    def test_unicode_escapes_are_decoded(self):
        msg = nodes._commit_message(self._stage("s"), "an em dash \\u2014 here")
        assert "\u2014" in msg
        assert "u2014" not in msg.replace("\u2014", "")

    def test_trailers_close_the_message_and_skip_empty_values(self):
        msg = nodes._commit_message(
            self._stage("s"), "done",
            trailers=[("Plan-Keys", "p.002 p.003"), ("Resolves", ""), ("Bay", "host-a")],
        )
        tail = msg.rsplit("\n\n", 1)[1]
        assert tail == "Plan-Keys: p.002 p.003\nBay: host-a"


class TestKeysAreClaimedWhenTheStageIsCut:
    """Precheck claims the stage's keys, and refuses a key that is no longer open."""

    def test_the_keys_are_claimed_before_the_branch_is_cut(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": planned_stage()}
        out = nodes.precheck(state, rt)
        assert out.get("stage_branch")
        claim = rt.ledger.views().state(THE_ITEM)
        assert (claim.state, claim.run_id, claim.stage_id) == ("claimed", "r1", "extract")

    def test_a_claim_by_this_stage_is_not_repeated(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": planned_stage()}
        state = {**state, **nodes.precheck(state, rt)}
        nodes.precheck({**state, "revision": 1}, rt)
        claims = [e for e in rt.ledger.events() if e.kind == CLAIMED]
        assert len(claims) == 1

    def test_a_key_landed_elsewhere_sends_the_stage_back_to_the_planner(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        rt.ledger.append(LANDED, key=THE_ITEM, sha="abc1234def", stage_id="someone-else")
        state = {**state, "current": planned_stage(), "stage_queue": [planned_stage(id="behind", plan_keys=[THE_OTHER_ITEM])]}
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "plan"
        assert out["failure_layer"] == "plan_keys"
        assert out["stage_queue"] == []
        assert THE_ITEM in out["last_failure"]["detail"]

    def test_a_key_claimed_by_another_run_is_refused_too(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        rt.ledger.append(CLAIMED, key=THE_ITEM, stage_id="theirs", run_id="r0")
        state = {**state, "current": planned_stage()}
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "plan"

    def test_a_stage_with_no_keys_claims_nothing(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        state = {**state, "current": planned_stage(plan_keys=[])}
        nodes.precheck(state, rt)
        assert not [e for e in rt.ledger.events() if e.kind == CLAIMED]


class TestTheNativeExecutorsMeasurementsSurviveTheTrip:
    """`execute` → state → `advance` → `stage-costs.md`, driven end to end.

    The class above tests the same file and did not catch this, because it
    seeds `executor_context_tokens` straight into state — it pins `advance`,
    which was never broken. What broke was one link earlier: `context_tokens`
    and `cost_usd` were set only by the console scrapers they replaced, the in-process
    loop set neither, and `advance`'s guard
    `if executor_context_tokens or executor_cost_usd` went quietly false. The
    file stopped being written the hour the executor switched and nothing
    failed, because a guard that suppresses noise suppresses the channel the
    same way. The planner reads that file on every call.

    `StubExecutor` could not have caught it either: its own comment says the
    journey starts in a subprocess's console output, which was true and stopped
    being true.
    """

    def _measured(self, repo, **fields):
        class Measured(StubExecutor):
            def run_agent_stage(
                self, stage, prompt, history_dir=None, since_sha="",
                agent_context=None, feedback=None, failure_layer=None, model="",
            ):
                self._apply()
                return ExecutionResult(ok=True, log="", **fields)

        return Measured(repo=repo, edits=[("app.py", "stage work\n")])

    def test_the_peak_context_and_the_cost_both_land(self, repo, tmp_path):
        from code_gantry.planner import recent_stage_costs

        ex = self._measured(repo, context_tokens=21_000, cost_usd=0.0092)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state.update(nodes.execute(state, rt))
        nodes.advance(state, rt)

        costs = recent_stage_costs(rt.project.project_dir)
        assert costs, "nothing was written to stage-costs.md at all"
        assert costs[0]["context_tokens"] == 21_000
        written = (rt.project.project_dir / "stage-costs.md").read_text()
        assert "$0.0092" in written

    def test_all_three_roles_reach_the_cost_line(self, repo, tmp_path):
        """The journey, through every node that spends money.

        The planner is 91% of the bill and had no per-stage record anywhere —
        it logged no token counts at all — so a question about what an
        unfolded progress log was costing could not be answered from this
        project's own artifacts. The reviewer's usage reached the log and
        stopped there.

        Driven through `plan`, `execute` and `advance` rather than asserted on
        `_stage_spend`, because the defect was never the arithmetic: the keys
        exist in `zero_usage` and nothing wrote them, and `fresh_stage_fields`
        zeroed the one that was written. Both are invisible to a test that
        calls the formatter with a dict it made up.
        """
        from code_gantry.planner import recent_stage_costs

        planner = StubPlanner([
            PlannerOutcome(
                "next_stage", "first", "e", stage_fields=planned_stage(),
                usage=PlannerUsage(
                    prompt_tokens=500_000, cached_tokens=480_000,
                    completion_tokens=9_000,
                ),
            )
        ])
        ex = self._measured(repo, context_tokens=21_000, cost_usd=0.0092)
        cfg, rt, state = make(repo, tmp_path, planner=planner, executor=ex)

        state.update(nodes.plan(state, rt))
        assert state["stage_usage"]["planner_prompt_tokens"] == 500_000, (
            "the derivation's cost was zeroed by the per-stage reset"
        )
        state = with_stage(state, rt)
        state.update(nodes.execute(state, rt))
        nodes.advance(state, rt)

        written = (rt.project.project_dir / "stage-costs.md").read_text()
        assert "planner 500,000 in (480,000 cached) / 9,000 out" in written
        assert "21,000 context" in written
        # And the head of the line still parses, so the planner's batch sizing
        # does not silently stop counting.
        assert recent_stage_costs(rt.project.project_dir)[0]["context_tokens"] == 21_000

    def test_an_unpriced_model_still_records_its_context(self, repo, tmp_path):
        # `cost_usd` is None for a model with no rate — the distinction the
        # pricing module exists to keep. The line must still be written, on the
        # strength of the context figure alone, or an unpriced executor silently
        # empties the planner's calibration data.
        from code_gantry.planner import recent_stage_costs

        ex = self._measured(repo, context_tokens=13_000, cost_usd=None)
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        state = with_stage(state, rt)
        state.update(nodes.execute(state, rt))
        nodes.advance(state, rt)

        costs = recent_stage_costs(rt.project.project_dir)
        assert costs and costs[0]["context_tokens"] == 13_000
        assert "$" not in (rt.project.project_dir / "stage-costs.md").read_text()


class TestTheExecuteLineReportsWhatItPaid:
    """Peak context, output tokens, cache rate — not the reviewer's pair.

    The executor's cache behaviour was unmeasurable for most of this project's
    life: the accounting it replaced never read OpenAI's
    `prompt_tokens_details.cached_tokens`, so a silent zero was the instrument
    rather than the cache. Owning the client made the figure available; putting
    it where the operator already looks is what makes it seen.

    Deliberately *not* the reviewer's `(N prompt, M cached)`. That line reports
    a single call. This loop resends its conversation every turn, so a summed
    prompt re-counts one prefix up to 45 times — measured over 49 attempts it
    spans 23k to 3.4M, which is `turns × context` restated and describes
    billing, not work. Billing is `cost_usd`, and it goes to `stage-costs.md`.

    The three that survive answer independent questions: peak is the constraint
    that decides whether a batch fits, output is the only figure not re-counting
    context the model was handed, and the rate is scale-free with its useful
    reading at the *bottom* — the floor over that window was 50.3%, a prefix
    that broke, which the raw pair would bury.
    """

    def _with_usage(self, repo, **usage):
        from code_gantry.openaiclient import TokenUsage

        class Counted(StubExecutor):
            def run_agent_stage(
                self, stage, prompt, history_dir=None, since_sha="",
                agent_context=None, feedback=None, failure_layer=None, model="",
            ):
                self._apply()
                return ExecutionResult(
                    ok=True, log="", context_tokens=21_000,
                    tool_counts={"read_file": 3},
                    usage=TokenUsage(**usage),
                )

        return Counted(repo=repo, edits=[("app.py", "stage work\n")])

    def test_the_counts_are_appended_to_the_tool_summary(self, repo, tmp_path):
        ex = self._with_usage(
            repo, prompt_tokens=3_423_327, cached_tokens=3_322_008,
            cache_write_tokens=96_040, completion_tokens=32_037,
        )
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        lines: list[str] = []
        rt.log = lines.append
        state = with_stage(state, rt)
        nodes.execute(state, rt)

        line = next(m for m in lines if "tool call(s)" in m)
        # 3,322,008 / 3,423,327 = 97%. The sum itself never appears.
        assert "97% cached" in line
        assert "32037 out" in line
        assert "21000 peak" in line
        assert "3423327" not in line, "the summed prompt is billing, not effort"

    def test_an_attempt_with_no_usage_says_nothing_extra(self, repo, tmp_path):
        # A provider that reported nothing must not render "0% cached", which
        # reads as a measurement rather than its absence — the same reason
        # `stage-costs.md` omits a dollar it does not have.
        class NoUsage(StubExecutor):
            def run_agent_stage(
                self, stage, prompt, history_dir=None, since_sha="",
                agent_context=None, feedback=None, failure_layer=None, model="",
            ):
                self._apply()
                return ExecutionResult(ok=True, log="", tool_counts={"read_file": 3})

        ex = NoUsage(repo=repo, edits=[("app.py", "stage work\n")])
        cfg, rt, state = make(repo, tmp_path, executor=ex)
        lines: list[str] = []
        rt.log = lines.append
        state = with_stage(state, rt)
        nodes.execute(state, rt)

        line = next(m for m in lines if "tool call(s)" in m)
        assert "cached" not in line and "peak" not in line


class TestThePlannersPeakReachesTheCostLine:
    """The figure that answers "how close did that call come to the window".

    Everything recorded about the planner was a total, and a tool loop bills
    the whole conversation once per turn — so one derivation showed 6,604,374
    input tokens against a prompt of 187k and 32 calls. Read as a context
    figure it is nonsense by a factor of thirty; read as a bill it is correct
    and answers a different question than the one the read budgets are about.

    The executor has tracked its peak since it went in-process. The planner
    never did, and the planner is the role that has actually overrun a context
    limit — rejected at 1,103,000 tokens against a 1,000,000 ceiling, with
    nothing recorded that would have seen it coming.
    """

    def test_the_peak_survives_plan_execute_and_advance(self, repo, tmp_path):
        from code_gantry.planner import recent_stage_costs

        planner = StubPlanner([
            PlannerOutcome(
                "next_stage", "first", "e", stage_fields=planned_stage(),
                usage=PlannerUsage(
                    prompt_tokens=6_604_374, cached_tokens=6_309_958,
                    completion_tokens=20_882, cache_write_tokens=294_370,
                    peak_prompt_tokens=480_120,
                ),
            )
        ])
        cfg, rt, state = make(repo, tmp_path, planner=planner)

        state.update(nodes.plan(state, rt))
        assert state["stage_usage"]["planner_peak_prompt_tokens"] == 480_120
        state = with_stage(state, rt)
        state.update(nodes.execute(state, rt))
        nodes.advance(state, rt)

        written = (rt.project.project_dir / "stage-costs.md").read_text()
        assert "peak 480,120" in written
        # Beside the total rather than instead of it: one is the bill, the
        # other is the size of the largest call, and each answers a question
        # the other cannot.
        assert "6,604,374 in" in written
        assert recent_stage_costs(rt.project.project_dir), "the line stopped parsing"

    def test_a_role_with_no_peak_recorded_says_nothing(self, repo, tmp_path):
        # The reviewer has no peak of its own yet. An absent figure is left
        # out rather than rendered as zero, which would read as a call that
        # carried nothing.
        cfg, rt, state = make(repo, tmp_path)
        spend = nodes._stage_spend(cfg, {"prompt_tokens": 500, "completion_tokens": 9})
        assert spend and "peak" not in spend[0]


class TestContextIsSummedAcrossAttempts:
    """A stage that took three passes loaded context three times.

    `executor_context_tokens` was assigned rather than accumulated, eleven
    lines above `executor_cost_usd`, which accumulates and says in its comment
    why: "a stage that took four attempts paid for four and the figure worth
    recording is the stage's, not the last attempt's." The same sentence is
    true of context and was not applied to it.

    The consequence is not a slightly-low number. A stage whose final attempt
    is a one-line fix records that attempt's high-water mark as the whole
    stage's. Measured on one run: two stages of two and three attempts,
    together 4.4M and 1.5M prompt tokens, recorded 12,933 and 16,079 — below
    the opening prompt of a single turn, in the figure the planner sizes
    batches from.

    Summed peaks rather than summed cache writes, which was the alternative
    considered. Writes count only newly-cached material, so a stage that
    reuses an earlier one's prefix looks small precisely because it was
    efficient — one stage on that run wrote 21,547 while carrying 82,015.
    Peaks are per conversation: they neither double-count inside an attempt
    nor vary with how well the cache held.
    """

    def _measured(self, repo, peak):
        class Measured(StubExecutor):
            def run_agent_stage(
                self, stage, prompt, history_dir=None, since_sha="",
                agent_context=None, feedback=None, failure_layer=None, model="",
            ):
                self._apply()
                return ExecutionResult(ok=True, log="", context_tokens=peak)

        return Measured(repo=repo, edits=[("app.py", "stage work\n")])

    def test_a_second_attempt_adds_rather_than_replaces(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, executor=self._measured(repo, 60_000))
        state = with_stage(state, rt)
        state.update(nodes.execute(state, rt))
        assert state["executor_context_tokens"] == 60_000

        rt.executor = self._measured(repo, 9_000)
        state.update(nodes.execute(state, rt))
        assert state["executor_context_tokens"] == 69_000, (
            "a small final attempt replaced the stage's figure"
        )

    def test_it_resets_between_stages(self, repo, tmp_path):
        # Accumulating across attempts is only safe because the per-stage
        # reset clears it; without that a long run would report one
        # monotonically rising number keyed by unrelated merge shas.
        from code_gantry.state import fresh_stage_fields

        assert fresh_stage_fields()["executor_context_tokens"] == 0

    def test_the_attempt_artifact_records_its_own_peak(self, repo, tmp_path):
        """A sum cannot be taken apart afterwards.

        Finding the assignment bug meant inferring per-attempt figures from
        cache writes, because `executor-loop.json` — the per-attempt record —
        did not carry the one number it is about.
        """
        import json

        from code_gantry.executor import _write_loop_record
        from code_gantry.executor import ExecutionResult as ER

        out = ER(ok=True, log="")
        out.context_tokens = 47_000
        _write_loop_record(tmp_path, out)
        written = json.loads((tmp_path / "executor-loop.json").read_text())
        assert written["peak_prompt_tokens"] == 47_000


class TestTheReviewLogLineIsASummary:
    """`run.log` gets counts; the calls themselves are already in two places.

    The reviewer's reads were joined into one `run.log` line with `"; "`, and
    on a large review that is a wall of text — one observed line carried
    nineteen rendered calls including two `semantic_search` queries and a
    two-hundred-character regex, several thousand characters on a single line
    of a timeline meant to be skimmed.

    Nothing is lost by summarising, and that is the point worth checking rather
    than asserting: `reviewer._log_new_calls` already streams every call to
    `tools.log` one per line as it happens, and `review.json` keeps the ordered
    list. So the run log was the third copy, and the only one whose reader
    cannot afford it. The executor's line settled this the same way for the
    same reason, and its comment says so.

    What must survive is the property the line exists for — that a verdict
    reached after reading is distinguishable from one reached from the diff
    alone. A count answers that; the wall of text answered it no better.
    """

    def _review(self, repo, tmp_path, calls, counts=None):
        reviewer = StubReviewer(
            [
                ReviewOutcome(
                    verdict="approved",
                    summary="fine",
                    tool_calls=calls,
                    tool_counts=counts or {},
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        lines = []
        rt.log = lines.append
        nodes.review(state, rt)
        return [ln for ln in lines if "read" in ln and "[review]" in ln]

    def test_it_reports_counts_not_the_calls(self, repo, tmp_path):
        calls = [f"read_file(app/f{i}.rb:1-80) -> 80 line(s)" for i in range(18)]
        calls += ["search(a_very_long_pattern in app/**/*) -> 3 line(s)"]
        found = self._review(
            repo, tmp_path, calls, {"read_file": 18, "search": 1}
        )
        assert found, "the reviewer's reads must still be reported"
        line = found[0]
        assert "18 read_file" in line
        assert "1 search" in line
        assert "a_very_long_pattern" not in line

    def test_the_line_stays_skimmable(self, repo, tmp_path):
        calls = [f"read_file(app/f{i}.rb:1-80) -> 80 line(s)" for i in range(60)]
        line = self._review(repo, tmp_path, calls, {"read_file": 60})[0]
        assert len(line) < 200, f"{len(line)} chars is not a timeline entry"

    def test_a_review_that_read_nothing_says_so_distinguishably(self, repo, tmp_path):
        # The whole reason the line exists: an approval reached from the diff
        # alone and one reached after reading the file it turns on must not
        # read identically.
        assert self._review(repo, tmp_path, []) == []

    def test_the_artifact_still_carries_every_call(self, repo, tmp_path):
        import json

        calls = [f"read_file(app/f{i}.rb:1-80) -> 80 line(s)" for i in range(18)]
        reviewer = StubReviewer(
            [ReviewOutcome(verdict="approved", summary="fine", tool_calls=calls)]
        )
        cfg, rt, state = make(repo, tmp_path, reviewer=reviewer)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("changed\n")
        nodes.review(state, rt)
        written = json.loads(
            next(rt.paths.run_dir.glob("stages/*/review.json")).read_text()
        )
        assert written["tool_calls"] == calls


class TestTheModelIsChosenPerStage:
    """A routing policy resolved once per run, and the run is the wrong unit.

    Measured: one run held `google/gemini-3.7-flash` for 30 stages and
    `z-ai/glm-5.3-flash` for the 11 after it — and the switch was not the
    router changing its mind, it was a human resuming after a planner block.
    So the frequency at which the frontier got re-sampled was set by
    operational accidents: a run that goes forty hours uninterrupted never
    re-asks, one interrupted five times asks five times.

    Per stage follows a price move mid-run, gives every stage one model to
    attribute its cost and its rework to, and lets a model that is serving
    badly stop at the next stage instead of lasting the run.

    What it must not do is change *within* a stage. Measured on the artifacts:
    2 attempts of 468 had two models serving one conversation, and in one of
    them a single foreign turn sat inside 32 of another model's, reading
    nothing of the prefix they had built.

    `resolve_policy` is patched here rather than the probe beneath it: `ask`
    is a default argument bound at definition, so patching the probe does not
    reach it — two tests in the first draft of this class passed without ever
    touching the code they named. What that function does with a concrete
    model, a policy and a failed probe is its own file's business.
    """

    def test_a_policy_is_resolved_at_stage_start(self, repo, tmp_path, monkeypatch):
        cfg, rt, state = make(repo, tmp_path)
        monkeypatch.setattr(
            nodes, "resolve_policy",
            lambda c, log=None: c.model_copy(update={"model": "vendor/concrete-1"}),
        )
        state["current"] = planned_stage()

        out = nodes.precheck(state, rt)
        assert out["stage_executor_model"] == "vendor/concrete-1"

    def test_it_is_not_asked_again_inside_a_stage(self, repo, tmp_path, monkeypatch):
        # The invariant the whole change exists for. `precheck` runs again on a
        # revision that discards the branch, and asking again there would put a
        # second model inside one stage's conversation.
        asked = []

        def counting(c, log=None):
            asked.append(1)
            return c.model_copy(update={"model": f"vendor/pick-{len(asked)}"})

        cfg, rt, state = make(repo, tmp_path)
        monkeypatch.setattr(nodes, "resolve_policy", counting)
        state["current"] = planned_stage()
        first = nodes.precheck(state, rt)

        state["stage_executor_model"] = first["stage_executor_model"]
        state["stage_branch"] = None
        again = nodes.precheck(state, rt)

        assert asked == [1], "the stage was re-routed part-way through"
        assert "stage_executor_model" not in again

    def test_a_new_stage_asks_again(self):
        # The bug this class did not catch on its first draft: the field was
        # set once and never cleared, so `if not state.get(...)` held the first
        # stage's model for the whole run — per-run locking again, keyed on
        # whichever stage happened to be first. Observed live: stage 042
        # resolved, stage 043 cut its branch and never asked.
        from code_gantry.state import fresh_stage_fields

        assert fresh_stage_fields()["stage_executor_model"] == ""

    def test_a_revision_keeps_the_stage_s_model(self, repo, tmp_path, monkeypatch):
        # And the other half, which is why the reset belongs where it is rather
        # than in `precheck`: the two nodes that spread `fresh_stage_fields`
        # are the derive path and the landing, and a revision takes neither.
        # A revision is the same stage, so it keeps the model it was given.
        asked = []
        cfg, rt, state = make(repo, tmp_path)
        monkeypatch.setattr(
            nodes, "resolve_policy",
            lambda c, log=None: asked.append(1) or c.model_copy(
                update={"model": f"vendor/pick-{len(asked)}"}),
        )
        state["current"] = planned_stage()
        state.update(nodes.precheck(state, rt))

        state["revision"] = 1
        state["stage_branch"] = None
        state.update(nodes.precheck(state, rt))

        assert asked == [1]
        assert state["stage_executor_model"] == "vendor/pick-1"

    def test_the_choice_reaches_the_attempt(self, repo, tmp_path):
        # The journey, not its endpoints: this value is set by one node, kept
        # in state, and read by another. Four defects in this codebase have
        # been values computed correctly and lost in transit, and every one
        # passed its unit tests on both ends.
        seen = {}

        class Watching(StubExecutor):
            def run_agent_stage(self, stage, prompt, history_dir=None,
                                since_sha="", agent_context=None, feedback=None,
                                failure_layer=None, model=""):
                seen["model"] = model
                return ExecutionResult(ok=True, log="")

        cfg, rt, state = make(repo, tmp_path, executor=Watching(repo=repo))
        state = with_stage(state, rt)
        state["stage_executor_model"] = "vendor/locked-for-this-stage"

        nodes.execute(state, rt)
        assert seen["model"] == "vendor/locked-for-this-stage"


class TestTheRunScope:
    """A run with a scope draws only inside it: the plan shows the rest as
    out of scope, and a stage citing an outside key is refused for that."""

    def test_the_plan_marks_what_is_outside(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        rt.key_scope = {THE_ITEM}
        text = rt.plan_text()
        assert "**do the other thing** (outside this run's scope)" in text
        assert "**do the thing** (outside" not in text

    def test_a_stage_inside_the_scope_is_drawn(self, repo, tmp_path):
        planner = StubPlanner([PlannerOutcome("next_stage", "next", "e", stage_fields=planned_stage())])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.key_scope = {THE_ITEM}
        out = nodes.plan(state, rt)
        assert out.get("current", {}).get("plan_keys") == [THE_ITEM]

    def test_a_stage_outside_the_scope_is_refused_for_that_reason(self, repo, tmp_path):
        planner = StubPlanner([
            PlannerOutcome("next_stage", "next", "e", stage_fields=planned_stage(plan_keys=[THE_OTHER_ITEM])),
        ])
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        rt.key_scope = {THE_ITEM}
        out = nodes.plan(state, rt)
        assert "outside this run's scope" in json.dumps(out), out
        assert (out.get("current") or {}).get("plan_keys") != [THE_OTHER_ITEM]


class _NoPlanner:
    """A planner that must not be called."""

    def plan(self, messages):
        raise AssertionError("the planner was called")


class TestDerivedStagesInTheLedger:
    """A derivation is recorded before it runs, so a run that dies, or a
    second bay, takes what was drawn rather than drawing it again."""

    def _batch(self):
        return StubPlanner([
            PlannerOutcome(
                "next_stage", "next", "e",
                stage_fields=planned_stage(),
                additional_stage_fields=[planned_stage(id="second", plan_keys=[THE_OTHER_ITEM])],
            )
        ])

    def _make(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, planner=self._batch())
        # `make` takes the stub under the same name as the config section.
        rt.cfg = rt.cfg.model_copy(
            update={"planner": rt.cfg.planner.model_copy(update={"max_batch_stages": 3})}
        )
        return rt.cfg, rt, state

    def _second_run(self, rt, state):
        from dataclasses import replace

        from code_gantry.ledger import open_ledger
        from code_gantry.runtime import RunPaths

        paths = RunPaths(rt.project, "r2")
        paths.ensure()
        other = replace(
            rt, paths=paths, planner=_NoPlanner(),
            ledger=open_ledger(rt.project.ledger, origin="test-host", actor="run:r2"),
        )
        return other, {**state, "run_id": "r2"}

    def test_a_batch_is_recorded_and_the_head_is_taken_at_precheck(self, repo, tmp_path):
        cfg, rt, state = self._make(repo, tmp_path)
        out = nodes.plan(state, rt)
        records = rt.views().derived
        assert [d.status for d in records.values()] == ["derived", "derived"]
        head_id = out["current"]["derived_id"]
        assert records[head_id].stage_id == "extract" and records[head_id].rank == 0
        assert out["stage_queue"][0]["derived_id"] in records
        state = {**state, **out}
        nodes.precheck(state, rt)
        assert rt.views().derived[head_id].status == "taken"
        assert rt.views().derived[head_id].taken_run == "r1"

    def test_a_second_run_takes_the_waiting_stage_instead_of_deriving(self, repo, tmp_path):
        cfg, rt, state = self._make(repo, tmp_path)
        first = {**state, **nodes.plan(state, rt)}
        nodes.precheck(first, rt)
        other, other_state = self._second_run(rt, state)
        out = nodes.plan(other_state, other)
        assert out["next_hop"] == "precheck"
        assert out["current"]["id"] == "second"
        nodes.precheck({**other_state, **out}, other)
        record = other.views().derived[out["current"]["derived_id"]]
        assert record.status == "taken" and record.taken_run == "r2"
        # The first run's own queue no longer offers it.
        promoted = nodes._next_from_queue(first, 0, views=rt.views())
        assert promoted["next_hop"] == "plan"

    def test_nothing_waiting_means_the_planner_is_called(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, planner=StubPlanner([
            PlannerOutcome("next_stage", "next", "e", stage_fields=planned_stage())
        ]))
        out = nodes.plan(state, rt)
        assert out["current"]["id"] == "extract"

    def test_the_landing_closes_the_record(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path, planner=StubPlanner([
            PlannerOutcome("next_stage", "next", "e", stage_fields=planned_stage())
        ]))
        state = {**state, **nodes.plan(state, rt)}
        state = {**state, **nodes.precheck(state, rt)}
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        nodes.advance(state, rt)
        assert rt.views().derived[state["current"]["derived_id"]].status == "done"

    def test_the_planner_lock_is_held_per_ledger(self, repo, tmp_path, monkeypatch):
        locks = tmp_path / "locks"
        monkeypatch.setenv("CODE_GANTRY_LOCK_DIR", str(locks))
        cfg, rt, state = make(repo, tmp_path, planner=StubPlanner([
            PlannerOutcome("next_stage", "next", "e", stage_fields=planned_stage())
        ]))
        nodes.plan(state, rt)
        files = list(locks.glob("planner-*.lock"))
        assert len(files) == 1 and "planner, run r1" in files[0].read_text()


class TestAStageDrawnFromAFinding:
    def _finding(self, rt):
        return rt.ledger.open_finding(keys=[THE_ITEM], by="reviewer", claim="a loose end").finding_id

    def test_it_needs_no_plan_key_and_holds_the_finding(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        fid = self._finding(rt)
        rt.planner = StubPlanner([PlannerOutcome(
            "next_stage", "next", "e", stage_fields=planned_stage(plan_keys=[], resolves=[fid]),
        )])
        out = nodes.plan(state, rt)
        assert out["next_hop"] == "precheck", out
        state = {**state, **out}
        nodes.precheck(state, rt)
        finding = rt.views().findings[fid]
        assert finding.claimed_run == "r1" and finding.claimed_stage == "extract"

    def test_a_finding_another_run_holds_sends_the_stage_back(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        fid = self._finding(rt)
        rt.ledger.append(FINDING_CLAIMED, stage_id="elsewhere", run_id="r9", finding_id=fid, pid=1)
        rt.planner = StubPlanner([PlannerOutcome(
            "next_stage", "next", "e", stage_fields=planned_stage(plan_keys=[], resolves=[fid]),
        )])
        state = {**state, **nodes.plan(state, rt)}
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "plan"
        assert "held by r9" in json.dumps(out)

    def test_an_unconfirmed_finding_is_released_at_landing(self, repo, tmp_path):
        cfg, rt, state = make(repo, tmp_path)
        fid = self._finding(rt)
        rt.planner = StubPlanner([PlannerOutcome(
            "next_stage", "next", "e", stage_fields=planned_stage(plan_keys=[], resolves=[fid]),
        )])
        state = {**state, **nodes.plan(state, rt)}
        state = {**state, **nodes.precheck(state, rt)}
        (repo / "app.py").write_text("stage work\n")
        state = {**state, **nodes.review(state, rt)}
        out = nodes.advance(state, rt)
        assert out["next_hop"] != "escalate"
        finding = rt.views().findings[fid]
        assert finding.status == "open" and finding.claimed_run is None

    def test_drawn_from_nothing_is_refused(self):
        from code_gantry.config import validate_stage

        cfg = parse_config(as_test_tools({
            "target_repo": "/tmp/x", "base_ref": "main", "project_branch": "p", "plan_root": "PLAN.md",
            "full_test_command": "true", "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"}, "reviewer": {"model": "gpt-5.5"},
        }))
        stage = cfg.stage_from_planner(planned_stage(plan_keys=[], resolves=[]))
        problems = validate_stage(stage, cfg, known_keys={THE_ITEM}, open_findings=set())
        assert any("drawn from nothing" in p for p in problems)
