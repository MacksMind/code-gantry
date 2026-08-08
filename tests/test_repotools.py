"""The planner's read access to the target repository.

Every expensive failure of the first long run traced to the planner guessing
about a repository it could not see: it invented `spec/requests/godata_spec.rb`
for a project with no godata specs and the executor hung for fifteen minutes
repairing a phantom; it took a call-site count from a stale checklist and the
reviewer blocked the stage; it left `test_paths` empty because it could not
tell which specs covered a controller, so every verify ran the whole suite.

None of that is fixable by prompting. The planner was given a directory listing
and asked questions only the files could answer.

So it gets to read. Not to run — reading is not executing, and the partition
that keeps commands out of the planner's schema is untouched. What these tests
pin is the boundary of that read: inside the repository, tracked files only,
bounded in volume, and recorded.

The tracked-only rule is a security boundary rather than tidiness. This
operator's convention puts identifiable infrastructure values — account ids,
hosted zone ids, ARNs, real domains — in environment variables or gitignored
files precisely so they are not committed. Planner context goes to a cloud API.
Tracked-only means the planner cannot read `.env`, `.agent.env` or
`cdk.context.json` even if it asks by name.
"""

import subprocess

import pytest

from orchestrator.gitops import Git
from orchestrator.repotools import (
    SEPARATOR,
    ReadBudget,
    RepoReader,
    ToolError,
    number_lines,
)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "app" / "controllers").mkdir(parents=True)
    (r / "spec" / "models").mkdir(parents=True)
    (r / "docs").mkdir()

    (r / "app" / "controllers" / "orders_controller.rb").write_text(
        "class OrdersController\n"
        "  def index\n"
        "    render text: 'one'\n"
        "  end\n"
        "  def show\n"
        "    render text: 'two'\n"
        "  end\n"
        "end\n"
    )
    (r / "spec" / "models" / "order_spec.rb").write_text("describe Order do\nend\n")
    (r / "docs" / "plan.md").write_text("# Plan\n\nStep one.\n")
    # Tracked, and hidden — the two properties that pull in opposite
    # directions once the search tool walks a filesystem instead of an index.
    (r / ".rubocop.yml").write_text("Metrics/LineLength:\n  Max: 120\n")
    (r / ".gitignore").write_text(".agent.env\nsecrets/\n")
    (r / ".agent.env").write_text("AWS_ACCOUNT_ID=123456789012\n")

    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    # Without this the fixture inherits a global `commit.gpgsign = true` and
    # every test commit raises a pinentry prompt on the operator's desktop.
    # `tests/conftest.py` has always done it; this fixture is the one that
    # forgot, and it is not a preference — a suite that blocks on a passphrase
    # dialog cannot run unattended.
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=r, check=True)
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=r, check=True)
    return r


def reader(repo, **over):
    budget = ReadBudget(**over) if over else ReadBudget()
    return RepoReader(Git(repo), repo, budget)


class TestReadFile:
    def test_reads_a_tracked_file(self, repo):
        out = reader(repo).read_file("docs/plan.md")
        assert "Step one." in out

    def test_reads_a_line_range(self, repo):
        out = reader(repo).read_file(
            "app/controllers/orders_controller.rb", start=2, end=4
        )
        assert "def index" in out and "def show" not in out

    def test_line_numbers_are_included(self, repo):
        # The planner cites what it read; without numbers it cannot, and a
        # `test_paths` or `forbidden_patterns` claim becomes unverifiable.
        out = reader(repo).read_file("docs/plan.md", start=1, end=1)
        assert out.strip().startswith("1")

    def test_a_missing_file_says_so_rather_than_returning_nothing(self, repo):
        # The godata failure: a plausible name that does not exist. An empty
        # result reads as "no matches"; the planner must learn it was wrong.
        with pytest.raises(ToolError, match="does not exist"):
            reader(repo).read_file("spec/requests/godata_spec.rb")


