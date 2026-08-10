"""A plan note declares what kind of thing it is, and one kind never reaches the run.

The planner reads the plan against the code and finds three different things.
Two of them are about the plan — a step is further along than the plan says, or
the plan was wrong when it was written — and belong in the progress log, which
is spliced live into every planner prompt and folded into the documents later.

The third is a defect in the code that this plan is not about. Measured over 926
notes on one project, 46 of them said so in their own prose: "classified
pre-existing, so it belongs in the general technical debt plan". The planner was
already routing them, and it had nowhere to route them *to* — so they landed in
the log with everything else and were read back on every subsequent derivation
until the next fold. That is the whole of the scope-creep mechanism: a finding
about work nobody asked for, arriving in the prompt that decides what work to
do next.

Classifying at fold time cannot fix it, because the damage happens in the hours
before the fold. So the kind is declared where the evidence is, and `advance`
routes on it: in-scope notes to the log, out-of-scope findings to a file in the
work directory that nothing reads back.

`kind` is required rather than optional. `observations` was optional with a
conditional trigger and came back empty 278 times out of 278; a question that
has a true answer for every note does not get declined.
"""

from pathlib import Path

import pytest

from orchestrator.addendum import append_findings, append_notes


def note(kind="progress", **over):
    base = {
        "kind": kind,
        "plan_path": "docs/plan.md",
        "anchor": "convert the remaining sites",
        "finding": "7 of 24 remain",
        "observation": "Seven sites remain, all inline renders.",
    }
    base.update(over)
    return base


class TestSchema:
    def test_kind_is_required(self):
        from pydantic import ValidationError

        from orchestrator.planner import PlanNote

        with pytest.raises(ValidationError):
            PlanNote(
                plan_path="docs/plan.md",
                anchor="a",
                observation="b",
            )

    def test_kind_rejects_anything_unlisted(self):
        from pydantic import ValidationError

        from orchestrator.planner import PlanNote

        with pytest.raises(ValidationError):
            PlanNote(
                kind="interesting",
                plan_path="docs/plan.md",
                anchor="a",
                observation="b",
            )

    def test_the_three_kinds_are_accepted(self):
        from orchestrator.planner import PlanNote

        for kind in ("progress", "correction", "out_of_scope"):
            assert PlanNote(
                kind=kind, plan_path="docs/plan.md", anchor="a", observation="b"
            ).kind == kind


class TestRouting:
    """The log takes two kinds; the findings file takes the third."""

    def test_out_of_scope_never_reaches_the_progress_log(self, tmp_path):
        (tmp_path / "docs").mkdir()
        written = append_notes(
            tmp_path,
            "docs/progress.md",
            [note("out_of_scope", observation="A GET mutates in checkout.")],
            stage_id="s1",
        )
        assert written is None
        assert not (tmp_path / "docs" / "progress.md").exists()

    def test_in_scope_kinds_still_reach_the_log(self, tmp_path):
        (tmp_path / "docs").mkdir()
        written = append_notes(
            tmp_path,
            "docs/progress.md",
            [note("progress"), note("correction", observation="The plan names a gone file.")],
            stage_id="s1",
        )
        assert written is not None
        body = written.read_text()
        assert "Seven sites remain" in body
        assert "gone file" in body

    def test_a_mixed_batch_is_split_rather_than_dropped(self, tmp_path):
        (tmp_path / "docs").mkdir()
        written = append_notes(
            tmp_path,
            "docs/progress.md",
            [note("progress"), note("out_of_scope", observation="Unrelated defect.")],
            stage_id="s1",
        )
        body = written.read_text()
        assert "Seven sites remain" in body
        assert "Unrelated defect" not in body


class TestFindingsFile:
    def test_writes_to_the_top_of_the_work_directory(self, tmp_path):
        target = append_findings(
            tmp_path, [note("out_of_scope", observation="A GET mutates.")], stage_id="s1"
        )
        assert target == tmp_path / "findings.md"
        assert "A GET mutates." in target.read_text()

    def test_names_the_stage_that_found_it(self, tmp_path):
        target = append_findings(tmp_path, [note("out_of_scope")], stage_id="checkout-specs")
        assert "checkout-specs" in target.read_text()

    def test_appends_rather_than_replacing(self, tmp_path):
        append_findings(tmp_path, [note("out_of_scope", observation="First.")], stage_id="a")
        target = append_findings(
            tmp_path, [note("out_of_scope", observation="Second.")], stage_id="b"
        )
        body = target.read_text()
        assert "First." in body and "Second." in body

    def test_nothing_to_write_writes_nothing(self, tmp_path):
        assert append_findings(tmp_path, [], stage_id="a") is None
        assert not (tmp_path / "findings.md").exists()

    def test_the_findings_file_is_not_a_plan_document(self, tmp_path):
        """It lives in the work dir, which is gitignored and never read back.

        The point of the split is that a planner prompt cannot contain these.
        """
        from orchestrator.config import parse_config

        cfg = parse_config(
            {
                "target_repo": str(tmp_path),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "docs/plan.md",
                "plan_addendum_path": "docs/progress.md",
                "test_command": "true",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        assert Path(cfg.work_dir).is_relative_to(cfg.target_repo)
        assert "findings.md" not in str(cfg.plan_addendum_path)
