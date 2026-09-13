"""A plan note names its key, its subject, who can act on it — and opens a finding.

The planner reads the plan against the code and finds three kinds of thing:
a step further along than the plan says, a plan that was wrong when written,
and a defect in code this plan is not about. All three open a finding in the
ledger, keyed to a plan item. `needs` decides who can act on it: `pipeline`
findings are shown to every later derivation; `human` findings are queued for
a person and left intact until answered.

Every field is required. An optional field with a conditional trigger is
declined in good conscience; a question with a true answer for every note is
answered every time.
"""

import pytest
from pydantic import ValidationError

from code_gantry.ledger import open_ledger
from code_gantry.nodes import open_findings
from code_gantry.planmodel import import_documents, parse_markdown


def note(kind="progress", **over):
    base = {
        "kind": kind,
        "key": "p.002",
        "subject": "remaining sites",
        "total": "7 sites in 1 controller",
        "needs": "pipeline",
        "finding": "7 of 24 remain",
        "observation": "Seven sites remain, all inline renders.",
    }
    base.update(over)
    return base


@pytest.fixture
def led(tmp_path):
    led = open_ledger(tmp_path / "ledger.db", origin="t", actor="t")
    import_documents(led, [parse_markdown("# Plan\n\n- [ ] **do the thing**\n")], prefix="p")
    return led


class FakeGit:
    def head_sha(self):
        return "abc1234def"


class TestSchema:
    def _build(self, **over):
        from code_gantry.planner import PlanNote

        return PlanNote(**note(**over))

    @pytest.mark.parametrize("field", ["kind", "key", "subject", "total", "needs", "observation"])
    def test_every_field_is_required(self, field):
        from code_gantry.planner import PlanNote

        fields = note()
        del fields[field]
        with pytest.raises(ValidationError):
            PlanNote(**fields)

    def test_kind_rejects_anything_unlisted(self):
        with pytest.raises(ValidationError):
            self._build(kind="interesting")

    def test_the_three_kinds_are_accepted(self):
        for kind in ("progress", "correction", "out_of_scope"):
            assert self._build(kind=kind).kind == kind

    def test_needs_is_pipeline_or_human(self):
        for needs in ("pipeline", "human"):
            assert self._build(needs=needs).needs == needs
        with pytest.raises(ValidationError):
            self._build(needs="someone")

    def test_there_is_no_quoted_anchor_any_more(self):
        from code_gantry.planner import PlanNote

        assert "anchor" not in PlanNote.model_fields
        assert "plan_path" not in PlanNote.model_fields


class TestRouting:
    """Every note opens a finding; `needs` decides who sees it first."""

    def test_a_note_opens_a_finding_on_its_key(self, led):
        opened = open_findings(led, FakeGit(), [note()], by="planner", stage_id="s1", run_id="r1")
        assert opened == 1
        (finding,) = led.views().open_findings()
        assert finding.keys == ["p.002"]
        assert finding.by == "planner" and finding.needs == "pipeline"
        assert finding.total == "7 sites in 1 controller"
        assert finding.subject == "remaining sites"
        assert "7 of 24 remain" in finding.claim and "Seven sites remain" in finding.claim
        assert (finding.at_sha, finding.stage_id, finding.run_id) == ("abc1234def", "s1", "r1")

    def test_a_key_written_as_its_plan_marker_is_the_key(self, led):
        # The plan renders a key as `{#p.002}`, and the schema told the
        # planner to copy the marker exactly; every finding of one project
        # was filed keyless because the braces were looked up as the key.
        opened = open_findings(led, FakeGit(), [note(key="{#p.002}"), note(key=" #p.002 ", subject="another")], by="planner", stage_id="s1", run_id="r1")
        assert opened == 2
        assert [f.keys for f in led.views().open_findings()] == [["p.002"], ["p.002"]]

    def test_a_human_finding_is_queued_intact(self, led):
        open_findings(led, FakeGit(), [note("out_of_scope", needs="human")], by="planner", stage_id="s1", run_id="r1")
        (finding,) = led.views().open_findings()
        assert finding.needs == "human" and finding.status == "open"

    def test_a_total_of_none_is_no_total(self, led):
        open_findings(led, FakeGit(), [note(total="none")], by="planner", stage_id="s1", run_id="r1")
        (finding,) = led.views().open_findings()
        assert finding.total is None

    def test_an_unknown_key_is_filed_without_one_and_said_so(self, led):
        said = []
        open_findings(led, FakeGit(), [note(key="p.999")], by="planner", stage_id="s1", run_id="r1", log=said.append)
        (finding,) = led.views().open_findings()
        assert finding.keys == []
        assert any("p.999" in line for line in said)

    def test_a_later_reading_of_the_same_subject_supersedes(self, led):
        open_findings(led, FakeGit(), [note(total="7")], by="planner", stage_id="s1", run_id="r1")
        open_findings(led, FakeGit(), [note(total="3", observation="Three remain.")], by="planner", stage_id="s2", run_id="r1")
        open_ones = led.views().open_findings()
        assert len(open_ones) == 1 and open_ones[0].total == "3"

    def test_no_ledger_opens_nothing(self):
        assert open_findings(None, FakeGit(), [note()], by="planner", stage_id="s", run_id="r") == 0


class TestCorrectionNamesADependencyError:
    """The most useful correction is a wrong prerequisite, in either direction."""

    def _correction_text(self) -> str:
        from code_gantry.planner import PlanNote

        return PlanNote.model_fields["kind"].description.lower()

    def test_it_names_a_wrong_dependency(self):
        text = self._correction_text()
        assert "depend" in text or "prerequisite" in text

    def test_it_covers_both_directions(self):
        text = self._correction_text()
        assert "independent" in text or "does not" in text or "no such" in text

    def test_the_other_two_kinds_are_untouched(self):
        text = self._correction_text()
        assert "progress" in text and "out_of_scope" in text

    def test_it_names_no_project_vocabulary(self):
        text = self._correction_text()
        for word in ("rails", "ruby", "gem", "prototype", "rspec", "ujs", ".rb"):
            assert word not in text, f"{word!r} is project knowledge in a schema"