class TestTheLineNumberSeparator:
    """Whether a reader can tell our prefix from the file's own indentation.

    For most of this project's life it could not. The separator was two spaces
    and indentation is spaces, so a line indented by two arrived as four with
    nothing marking the boundary, and a model quoting it back into an `edit`
    quoted our padding as if it were code. Measured over 117 refused
    `old_string`s on one run, 74 — 63% — matched the file exactly once two
    spaces were removed from every line. That is not the model misremembering:
    the median gap between reading a file and failing to edit it was zero
    conversation items. It read what we sent and reproduced it faithfully.

    A non-space delimiter is the whole fix, and these tests are about the
    property rather than the character: the prefix must be unambiguous, and a
    blank line must come back blank rather than carrying the separator as
    trailing whitespace.
    """

    def test_the_file_bytes_are_recoverable_from_the_rendering(self, repo):
        # The property the model needs and did not have. Whatever the prefix
        # is, taking everything after the delimiter must give back the line.
        indented = repo / "app" / "controllers" / "orders_controller.rb"
        original = indented.read_text().splitlines()
        out = reader(repo).read_file("app/controllers/orders_controller.rb")
        for rendered, source in zip(out.splitlines(), original, strict=True):
            assert rendered.split(SEPARATOR, 1)[1] == source

    def test_the_separator_is_not_whitespace(self, repo):
        # The one thing it may not be, for the reason in the docstring.
        assert not SEPARATOR.strip() == ""

    def test_a_blank_line_carries_no_trailing_whitespace(self):
        # Under the old format a blank line rendered as ` 1470  ` — the
        # separator became trailing whitespace on a line with no content, so
        # quoting a range that spanned one failed on the emptiest line in it.
        blank = number_lines(["one", "", "three"]).splitlines()[1]
        assert blank == blank.rstrip()

    def test_real_trailing_whitespace_survives(self):
        # The rendering may not lie about the file in the other direction
        # either. Only an empty line loses the pad; a line whose content is
        # whitespace still has that content, and trimming it would be the same
        # class of defect as the one this replaces.
        out = number_lines(["one   "])
        assert out.split(SEPARATOR, 1)[1] == "one   "

    def test_every_renderer_in_the_codebase_agrees(self, repo):
        # Three places number lines: this one, the nearest-match window in a
        # refusal — whose message tells the model the bytes are "numbered as
        # `read_file` numbers them" — and the planner's excerpts. They were
        # three copies of one format string, which is how a format drifts while
        # every test stays green. They are one function now and this is what
        # says so.
        from orchestrator import edittools, executor

        assert edittools.number_lines is number_lines
        assert executor.number_lines is number_lines

    def test_numbers_are_right_aligned_so_the_text_starts_in_one_column(self):
        # Ragged numbering would reintroduce the problem it fixes: the model
        # would be reading the indentation of the *rendering* rather than of
        # the file.
        out = number_lines(["a", "b"], first=9).splitlines()
        assert len(out[0].split(SEPARATOR)[0]) == len(out[1].split(SEPARATOR)[0])


class TestConfinement:
    def test_absolute_paths_are_refused(self, repo):
        with pytest.raises(ToolError, match="outside the repository"):
            reader(repo).read_file("/etc/passwd")

    def test_parent_traversal_is_refused(self, repo):
        with pytest.raises(ToolError, match="outside the repository"):
            reader(repo).read_file("../../etc/passwd")

    def test_a_symlink_out_of_the_repo_is_refused(self, repo, tmp_path):
        secret = tmp_path / "outside.txt"
        secret.write_text("nope\n")
        (repo / "link.txt").symlink_to(secret)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "link"], cwd=repo, check=True)
        with pytest.raises(ToolError, match="outside the repository"):
            reader(repo).read_file("link.txt")

    def test_an_untracked_but_unignored_file_is_readable(self, repo):
        # Git draws this line, not us: untracked and not ignored means part of
        # the working project and simply not committed yet. It is the spec the
        # executor just wrote, or the file a human left before resuming —
        # which the agent must see, or the fix that prompted the resume is
        # invisible to it.
        (repo / "scratch.rb").write_text("x\n")
        assert "x" in reader(repo).read_file("scratch.rb")

    def test_a_gitignored_secret_is_refused_by_name(self, repo):
        # The whole point of the tracked-only rule. This file exists, is
        # readable, and holds exactly the kind of value that must not reach a
        # cloud API.
        assert (repo / ".agent.env").exists()
        with pytest.raises(ToolError, match="not tracked"):
            reader(repo).read_file(".agent.env")

    def test_absent_and_forbidden_are_different_answers(self, repo):
        # They lead to different next moves — fix the path, or stop asking —
        # so the planner must be able to tell them apart.
        r = reader(repo)
        with pytest.raises(ToolError, match="does not exist"):
            r.read_file("spec/requests/nothing_spec.rb")
        with pytest.raises(ToolError, match="not tracked"):
            r.read_file(".agent.env")


