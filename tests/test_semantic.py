"""Semantic search over the target repository, for the planner.

`git grep` answers "where does this exact string appear". It cannot answer
"where is the CSV feed generated" when the code says `feed` and never says CSV.
Both questions came up in the first long run and only one was answerable.

This talks to Qdrant and to an OpenAI-compatible embeddings endpoint directly
rather than through the project's `semantic-search` MCP server. Reading that
server showed it to be a wrapper over exactly two HTTP calls — embed the query,
search the collection — and the orchestrator is a Python tool with no Node
dependency. Adding `npx`, a subprocess and a JSON-RPC client to reach two
endpoints it can already reach would buy nothing and cost a moving part.

The index is built by the target project's own `bin/reindex`, which indexes
`git ls-files` only, so the tracked-only boundary that keeps `.agent.env` out of
planner context holds here too. A post-commit hook reindexes incrementally, so
it tracks HEAD rather than drifting.

What it returns is a *lead*, not a fact: a path and a line range. Chunks can be
stale between a commit and the hook finishing, and a nearest-neighbour hit is
not evidence. The planner is told to confirm with `read_file` before asserting
anything, which is safe precisely because every hit carries `path` and
`start_line`/`end_line`.
"""

import json

import pytest

from orchestrator.semantic import SemanticSearch, SemanticSearchConfig


class FakeHttp:
    """Records requests and replays canned responses."""

    def __init__(self, embedding=None, hits=None, fail=None):
        self.embedding = embedding or [0.1] * 8
        self.hits = hits if hits is not None else []
        self.fail = fail
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append((url, payload))
        if self.fail:
            raise self.fail
        if url.endswith("/embeddings"):
            return {"data": [{"embedding": self.embedding}]}
        return {"result": self.hits}


def hit(path, start, end, score=0.8, symbol="feed", kind="method"):
    return {
        "score": score,
        "payload": {
            "path": path,
            "source": f"{path}:{start}-{end}",
            "start_line": start,
            "end_line": end,
            "symbol": symbol,
            "symbol_type": kind,
            "content": "def feed\n  render body: csv\nend\n",
        },
    }


def search(http, **over):
    cfg = SemanticSearchConfig(
        api_base="http://endpoint/v1",
        qdrant_url="http://qdrant:6333",
        embedding_model="qwen3-embedding",
        collection="proj-v1",
        **over,
    )
    return SemanticSearch(cfg, http=http)


class TestQuerying:
    def test_embeds_the_query_then_searches_the_collection(self):
        http = FakeHttp(hits=[hit("app/controllers/godata_controller.rb", 4, 65)])
        out = search(http).query("where is the CSV product feed built")
        embed_url, embed_payload = http.calls[0]
        assert embed_url == "http://endpoint/v1/embeddings"
        assert embed_payload["model"] == "qwen3-embedding"
        assert embed_payload["input"] == ["where is the CSV product feed built"]
        qdrant_url, qdrant_payload = http.calls[1]
        assert qdrant_url == "http://qdrant:6333/collections/proj-v1/points/search"
        assert qdrant_payload["with_payload"] is True
        assert out

    def test_results_carry_a_citable_location(self):
        # The whole design rests on this: a lead the planner can confirm by
        # reading the current file at that range.
        http = FakeHttp(hits=[hit("app/controllers/godata_controller.rb", 4, 65)])
        line = search(http).query("csv feed")[0]
        assert "app/controllers/godata_controller.rb:4-65" in line

    def test_results_are_capped(self):
        http = FakeHttp(hits=[hit(f"a/{i}.rb", i, i + 5) for i in range(20)])
        assert len(search(http, max_results=3).query("x")) == 3

    def test_a_weak_match_is_dropped(self):
        # A nearest neighbour is always *something*. Without a floor the planner
        # is handed the least-bad chunk in the repository and told it is a lead.
        http = FakeHttp(hits=[hit("a.rb", 1, 2, score=0.9), hit("b.rb", 3, 4, score=0.11)])
        out = search(http, min_score=0.5).query("x")
        assert len(out) == 1 and "a.rb" in out[0]

    def test_no_matches_is_an_answer(self):
        assert search(FakeHttp(hits=[])).query("nothing like this exists") == []


class TestFailureIsNotFatal:
    def test_an_unreachable_index_degrades_to_a_message(self):
        # A planning step must not die because a side service is down. The
        # planner still has the plan, the layout and the read tools.
        http = FakeHttp(fail=OSError("connection refused"))
        out = search(http).query("x")
        assert out and "unavailable" in out[0].lower()

    def test_a_malformed_response_degrades_the_same_way(self):
        class Broken(FakeHttp):
            def __call__(self, url, payload, timeout):
                return {"unexpected": "shape"}

        out = search(Broken()).query("x")
        assert out and "unavailable" in out[0].lower()


class TestConfiguration:
    def test_it_is_absent_unless_configured(self):
        # No collection, no tool. A project without an index must not be
        # offered one, and must not fail for lacking it.
        assert SemanticSearchConfig.from_mapping(None) is None
        assert SemanticSearchConfig.from_mapping({}) is None

    def test_endpoints_come_from_the_environment(self, monkeypatch):
        # The tailnet host is an identifiable infrastructure value and must not
        # sit in a tracked config file.
        monkeypatch.setenv("TEST_API_BASE", "http://endpoint/v1")
        monkeypatch.setenv("TEST_QDRANT", "http://qdrant:6333")
        cfg = SemanticSearchConfig.from_mapping(
            {
                "api_base_env": "TEST_API_BASE",
                "qdrant_url_env": "TEST_QDRANT",
                "embedding_model": "qwen3-embedding",
                "collection": "proj-v1",
            }
        )
        assert cfg.api_base == "http://endpoint/v1"
        assert cfg.qdrant_url == "http://qdrant:6333"

    def test_a_missing_environment_variable_is_named(self, monkeypatch):
        monkeypatch.setenv("TEST_API_BASE", "http://endpoint/v1")
        monkeypatch.delenv("TEST_QDRANT", raising=False)
        with pytest.raises(KeyError, match="TEST_QDRANT"):
            SemanticSearchConfig.from_mapping(
                {
                    "api_base_env": "TEST_API_BASE",
                    "qdrant_url_env": "TEST_QDRANT",
                    "embedding_model": "m",
                    "collection": "c",
                }
            )


class TestProvenance:
    def test_queries_are_recorded(self):
        http = FakeHttp(hits=[hit("a.rb", 1, 2)])
        s = search(http)
        s.query("first")
        s.query("second")
        assert [c.detail for c in s.calls] == ["first", "second"]
        assert all(c.tool == "semantic_search" for c in s.calls)
