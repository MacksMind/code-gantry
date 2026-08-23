"""Plan document resolution and snapshotting.

The escape check is the one with security weight: a link in a plan document
must not be able to pull arbitrary files off disk into a payload that gets
pasted verbatim into every paid model call.
"""

from pathlib import Path

from code_gantry.gitops import Git
from code_gantry.plandoc import (
    PlanDocument,
    PlanTree,
    extract_links,
    load_snapshot,
    resolve_plan_tree,
    snapshot_tree,
)


def commit_docs(repo, run_git, files: dict[str, str]) -> str:
    for name, body in files.items():
        path = Path(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "plan docs")
    return Git(repo).head_sha()


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
        # A link to a .rb file is a code reference, not a plan child. Inlining
        # it would be both wrong and expensive.
        assert extract_links("[the model](app/models/order.rb)") == []

    def test_deduplicates(self):
        assert extract_links("[a](c.md) and again [b](c.md)") == ["c.md"]

    def test_preserves_document_order(self):
        links = extract_links("[b](b.md)\n[a](a.md)")
        assert links == ["b.md", "a.md"]


class TestResolution:
    def test_resolves_the_root_alone(self, repo, run_git):
        sha = commit_docs(repo, run_git, {"docs/plan.md": "# Plan\nno children"})
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert tree.ok
        assert tree.root.path == "docs/plan.md"
        assert tree.children == []

    def test_resolves_children_beside_the_root(self, repo, run_git):
        sha = commit_docs(
            repo,
            run_git,
            {
                "docs/plan.md": "# Plan\nsee [gems](gem_plan.md)",
                "docs/gem_plan.md": "# Gems",
            },
        )
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert [c.path for c in tree.children] == ["docs/gem_plan.md"]

    def test_resolves_children_in_a_subdirectory(self, repo, run_git):
        sha = commit_docs(
            repo,
            run_git,
            {
                "docs/plan.md": "[detail](sub/detail.md)",
                "docs/sub/detail.md": "# Detail",
            },
        )
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert [c.path for c in tree.children] == ["docs/sub/detail.md"]

    def test_does_not_recurse(self, repo, run_git):
        # One level, deliberately: recursive resolution makes payload size a
        # property of the documents rather than of the config.
        sha = commit_docs(
            repo,
            run_git,
            {
                "docs/plan.md": "[child](child.md)",
                "docs/child.md": "[grandchild](grandchild.md)",
                "docs/grandchild.md": "# Too deep",
            },
        )
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert [c.path for c in tree.children] == ["docs/child.md"]

    def test_a_link_back_to_the_root_is_not_duplicated(self, repo, run_git):
        sha = commit_docs(repo, run_git, {"docs/plan.md": "[self](plan.md)"})
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert tree.children == []

    def test_a_link_to_an_unwritten_document_is_skipped_not_fatal(self, repo, run_git):
        # Normal while a plan is being authored; not worth failing a run over.
        sha = commit_docs(repo, run_git, {"docs/plan.md": "[future](future.md)"})
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert tree.ok
        assert tree.skipped == ["docs/future.md"]

    def test_a_missing_root_is_fatal(self, repo, run_git):
        sha = commit_docs(repo, run_git, {"docs/other.md": "x"})
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert not tree.ok
        assert "could not be read" in tree.problems[0]


class TestEscapeChecks:
    def test_rejects_a_parent_traversal(self, repo, run_git):
        # The security case: this would pull a file from outside the plan root
        # into every review prompt.
        sha = commit_docs(
            repo,
            run_git,
            {"docs/plan.md": "[escape](../secrets.md)", "secrets.md": "sensitive"},
        )
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert not tree.ok
        assert "outside the plan root" in tree.problems[0]

    def test_rejects_a_deep_traversal(self, repo, run_git):
        sha = commit_docs(
            repo, run_git, {"docs/plan.md": "[escape](../../../../etc/passwd.md)"}
        )
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert not tree.ok

    def test_rejects_an_absolute_path(self, repo, run_git):
        sha = commit_docs(repo, run_git, {"docs/plan.md": "[abs](/etc/plan.md)"})
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert not tree.ok
        assert "absolute" in tree.problems[0]

    def test_allows_traversal_that_stays_inside(self, repo, run_git):
        sha = commit_docs(
            repo,
            run_git,
            {
                "docs/plan.md": "[sideways](sub/../sibling.md)",
                "docs/sibling.md": "# Sibling",
            },
        )
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert tree.ok
        assert [c.path for c in tree.children] == ["docs/sibling.md"]

    def test_a_root_at_the_repo_top_level_permits_siblings(self, repo, run_git):
        sha = commit_docs(
            repo, run_git, {"PLAN.md": "[child](child.md)", "child.md": "# Child"}
        )
        tree = resolve_plan_tree(Git(repo), "PLAN.md", sha)
        assert tree.ok
        assert [c.path for c in tree.children] == ["child.md"]