class TestListFiles:
    def test_lists_tracked_paths(self, repo):
        out = reader(repo).list_files()
        assert "docs/plan.md" in out
        assert ".agent.env" not in out

    def test_filters_by_glob(self, repo):
        out = reader(repo).list_files("spec/**/*.rb")
        assert out == ["spec/models/order_spec.rb"]

    def test_an_unmatched_glob_returns_empty_not_an_error(self, repo):
        # "Does anything match?" is a legitimate question with a legitimate
        # answer of no. That answer is what would have prevented the phantom
        # spec paths.
        assert reader(repo).list_files("spec/requests/*.rb") == []


class TestSearch:
    def test_finds_matches_with_paths_and_line_numbers(self, repo):
        hits = reader(repo).search(r"render text:")
        assert len(hits) == 2
        assert all("orders_controller.rb" in h for h in hits)
        assert any(":3:" in h for h in hits)

    def test_scopes_to_a_path_glob(self, repo):
        assert reader(repo).search(r"render", "spec/**") == []

    def test_a_double_star_glob_reaches_the_top_of_the_directory(self, repo):
        # The defect this tool was rewritten for. `git grep` was handed the
        # glob as a bare pathspec, where `*` crosses `/` and `**/` must match a
        # directory component — so `app/controllers/**/*` could match only
        # files two levels down and never saw the controller sitting directly
        # in `app/controllers`. Measured on one run's tool log: 27 of the 29
        # zero-result `**/*` searches had matches the model was never shown,
        # 8% of every search it made. A wrong answer would have been noticed;
        # an empty one reads as "not there".
        hits = reader(repo).search(r"render text:", "app/controllers/**/*")
        assert len(hits) == 2
        assert all("orders_controller.rb" in h for h in hits)

    def test_alternated_globs_are_separate_filters(self, repo):
        # The `pattern` field is alternated with `|`, so models alternate the
        # path the same way. git read the whole thing as one literal pathspec
        # containing a pipe, which matches nothing at all.
        hits = reader(repo).search(r"render text:|describe", "app/**/*|spec/**/*")
        assert any("orders_controller.rb" in h for h in hits)
        assert any("order_spec.rb" in h for h in hits)

    def test_a_bare_directory_means_everything_under_it(self, repo):
        # A third of the globs in that log were a bare name. As a ripgrep glob
        # it matches the directory entry and none of its contents, which would
        # have traded one silent empty answer for another.
        hits = reader(repo).search(r"render text:", "app")
        assert len(hits) == 2

    def test_a_tracked_dotfile_is_searchable(self, repo):
        # ripgrep skips hidden files by default and git grep does not, so the
        # swap would have quietly removed every dotfile from view — the
        # linter, CI and editor configs a migration reads constantly.
        hits = reader(repo).search(r"Metrics")
        assert hits == [".rubocop.yml:1:Metrics/LineLength:"]

    def test_the_git_directory_is_never_searched(self, repo):
        # `--hidden` walks `.git` unless it is excluded, and the exclusion is
        # order-sensitive: ripgrep lets the last matching glob win, so a
        # model's own `**/*` placed after it puts the walk straight back in.
        # What comes out is reflog lines and commit messages presented as
        # source — `.git/logs/HEAD` and `.git/COMMIT_EDITMSG` both matched a
        # commit message in the fixture that found this.
        for glob in (None, "**/*", "**"):
            hits = reader(repo).search("first", glob)
            assert not any(h.startswith(".git/") for h in hits), glob
        # Asked for directly, it is not quietly empty — it selected no files,
        # which is a different answer from "the pattern is not there".
        with pytest.raises(ToolError, match="no files matched"):
            reader(repo).search("first", ".git/**/*")

    def test_a_glob_that_selects_nothing_says_so(self, repo):
        # Distinct from an empty result, and the distinction is the next move:
        # widen the path, or fix the pattern. ripgrep gives both the same exit
        # code.
        with pytest.raises(ToolError, match="no files matched"):
            reader(repo).search("render", "nonexistent_dir/**/*")
        assert reader(repo).search("nothing_matches_this", "app/**/*") == []

    def test_a_pattern_needing_lookaround_still_matches(self, repo):
        # Models write Perl-flavoured regex. ripgrep's default engine rejects
        # lookaround outright rather than silently matching nothing, and
        # `--engine auto` retries such a pattern under PCRE2.
        hits = reader(repo).search(r"render(?= text:)")
        assert len(hits) == 2

    def test_searches_only_tracked_files(self, repo):
        (repo / "secrets").mkdir()
        (repo / "secrets" / "keys.txt").write_text("render text: leaked\n")
        hits = reader(repo).search(r"render text:")
        assert all("secrets" not in h for h in hits)

    def test_a_pattern_cannot_smuggle_an_option(self, repo):
        # The pattern is data. It reaches git as one argv element after `-e`
        # and `--`, so a leading dash is a pattern and not a flag.
        assert reader(repo).search("--output=/tmp/pwned") == []


