"""The cost section: what each role spent, at which model and which effort.

Two things were missing and only one of them was money. The report named the
model but never the reasoning effort, so a later reader comparing two runs had
to date them against `git log` on the config to know what produced the numbers
— a label inferred rather than recorded, about the one variable the comparison
is for.

The dollars come from the public rate table, so the figures in
this section and the ones in `stage-costs.md` cannot disagree about what a
token costs. An unpriced model reports as unpriced and never as $0.00: an
own accounting conflates those two, which is how a local endpoint and a missing
rate came to look identical.
"""

import json

import pytest

from code_gantry.config import parse_config
from code_gantry.report import build_report


PRICES = {
    "claude-opus-5": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 2.5e-05,
        "cache_read_input_token_cost": 5e-07,
        "cache_creation_input_token_cost": 6.25e-06,
    },
    "gpt-5.6-sol": {
        "input_cost_per_token": 5e-06,
        "output_cost_per_token": 3e-05,
        "cache_read_input_token_cost": 5e-07,
    },
}


def cfg_with(**over):
    data = {
        "target_repo": "/tmp/x",
        "project_branch": "work",
        "plan_root": "PLAN.md",
        "full_test_command": "pytest -q",
        "planner": {"model": "claude-opus-5", "effort": "xhigh"},
        "executor": {"model": "openai/gpt-5.6-luna", "reasoning_effort": "max"},
        "reviewer": {"model": "gpt-5.6-sol", "effort": "max"},
    }
    data.update(over)
    return parse_config(data)


def state_with(**usage):
    base = {
        "prompt_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0,
        "completion_tokens": 0, "planner_prompt_tokens": 0,
        "planner_cached_tokens": 0, "planner_cache_write_tokens": 0,
        "planner_completion_tokens": 0,
    }
    base.update(usage)
    return {"completed": [], "run_usage": base}


@pytest.fixture
def prices(tmp_path, monkeypatch):
    path = tmp_path / "model-prices.json"
    path.write_text(json.dumps(PRICES))
    monkeypatch.setenv("CODE_GANTRY_PRICE_MAP", str(path))
    return path


class TestTheCostSectionRecordsWhatProducedIt:
    def test_effort_is_named_beside_the_model(self, prices):
        out = build_report(state_with(), cfg_with())
        assert "claude-opus-5, effort xhigh" in out
        assert "gpt-5.6-sol, effort max" in out

    def test_planner_dollars_are_priced_from_the_table(self, prices):
        out = build_report(
            state_with(
                planner_prompt_tokens=1_000_000,
                planner_cached_tokens=0,
                planner_cache_write_tokens=0,
                planner_completion_tokens=1_000_000,
            ),
            cfg_with(),
        )
        # 1M in at $5 + 1M out at $25.
        assert "$30.00" in out

    def test_cache_reads_are_charged_at_the_cache_rate(self, prices):
        cheap = build_report(
            state_with(planner_prompt_tokens=1_000_000,
                       planner_cached_tokens=1_000_000),
            cfg_with(),
        )
        dear = build_report(
            state_with(planner_prompt_tokens=1_000_000), cfg_with()
        )
        assert "$0.50" in cheap
        assert "$5.00" in dear

    def test_cache_writes_cost_more_than_uncached_input(self, prices):
        # The whole reason the key was threaded through state. If writes were
        # folded into the uncached remainder this would read $5.00.
        out = build_report(
            state_with(planner_prompt_tokens=1_000_000,
                       planner_cache_write_tokens=1_000_000),
            cfg_with(),
        )
        assert "$6.25" in out

    def test_an_unpriced_model_says_so_rather_than_zero(self, prices):
        # And the same report shows the reviewer at $0.00, which is the whole
        # point of the distinction: that model *is* priced and spent nothing,
        # which is a different fact from having no rate. An accounting layer
        # renders both as zero.
        out = build_report(
            state_with(planner_prompt_tokens=1_000_000),
            cfg_with(planner={"model": "some-local-model", "effort": "xhigh"}),
        )
        planner_line = next(
            line for line in out.splitlines()
            if line.startswith("- Estimated cost")
            and "not priced" in line
        )
        assert "`some-local-model`" in planner_line
        assert "- Estimated cost: $0.00" in out

    def test_a_missing_price_file_does_not_break_the_report(self, tmp_path, monkeypatch):
        # The report is written at the end of every run, including runs that
        # failed. It must not be the thing that raises.
        monkeypatch.setenv("CODE_GANTRY_PRICE_MAP", str(tmp_path / "absent.json"))
        monkeypatch.setenv("LITELLM_MODEL_COST_MAP_URL", "http://127.0.0.1:9/none")
        out = build_report(state_with(planner_prompt_tokens=10), cfg_with())
        assert "## Cost" in out