class TestReadAtSha:
    def test_reads_the_committed_version_not_the_working_tree(self, repo, run_git):
        # A concurrent edit must not change what a run thinks it was asked to do.
        sha = commit_docs(repo, run_git, {"docs/plan.md": "# Original mandate"})
        (Path(repo) / "docs" / "plan.md").write_text("# Someone edited this")
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        assert "Original mandate" in tree.root.content
        assert "Someone edited" not in tree.root.content

    def test_reads_an_older_sha(self, repo, run_git):
        first = commit_docs(repo, run_git, {"docs/plan.md": "# Version one"})
        commit_docs(repo, run_git, {"docs/plan.md": "# Version two"})
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", first)
        assert "Version one" in tree.root.content


class TestSnapshot:
    def test_round_trips(self, repo, run_git, tmp_path):
        sha = commit_docs(
            repo,
            run_git,
            {"docs/plan.md": "# Root", "docs/child.md": "# Child"},
        )
        # Link so the child is resolved.
        commit_docs(repo, run_git, {"docs/plan.md": "# Root\n[c](child.md)"})
        sha = Git(repo).head_sha()

        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        dest = tmp_path / "plan-snapshot"
        snapshot_tree(tree, dest)

        loaded = load_snapshot(dest)
        assert loaded.root.path == "docs/plan.md"
        assert [c.path for c in loaded.children] == ["docs/child.md"]
        assert "# Child" in loaded.children[0].content

    def test_snapshot_is_flat(self, repo, run_git, tmp_path):
        # A flat directory cannot itself contain a traversal.
        sha = commit_docs(
            repo,
            run_git,
            {"docs/plan.md": "[d](sub/deep.md)", "docs/sub/deep.md": "# Deep"},
        )
        tree = resolve_plan_tree(Git(repo), "docs/plan.md", sha)
        dest = tmp_path / "snap"
        snapshot_tree(tree, dest)
        assert not (dest / "docs").exists()
        assert any("sub__deep.md" in p.name for p in dest.iterdir())

    def test_resnapshotting_replaces_stale_files(self, repo, run_git, tmp_path):
        # A run's snapshot must reflect that run, not accumulate history.
        sha = commit_docs(
            repo, run_git, {"docs/plan.md": "[a](a.md)", "docs/a.md": "# A"}
        )
        dest = tmp_path / "snap"
        snapshot_tree(resolve_plan_tree(Git(repo), "docs/plan.md", sha), dest)

        sha = commit_docs(repo, run_git, {"docs/plan.md": "# No children now"})
        snapshot_tree(resolve_plan_tree(Git(repo), "docs/plan.md", sha), dest)

        loaded = load_snapshot(dest)
        assert loaded.children == []
        assert not any("a.md" in p.name for p in dest.iterdir())

    def test_missing_snapshot_reports_a_problem(self, tmp_path):
        assert not load_snapshot(tmp_path / "absent").ok


class TestPromptPayload:
    def test_root_comes_first(self, repo, run_git):
        sha = commit_docs(
            repo,
            run_git,
            {"docs/plan.md": "ROOT [c](child.md)", "docs/child.md": "CHILD"},
        )
        payload = resolve_plan_tree(Git(repo), "docs/plan.md", sha).as_prompt_payload()
        assert payload.index("ROOT") < payload.index("CHILD")

    def test_names_each_document(self, repo, run_git):
        sha = commit_docs(repo, run_git, {"docs/plan.md": "# Plan"})
        payload = resolve_plan_tree(Git(repo), "docs/plan.md", sha).as_prompt_payload()
        assert "docs/plan.md" in payload

    def test_payload_is_byte_stable_across_calls(self, repo, run_git):
        # It is the cacheable prefix of every planner and reviewer call.
        sha = commit_docs(
            repo,
            run_git,
            {"docs/plan.md": "[c](child.md)", "docs/child.md": "CHILD"},
        )
        git = Git(repo)
        first = resolve_plan_tree(git, "docs/plan.md", sha).as_prompt_payload()
        second = resolve_plan_tree(git, "docs/plan.md", sha).as_prompt_payload()
        assert first == second


