"""Markdown links a plan document makes, for `plan import --follow-links`."""

from code_gantry.plandoc import extract_links


class TestExtractLinks:
    def test_finds_markdown_links(self):
        assert extract_links("see [child](child.md) for detail") == ["child.md"]

    def test_ignores_external_urls(self):
        assert extract_links("[docs](https://example.com/a.md)") == []

    def test_ignores_anchors(self):
        assert extract_links("[section](#later)") == []

    def test_strips_an_anchor_from_a_document_link(self):
        assert extract_links("[child](child.md#stage-2)") == ["child.md"]

    def test_ignores_non_markdown_targets(self):
        # A link to a .rb file is a code reference, not a plan child.
        assert extract_links("[the model](app/models/order.rb)") == []

    def test_deduplicates(self):
        assert extract_links("[a](c.md) and again [b](c.md)") == ["c.md"]

    def test_preserves_document_order(self):
        links = extract_links("[b](b.md)\n[a](a.md)")
        assert links == ["b.md", "a.md"]
