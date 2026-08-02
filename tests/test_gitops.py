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


class TestTrailingWhitespaceOnAddedLines:
    """What the orchestrator commits must not carry trailing whitespace.

    A repository-side hook rejecting it is common — `git diff --cached --check`
    in a pre-commit hook is the usual form — and the executor cannot be relied
    on to avoid it. `executor.lint_command` reaches Aider as `--lint-cmd`, but
    Aider's linter returns before consulting that command whenever
    `filename_to_lang` cannot name the file's language: ERB templates, YAML and
    most non-source files lint as nothing at all.

    The harder half is that the offending line need not be the executor's. A
    line that already carried trailing whitespace becomes an *added* line the
    moment the edit changes enough of its surroundings, and `--check` judges
    added lines. Observed: one pre-existing trailing space in a 156-line ERB
    partial killed a stage four times, and would have killed it on a fresh
    branch too — the executor had nothing to do differently.

    Only added lines are stripped. Rewriting whole files would put churn on
    lines no stage touched in front of the reviewer, which is the same mistake
    the line-ending exemption exists to undo.
    """

    def _repo_with(self, tmp_path, run_git, original: bytes):
        repo = tmp_path / "t"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@e.com")
        run_git(repo, "config", "user.name", "T")
        run_git(repo, "config", "commit.gpgsign", "false")
        # Hermetic: the developer's global hooksPath rejects trailing
        # whitespace, which would make these fixtures uncommittable.
        run_git(repo, "config", "core.hooksPath", str(repo / ".no-hooks"))
        (repo / "view.erb").write_bytes(original)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "base")
        return repo, run_git(repo, "rev-parse", "HEAD")

    def test_strips_whitespace_from_a_line_the_stage_added(self, tmp_path, run_git):
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\n")
        (repo / "view.erb").write_bytes(b"<a>\n<new>   \n")
        assert Git(repo).strip_added_trailing_whitespace(base) == ["view.erb"]
        assert (repo / "view.erb").read_bytes() == b"<a>\n<new>\n"

    def test_leaves_untouched_lines_alone(self, tmp_path, run_git):
        # The whitespace on `<a>` predates the stage and is on no added line.
        # Stripping it would be churn the stage never asked for.
        repo, base = self._repo_with(tmp_path, run_git, b"<a>  \n<b>\n")
        (repo / "view.erb").write_bytes(b"<a>  \n<B>\n")
        assert Git(repo).strip_added_trailing_whitespace(base) == []
        assert (repo / "view.erb").read_bytes() == b"<a>  \n<B>\n"

    def test_strips_inherited_whitespace_once_the_line_counts_as_added(
        self, tmp_path, run_git
    ):
        # The case that killed the stage. The trailing space on `<a>` is
        # inherited, but rewriting the line around it makes git call the line
        # added, and `--check` judges added lines.
        repo, base = self._repo_with(tmp_path, run_git, b"<a>  \n")
        (repo / "view.erb").write_bytes(b"<a href='x'>  \n")
        assert Git(repo).strip_added_trailing_whitespace(base) == ["view.erb"]
        assert (repo / "view.erb").read_bytes() == b"<a href='x'>\n"

    def test_preserves_carriage_returns(self, tmp_path, run_git):
        # A CRLF file is not a file with trailing whitespace. Treating the \r
        # as strippable would rewrite every line ending in the file, which is
        # exactly the churn the reviewer already cannot judge.
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\r\n")
        (repo / "view.erb").write_bytes(b"<a>\r\n<new>  \r\n")
        assert Git(repo).strip_added_trailing_whitespace(base) == ["view.erb"]
        assert (repo / "view.erb").read_bytes() == b"<a>\r\n<new>\r\n"

    def test_strips_tabs_as_well_as_spaces(self, tmp_path, run_git):
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\n")
        (repo / "view.erb").write_bytes(b"<a>\n<new>\t \n")
        assert Git(repo).strip_added_trailing_whitespace(base) == ["view.erb"]
        assert (repo / "view.erb").read_bytes() == b"<a>\n<new>\n"

    def test_covers_files_the_stage_created(self, tmp_path, run_git):
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\n")
        (repo / "fresh.rb").write_bytes(b"x = 1  \n")
        assert Git(repo).strip_added_trailing_whitespace(base) == ["fresh.rb"]
        assert (repo / "fresh.rb").read_bytes() == b"x = 1\n"

    def test_skips_binary_files(self, tmp_path, run_git):
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\n")
        blob = b"\x89PNG\x00 \x00 \n"
        (repo / "logo.png").write_bytes(blob)
        assert Git(repo).strip_added_trailing_whitespace(base) == []
        assert (repo / "logo.png").read_bytes() == blob

    def test_a_clean_stage_changes_nothing(self, tmp_path, run_git):
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\n")
        (repo / "view.erb").write_bytes(b"<a>\n<b>\n")
        assert Git(repo).strip_added_trailing_whitespace(base) == []
        assert (repo / "view.erb").read_bytes() == b"<a>\n<b>\n"

    def test_a_deleted_file_is_not_resurrected(self, tmp_path, run_git):
        repo, base = self._repo_with(tmp_path, run_git, b"<a>  \n")
        (repo / "view.erb").unlink()
        assert Git(repo).strip_added_trailing_whitespace(base) == []
        assert not (repo / "view.erb").exists()

    def test_the_resulting_commit_passes_gits_own_check(self, tmp_path, run_git):
        # End to end, against the gate that actually rejected the stage:
        # `git diff --cached --check`, which is what a pre-commit hook runs.
        repo, base = self._repo_with(tmp_path, run_git, b"<a>  \n")
        (repo / "view.erb").write_bytes(b"<a href='x'>  \n<new>\t\n")
        g = Git(repo)
        g.strip_added_trailing_whitespace(base)
        run_git(repo, "add", "-A")
        assert run_git(repo, "diff", "--cached", "--check") == ""


