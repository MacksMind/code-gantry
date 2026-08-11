"""What a semantic search returned, kept — and only for semantic search.

Every other read the planner and reviewer make is *reproducible*: the ledger
records `read_file(app/foo.rb:1-80) -> 80 line(s)`, the sha is known, and
anyone can fetch the same bytes back. Storing the content would be storing a
second copy of the repository.

A semantic hit is not reproducible. It depends on an index built from whatever
commits had been ingested at the time, on a similarity cutoff, and on an
embedding model — re-running the same question later can return different
chunks, and nothing on disk said what these ones were. So for the single tool
whose output cannot be reconstructed, the record kept the least: the question
and a line count.

That matters because of what the reviewer is for. It was given tool access
because a diff does not always carry the fact that decides it, and the artifact
is supposed to show what a gate *looked at* rather than only what it concluded.
A verdict resting on eighteen lines nobody can retrieve is the shape this was
meant to stop.

Bounded by construction: `max_results` chunks of `snippet_lines` each, at
around one call in a hundred.
"""

from orchestrator.repotools import ToolCall


class TestTheLedgerCanCarryAResult:
    def test_a_tool_call_records_no_result_by_default(self):
        assert ToolCall(tool="read_file", detail="a.rb", lines=3).result == ""

    def test_it_can_carry_one(self):
        call = ToolCall(tool="semantic_search", detail="q", lines=2, result="a\nb")
        assert call.result == "a\nb"


class TestOnlySemanticFillsIt:
    """A policy, and it is the whole design — so it is pinned, not trusted."""

    def test_a_read_records_no_content(self, tmp_path):
        import subprocess

        from orchestrator.gitops import Git
        from orchestrator.repotools import ReadBudget, RepoReader

        (tmp_path / "a.rb").write_text("class Foo\nend\n")
        run = lambda *a: subprocess.run(
            ["git", "-C", str(tmp_path), *a], capture_output=True, text=True
        )
        subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True)
        run("config", "core.hooksPath", str(tmp_path / ".git" / "hooks"))
        run("add", "-A")
        run("-c", "user.email=t@t", "-c", "user.name=t",
            "-c", "commit.gpgsign=false", "commit", "-qm", "x")

        reader = RepoReader(Git(tmp_path), tmp_path, ReadBudget())
        reader.read_file("a.rb")
        assert reader.calls[0].result == "", "a reproducible read must not be copied"

    def test_the_helper_returns_only_semantic_entries(self):
        from orchestrator.repotools import semantic_results

        ledger = [
            ToolCall(tool="read_file", detail="a.rb", lines=3, result="should be ignored"),
            ToolCall(tool="semantic_search", detail="where is X?", lines=2, result="a\nb"),
            ToolCall(tool="search", detail="X in app", lines=1),
        ]
        out = semantic_results(ledger)
        assert out == [{"question": "where is X?", "returned": "a\nb"}]


class TestTheSearchRecordsWhatItReturned:
    def _search(self, hits):
        from orchestrator.semantic import SemanticSearch, SemanticSearchConfig

        cfg = SemanticSearchConfig(
            api_base="http://x", qdrant_url="http://q",
            embedding_model="m", collection="c",
        )
        return SemanticSearch(cfg, http=lambda *a, **k: hits)

    def test_a_hit_is_kept(self):
        s = self._search(
            {"result": [{"payload": {"path": "app/a.rb", "start": 1, "end": 9,
                                     "content": "class Foo"}, "score": 0.9}],
             "data": [{"embedding": [0.1]}]}
        )
        out = s.query("where is Foo?")
        assert s.calls[-1].result == "\n".join(out)
        assert "app/a.rb" in s.calls[-1].result

    def test_a_failure_records_the_call_and_no_result(self):
        def boom(*a, **k):
            raise OSError("index down")

        from orchestrator.semantic import SemanticSearch, SemanticSearchConfig

        s = SemanticSearch(
            SemanticSearchConfig(api_base="http://x", qdrant_url="http://q",
                                 embedding_model="m", collection="c"),
            http=boom,
        )
        s.query("anything")
        assert s.calls[-1].tool == "semantic_search"
        assert s.calls[-1].result == "", "there was nothing to keep"


class TestItReachesTheArtifacts:
    def test_review_json_carries_it(self):
        from orchestrator.reviewer import ReviewOutcome

        out = ReviewOutcome(
            verdict="approved", summary="s",
            semantic_results=[{"question": "q", "returned": "a\nb"}],
        )
        assert out.as_dict()["semantic_results"] == [
            {"question": "q", "returned": "a\nb"}
        ]

    def test_an_outcome_with_none_still_has_the_key(self):
        # An absent field and an empty one are indistinguishable to a reader,
        # and this project has already answered a question wrongly that way.
        from orchestrator.reviewer import ReviewOutcome

        assert ReviewOutcome(verdict="approved", summary="s").as_dict()[
            "semantic_results"
        ] == []


class TestThePlannerArtifactToo:
    def test_planner_json_carries_the_key(self, repo, tmp_path):
        import json

        from orchestrator import nodes
        from test_nodes import StubPlanner, make, PlannerOutcome

        planner = StubPlanner(
            [
                PlannerOutcome(
                    "project_complete", "done", "e",
                    tool_calls=["semantic_search(where is X?) -> 2 line(s)"],
                    semantic_results=[{"question": "where is X?", "returned": "a\nb"}],
                )
            ]
        )
        cfg, rt, state = make(repo, tmp_path, planner=planner)
        nodes.plan(state, rt)
        written = json.loads(
            next(rt.paths.run_dir.glob("stages/*/planner.json")).read_text()
        )
        assert written["semantic_results"] == [
            {"question": "where is X?", "returned": "a\nb"}
        ]

    def test_the_key_is_present_when_nothing_was_asked(self, repo, tmp_path):
        import json

        from orchestrator import nodes
        from test_nodes import StubPlanner, make, PlannerOutcome

        cfg, rt, state = make(
            repo, tmp_path,
            planner=StubPlanner([PlannerOutcome("project_complete", "done", "e")]),
        )
        nodes.plan(state, rt)
        written = json.loads(
            next(rt.paths.run_dir.glob("stages/*/planner.json")).read_text()
        )
        assert written["semantic_results"] == []
