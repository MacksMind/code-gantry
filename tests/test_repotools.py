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
from orchestrator.repotools import ReadBudget, RepoReader, ToolError


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

    def test_an_untracked_file_is_refused(self, repo):
        (repo / "scratch.rb").write_text("x\n")
        with pytest.raises(ToolError, match="not tracked"):
            reader(repo).read_file("scratch.rb")

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