class TestHistory:
    def test_shows_a_file_at_a_ref(self, repo):
        out = reader(repo).git_show("HEAD", "docs/plan.md")
        assert "Step one." in out

    def test_diffs_against_a_ref(self, repo):
        (repo / "docs" / "plan.md").write_text("# Plan\n\nStep two.\n")
        out = reader(repo).git_diff("HEAD", path="docs/plan.md")
        assert "Step two." in out

    def test_history_respects_confinement(self, repo):
        with pytest.raises(ToolError, match="outside the repository"):
            reader(repo).git_show("HEAD", "../escape")


class TestBudget:
    def test_a_single_read_is_capped(self, repo):
        out = reader(repo, max_lines_per_call=2).read_file(
            "app/controllers/orders_controller.rb"
        )
        assert out.count("\n") <= 3  # 2 lines plus the truncation notice
        assert "truncated" in out

    def test_the_total_is_capped_across_calls(self, repo):
        r = reader(repo, max_total_lines=6)
        r.read_file("app/controllers/orders_controller.rb")
        with pytest.raises(ToolError, match="read budget"):
            r.read_file("app/controllers/orders_controller.rb")

    def test_the_call_count_is_capped(self, repo):
        r = reader(repo, max_calls=2)
        r.list_files()
        r.list_files()
        with pytest.raises(ToolError, match="too many"):
            r.list_files()

    def test_exhaustion_is_a_message_not_a_crash(self, repo):
        # The planner has to be able to finish its answer with what it has.
        # A raised ToolError becomes a tool_result the model can read; killing
        # the call would lose the reasoning it had already done.
        r = reader(repo, max_calls=1)
        r.list_files()
        with pytest.raises(ToolError) as e:
            r.list_files()
        assert "too many" in str(e.value)


