"""Git operations against the target repo.

Two decisions carry the most weight. Stage diffs are computed against the
working tree, not `<sha>..HEAD`, because script stages and human fixes leave
work uncommitted — if that is wrong, the scope guard passes vacuously and the
reviewer approves an empty diff. And stages land by squash merge, which is what
lets Aider commit before testing while the project branch stays green.
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

    def test_dirty_tree_from_untracked_file(self, repo):
        (repo / "new.py").write_text("x\n")
        assert not Git(repo).is_clean()

    def test_ignored_files_do_not_make_a_tree_dirty(self, repo):
        (repo / "secrets.local").write_text("x\n")
        assert Git(repo).is_clean()

    def test_rev_parse_unknown_ref_raises(self, repo):
        with pytest.raises(GitError):
            Git(repo).rev_parse("no-such-ref")

    def test_is_ancestor(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("changed\n")
        g.commit_all("second")
        assert g.is_ancestor(base, "HEAD")
        assert not g.is_ancestor("HEAD", base)

    def test_no_push_method_exists(self):
        # The safety requirements forbid pushing. The way to guarantee that is
        # to have no code that can.
        assert not any("push" in name for name in dir(Git))


class TestProjectBranch:
    def test_cuts_the_project_branch_from_base_ref(self, repo):
        g = Git(repo)
        base_sha = g.ensure_project_branch("upgrade/rails-5", "main")
        assert g.current_branch() == "upgrade/rails-5"
        assert base_sha == g.rev_parse("main")

    def test_is_idempotent(self, repo):
        # `run` calls this on every invocation, including a resume.
        g = Git(repo)
        g.ensure_project_branch("upgrade/rails-5", "main")
        (repo / "app.py").write_text("work\n")
        g.commit_all("stage work")
        tip = g.head_sha()
        g.ensure_project_branch("upgrade/rails-5", "main")
        assert g.head_sha() == tip

    def test_checks_out_an_existing_project_branch(self, repo):
        g = Git(repo)
        g.ensure_project_branch("upgrade/rails-5", "main")
        g.checkout("main")
        g.ensure_project_branch("upgrade/rails-5", "main")
        assert g.current_branch() == "upgrade/rails-5"


class TestStageBranch:
    def test_cuts_from_the_project_branch_tip(self, repo):
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        (repo / "app.py").write_text("landed\n")
        g.commit_all("earlier stage")
        tip = g.head_sha()

        start = g.cut_stage_branch("proj-stage/001-x", "proj")
        assert g.current_branch() == "proj-stage/001-x"
        assert start == tip

    def test_reuses_an_existing_stage_branch(self, repo):
        # A scope-widening revision extends existing work rather than
        # discarding it.
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-x", "proj")
        (repo / "app.py").write_text("partial work\n")
        g.commit_all("partial")
        sha = g.head_sha()

        g.checkout("proj")
        g.cut_stage_branch("proj-stage/001-x", "proj")
        assert g.head_sha() == sha

    def test_branches_matching_finds_the_namespace(self, repo):
        # Fifty child branches must stay greppable and deletable as a group.
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-a", "proj")
        g.checkout("proj")
        g.cut_stage_branch("proj-stage/002-b", "proj")
        assert sorted(g.branches_matching("proj-stage/")) == [
            "proj-stage/001-a",
            "proj-stage/002-b",
        ]

    def test_delete_branch(self, repo):
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-x", "proj")
        g.checkout("proj")
        g.delete_branch("proj-stage/001-x")
        assert not g.branch_exists("proj-stage/001-x")


class TestBranchIdentity:
    def setup_project(self, repo):
        g = Git(repo)
        base_sha = g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-x", "proj")
        return g, base_sha

    def test_no_problems_on_a_healthy_stage(self, repo):
        g, base_sha = self.setup_project(repo)
        (repo / "app.py").write_text("work\n")
        g.commit_all("work")
        assert g.branch_identity_problems("proj-stage/001-x", "proj", "main", base_sha) == []

    def test_detects_a_stray_checkout(self, repo):
        # A `git checkout` inside an operator-authored check or script is the
        # failure you would least like to find after a ten-hour run.
        g, base_sha = self.setup_project(repo)
        g.checkout("main")
        problems = g.branch_identity_problems("proj-stage/001-x", "proj", "main", base_sha)
        assert problems and "HEAD is on" in problems[0]

    def test_detects_a_moved_base_ref(self, repo, run_git):
        g, base_sha = self.setup_project(repo)
        run_git(repo, "checkout", "-q", "main")
        (repo / "app.py").write_text("someone else's commit\n")
        run_git(repo, "commit", "-aqm", "concurrent work on main")
        run_git(repo, "checkout", "-q", "proj-stage/001-x")
        problems = g.branch_identity_problems("proj-stage/001-x", "proj", "main", base_sha)
        assert any("moved during the run" in p for p in problems)

    def test_detects_a_rewritten_project_branch(self, repo, run_git):
        g, base_sha = self.setup_project(repo)
        (repo / "app.py").write_text("stage work\n")
        g.commit_all("stage work")
        # Rewrite the project branch out from under the stage.
        run_git(repo, "checkout", "-q", "proj")
        (repo / "other.py").write_text("divergent\n")
        run_git(repo, "add", "-A")
        run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "divergent")
        run_git(repo, "checkout", "-q", "proj-stage/001-x")
        problems = g.branch_identity_problems("proj-stage/001-x", "proj", "main", base_sha)
        assert any("diverged" in p for p in problems)

    def test_detects_a_deleted_project_branch(self, repo, run_git):
        g, base_sha = self.setup_project(repo)
        run_git(repo, "branch", "-D", "-q", "proj")
        problems = g.branch_identity_problems("proj-stage/001-x", "proj", "main", base_sha)
        assert any("no longer exists" in p for p in problems)


class TestDiff:
    def test_sees_uncommitted_change(self, repo):
        # A script stage leaves its transform uncommitted. `<sha>..HEAD` would
        # report an empty diff and every gate downstream would pass vacuously.
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("def hello():\n    return 3\n")
        assert "return 3" in g.diff(base)

    def test_sees_untracked_new_file(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "brand_new.py").write_text("print('hi')\n")
        assert "brand_new.py" in g.diff(base)

    def test_ignores_gitignored_files(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "junk.local").write_text("noise\n")
        assert "junk.local" not in g.diff(base)

    def test_diff_names_lists_changed_paths(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("changed\n")
        (repo / "other.py").write_text("new\n")
        assert set(g.diff_names(base)) == {"app.py", "other.py"}

    def test_intent_to_add_does_not_commit_anything(self, repo):
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
        texts = [t for _, t in g.added_lines(base)]
        assert any("return 99" in t for t in texts)
        assert not any("return 1" in t for t in texts)

    def test_attributes_lines_to_their_file(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "one.py").write_text("ALPHA\n")
        (repo / "two.py").write_text("BETA\n")
        added = dict((text.strip(), path) for path, text in g.added_lines(base))
        assert added["ALPHA"] == "one.py"

    def test_removed_lines_are_not_reported(self, repo):
        # What lets a stage that removes a construct forbid it without flagging
        # its own success.
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("")
        texts = [t for _, t in g.added_lines(base)]
        assert not any("return 1" in t for t in texts)


class TestSquashMerge:
    def test_lands_a_stage_as_one_commit(self, repo):
        # Aider commits before it tests, so the child branch has red commits in
        # it. Squashing is what keeps the project branch green.
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        before = g.head_sha()
        g.cut_stage_branch("proj-stage/001-x", "proj")
        for i in range(3):
            (repo / "app.py").write_text(f"attempt {i}\n")
            g.commit_all(f"intermediate {i}")

        sha = g.squash_merge("proj-stage/001-x", "proj", "[stage-001-x] do the thing")
        assert sha
        assert g.current_branch() == "proj"
        assert g.commit_subject() == "[stage-001-x] do the thing"
        # Exactly one commit was added to the project branch.
        assert g.rev_parse("proj~1") == before

    def test_content_survives_the_squash(self, repo):
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-x", "proj")
        (repo / "app.py").write_text("final content\n")
        g.commit_all("work")
        g.squash_merge("proj-stage/001-x", "proj", "[x] work")
        assert (repo / "app.py").read_text() == "final content\n"

    def test_returns_none_when_the_child_added_nothing(self, repo):
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-x", "proj")
        assert g.squash_merge("proj-stage/001-x", "proj", "[x] nothing") is None

    def test_leaves_base_ref_untouched(self, repo):
        g = Git(repo)
        base_before = g.rev_parse("main")
        g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-x", "proj")
        (repo / "app.py").write_text("work\n")
        g.commit_all("work")
        g.squash_merge("proj-stage/001-x", "proj", "[x] work")
        assert g.rev_parse("main") == base_before


class TestRevertPaths:
    def test_reverts_only_the_named_paths(self, repo):
        # The scope-quarantine rule: hours of in-scope work must survive one
        # unexpected file being touched.
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("in scope, keep me\n")
        (repo / "wandered.py").write_text("out of scope\n")

        g.revert_paths(base, ["wandered.py"])
        assert not (repo / "wandered.py").exists()
        assert (repo / "app.py").read_text() == "in scope, keep me\n"

    def test_restores_a_modified_file_to_its_baseline(self, repo):
        g = Git(repo)
        base = g.head_sha()
        original = (repo / "app.py").read_text()
        (repo / "app.py").write_text("meddled with\n")
        g.revert_paths(base, ["app.py"])
        assert (repo / "app.py").read_text() == original

    def test_deletes_a_file_that_did_not_exist_at_the_baseline(self, repo):
        # `git checkout <sha> -- path` fails for a path absent at that sha.
        g = Git(repo)
        base = g.head_sha()
        (repo / "brand_new.py").write_text("new\n")
        g.revert_paths(base, ["brand_new.py"])
        assert not (repo / "brand_new.py").exists()

    def test_handles_nested_paths(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "deep").mkdir()
        (repo / "deep" / "nested.py").write_text("x\n")
        g.revert_paths(base, ["deep/nested.py"])
        assert not (repo / "deep" / "nested.py").exists()


class TestReset:
    def test_discards_committed_work(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "app.py").write_text("rejected\n")
        g.commit_all("rejected attempt")
        g.reset_hard(base)
        assert g.head_sha() == base

    def test_removes_untracked_files_from_the_attempt(self, repo):
        g = Git(repo)
        base = g.head_sha()
        (repo / "half_finished.py").write_text("junk\n")
        g.reset_hard(base)
        assert not (repo / "half_finished.py").exists()

    def test_preserves_gitignored_files(self, repo):
        # Env files, caches, and test databases must survive a rework.
        g = Git(repo)
        base = g.head_sha()
        (repo / "config.local").write_text("secret\n")
        g.reset_hard(base)
        assert (repo / "config.local").exists()


class TestGcHandling:
    def test_disables_and_restores_gc(self, repo):
        # The reflog is the only recovery path for a discarded attempt.
        g = Git(repo)
        previous = g.disable_gc()
        assert g.get_config("gc.auto") == "0"
        g.restore_gc(previous)
        assert g.get_config("gc.auto") is None

    def test_restores_a_preexisting_value(self, repo):
        g = Git(repo)
        g.set_config("gc.auto", "256")
        previous = g.disable_gc()
        assert previous == "256"
        g.restore_gc(previous)
        assert g.get_config("gc.auto") == "256"


class TestCommit:
    def test_commits_everything_including_untracked(self, repo):
        g = Git(repo)
        (repo / "app.py").write_text("changed\n")
        (repo / "added.py").write_text("new\n")
        assert g.commit_all("stage: thing")
        assert g.is_clean()

    def test_returns_none_when_nothing_to_commit(self, repo):
        assert Git(repo).commit_all("nothing") is None