class TestRestartingAStageBranch:
    """A restart must not inherit the attempt it is restarting from.

    `plan` signals a restart by clearing `stage_branch` in state, but the git
    branch survives on disk and its name is deterministic — index plus stage id
    — so `precheck` recomputed the same name and checked the old branch out.
    Restart and extend were indistinguishable in git, which is to say restart
    never happened.

    Observed live: a stage whose first attempt was reviewer-approved and lost to
    a suite flake was "restarted" onto its own abandoned commit. The reviewer
    then sees only the delta and can approve work it never looked at.
    """

    def test_a_fresh_cut_discards_an_existing_branch(self, repo):
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        project_tip = g.rev_parse("proj")

        g.cut_stage_branch("proj-stage/001-s", "proj")
        (repo / "app.py").write_text("abandoned attempt\n")
        g.commit_all("abandoned")
        assert g.rev_parse("proj-stage/001-s") != project_tip

        start = g.cut_stage_branch("proj-stage/001-s", "proj", fresh=True)
        assert start == project_tip
        assert g.rev_parse("proj-stage/001-s") == project_tip

    def test_extending_keeps_the_existing_work(self, repo):
        # The other revision mode: scope was too narrow, so the work survives.
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        g.cut_stage_branch("proj-stage/001-s", "proj")
        (repo / "app.py").write_text("kept\n")
        g.commit_all("earlier work")
        tip = g.rev_parse("proj-stage/001-s")

        assert g.cut_stage_branch("proj-stage/001-s", "proj", fresh=False) == tip

    def test_a_fresh_cut_of_a_new_branch_is_unremarkable(self, repo):
        g = Git(repo)
        g.ensure_project_branch("proj", "main")
        start = g.cut_stage_branch("proj-stage/002-t", "proj", fresh=True)
        assert start == g.rev_parse("proj")


class TestLineEndingChurnIsHiddenFromReview:
    """The editor rewrites line endings; the reviewer must not judge that.

    aider reads with universal newlines and writes with `--line-endings
    platform`, so every file it touches is rewritten to the host's convention.
    On a repository with mixed endings — most have some — a one-line semantic
    change arrives as a whole-file rewrite.

    Observed: a stage converting one Prototype call in a 156-line CRLF template
    was rejected with "the semantic conversion matches the stage, but the
    whole-file line-ending churn is out of scope and must be removed". The
    executor cannot comply, so it reproduced the identical diff until the
    progress guard stopped it.

    Symmetric deliberately. On Linux and macOS the platform default turns CRLF
    into LF; on Windows it turns LF into CRLF. Treating either direction as the
    correct one would be wrong for half the operators, so direction is not
    judged at all.
    """

    @staticmethod
    def _changed(diff: str) -> str:
        """Only the +/- lines. Context lines are not changes."""
        return "\n".join(
            l for l in diff.splitlines()
            if (l.startswith("+") or l.startswith("-"))
            and not l.startswith(("+++", "---"))
        )

    def _repo_with(self, tmp_path, run_git, original: bytes):
        repo = tmp_path / "t"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@e.com")
        run_git(repo, "config", "user.name", "T")
        run_git(repo, "config", "commit.gpgsign", "false")
        # Hermetic: a global core.hooksPath on the developer's machine counts
        # a carriage return as trailing whitespace, which would make a CRLF
        # fixture uncommittable and this test unrunnable.
        run_git(repo, "config", "core.hooksPath", str(repo / ".no-hooks"))
        (repo / "view.erb").write_bytes(original)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "base")
        return repo, run_git(repo, "rev-parse", "HEAD")

    def test_crlf_to_lf_churn_is_invisible(self, tmp_path, run_git):
        # The macOS and Linux case.
        repo, base = self._repo_with(
            tmp_path, run_git, b"<a>\r\n<b>\r\n<c>\r\n"
        )
        (repo / "view.erb").write_bytes(b"<a>\n<B>\n<c>\n")  # one real change
        g = Git(repo)
        shown = self._changed(g.diff(base, ignore_line_endings=True))
        assert "<B>" in shown, "the semantic change must still be visible"
        assert "<a>" not in shown, "churn on untouched lines must not be"

    def test_lf_to_crlf_churn_is_invisible(self, tmp_path, run_git):
        # The Windows case, which is the same problem pointed the other way.
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\n<b>\n<c>\n")
        (repo / "view.erb").write_bytes(b"<a>\r\n<B>\r\n<c>\r\n")
        g = Git(repo)
        shown = self._changed(g.diff(base, ignore_line_endings=True))
        assert "<B>" in shown
        assert "<a>" not in shown

    def test_without_the_flag_the_churn_is_visible(self, tmp_path, run_git):
        # The default is unchanged: every other caller still sees everything.
        repo, base = self._repo_with(tmp_path, run_git, b"<a>\r\n<b>\r\n<c>\r\n")
        (repo / "view.erb").write_bytes(b"<a>\n<B>\n<c>\n")
        assert "<a>" in self._changed(Git(repo).diff(base))

    def test_a_content_change_cannot_hide_in_the_churn(self, tmp_path, run_git):
        # The reason this is safe: only a trailing CR is disregarded. Anything
        # else about a line still shows, so a real edit cannot ride along.
        repo, base = self._repo_with(tmp_path, run_git, b"x = 1;\r\ny = 2;\r\n")
        (repo / "view.erb").write_bytes(b"x = 1;\ny = 99;\n")
        shown = self._changed(Git(repo).diff(base, ignore_line_endings=True))
        assert "y = 99" in shown
        assert "x = 1" not in shown