class TestProvenance:
    def test_every_call_is_recorded(self, repo):
        # Once the planner chooses its own inputs, this log is the only way to
        # explain a stage after the fact.
        r = reader(repo)
        r.read_file("docs/plan.md")
        r.search("render")
        assert [c.tool for c in r.calls] == ["read_file", "search"]
        assert r.calls[0].detail == "docs/plan.md"
        assert r.calls[0].lines > 0

    def test_a_refusal_is_recorded_as_one(self, repo):
        """What was denied, not only what was answered.

        `calls` was appended to in `_spend`, which only runs after a tool
        succeeds, so a refusal appeared in neither the run log nor
        `planner.json`. Measured over one run of 65 planning steps, 24 stopped
        at exactly the 25-call ceiling and zero refusals were recorded — a step
        that stopped because it was finished and a step that stopped because it
        was cut off were indistinguishable in the artifact.
        """
        r = reader(repo)
        r.record_refusal("read_file", "spec/models/ghost_spec.rb", "does not exist")
        assert [c.tool for c in r.calls] == ["read_file"]
        assert r.calls[0].refusal == "does not exist"
        assert r.calls[0].lines == 0

    def test_a_refusal_does_not_spend_the_call_budget(self, repo):
        """Recording the denial must not make the denial more likely.

        The ceiling counts answered calls, so a run of refusals cannot starve
        a planner of the reads it has not yet made. `_max_tool_turns` is what
        bounds a model that ignores the refusal and keeps asking.
        """
        r = reader(repo, max_calls=2)
        r.record_refusal("read_file", "nope.rb", "does not exist")
        r.record_refusal("read_file", "also-nope.rb", "does not exist")
        r.list_files()
        r.list_files()
        with pytest.raises(ToolError, match="too many"):
            r.list_files()


class TestSearchDialect:
    """A model writes Perl-flavoured regex; git's default engine is not.

    Measured on the first live run of these tools: six of twenty-four searches
    returned nothing because `\\s`, `\\b` and `(:|=>)` mean nothing to basic
    regex. Every one was a wasted call against a budget of twenty-five, and the
    pattern that eventually found the answer matched 7 sites with `-P` and 0
    without.
    """

    def test_word_boundaries_work(self, repo):
        hits = reader(repo).search(r"render\s+text:")
        assert len(hits) == 2

    def test_alternation_works(self, repo):
        hits = reader(repo).search(r"(render|def)\s+(text|index)")
        assert hits

    def test_a_genuinely_bad_pattern_still_reports(self, repo):
        with pytest.raises(ToolError, match="search failed"):
            reader(repo).search("(unclosed")


class TestPinnedToACommit:
    """Reads answer for one commit, not for the working tree.

    A live review needs no pinning: the stage's work is committed on the stage
    branch, so the tree is the state being judged. Anything reading after the
    fact needs it — replaying a review against a tree thirty stages ahead
    would let it approve a deletion because a permit list landed later, which
    is the right answer for the wrong reason and looks exactly like judgement.
    """

    def _repo(self, tmp_path):
        from orchestrator.gitops import Git

        repo = tmp_path / "r"
        repo.mkdir()
        git = Git(repo)
        git._run("init", "-q")
        git._run("config", "user.email", "t@example.com")
        git._run("config", "user.name", "T")
        (repo / "a.rb").write_text("first version\n")
        git._run("add", "-A")
        git._run("-c", "commit.gpgsign=false", "commit", "-q", "-m", "one")
        first = git.head_sha()

        (repo / "a.rb").write_text("second version\n")
        (repo / "b.rb").write_text("added later\n")
        git._run("add", "-A")
        git._run("-c", "commit.gpgsign=false", "commit", "-q", "-m", "two")
        return repo, git, first

    def test_read_file_returns_the_pinned_revision(self, tmp_path):
        repo, git, first = self._repo(tmp_path)
        pinned = RepoReader(git, repo, at_sha=first)
        assert "first version" in pinned.read_file("a.rb")
        assert "second version" in RepoReader(git, repo).read_file("a.rb")

    def test_a_file_added_later_is_refused(self, tmp_path):
        # The whole point. Without this the replay reads the future.
        repo, git, first = self._repo(tmp_path)
        with pytest.raises(ToolError) as e:
            RepoReader(git, repo, at_sha=first).read_file("b.rb")
        assert "not in the tree" in str(e.value)

    def test_a_file_deleted_later_is_still_readable(self, tmp_path):
        # Disk is not the authority when pinned: a file absent from the working
        # copy because a later stage deleted it demonstrably existed here.
        repo, git, first = self._repo(tmp_path)
        (repo / "a.rb").unlink()
        assert "first version" in RepoReader(git, repo, at_sha=first).read_file("a.rb")

    def test_list_files_reflects_the_pinned_tree(self, tmp_path):
        repo, git, first = self._repo(tmp_path)
        assert RepoReader(git, repo, at_sha=first).list_files() == ["a.rb"]
        assert RepoReader(git, repo).list_files() == ["a.rb", "b.rb"]

    def test_a_pinned_search_refuses_rather_than_answering_from_the_tree(
        self, tmp_path
    ):
        # ripgrep walks a filesystem and cannot read a git object, so a pinned
        # reader has no way to answer this. It was answerable under `git grep`
        # and nothing in `src/` ever asked — the only caller was this test.
        # Keeping it would have meant two regex dialects and two glob
        # semantics, one of them exercised by the suite alone, which is the
        # shape of a path that drifts unnoticed. Refusing is the honest
        # option: silently searching the working tree would be a pinned reader
        # reporting unpinned results.
        repo, git, first = self._repo(tmp_path)
        with pytest.raises(ToolError) as e:
            RepoReader(git, repo, at_sha=first).search("version")
        assert "pinned" in str(e.value)
        assert RepoReader(git, repo).search("version") == ["a.rb:1:second version"]

    def test_the_repository_boundary_still_holds_when_pinned(self, tmp_path):
        repo, git, first = self._repo(tmp_path)
        with pytest.raises(ToolError) as e:
            RepoReader(git, repo, at_sha=first).read_file("../outside.txt")
        assert "outside the repository" in str(e.value)


