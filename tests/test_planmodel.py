"""Markdown to plan tree and back, against the real documents."""

from pathlib import Path

import pytest

from code_gantry.ledger import LANDED, STRUCK, open_ledger
from code_gantry.planmodel import (
    export_markdown,
    import_documents,
    parse_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rails5"


@pytest.fixture
def led(tmp_path):
    return open_ledger(tmp_path / "ledger.db", origin="test", actor="test")


def real(name):
    return parse_markdown((FIXTURES / name).read_text(), path=name)


class TestParsing:
    def test_a_title_a_preamble_and_nested_sections(self):
        doc = parse_markdown(
            "# Plan\n\nintro\n\n## A\n\nabout a\n\n### A1\n\n- [ ] **one.** body\n\n## B\n"
        )
        assert doc.title == "Plan" and doc.preamble == "intro"
        assert [s.title for s in doc.sections] == ["A", "B"]
        assert [s.title for s in doc.sections[0].children] == ["A1"]
        (item,) = doc.sections[0].children[0].items
        assert (item.title, item.body, item.checked) == ("one.", "body", False)

    def test_keys_are_read_from_headings_and_items(self):
        doc = parse_markdown("# T {#p.001}\n\n## S {#p.002}\n\n- [ ] {#p.003} **x**\n")
        assert doc.key == "p.001"
        assert doc.sections[0].key == "p.002"
        assert doc.sections[0].items[0].key == "p.003"

    def test_a_wrapped_bold_title_is_one_title(self):
        doc = parse_markdown(
            "## S\n\n- [ ] **`csrf_loader.js#init` installs nothing unless a POST\n"
            "      form happens to share the page.** It gates on a form.\n"
        )
        (item,) = doc.sections[0].items
        assert item.title == "`csrf_loader.js#init` installs nothing unless a POST form happens to share the page."
        assert item.body == "It gates on a form."

    def test_a_closed_item_yields_its_sha_and_evidence(self):
        doc = parse_markdown("## S\n\n- [x] ~~**done.**~~ — `5d2d05bcb`. It works\n      now.\n")
        (item,) = doc.sections[0].items
        assert item.checked and item.mark.sha == "5d2d05bcb"
        assert item.mark.evidence == "`5d2d05bcb`. It works now."
        assert item.body == ""

    def test_a_struck_item_yields_its_reason(self):
        doc = parse_markdown("## S\n\n- [x] ~~**gone.**~~ — **STRUCK: zero population.** Nobody reads it.\n")
        (item,) = doc.sections[0].items
        assert item.mark.struck == "zero population"
        assert item.mark.sha is None
        assert item.mark.evidence == "Nobody reads it."

    def test_items_before_any_section_belong_to_the_document(self):
        doc = parse_markdown("# T\n\n- [ ] **a**\n- [ ] **b**\n\n## S\n\n- [ ] **c**\n")
        assert [i.title for i in doc.items] == ["a", "b"]
        assert [i.title for i in doc.all_items()] == ["a", "b", "c"]

    def test_fenced_code_is_prose_not_structure(self):
        doc = parse_markdown("## S\n\n```\n## not a heading\n- [ ] not an item\n```\n")
        assert len(doc.sections) == 1 and doc.sections[0].items == []
        assert "## not a heading" in doc.sections[0].body

    def test_prose_after_items_is_kept_with_the_section(self):
        doc = parse_markdown("## S\n\n- [ ] **a**\n\nafterwards\n")
        assert doc.sections[0].trailing == "afterwards"

    @pytest.mark.parametrize(
        "name, sections, items, checked, struck",
        [("PLAN.md", 5, 13, 13, 5), ("technical_debt.md", 16, 95, 86, 4)],
    )
    def test_the_real_documents_parse_to_their_known_counts(self, name, sections, items, checked, struck):
        doc = real(name)
        all_items = doc.all_items()
        assert len(doc.all_sections()) == sections
        assert len(all_items) == items
        assert sum(i.checked for i in all_items) == checked
        assert sum(i.mark.struck is not None for i in all_items) == struck
        assert all(i.title for i in all_items)


class TestImport:
    def test_assigns_keys_in_document_order_and_records_marks(self, led):
        doc = parse_markdown("# T\n\n## S\n\n- [x] ~~**a**~~ — `abc1234`. done\n- [ ] **b**\n- [x] ~~**c**~~ — **STRUCK: moot.**\n")
        counts = import_documents(led, [doc], prefix="p")
        assert counts == {"document": 1, "section": 1, "item": 3, "landed": 1, "struck": 1}
        views = led.views()
        assert [n.key for n in views.walk()] == ["p.001", "p.002", "p.003", "p.004", "p.005"]
        assert views.state("p.003").state == "landed" and views.state("p.003").sha == "abc1234"
        assert views.state("p.004").state == "open"
        assert views.state("p.005").state == "struck"
        assert views.state("p.005").reason == "moot"

    def test_existing_keys_are_kept_and_new_ones_continue_the_sequence(self, led):
        doc = parse_markdown("# T {#p.001}\n\n## S {#p.007}\n\n- [ ] **a**\n")
        import_documents(led, [doc], prefix="p")
        assert sorted(led.views().nodes) == ["p.001", "p.007", "p.008"]

    def test_a_second_import_does_not_reland_or_renumber(self, led):
        doc = parse_markdown("# T\n\n## S\n\n- [x] ~~**a**~~ — `abc1234`.\n")
        import_documents(led, [doc], prefix="p")
        again = parse_markdown(export_markdown(led.views(), "p.001"))
        counts = import_documents(led, [again], prefix="p")
        assert counts["landed"] == 0
        assert sorted(led.views().nodes) == ["p.001", "p.002", "p.003"]
        assert sum(e.kind == LANDED for e in led.events()) == 1

    def test_owner_and_blocking_travel_to_every_node(self, led):
        doc = parse_markdown("# T\n\n## S\n\n- [ ] **a**\n")
        import_documents(led, [doc], prefix="h", owner="human", blocking=True)
        assert all(n.owner == "human" and n.blocking for n in led.views().nodes.values())

    def test_the_real_documents_round_trip(self, led):
        docs = [real("PLAN.md"), real("technical_debt.md")]
        counts = import_documents(led, docs, prefix="r5")
        assert counts["item"] == 108 and counts["landed"] + counts["struck"] == 99
        views = led.views()
        for original in docs:
            again = parse_markdown(export_markdown(views, original.key))
            assert [i.title for i in again.all_items()] == [i.title for i in original.all_items()]
            assert [s.title for s in again.all_sections()] == [s.title for s in original.all_sections()]
            assert [(i.checked, i.mark.sha, i.mark.struck) for i in again.all_items()] == [
                (i.checked, i.mark.sha, i.mark.struck) for i in original.all_items()
            ]
            assert all(i.key for i in again.all_items())
        assert sum(e.kind in (LANDED, STRUCK) for e in led.events()) == 99
