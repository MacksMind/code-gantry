"""A note states what is true now, never what changed since the last one.

`CLAUDE.md` has carried *assert state, not change* for a long time, and it was
never said to the two roles that write the project's durable prose. The result
is visible in a real plan tree: **51 parenthetical asides across nine
documents** recording what the document used to say — `*(was 23 patterns under
12 headings)*`, `*(was recorded as 1, 8, 9, 11-14 and 19)*`, `*(recorded at
various times as a browser session, then two blockers, then one — none of them
was real)*` — with the plan root carrying twenty of them on its own.

They are not merely untidy. They sit inside the cached prefix and are re-read on
every call; they cannot be checked against the tree, because they describe a
document's past rather than the code's present; and a rule phrased as a change
becomes unsatisfiable the moment the change is already true, which is the same
defect `forbidden_patterns` and `must_not_remain` exist to keep apart.

And they regenerate. A note written in that register is folded into the plan,
and the plan is what the next planner reads before writing its next note, so the
register is taught by the corpus faster than a fold can remove it. Measured on
one derivation: the finding itself was outcome-shaped — "the bump is one
indivisible change" — and the body beneath it was three clauses of delta about
what an earlier run concluded and which documents still disagreed. Nothing asked
for that.

So the discipline goes to the roles that write, not only to the pass that
cleans up afterwards. Fixing the fold alone cannot converge: the fold controls
what is copied in, and this controls what is written.
"""

import pytest


def _planner_note_text() -> str:
    from orchestrator.planner import PlannerResponse

    return PlannerResponse.model_fields["plan_notes"].description


def _reviewer_text() -> str:
    """What the reviewer is actually sent, not the base constant.

    The `record` and `observations` guidance lives in `REVIEW_TOOLS_PROMPT`,
    which `_review_system_prompt` appends only when the project grants repo
    access — so asserting against `REVIEW_SYSTEM_PROMPT` would be asking about
    a string the reviewer never receives on its own.
    """
    from types import SimpleNamespace

    from orchestrator.prompts import _review_system_prompt

    cfg = SimpleNamespace(reviewer=SimpleNamespace(repo_access=True))
    return _review_system_prompt(cfg)


class TestBothRolesGetTheSameSentence:
    """One constant, substituted twice — not two paraphrases that agree today.

    Written the wrong way first: a paragraph in `planner.py` and another in
    `prompts.py`, which is how `REPOSITORY_TEXT_IS_EVIDENCE` came to exist and
    exactly what its docstring warns against. The two copies had already
    diverged before either was committed — one said "a note says what is true
    now", the other "say what is true now" — and nothing would have failed as
    they drifted further, because each half goes on reading as correct.

    Keyword assertions would not have caught it either: both copies contained
    "true now", "previously" and "commit log". A test that checks for the words
    passes on two prompts that have come to mean different things, so this
    checks for the constant.
    """

    @pytest.mark.parametrize(
        "text", [_planner_note_text, _reviewer_text], ids=["planner", "reviewer"]
    )
    def test_the_shared_constant_reaches_the_prompt(self, text):
        from orchestrator.plannertools import STATE_NOT_CHANGE

        assert STATE_NOT_CHANGE in text()

    def test_no_marker_survives_into_a_prompt(self):
        # A template marker reaching a model is worse than a missing rule: it
        # is unreadable and says nothing about how to write anything.
        for text in (_planner_note_text(), _reviewer_text()):
            assert "%%" not in text

    def test_it_names_the_thing_not_to_write(self):
        # The rule has to say what the bad shape *is*. "Be concise" would not
        # have stopped any of the fifty-one, every one of which is a short,
        # well-written sentence about what a document used to say.
        from orchestrator.plannertools import STATE_NOT_CHANGE

        body = STATE_NOT_CHANGE.lower()
        assert "used to" in body or "previously" in body

    def test_it_says_where_history_belongs_instead(self):
        # Deleting a channel without naming its replacement is how a fact that
        # had a home ends up with none. History is answerable from the commit
        # log, which cannot go stale and costs nothing to carry.
        from orchestrator.plannertools import STATE_NOT_CHANGE

        assert "commit log" in STATE_NOT_CHANGE.lower()


class TestItStaysProjectAgnostic:
    """No project's vocabulary in a string every project's models read."""

    @pytest.mark.parametrize(
        "text", [_planner_note_text, _reviewer_text], ids=["planner", "reviewer"]
    )
    def test_no_framework_or_language_names(self, text):
        body = text().lower()
        for word in ("rails", "ruby", "rspec", "gem", "paperclip", "django", "npm"):
            assert word not in body, f"{word!r} is one project's vocabulary"

    @pytest.mark.parametrize(
        "text", [_planner_note_text, _reviewer_text], ids=["planner", "reviewer"]
    )
    def test_no_path_shaped_examples(self, text):
        # A path in a shared prompt is a hint about a layout the reader may not
        # have. The layout block already shows each project its own.
        body = text()
        for fragment in ("app/", "spec/", "src/", "docs/", ".rb", ".py"):
            assert fragment not in body, f"{fragment!r} is one project's layout"
