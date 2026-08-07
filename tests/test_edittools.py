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


class TestNearestTextFoldsTheReadIntoTheRefusal:
    """A not-found refusal used to cost a read before the model could retry.

    Measured on the misses that prompted it, both from a 1,700-line routes
    file: the model wrote the file as it believed it to be — wrong indentation
    *and* a missing line — so normalising whitespace would not have caught
    either. What it needs back is the bytes.

    Every test here also holds the line that matters: the edit is still
    refused. This hands over text to read, never a repaired edit.
    """

    def test_each_route_names_itself(self):
        """Which fallback produced a window, recorded rather than inferred.

        Three mechanisms now sit between a bad `old_string` and a stalled
        attempt — the line-number separator, the anchor window and the semantic
        window — and they act on different quantities. The separator stops
        refusals happening; the windows convert a refusal into a recoverable
        one. A single refusal rate blends all three, so if the number moves
        after the next run there is no way to say which lever moved it.

        The route is therefore carried, not derived. `_refusal_kind` buckets by
        matching words in the message, and every window renders with the same
        words — the distinction is invisible to it by construction, which is
        the failure mode where a mechanism can be reported as *available*
        forever and never shown to have *fired*.
        """
        from orchestrator.edittools import nearest_text

        text = "class A\n  def go\n    work\n  end\nend\n"

        # The first line matches exactly once: a location, not a guess.
        assert nearest_text(text, "  def go\n    work").route == "anchor"

        # No exact anchor, but the block is close enough on a whole-file scan.
        assert nearest_text(text, "  def going\n    work\n  end").route == "window"

        # Neither of the above, and the index supplies a line that still
        # exists. Its own text is the key, because the `old_string` has already
        # been shown to be wrong and the chunk was at some point real.
        #
        # Note what this fixture has to do that the others do not: the model's
        # *first* line is wrong too. Every earlier locator test quoted a first
        # line that matched the file exactly modulo indentation — which is the
        # anchor's case — so the locator was never consulted in any of them,
        # and a probe over the whole suite found this branch returning a window
        # zero times. A fallback can be exercised by name and never once run.
        indexed = "\n".join(
            ["filler line here"] * 20
            + ["  def calculate_total_amount", "    brand_new_body_here", "  end"]
            + ["tail line here"] * 20
        )
        stale = "  def calculate_total_amount\n    old_body\n  end"
        want = "  def compute_sum\n    old_body\n  end"
        assert nearest_text(indexed, want).route == "none", "no lead without one"
        assert nearest_text(indexed, want, locate=lambda _: [stale]).route == "semantic"

    def test_a_declined_window_is_distinguishable_from_no_lead(self):
        # Both refuse without a window and they mean opposite things: one
        # found too many places, the other none. Told apart, "the anchor is
        # ambiguous" is a threshold to tune; blended, it is noise.
        from orchestrator.edittools import nearest_text

        ambiguous = nearest_text("  end\nx\n  end\ny\n  end\n", "  end")
        assert ambiguous is not None and ambiguous.text == ""
        assert ambiguous.route == "ambiguous"

        nothing = nearest_text("completely\nunrelated\ncontent\n", "def m\n  raise\nend")
        assert nothing is not None and nothing.text == ""
        assert nothing.route == "none"

    def test_it_finds_the_block_when_the_indent_is_wrong(self):
        from orchestrator.edittools import nearest_text

        text = "class A\n  def go\n    work\n  end\nend\n"
        near = nearest_text(text, "      def go\n        work\n      end")
        assert "  def go" in near.text
        assert near.text.lstrip().startswith("1"), "numbered like read_file"

    def test_it_finds_it_when_a_line_was_left_out(self):
        # The real failure: the model omitted a line the block contains.
        from orchestrator.edittools import nearest_text

        text = "a\n  scope 'x' do\n    get '/'\n    post 'y'\n  end\nb\n"
        near = nearest_text(text, "    scope 'x' do\n      post 'y'\n    end")
        assert "get '/'" in near.text

    def test_it_declines_when_the_anchor_is_ambiguous(self):
        # Several places look alike, so a single window would be a guess about
        # which — and a confident wrong location invites an edit somewhere the
        # model never meant.
        from orchestrator.edittools import nearest_text

        text = "  end\nx\n  end\ny\n  end\n"
        assert nearest_text(text, "  end").text == ""

    def test_it_declines_when_nothing_is_close(self):
        from orchestrator.edittools import nearest_text

        text = "completely\nunrelated\ncontent\nhere\n"
        assert nearest_text(text, "def some_method\n  raise\nend").text == ""

    def test_the_refusal_carries_it_and_still_refuses(self, editor):
        from orchestrator.edittools import Edit

        (editor.repo / "app" / "order.rb").write_text(
            "class Order\n  def total\n    sum\n  end\nend\n"
        )
        before = (editor.repo / "app" / "order.rb").read_bytes()

        with pytest.raises(ToolError) as e:
            editor.edit("app/order.rb", [Edit("    def total\n      sum\n    end", "x")])

        message = str(e.value)
        assert "does not appear" in message
        assert "closest place" in message
        assert "def total" in message, "the real bytes are in the refusal"
        assert "Nothing has been changed." in message
        assert (editor.repo / "app" / "order.rb").read_bytes() == before

    def test_a_hopeless_miss_still_says_read_it_again(self, editor):
        from orchestrator.edittools import Edit

        with pytest.raises(ToolError) as e:
            editor.edit("app/order.rb", [Edit("nothing like this at all", "x")])
        assert "Read it again" in str(e.value)