class TestTheGrowingDocumentGoesLast:
    """A cached prefix is matched as a prefix, so growth must go at the end.

    Children are ordered by where the root links them, and this project's plan
    links its progress log in the opening paragraph — putting the one document
    that grows (~2KB per landed stage) ahead of seven static runbooks totalling
    ~168KB. Every byte the log gained re-billed all seven behind it. Order
    carries no meaning to the reader: documents are labelled by path and the
    planner is told which one records progress. It only decides where the churn
    is allowed to land.
    """

    def _tree(self):
        return PlanTree(
            root=PlanDocument(path="PLAN.md", content="# Plan"),
            children=[
                PlanDocument(path="progress_log.md", content="log"),
                PlanDocument(path="stream_one.md", content="one"),
                PlanDocument(path="stream_two.md", content="two"),
            ],
        )

    def test_the_named_document_is_sunk_to_the_end(self):
        payload = self._tree().as_prompt_payload(last="progress_log.md")
        assert payload.index("stream_one.md") < payload.index("progress_log.md")
        assert payload.index("stream_two.md") < payload.index("progress_log.md")

    def test_the_root_still_leads(self):
        payload = self._tree().as_prompt_payload(last="progress_log.md")
        assert payload.startswith("### PLAN.md")

    def test_every_document_survives_the_reordering(self):
        payload = self._tree().as_prompt_payload(last="progress_log.md")
        for path in ("PLAN.md", "progress_log.md", "stream_one.md", "stream_two.md"):
            assert f"### {path}" in payload

    def test_without_a_name_the_order_is_unchanged(self):
        payload = self._tree().as_prompt_payload()
        assert payload.index("progress_log.md") < payload.index("stream_one.md")

    def test_a_name_that_is_not_in_the_tree_changes_nothing(self):
        payload = self._tree().as_prompt_payload(last="nowhere.md")
        assert payload.index("progress_log.md") < payload.index("stream_one.md")


class TestTheGrowingDocumentComesOutOfTheCachedBlock:
    """Sinking the log inside the block was half the fix.

    `last=` orders the progress log after the static documents so it
    invalidates only what follows it. But the planner's cache breakpoint sits
    at the *end* of that block, so the entry covers the log too and one
    appended note rewrites the whole 735KB — measured as block 0 being
    99.2-99.7% shared with the previous derivation and read back never.
    Shared is not cached.

    Splitting it lets the marked block hold conventions, layout and the entire
    plan, stable for the life of a run and invalidated only by a fold; the log
    follows the mark and pays for its own size. Document order is unchanged,
    because the log was already last of the documents and now leads the block
    behind them.
    """

    def _tree(self, repo, run_git):
        sha = commit_docs(
            repo,
            run_git,
            {
                "docs/plan.md": "ROOT [c](child.md) [log](progress_log.md)",
                "docs/child.md": "CHILD",
                "docs/progress_log.md": "LOG",
            },
        )
        return resolve_plan_tree(Git(repo), "docs/plan.md", sha)

    def test_the_stable_half_omits_the_growing_document(self, repo, run_git):
        stable, trailing = self._tree(repo, run_git).split_payload(
            "docs/progress_log.md"
        )
        assert "LOG" not in stable
        assert "LOG" in trailing
        assert "CHILD" in stable

    def test_together_they_are_what_one_block_used_to_be(self, repo, run_git):
        """No content is dropped by the split — only where the mark falls."""
        tree = self._tree(repo, run_git)
        stable, trailing = tree.split_payload("docs/progress_log.md")
        whole = tree.as_prompt_payload(last="docs/progress_log.md")
        assert "\n\n".join(x for x in (stable, trailing) if x) == whole

    def test_no_growing_document_named_leaves_nothing_trailing(self, repo, run_git):
        tree = self._tree(repo, run_git)
        stable, trailing = tree.split_payload(None)
        assert trailing == ""
        assert stable == tree.as_prompt_payload()