class TestIgnoredIsTheBoundaryNotTracked:
    """`.gitignore` is what keeps a secret out, and it always was.

    Tracked-only was a proxy for it: the convention on these projects puts
    account ids, hosted zone ids and ARNs in environment variables or ignored
    files *specifically* so they are never committed. Reading untracked but
    unignored files does not touch that — ignored is exactly what stays out.

    An earlier version scoped the exception to the stage's `edit_files`,
    reasoning that an untracked file in scope must be the executor's own work.
    That holds only for a fresh stage on a normal run — precheck's clean-tree
    guard exempts resumes and revisions — and it answered the wrong question:
    a human's file after a resume is one the agent needs regardless of whose
    scope it falls in.
    """

    def test_an_ignored_secret_is_still_refused_by_name(self, repo):
        assert (repo / ".agent.env").exists()
        with pytest.raises(ToolError, match="not tracked"):
            reader(repo).read_file(".agent.env")

    def test_a_file_in_an_ignored_directory_is_refused(self, repo):
        (repo / "secrets").mkdir(exist_ok=True)
        (repo / "secrets" / "keys.txt").write_text("AWS_ACCOUNT_ID=1\n")
        with pytest.raises(ToolError, match="not tracked"):
            reader(repo).read_file("secrets/keys.txt")

    def test_a_new_spec_anywhere_is_readable(self, repo):
        # No allowlist consulted: it is readable because git does not ignore
        # it, not because some stage happened to declare it.
        (repo / "elsewhere_spec.rb").write_text("describe X do\nend\n")
        assert "describe X" in reader(repo).read_file("elsewhere_spec.rb")


class TestSearchSeesWhatIsNotCommittedYet:
    """`git grep` reads the working tree — but only for *tracked* paths.

    So an edit to a tracked file is found and a newly created one is not. The
    ones that matter are precisely the new ones: a spec this attempt wrote, or
    a file a human left before resuming.

    `--untracked` is the whole fix, and it costs nothing here. This repository
    carries 617,485 untracked files and zero untracked-but-unignored, because
    the flag excludes ignored paths by default — measured, the search is
    *faster* with it than without.
    """

    def test_an_untracked_file_is_searched(self, repo):
        (repo / "spec" / "models" / "new_spec.rb").write_text(
            "describe Order do\n  it 'does the needful' do\n  end\nend\n"
        )
        hits = reader(repo).search("does the needful")
        assert any("new_spec.rb" in h for h in hits)

    def test_an_ignored_file_is_never_searched(self, repo):
        # The same boundary as reading, drawn by the same tool.
        assert reader(repo).search("AWS_ACCOUNT_ID") == []

    def test_a_pinned_reader_refuses_to_search_rather_than_seeing_the_tree(
        self, repo
    ):
        # This test used to assert the opposite — that a pinned reader saw the
        # commit and not the file written afterwards. ripgrep cannot read a
        # commit, and the risk it was guarding against is real: a reviewer
        # replaying a stage must not see work that happened later. So the
        # answer is a refusal, which cannot be mistaken for "nothing matched".
        (repo / "spec" / "later_spec.rb").write_text("does the needful\n")
        r = reader(repo)
        r.at_sha = Git(repo).head_sha()
        with pytest.raises(ToolError, match="cannot be pinned"):
            r.search("does the needful")

    def test_tracked_files_still_answer_as_before(self, repo):
        assert len(reader(repo).search(r"render text:")) == 2


