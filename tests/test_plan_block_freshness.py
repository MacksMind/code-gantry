"""What the plan block claims about its own documents.

The block used to tell the planner two things, and they were the wrong way
round. The frozen documents "are the plan as it stood then" — true, and its
implicature false, because no stage can edit a plan document and the run's own
findings go to the addendum, so there is no later version to have stood
differently. The reassurance that there is "no later version to go and fetch"
went instead to the *log*, which is the one document whose payload copy and
`read_file` answer are both the live worktree, so fetching it was harmless.

Measured across the recorded runs: 27 planner reads of documents already in the
payload, 20 of them the two frozen documents against 7 of the log, and 23 of the
27 ranged — the shape of fetching a span already located rather than of looking
for content. The counts follow the instruction rather than the need.

Phrased as a claim about the *run*, not the file. A human editing a plan
document from another session is possible and preflight catches it only on a
resume, so "nothing in this run changes them" stays true where "this file has
not moved" would be a promise the prompt cannot keep.
"""

from code_gantry.plandoc import PlanDocument, PlanTree
from code_gantry.prompts import _plan_block

LOG = "docs/p/progress_log.md"


def tree():
    return PlanTree(
        root=PlanDocument(path="docs/p/PLAN.md", content="ROOT"),
        children=[
            PlanDocument(path="docs/p/technical_debt.md", content="TD"),
            PlanDocument(path=LOG, content="LOG"),
        ],
    )


def intro(addendum=LOG):
    return _plan_block(tree(), addendum)[0].split("### ")[0]


class TestTheFrozenDocumentsAreNotDescribedAsStale:
    def test_it_says_nothing_in_the_run_changes_them(self):
        assert "nothing in this run changes them" in intro()

    def test_the_old_implicature_is_gone(self):
        # The exact sentence that invited the fetch.
        assert "are the plan as it stood then" not in intro()

    def test_it_says_a_read_would_return_the_same_thing(self):
        # The actionable half: not merely "unchanged" but "so do not fetch it".
        assert "what reading those paths would return" in intro()

    def test_the_claim_is_about_the_run_and_not_the_file(self):
        # "this file has not moved" would be a promise a human in another
        # session can break, and preflight only checks that on a resume.
        text = intro()
        assert "this run" in text
        assert "has not moved" not in text
        assert "identical" not in text


class TestTheLogIsStillDistinguished:
    def test_the_log_is_named_as_growing(self):
        assert "grows as stages land" in intro()

    def test_both_are_told_there_is_nothing_later_to_fetch(self):
        assert "Neither has a later version to go and fetch" in intro()

    def test_the_log_is_still_the_later_authority_on_what_is_done(self):
        assert "the log is later" in intro()


class TestAProjectWithNoLog:
    def test_the_frozen_documents_are_still_described(self):
        # Without this the only thing said about them is that they are the
        # authority, and a planner with a read tool has no reason not to check.
        text = intro(addendum=None)
        assert "nothing in this run changes them" in text
        assert "what reading those paths would return" in text

    def test_it_does_not_mention_a_log_that_does_not_exist(self):
        text = intro(addendum=None)
        assert "log" not in text.lower()