class TestALandingLeavesNoHalfMergedBranch:
    """`merge --squash` stages; `commit` is a second step that can fail.

    Between them the project branch holds a staged merge and a modified
    worktree with no commit. Seen three times: a pre-commit hook rejecting
    trailing whitespace is the usual cause, and neither the executor nor its
    linter reliably avoids one. The run ends there, and the next resume opens
    on a project branch that is dirty in a way nothing in the pipeline
    produced — which the workspace guard now escalates, correctly but
    unhelpfully, because the real fault happened one step earlier.

    Nothing is at risk in a rollback: the stage branch still holds every commit,
    so restoring the project branch loses no work and the landing can be retried.
    """

    def _project_with_a_stage(self, repo, run_git):
        run_git(repo, "checkout", "-q", "-b", "proj")
        run_git(repo, "checkout", "-q", "-b", "stage")
        (repo / "app.py").write_text("stage work\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "stage work")
        run_git(repo, "checkout", "-q", "proj")
        return Git(repo)

    def test_a_failing_commit_restores_the_project_branch(
        self, repo, run_git, monkeypatch
    ):
        git = self._project_with_a_stage(repo, run_git)
        before = git.rev_parse("proj")

        real = git._run

        def fail_the_commit(*args, **kw):
            if args and args[0] == "-c" and "commit" in args:
                raise GitError("pre-commit hook rejected trailing whitespace")
            return real(*args, **kw)

        monkeypatch.setattr(git, "_run", fail_the_commit)

        with pytest.raises(GitError, match="pre-commit"):
            git.squash_merge("stage", "proj", "[stage] work")

        assert git.rev_parse("proj") == before, "the branch moved"
        assert git.is_clean(), "a staged merge was left behind"

    def test_the_stage_branch_still_holds_the_work(self, repo, run_git, monkeypatch):
        # The rollback is safe precisely because this is true.
        git = self._project_with_a_stage(repo, run_git)
        real = git._run

        def fail_the_commit(*args, **kw):
            if args and args[0] == "-c" and "commit" in args:
                raise GitError("nope")
            return real(*args, **kw)

        monkeypatch.setattr(git, "_run", fail_the_commit)
        with pytest.raises(GitError):
            git.squash_merge("stage", "proj", "[stage] work")

        assert "stage work" in git.show_file("stage", "app.py")

    def test_a_successful_landing_is_unaffected(self, repo, run_git):
        git = self._project_with_a_stage(repo, run_git)
        sha = git.squash_merge("stage", "proj", "[stage] work")
        assert sha == git.rev_parse("proj")
        assert git.is_clean()

    def test_an_empty_child_still_returns_none(self, repo, run_git):
        # Nothing to land is not a failure, and must not trigger a rollback.
        run_git(repo, "checkout", "-q", "-b", "proj")
        run_git(repo, "checkout", "-q", "-b", "empty")
        run_git(repo, "checkout", "-q", "proj")
        assert Git(repo).squash_merge("empty", "proj", "m") is None
