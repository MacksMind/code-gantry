"""The write side of the repository.

Two things are being pinned here. The first is that a refusal is *specific*:
"not found" and "found twice" lead a model to opposite corrections, and a tool
that says only "could not edit" makes it guess. The second is that a refused
batch changes nothing at all — a half-applied one leaves the model reasoning
against a file neither party has seen, which is worse than no edit.
"""

import pytest

from orchestrator.edittools import Edit, FileEditor, apply_edits, normalise
from orchestrator.repotools import ToolError


@pytest.fixture
def editor(tmp_path):
    repo = tmp_path / "target"
    (repo / "app").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "app" / "order.rb").write_text("class Order\n  def total\n  end\nend\n")
    (repo / "docs" / "plan.md").write_text("# Plan\n")
    (repo / ".env").write_text("SECRET=1\n")
    return FileEditor(
        repo=repo,
        edit_files=["app/**", "spec/**"],
        protected=lambda rel: rel == "docs/plan.md",
    )


class TestUniqueness:
    def test_text_that_appears_twice_is_refused_with_the_count(self, editor):
        (editor.repo / "app" / "order.rb").write_text("x = 1\nx = 1\n")
        with pytest.raises(ToolError) as e:
            editor.edit("app/order.rb", [Edit("x = 1", "x = 2")])
        assert "appears 2 times" in str(e.value)
        assert "nothing has been changed" in str(e.value)

    def test_text_that_is_absent_is_a_different_refusal(self, editor):
        # Different next move: re-read, versus widen the anchor. A tool that
        # collapses these leaves the model guessing which it hit.
        with pytest.raises(ToolError) as e:
            editor.edit("app/order.rb", [Edit("def missing", "def present")])
        message = str(e.value)
        assert "does not appear" in message
        # Not the ambiguity refusal: that one counts occurrences and tells the
        # model to widen its anchor, which is the wrong advice for a miss.
        assert "times" not in message
        assert "replace_all" not in message

    def test_replace_all_changes_every_occurrence(self, editor):
        (editor.repo / "app" / "order.rb").write_text("x = 1\nx = 1\n")
        editor.edit("app/order.rb", [Edit("x = 1", "x = 2", replace_all=True)])
        assert (editor.repo / "app" / "order.rb").read_text() == "x = 2\nx = 2\n"


class TestABatchIsAllOrNothing:
    def test_a_failing_second_edit_leaves_the_file_byte_identical(self, editor):
        path = editor.repo / "app" / "order.rb"
        before = path.read_bytes()
        with pytest.raises(ToolError):
            editor.edit(
                "app/order.rb",
                [Edit("class Order", "class Purchase"), Edit("nope", "never")],
            )
        assert path.read_bytes() == before

    def test_edits_apply_in_order_and_may_match_earlier_output(self, editor):
        # Threading the buffer through is what makes this work, and it is the
        # reason each edit is not checked against the original text.
        editor.edit(
            "app/order.rb",
            [Edit("class Order", "class Purchase"), Edit("class Purchase", "class Sale")],
        )
        assert "class Sale" in (editor.repo / "app" / "order.rb").read_text()

    def test_an_empty_old_string_is_refused(self, editor):
        with pytest.raises(ToolError) as e:
            editor.edit("app/order.rb", [Edit("", "anything")])
        assert "create_file" in str(e.value)


class TestScopeIsEnforcedAtTheToolBoundary:
    def test_a_write_outside_edit_files_names_the_allowlist(self, editor):
        (editor.repo / "other.rb").write_text("x\n")
        with pytest.raises(ToolError) as e:
            editor.edit("other.rb", [Edit("x", "y")])
        assert "not in this stage's scope" in str(e.value)
        assert "app/**" in str(e.value)

    def test_a_plan_document_is_refused_even_when_in_scope(self, tmp_path):
        # Whatever the planner draws from, the executor may not edit. A stage
        # able to change its own instructions moves the goalposts it is judged
        # against, with a green suite behind it.
        repo = tmp_path / "t"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "plan.md").write_text("# Plan\n")
        ed = FileEditor(
            repo=repo,
            edit_files=["docs/**"],
            protected=lambda rel: rel == "docs/plan.md",
        )
        with pytest.raises(ToolError) as e:
            ed.edit("docs/plan.md", [Edit("# Plan", "# Other")])
        assert "plans from" in str(e.value)

    def test_an_escaping_relative_path_is_refused(self, editor):
        with pytest.raises(ToolError) as e:
            editor.edit("../outside.rb", [Edit("a", "b")])
        assert "outside the repository" in str(e.value)

    def test_a_symlink_pointing_out_of_the_repo_is_refused(self, editor, tmp_path):
        # A different arrangement from the one above and it fails for a
        # different reason: the path is inside the repo and the bytes are not.
        outside = tmp_path / "secret.txt"
        outside.write_text("secret\n")
        link = editor.repo / "app" / "link.rb"
        link.symlink_to(outside)
        with pytest.raises(ToolError) as e:
            editor.edit("app/link.rb", [Edit("secret", "leaked")])
        assert "outside the repository" in str(e.value)

    def test_an_absolute_path_outside_the_repo_is_refused(self, editor):
        with pytest.raises(ToolError) as e:
            editor.edit("/etc/hosts", [Edit("a", "b")])
        assert "outside the repository" in str(e.value)


class TestCreateAndDelete:
    def test_creating_inside_scope_works_and_makes_parents(self, editor):
        editor.create_file("spec/models/order_spec.rb", "describe Order do\nend\n")
        assert (editor.repo / "spec" / "models" / "order_spec.rb").is_file()

    def test_creating_over_a_non_empty_file_is_refused(self, editor):
        with pytest.raises(ToolError) as e:
            editor.create_file("app/order.rb", "clobbered\n")
        assert "already exists" in str(e.value)

    def test_deleting_is_its_own_tool_not_an_empty_replacement(self, editor):
        # An `old_string` the model got slightly wrong must never silently
        # empty a file, so emptying has to be asked for by name.
        editor.delete_file("app/order.rb")
        assert not (editor.repo / "app" / "order.rb").exists()

    def test_deleting_something_absent_says_so(self, editor):
        with pytest.raises(ToolError) as e:
            editor.delete_file("app/ghost.rb")
        assert "nothing to delete" in str(e.value)


class TestNormalisation:
    def test_a_missing_final_newline_is_added(self):
        assert normalise("a\nb") == "a\nb\n"

    def test_crlf_becomes_lf(self):
        assert normalise("a\r\nb\r\n") == "a\nb\n"

    def test_an_empty_file_stays_empty(self):
        # Adding a newline to nothing would make "create an empty file"
        # impossible to express.
        assert normalise("") == ""


class TestProvenance:
    def test_every_change_is_recorded(self, editor):
        editor.edit("app/order.rb", [Edit("class Order", "class Purchase")])
        assert [c.tool for c in editor.calls] == ["edit"]
        assert editor.calls[0].detail == "app/order.rb"
        assert editor.touched == {"app/order.rb"}

    def test_a_refusal_can_be_recorded_as_one(self, editor):
        editor.record_refusal("edit", "app/ghost.rb", "does not exist")
        assert editor.calls[0].refusal == "does not exist"
        assert editor.calls[0].lines == 0


class TestApplyEditsIsPure:
    def test_it_returns_text_and_touches_no_file(self):
        assert apply_edits("a b c", [Edit("b", "B")]) == "a B c"
