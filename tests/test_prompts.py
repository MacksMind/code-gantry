"""Prompt construction.

Two things are load-bearing here. The executor prompt must carry the stage's
constraints and scope, because the executor only ever sees one stage and
cannot otherwise know that a correct-looking edit is illegal in this one. The
review prompt's *order* is the caching strategy: stable payload first,
stage-specific diff last.
"""

from orchestrator.config import parse_config
from orchestrator.prompts import build_executor_prompt, build_review_messages


def cfg_with(stage_overrides=None, **cfg_overrides):
    stage = {"id": "s1", "instruction": "Extract the service object.", "edit_files": ["app/**"]}
    stage.update(stage_overrides or {})
    data = {
        "target_repo": "/tmp/x",
        "branch": "work",
        "test_command": "pytest",
        "executor": {"model": "m"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": [stage],
    }
    data.update(cfg_overrides)
    return parse_config(data)


class TestExecutorPromptFirstAttempt:
    def test_includes_the_instruction(self):
        cfg = cfg_with()
        prompt = build_executor_prompt(cfg.stages[0], cfg)
        assert "Extract the service object." in prompt

    def test_includes_the_editable_scope(self):
        cfg = cfg_with()
        prompt = build_executor_prompt(cfg.stages[0], cfg)
        assert "app/**" in prompt

    def test_includes_constraints(self):
        # The executor needs these, not just the reviewer. Finding out at
        # review time that an API was illegal wastes a whole attempt.
        cfg = cfg_with(stage_overrides={"constraints": "Must remain valid on Rails 4.2."})
        prompt = build_executor_prompt(cfg.stages[0], cfg)
        assert "Rails 4.2" in prompt

    def test_includes_forbidden_patterns_as_prose(self):
        # The regex gate is deterministic, but telling the executor up front
        # is cheaper than letting it fail and retry.
        cfg = cfg_with(stage_overrides={"forbidden_patterns": [r"optional:\s*true"]})
        prompt = build_executor_prompt(cfg.stages[0], cfg)
        assert r"optional:\s*true" in prompt

    def test_includes_acceptance_criteria_when_present(self):
        cfg = cfg_with(stage_overrides={"acceptance": "A package that imports cleanly."})
        prompt = build_executor_prompt(cfg.stages[0], cfg)
        assert "imports cleanly" in prompt

    def test_asks_for_tests_first_when_required(self):
        cfg = cfg_with(stage_overrides={"require_new_tests": True})
        prompt = build_executor_prompt(cfg.stages[0], cfg)
        assert "test" in prompt.lower()

    def test_omits_rework_framing_on_a_first_attempt(self):
        cfg = cfg_with()
        prompt = build_executor_prompt(cfg.stages[0], cfg)
        assert "rejected" not in prompt.lower()

    def test_includes_context_command_output(self):
        cfg = cfg_with()
        prompt = build_executor_prompt(
            cfg.stages[0], cfg, context=[("bin/inventory", "index\nshow\ncreate")]
        )
        assert "bin/inventory" in prompt
        assert "create" in prompt


class TestExecutorPromptRework:
    def test_states_that_a_previous_attempt_was_rejected(self):
        cfg = cfg_with()
        prompt = build_executor_prompt(cfg.stages[0], cfg, feedback=["Missing edge case."])
        assert "rejected" in prompt.lower()

    def test_includes_the_feedback(self):
        cfg = cfg_with()
        prompt = build_executor_prompt(cfg.stages[0], cfg, feedback=["Missing edge case."])
        assert "Missing edge case." in prompt

    def test_still_includes_the_original_instruction(self):
        # A fresh invocation carries no conversation history, so the
        # instruction must be restated in full.
        cfg = cfg_with()
        prompt = build_executor_prompt(cfg.stages[0], cfg, feedback=["x"])
        assert "Extract the service object." in prompt

    def test_accumulates_multiple_rounds_of_feedback(self):
        cfg = cfg_with()
        prompt = build_executor_prompt(cfg.stages[0], cfg, feedback=["First round.", "Second round."])
        assert "First round." in prompt
        assert "Second round." in prompt


class TestReviewMessagesOrdering:
    def build(self, **kw):
        cfg = cfg_with(
            stage_overrides={"constraints": "Rails 4.2 only."},
            stages=[
                {"id": "s1", "instruction": "Extract the service object.", "edit_files": ["app/**"],
                 "constraints": "Rails 4.2 only."},
                {"id": "s2", "instruction": "Introduce a result type.", "edit_files": ["app/**"]},
            ],
        )
        return cfg, build_review_messages(
            stage=cfg.stages[0],
            cfg=cfg,
            diff="--- a/app/x.rb\n+++ b/app/x.rb\n+UNIQUE_DIFF_TOKEN\n",
            documents=kw.get("documents", [("docs/plan.md", "THE PLAN BODY")]),
        )

    def test_returns_messages_for_the_chat_api(self):
        _, messages = self.build()
        assert all("role" in m and "content" in m for m in messages)
        assert messages[0]["role"] == "system"

    def test_reference_documents_come_before_the_diff(self):
        # Prefix caching only helps if the stable payload is first. Reordering
        # these for readability silently doubles the cost of every review.
        _, messages = self.build()
        joined = [m["content"] for m in messages]
        doc_index = next(i for i, c in enumerate(joined) if "THE PLAN BODY" in c)
        diff_index = next(i for i, c in enumerate(joined) if "UNIQUE_DIFF_TOKEN" in c)
        assert doc_index < diff_index

    def test_the_stable_prefix_contains_no_stage_specific_content(self):
        # If the diff leaked into an early message the prefix would change
        # every stage and never hit cache.
        _, messages = self.build()
        prefix = "".join(m["content"] for m in messages[:-1])
        assert "UNIQUE_DIFF_TOKEN" not in prefix

    def test_all_stages_are_listed_in_the_stable_prefix(self):
        # Cross-stage drift is what this reviewer exists to catch.
        _, messages = self.build()
        prefix = "".join(m["content"] for m in messages[:-1])
        assert "Introduce a result type." in prefix

    def test_stable_prefix_is_identical_across_stages_of_a_run(self):
        cfg, messages_one = self.build()
        second = build_review_messages(
            stage=cfg.stages[1],
            cfg=cfg,
            diff="different diff",
            documents=[("docs/plan.md", "THE PLAN BODY")],
        )
        assert [m["content"] for m in messages_one[:-1]] == [
            m["content"] for m in second[:-1]
        ]


class TestReviewMessagesContent:
    def messages(self, stage_overrides=None, documents=None):
        cfg = cfg_with(stage_overrides=stage_overrides)
        return build_review_messages(
            stage=cfg.stages[0],
            cfg=cfg,
            diff="+added line\n",
            documents=documents or [],
        )

    def test_includes_the_current_instruction(self):
        content = "".join(m["content"] for m in self.messages())
        assert "Extract the service object." in content

    def test_constraints_are_framed_as_reject_criteria(self):
        content = "".join(
            m["content"] for m in self.messages(stage_overrides={"constraints": "Rails 4.2 only."})
        )
        assert "Rails 4.2 only." in content
        assert "reject" in content.lower()

    def test_asks_for_cross_stage_drift(self):
        content = "".join(m["content"] for m in self.messages())
        assert "earlier" in content.lower()

    def test_explains_the_blocked_verdict(self):
        # Without this the model has no reason to ever use it, and a wrong
        # stage instruction gets reworked forever instead of escalating.
        content = "".join(m["content"] for m in self.messages())
        assert "blocked" in content.lower()

    def test_includes_named_reference_documents(self):
        content = "".join(
            m["content"] for m in self.messages(documents=[("docs/plan.md", "BODY")])
        )
        assert "docs/plan.md" in content
        assert "BODY" in content

    def test_includes_acceptance_criteria_for_greenfield(self):
        content = "".join(
            m["content"]
            for m in self.messages(stage_overrides={"acceptance": "Ships with tests."})
        )
        assert "Ships with tests." in content

    def test_includes_the_diff(self):
        content = "".join(m["content"] for m in self.messages())
        assert "+added line" in content
