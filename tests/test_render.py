"""What the ledger renders for the planner: stable text, and a bounded projection."""

import pytest

from code_gantry.ledger import BLOCKED, CLAIMED, LANDED, STRUCK, apply_fold, open_ledger
from code_gantry.planmodel import import_documents, parse_markdown
from code_gantry.render import render_plan, render_projection


@pytest.fixture
def led(tmp_path):
    led = open_ledger(tmp_path / "ledger.db", origin="test", actor="test")
    doc = parse_markdown(
        "# Plan\n\nintro\n\n## Routes\n\nabout routes\n\n- [ ] **a.** body a\n- [ ] **b.**\n\n## Later\n\n- [ ] **c.**\n"
    )
    import_documents(led, [doc], prefix="p")
    return led


class TestRenderPlan:
    def test_is_byte_identical_for_equal_views(self, led):
        assert render_plan(led.views()) == render_plan(led.views())

    def test_carries_keys_bodies_and_headings(self, led):
        text = render_plan(led.views())
        assert "# Plan {#p.001}" in text
        assert "## Routes {#p.002}" in text
        assert "- [ ] {#p.003} **a.** body a" in text
        assert "about routes" in text and "intro" in text

    def test_does_not_change_when_a_key_lands_until_a_fold(self, led):
        before = render_plan(led.views())
        led.append(LANDED, key="p.003", sha="abc", evidence="done")
        assert render_plan(led.views()) == before
        apply_fold(led)
        after = render_plan(led.views())
        assert "- [x] {#p.003} ~~**a.**~~ — landed `abc`. done" in after

    def test_flags_human_owned_nodes(self, tmp_path):
        led = open_ledger(tmp_path / "l.db", origin="t")
        import_documents(led, [parse_markdown("# H\n\n- [ ] **needs a person**\n")], prefix="h", owner="human")
        text = render_plan(led.views())
        assert "{#h.002} (human) **needs a person**" in text

    def test_scope_hides_bodies_outside_it(self, led):
        text = render_plan(led.views(), scope={"p.003"})
        assert "**a.** body a" in text
        assert "- [ ] {#p.004} **b.** (outside this run's scope)" in text


class TestRenderProjection:
    def test_empty_when_nothing_has_happened(self, led):
        assert render_projection(led.views(), note_chars=200) == ""

    def test_lists_each_state_by_document_position(self, led):
        led.append(LANDED, key="p.006", sha="ccc", stage_id="stage-c")
        led.append(CLAIMED, key="p.004", stage_id="stage-b", actor="run:1")
        led.append(BLOCKED, key="p.003", question="which colour?")
        text = render_projection(led.views(), note_chars=200)
        assert "### Landed" in text and "- {#p.006} **c.** — `ccc` (stage-c)" in text
        assert "### Claimed" in text and "claimed by run:1, stage `stage-b`" in text
        assert "### Blocked, waiting on a person" in text and "which colour?" in text

    def test_a_folded_landing_leaves_the_projection(self, led):
        led.append(LANDED, key="p.003", sha="abc")
        assert "p.003" in render_projection(led.views(), note_chars=200)
        apply_fold(led)
        assert render_projection(led.views(), note_chars=200) == ""

    def test_open_findings_are_clipped_and_labelled(self, led):
        led.open_finding(keys=["p.003"], by="planner", claim="x" * 1000, needs="human", total="3")
        text = render_projection(led.views(), note_chars=100)
        line = next(l for l in text.splitlines() if l.startswith("- `f-test-"))
        assert "on {#p.003} — by planner (needs a person):" in line
        assert "[3]" in line
        assert len(line) < 250

    def test_the_bound_is_open_keys_times_the_cap(self, led):
        for i in range(20):
            led.open_finding(keys=["p.003"], by="planner", claim="y" * 5000, subject=f"s{i}")
        text = render_projection(led.views(), note_chars=100)
        assert len(text) < 20 * 300

    def test_struck_and_answered_findings_render(self, led):
        led.append(STRUCK, key="p.004", reason="zero population")
        f = led.open_finding(keys=["p.003"], by="reviewer", claim="a claim")
        led.answer_finding(f.finding_id, disposition="debt", text="file it")
        text = render_projection(led.views(), note_chars=200)
        assert "### Struck" in text and "zero population" in text
        assert "### Answered findings not yet folded" in text and "→ debt: file it" in text

    def test_is_byte_identical_for_equal_views(self, led):
        led.append(LANDED, key="p.003", sha="abc")
        assert render_projection(led.views(), note_chars=200) == render_projection(led.views(), note_chars=200)


class TestTheProjectionShowsWhatIsDrawnAndHeld:
    def test_a_waiting_stage_is_listed_and_a_taken_one_is_not(self, led):
        from code_gantry.ledger import STAGE_DERIVED, STAGE_TAKEN

        did = led.append(
            STAGE_DERIVED, stage_id="fix-routes", run_id="r1", fields={"id": "fix-routes"},
            keys=["p.003"], findings=[], batch=None, rank=0,
        ).derived_id
        text = render_projection(led.views(), note_chars=600)
        assert "### Drawn and waiting for a run to take them" in text
        assert f"- `{did}` **fix-routes** on {{#p.003}} — drawn by r1" in text
        led.append(STAGE_TAKEN, run_id="r2", derived_id=did, pid=1)
        assert "Drawn and waiting" not in render_projection(led.views(), note_chars=600)

    def test_a_waiting_stage_outside_the_scope_is_not_listed(self, led):
        from code_gantry.ledger import STAGE_DERIVED

        led.append(
            STAGE_DERIVED, stage_id="fix-routes", run_id="r1", fields={"id": "fix-routes"},
            keys=["p.003"], findings=[], batch=None, rank=0,
        )
        assert "fix-routes" not in render_projection(led.views(), note_chars=600, scope={"p.004"})
        assert "fix-routes" in render_projection(led.views(), note_chars=600, scope={"p.003"})

    def test_a_held_finding_says_who_holds_it(self, led):
        from code_gantry.ledger import FINDING_CLAIMED

        fid = led.open_finding(keys=["p.003"], by="reviewer", claim="a loose end").finding_id
        led.append(FINDING_CLAIMED, run_id="r7", stage_id="s", finding_id=fid, pid=1)
        assert f"- `{fid}` on {{#p.003}} — by reviewer (held by r7): a loose end" in render_projection(led.views(), note_chars=600)
