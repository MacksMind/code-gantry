"""Git operations against the target repo.

The load-bearing decision here is PLAN.md's "Diffs and commits": stage diffs
are computed against the working tree, not `<sha>..HEAD`, because script
stages and resumed manual stages leave work uncommitted. If that is wrong,
the scope guard passes vacuously and the reviewer approves an empty diff.
"""

import pytest

from orchestrator.gitops import Git, GitError


class TestInspection:
    def test_detects_a_repo(self, repo):
        assert Git(repo).is_repo()

    def test_detects_a_non_repo(self, tmp_path):
        assert not Git(tmp_path).is_repo()

    def test_clean_tree(self, repo):
        assert Git(repo).is_clean()

    def test_dirty_tree_from_modification(self, repo):
        (repo / "app.py").write_text("changed\n")
        assert not Git(repo).is_clean()

    def test_dirty_tree_from_untracked_file(self, repo):
        # An untracked file is uncommitted work; refusing to start on it is
        # the point of the clean-tree safety requirement.
        (repo / "new.py").write_text("x\n")
        assert not Git(repo).is_clean()

    def test_ignored_files_do_not_make_a_tree_dirty(self, repo):
        (repo / "secrets.local").write_text("x\n")
        assert Git(repo).is_clean()

    def test_head_sha(self, repo):
        assert len(Git(repo).head_sha()) == 40

    def test_current_branch(self, repo):
        assert Git(repo).current_branch() == "main"

    def test_rev_parse_unknown_ref_raises(self, repo):
        with pytest.raises(GitError):
            Git(repo).rev_parse("no-such-ref")


class TestBranching:
    def test_branch_exists(self, repo):
        g = Git(repo)
        assert g.branch_exists("main")
        assert not g.branch_exists("nope")

    def test_create_and_checkout_branch(self, repo):
        g = Git(repo)
        g.create_branch("refactor/thing", base="main")
        assert g.current_branch() == "refactor/thing"

    def test_checkout_existing_branch(self, repo):
        g = Git(repo)
        g.create_branch("refactor/thing", base="main")
        g.checkout("main")
        assert g.current_branch() == "main"
        g.checkout("refactor/thing")
        assert g.current_branch() == "refactor/thing"

    def test_no_push_method_exists(self):
        # The safety requirements forbid pushing. The way to guarantee that is
        # to have no code that can.
        assert not any("push" in name for name in dir(Git))


class TestDiff:
    def test_diff_sees_committed_change(self, repo, run_git):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("def hello():\n    return 2\n")
        run_git(repo, "commit", "-aqm", "change")
        assert "return 2" in g.diff(base)

    def test_diff_sees_uncommitted_change(self, repo):
        # A script stage leaves its transform uncommitted. `<sha>..HEAD` would
        # report an empty diff here and every gate downstream would pass
        # vacuously.
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("def hello():\n    return 3\n")
        assert "return 3" in g.diff(base)

    def test_diff_sees_untracked_new_file(self, repo):
        # A greenfield stage creates files that were never added. Plain
        # `git diff` ignores untracked paths entirely.
        g = Git(repo)
        base = g.head_sha()
        (repo / "brand_new.py").write_text("print('hi')\n")
        diff = g.diff(base)
        assert "brand_new.py" in diff
        assert "print('hi')" in diff

    def test_diff_ignores_gitignored_files(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "junk.local").write_text("noise\n")
        assert "junk.local" not in g.diff(base)

    def test_empty_diff_when_nothing_changed(self, repo):
        g = Git(repo)
        assert g.diff(g.head_sha()).strip() == ""

    def test_diff_names_lists_changed_paths(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("changed\n")
        (repo / "other.py").write_text("new\n")
        assert set(g.diff_names(base)) == {"app.py", "other.py"}

    def test_diff_names_empty_when_clean(self, repo):
        g = Git(repo)
        assert g.diff_names(g.head_sha()) == []

    def test_intent_to_add_does_not_commit_anything(self, repo):
        # Making untracked files visible to `git diff` must not quietly create
        # a commit or stage content for real.
        g = Git(repo)
        base = g.head_sha()
        (repo / "brand_new.py").write_text("x\n")
        g.diff(base)
        assert g.head_sha() == base


class TestAddedLines:
    def test_extracts_only_added_lines(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("def hello():\n    return 99\n")
        added = g.added_lines(base)
        texts = [text for _, text in added]
        assert any("return 99" in t for t in texts)
        assert not any("return 1" in t for t in texts)

    def test_attributes_lines_to_their_file(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "one.py").write_text("ALPHA\n")
        (repo / "two.py").write_text("BETA\n")
        added = dict((text.strip(), path) for path, text in g.added_lines(base))
        assert added["ALPHA"] == "one.py"
        assert added["BETA"] == "two.py"

    def test_excludes_the_plus_plus_plus_header(self, repo):
        # `+++ b/app.py` starts with '+' but is not added content. Treating it
        # as content makes any pattern matching a filename fire spuriously.
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("x\n")
        texts = [t for _, t in g.added_lines(base)]
        assert not any(t.startswith("++") for t in texts)

    def test_removed_lines_are_not_reported(self, repo):
        # This is what makes a stage that *removes* a construct able to
        # forbid that construct without flagging its own success.
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("")
        texts = [t for _, t in g.added_lines(base)]
        assert not any("return 1" in t for t in texts)


class TestCommit:
    def test_commits_everything_including_untracked(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("changed\n")
        (repo / "added.py").write_text("new\n")
        sha = g.commit_all("stage: thing")
        assert sha and sha != base
        assert g.is_clean()

    def test_returns_none_when_nothing_to_commit(self, repo):
        # advance() calls this unconditionally; an agent stage whose executor
        # already auto-committed leaves nothing to do.
        g = Git(repo)
        assert g.commit_all("nothing") is None

    def test_commit_message_is_used(self, repo, run_git):
        g = Git(repo)
        (repo / "app.py").write_text("changed\n")
        g.commit_all("stage: extract-service")
        assert "extract-service" in run_git(repo, "log", "-1", "--pretty=%s")


class TestReset:
    def test_reset_discards_committed_work(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("rejected\n")
        g.commit_all("rejected attempt")
        g.reset_hard(base)
        assert g.head_sha() == base
        assert (repo / "app.py").read_text() == "def hello():\n    return 1\n"

    def test_reset_removes_untracked_files_from_the_attempt(self, repo):
        # Without this, a rejected attempt's new files survive into the next
        # attempt's diff and the "one clean single-purpose diff" guarantee
        # that rework_reset exists to provide is false.
        g = Git(repo)
        base = g.head_sha()
        (repo / "half_finished.py").write_text("junk\n")
        g.reset_hard(base)
        assert not (repo / "half_finished.py").exists()

    def test_reset_preserves_gitignored_files(self, repo):
        # Resetting must not destroy .env.local, test databases, or caches.
        g = Git(repo)
        base = g.head_sha()
        (repo / "config.local").write_text("secret\n")
        g.reset_hard(base)
        assert (repo / "config.local").exists()