class TestTheExecutorsCacheRateIsReported:
    """Computable per attempt is not the same as visible for the run.

    The executor's usage reached `executor-loop.json` and stopped there, so a
    cache hit rate could be worked out one attempt at a time and never for the
    run — which is the shape of every value this codebase has lost crossing a
    schema. The section appears only when there is something to report, so the
    subprocess path, which has no real counts, still renders as it did.
    """

    def test_it_appears_with_a_hit_rate_when_the_executor_reported_usage(self):
        from code_gantry.report import _cost_section

        state = state_with(
            executor_prompt_tokens=100_000,
            executor_cached_tokens=90_000,
            executor_completion_tokens=5_000,
        )
        text = "\n".join(_cost_section(state, cfg_with()))
        assert "**Executor**" in text
        assert "90,000 cached, 90%" in text
        assert "Uncached prompt tokens: 10,000" in text

    def test_it_is_absent_when_nothing_was_reported(self):
        from code_gantry.report import _cost_section

        text = "\n".join(_cost_section(state_with(), cfg_with()))
        assert "**Executor**" not in text


class TestTheBudgetProjectionDoesNotDemandAChange:
    """`wall_clock_hours` and `max_stages` measure different things.

    The report compared them and concluded "the two limits disagree about how
    big this project is; one of them needs raising" — telling the operator to
    change a setting that is correctly set. `wall_clock_hours` bounds one
    *unattended stretch*: how long the operator is willing to let the run go
    without looking at it. `max_stages` bounds the *project*. A run that stops
    on the clock and is resumed is the designed behaviour, not a
    misconfiguration, and every long project will trip this arithmetic.

    The projection is still worth printing — how many sessions the work implies
    is a real fact — so what changes is the conclusion drawn from it.
    """

    def _report(self, tmp_path, per_stage_hours, budget, max_stages):
        from code_gantry.config import parse_config
        from code_gantry.report import build_report

        cfg = parse_config({
            "target_repo": str(tmp_path), "base_ref": "main",
            "project_branch": "p", "plan_root": "PLAN.md", "full_test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
            "limits": {"wall_clock_hours": budget, "max_stages": max_stages},
        })
        state = {
            "status": "complete", "run_id": "r", "session_seconds": 3600.0,
            "completed": [
                {"id": "s1", "wall_seconds": per_stage_hours * 3600.0,
                 "plan_seconds": 0.0, "merge_sha": "abc", "review_verdict": "approved"}
            ],
        }
        return build_report(state, cfg)

    def test_it_does_not_say_a_limit_needs_raising(self, tmp_path):
        text = self._report(tmp_path, per_stage_hours=0.25, budget=12, max_stages=1000)
        assert "needs raising" not in text
        assert "disagree" not in text

    def test_it_still_reports_what_the_pace_implies(self, tmp_path):
        # The arithmetic is useful; only the conclusion was wrong.
        text = self._report(tmp_path, per_stage_hours=0.25, budget=12, max_stages=1000)
        assert "0.25h per landed stage" in text

    def test_it_frames_the_budget_as_a_supervision_window(self, tmp_path):
        text = self._report(tmp_path, per_stage_hours=0.25, budget=12, max_stages=1000)
        assert "session" in text.lower()
        assert "resume" in text.lower()
