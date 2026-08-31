"""The two git tools, and the argument nobody described.

`git_diff` and `git_show` carried the only five properties in the whole tool
set with no `description` — and they account for every argument-shaped refusal
in the recorded runs: 12 of 431 `git_diff` calls and 6 of 85 `git_show` calls.
Every one of the twelve is a model reaching for "compare this against the
working tree" and finding no way to say it.

The shape it takes is a consequence of strict mode. `as_strict_tool` rewrites
an optional property as `["string", "null"]` **and required**, which is how
strict mode spells "may be omitted" — so `other` must be present on every call
and JSON `null` is the only way to mean absent. Four calls sent the *string*
`"null"`, two invented `working_tree` and `WORKTREE`, and two put `HEAD` in
`other` and left `ref` empty. All eight went to git as revisions.

**Not fixed by defaulting `ref` to `HEAD`.** That covers none of the twelve,
and on the `{"ref": "", "other": "HEAD"}` shape it builds `git diff HEAD HEAD`
and answers *empty* — trading a loud refusal for a silent wrong answer, in a
tool whose whole job is to say what changed. A tool that returns a wrong answer
gets caught; one that returns nothing gets believed.

So: the properties are described, an empty `ref` refuses with what to pass
instead, and the spellings models actually produce for "nothing" are read as
nothing.
"""

import subprocess

import pytest

from code_gantry.gitops import Git
from code_gantry.plannertools import READ_TOOLS, SEMANTIC_TOOL, as_strict_tool
from code_gantry.repotools import ReadBudget, RepoReader, ToolError, _absent


@pytest.fixture
def reader(tmp_path):
    repo = tmp_path / "r"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "a.rb").write_text("one\n")
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "T"],
        ["git", "config", "commit.gpgsign", "false"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "first"],
    ):
        subprocess.run(args, cwd=repo, check=True)
    # An uncommitted change, so a working-tree diff has something to report and
    # a test asserting one cannot pass by finding nothing.
    (repo / "app" / "a.rb").write_text("one\ntwo\n")
    return RepoReader(Git(repo), repo, ReadBudget())


class TestTheArgumentsAreDescribed:
    def test_every_property_of_both_git_tools_says_what_it_is(self):
        # The sweep that found this: these five were the only undescribed
        # properties in the tool set, and they are the two tools every
        # argument-shaped refusal came from.
        for spec in READ_TOOLS:
            if spec["name"] not in {"git_show", "git_diff"}:
                continue
            for prop, schema in spec["input_schema"]["properties"].items():
                assert schema.get("description"), f"{spec['name']}.{prop}"

    def test_no_property_anywhere_is_left_undescribed(self):
        # The general form, so the next tool cannot be the one nobody
        # remembered. This is how the two were missed.
        for spec in [*READ_TOOLS, SEMANTIC_TOOL]:
            for prop, schema in spec["input_schema"].get("properties", {}).items():
                assert schema.get("description"), f"{spec['name']}.{prop}"

    def test_the_working_tree_comparison_is_spelled_out(self):
        spec = next(s for s in READ_TOOLS if s["name"] == "git_diff")
        assert "working tree" in spec["description"]
        assert "working tree" in spec["input_schema"]["properties"]["other"][
            "description"
        ]

    def test_the_description_survives_the_strict_rewrite(self):
        # Strict mode rewrites the property types. A description lost in that
        # rewrite would be invisible exactly where it is needed, since this is
        # the form the model is sent.
        spec = next(s for s in READ_TOOLS if s["name"] == "git_diff")
        props = as_strict_tool(spec)["parameters"]["properties"]
        assert props["other"]["type"] == ["string", "null"]
        assert "working tree" in props["other"]["description"]


class TestTheSpellingsModelsActuallySent:
    """Each of these is an argument taken verbatim from a recorded refusal."""

    @pytest.mark.parametrize("other", ["null", "working_tree", "WORKTREE", ""])
    def test_a_spelling_of_nothing_is_read_as_nothing(self, reader, other):
        out = reader.git_diff("HEAD", other, "app/a.rb")
        assert "+two" in out

    def test_a_real_second_ref_is_still_a_second_ref(self, reader):
        # The denylist must not eat a legitimate value. Two commits compared
        # with each other is the whole reason `other` exists.
        with pytest.raises(ToolError) as e:
            reader.git_diff("HEAD", "no-such-branch", "app/a.rb")
        assert "bad revision" in str(e.value) or "unknown revision" in str(e.value)

    def test_git_show_reads_the_string_null_as_the_pathless_form(self, reader):
        out = reader.git_show("HEAD", "null")
        assert "first" in out

    def test_a_pattern_of_null_is_untouched_because_search_is_not_filtered(
        self, reader
    ):
        # Scope check. Searching for the literal text `null` is an ordinary
        # thing to do, and a filter over every string argument would eat it.
        (reader.repo / "app" / "b.rb").write_text("x = null\n")
        subprocess.run(["git", "add", "-A"], cwd=reader.repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "second"], cwd=reader.repo, check=True
        )
        assert any("null" in line for line in reader.search("null", "app/**"))


class TestRefIsNeverAssumed:
    def test_an_empty_ref_refuses_and_says_what_to_pass(self, reader):
        with pytest.raises(ToolError) as e:
            reader.git_diff("", "HEAD", "app/a.rb")
        assert "never assumed" in str(e.value)
        assert "HEAD" in str(e.value)

    def test_it_refuses_rather_than_answering_empty(self, reader):
        # The case for not defaulting, stated as a test. Defaulted, this call
        # becomes `git diff HEAD HEAD` and returns "" — which reads as "nothing
        # changed" while a file is sitting modified in the tree.
        with pytest.raises(ToolError):
            reader.git_diff("", "HEAD")
        assert "+two" in reader.git_diff("HEAD")


class TestTheHelper:
    def test_it_is_a_denylist_of_observed_spellings(self):
        for spelling in ("null", "NULL", " none ", "nil", "working_tree", ""):
            assert _absent(spelling) is None
        for real in ("HEAD", "main", "abc123", "app/a.rb", "nullify.rb"):
            assert _absent(real) == real

    def test_none_stays_none(self):
        assert _absent(None) is None