class TestTheLedgerRecordsWhichLinesWereRead:
    """A path alone cannot answer whether a re-read saw the same bytes.

    Measured across 97 planner decisions of one run: 188 paths were read in
    more than one decision, and 73 of them were never modified by the run at
    all — 184 repeat decisions, ~11.8% of all `read_file` output, returning
    bytes the planner had already been shown. Whether a cached excerpt would
    remove that depends entirely on whether those were the *same lines* or
    different windows of a large file, and `call_detail` records only the
    path, so the artifact cannot say. Two of the top four are files of a few
    hundred lines, where it is probably the same read; `config/routes.rb` is
    1,700 and probably is not.

    The range recorded is the one **served**, not the one asked for. A request
    for 1-999 against a 40-line file saw 1-40, and a request clipped by the
    read budget saw less than it named — what a later reading needs is which
    bytes reached the model. This is the same choice `resolve_excerpts` makes
    when it labels a clipped excerpt with what actually arrived.

    A whole-file read keeps the bare path. That is the common case, it is
    unambiguous already, and appending `:1-40` to every one of them would
    churn the ledger for nothing.
    """

    def test_a_whole_file_read_is_still_recorded_as_the_bare_path(self, repo):
        r = reader(repo)
        r.read_file("docs/plan.md")
        assert r.calls[0].detail == "docs/plan.md"

    def test_a_ranged_read_records_the_range(self, repo):
        r = reader(repo)
        r.read_file("docs/plan.md", start=2, end=3)
        assert r.calls[0].detail == "docs/plan.md:2-3"

    def test_the_range_recorded_is_the_one_served(self, repo):
        # Asked past the end of the file. The ledger says what arrived.
        r = reader(repo)
        text = (repo / "docs" / "plan.md").read_text()
        last = len(text.splitlines())
        r.read_file("docs/plan.md", start=1, end=last + 500)
        assert r.calls[0].detail == f"docs/plan.md:1-{last}"

    def test_an_open_ended_start_records_where_it_actually_stopped(self, repo):
        r = reader(repo)
        last = len((repo / "docs" / "plan.md").read_text().splitlines())
        r.read_file("docs/plan.md", start=2)
        assert r.calls[0].detail == f"docs/plan.md:2-{last}"

    def test_a_budget_clip_is_visible_in_the_ledger(self, repo):
        # A whole-file read that the budget cut short is no longer a
        # whole-file read, and recording it as the bare path would say it was.
        from orchestrator.repotools import ReadBudget, RepoReader
        from orchestrator.gitops import Git

        body = "".join(f"line{i}\n" for i in range(1, 41))
        (repo / "docs" / "plan.md").write_text(body)
        r = RepoReader(Git(repo), repo, ReadBudget(max_lines_per_call=5))
        r.read_file("docs/plan.md")
        assert r.calls[0].detail == "docs/plan.md:1-5"

    def test_an_empty_file_records_the_bare_path(self, repo):
        # Nothing was served, so there is no range to name, and `lines: 0`
        # already says so.
        (repo / "docs" / "empty.md").write_text("")
        import subprocess
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        r = reader(repo)
        r.read_file("docs/empty.md")
        assert r.calls[0].detail == "docs/empty.md"