class TestALocatorAnchorsOnTextKnownToHaveBeenReal:
    """A locator's hit is a chunk boundary, not the start of what was wanted.

    Windowing straight from it could return a block that does not contain the
    text at all. So it narrows the search and the same local matcher picks the
    window inside that neighbourhood — which is also more accurate than the
    global scan, since there is less file in which to find a coincidence.
    """

    def test_a_chunks_own_line_places_the_window(self):
        from orchestrator.edittools import nearest_text

        lines = (
            ["filler line here"] * 20
            + ["  def target_method", "    body here now", "  end"]
            + ["tail line here"] * 20
        )
        text = "\n".join(lines)
        near = nearest_text(
            text,
            # The model's own first line is wrong too — it renamed the method
            # earlier in the session and is quoting the name it wrote. That is
            # what makes this the locator's case rather than the anchor's, and
            # the earlier version of this test got it wrong: it quoted the real
            # name at the wrong indent, which the anchor places on its own, so
            # the locator was never consulted and this assertion passed without
            # the branch it is named for ever running.
            "      def renamed_method\n        something else\n      end",
            # Stale indexed text: the body has since changed, the declaration
            # has not. That surviving line is what places the window.
            locate=lambda _: ["    def target_method\n      old body\n    end"],
        )
        assert near.route == "semantic"
        assert "def target_method" in near.text

    def test_a_locator_pointing_at_nothing_relevant_returns_nothing(self):
        # Corroboration lowers the bar; it does not remove it.
        from orchestrator.edittools import nearest_text

        text = "\n".join(["unrelated"] * 200)
        assert nearest_text(
            text, "def a\n  b\nend", locate=lambda _: ["nothing at all like the file"]
        ).text == ""

    def test_no_locator_leaves_the_old_behaviour_exactly(self):
        from orchestrator.edittools import nearest_text

        text = "a\n  scope 'x' do\n    get '/'\n  end\nb\n"
        want = "    scope 'x' do\n      get '/'\n    end"
        assert nearest_text(text, want) == nearest_text(text, want, locate=lambda _: [])

    def test_the_anchor_still_wins_before_any_locator_is_consulted(self):
        # The cheap, exact path first: a locator is a last resort, and calling
        # one when the anchor already placed the text would be a network round
        # trip for an answer we have.
        from orchestrator.edittools import nearest_text

        called = []
        text = "a\n  def go\n    work\n  end\nb\n"
        nearest_text(
            text, "      def go\n        work\n      end",
            locate=lambda w: called.append(w) or ["irrelevant"],
        )
        assert called == [], "the locator was consulted despite an anchor match"


class TestSeveralChunksComeBackForOneFile:
    def test_the_anchor_narrows_and_the_matcher_still_picks(self):
        """The first surviving line places the *search*, not the window.

        Ranking is the index's and the walk keeps it, but a chunk ranked first
        is not thereby the answer — it only says where to look. Here the
        highest-ranked chunk anchors on `alpha`, and the matcher still lands on
        `beta`, which is what the wanted text actually resembles.
        """
        from orchestrator.edittools import nearest_text

        lines = [
            "def alpha_method_here", "  a", "end",
            "def beta_method_here", "  b", "end",
        ]
        text = "\n".join(lines)
        near = nearest_text(
            text,
            # Wrong name, so the anchor cannot place it and the walk is
            # reached. Quoting `beta_method_here` here would be an exact
            # unique first line, which the anchor handles before any locator
            # is consulted — the shape this test used to have.
            "def renamed_beta\n  something stale\nend",
            locate=lambda _: [
                "def alpha_method_here\n  old\nend",   # ranked first, still present
                "def beta_method_here\n  older\nend",
            ],
        )
        assert near.route == "semantic"
        assert "beta_method_here" in near.text

    def test_a_line_appearing_twice_is_never_the_anchor(self):
        # It names no single place, and a wrong place is worse than none.
        from orchestrator.edittools import nearest_text

        text = "\n".join(["  duplicated_line_here"] * 2 + ["unique_line_over_here", "x"])
        near = nearest_text(
            text,
            "totally different\ncontent entirely\nnot here",
            locate=lambda _: ["  duplicated_line_here\nunique_line_over_here"],
        )
        # The duplicated line is skipped; the unique one anchors, but nothing
        # nearby resembles the wanted text, so it still declines.
        assert near.text == ""
