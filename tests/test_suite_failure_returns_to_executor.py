"""A red suite after approval is the executor's problem, with the spec in hand.

Measured on `order-edit-item-personalization-explicit-scope`: the reviewer
approved it, the full suite failed on one spec that failed twice more when
re-run alone, and it went to the planner — which spent 884 seconds and chose
`restart`, discarding an approved diff. It went to the planner only because
`max_rework_retries: 2` was already spent on two reviewer reworks, neither of
which was about anything the suite later found.

Two things follow, and neither is a judgement about how serious a finding was —
that is not something to decide mechanically.

**Approval resets the rework budget.** Approval is a real milestone: the diff is
right as far as the reviewer can tell. Failures found *after* it are a different
question from "this diff is not there yet", and the budget spent reaching
approval should not decide how the run responds to them. Once per revision, so
it stays bounded — worst case two attempts to reach approval and two to answer
the suite, then the planner.

**The failing spec joins the executor's inner loop.** Handing back "the suite
was red" without the spec asks the executor to fix something it cannot run. The
loop builds its command from what the stage declares, so the failing paths are
recorded on the stage and included when `for_loop=True`. Running a spec is not
editing it: a spec outside `edit_files` still cannot be modified, which leaves
the executor fixing the code, and that is the right outcome for a real failure.
"""

import subprocess

import pytest

from code_gantry.config import Stage


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "target"
    (r / "spec" / "features").mkdir(parents=True)
    (r / "app").mkdir()
    (r / "app" / "a.rb").write_text("x\n")
    (r / "spec" / "a_spec.rb").write_text("describe A do\nend\n")
    (r / "spec" / "features" / "big_spec.rb").write_text("describe Big do\nend\n")
    for args in (
        ["init", "-q", "-b", "main"], ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"], ["config", "commit.gpgsign", "false"],
        ["add", "-A"], ["commit", "-qm", "init"],
    ):
        subprocess.run(["git", *args], cwd=r, check=True, capture_output=True)
    return r


def _cfg(repo):
    from code_gantry.config import parse_config

    return parse_config({
        "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
        "plan_root": "PLAN.md", "full_test_command": "rspec",
        "scoped_test_command": "rspec {paths}",
        "test_file_patterns": ["spec/**/*_spec.rb"],
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.6-sol"},
    })


class TestTheFailingSpecReachesTheInnerLoop:
    def test_it_is_added_to_the_loop_command(self, repo):
        from code_gantry.gates import resolve_test_paths

        stage = Stage(
            id="s", instruction="do it", edit_files=["app/a.rb"],
            test_paths=["spec/a_spec.rb"],
            suite_failing_paths=["spec/features/big_spec.rb"],
        )
        paths = resolve_test_paths(stage, _cfg(repo), for_loop=True)
        assert "spec/features/big_spec.rb" in paths
        assert "spec/a_spec.rb" in paths

    def test_the_gate_uses_them_too(self, repo):
        """This assertion used to be its inverse, and the reversal is measured.

        It read: "the gate reads the diff, and a spec the suite happened to
        fail is not part of this stage's scope question." True of a spec the
        suite *happened* to fail — and that is not what lands in this field.
        `advance` records it only after the reviewer approved the diff, the
        full suite went red, and the baseline check attributed the failure to
        this stage rather than to the tree it started from; the predates case
        routes to the planner several lines earlier and never gets here. So by
        construction these are the specs this stage's own approved diff broke,
        and the retry exists for them.

        Measured on `remove-non-admin-catch-all-retry`, attempt 2: the loop ran
        its 25-file list — six declared paths plus nineteen recorded here — and
        it was red at 15:45:38 and red again at 15:47:41, both recorded in
        `in_loop_failures`. The gate then ran the six alone, passed in 10.3s,
        and the reviewer approved. The 246s full suite and the 106s baseline
        re-run rediscovered what the loop had held for eight and a half
        minutes, and it cost a reviewer call and a 603s planner revision.

        A gate narrower than the loop it follows can only ever ratify. This is
        `CLAUDE.md`'s "an inner loop that skips the file under edit is worse
        than none" seen from the other end: there the loop was blind to what
        the gate would judge, here the gate is blind to what the loop already
        proved, and both end with an attempt believing it succeeded.
        """
        from code_gantry.gitops import Git
        from code_gantry.gates import resolve_test_paths

        stage = Stage(
            id="s", instruction="do it", edit_files=["app/a.rb"],
            suite_failing_paths=["spec/features/big_spec.rb"],
        )
        paths = resolve_test_paths(
            stage, _cfg(repo), Git(repo), "HEAD", for_loop=False
        )
        assert "spec/features/big_spec.rb" in paths

    def test_the_gate_still_drops_one_the_stage_deleted(self, repo):
        # The loop's reason for requiring existence rather than `runnable`
        # holds identically here: naming a file the stage removed makes the
        # command unable to pass, and the gate has no more room to survive
        # that than the loop does.
        from code_gantry.gitops import Git
        from code_gantry.gates import resolve_test_paths

        stage = Stage(
            id="s", instruction="do it", edit_files=["app/a.rb"],
            suite_failing_paths=["spec/gone_spec.rb"],
        )
        paths = resolve_test_paths(
            stage, _cfg(repo), Git(repo), "HEAD", for_loop=False
        )
        assert "spec/gone_spec.rb" not in paths

    def test_a_path_that_no_longer_exists_is_dropped(self, repo):
        from code_gantry.gates import resolve_test_paths

        stage = Stage(
            id="s", instruction="do it", edit_files=["app/a.rb"],
            suite_failing_paths=["spec/gone_spec.rb"],
        )
        assert resolve_test_paths(stage, _cfg(repo), for_loop=True) == []

    def test_an_extend_keeps_them(self):
        # The branch survives, so the failures on it survive; the record of
        # what they were has to survive with them or the gate that is supposed
        # to catch them has nothing to run. Driven through `nodes.plan` in
        # `test_nodes.TestRevision` — this pins the seam.
        from code_gantry.state import evidence_surviving_a_revision

        previous = {"id": "s", "suite_failing_paths": ["spec/b_spec.rb"]}
        assert evidence_surviving_a_revision(previous, keep_branch=True) == {
            "suite_failing_paths": ["spec/b_spec.rb"]
        }

    def test_a_restart_drops_them(self):
        # The branch is discarded and re-cut from the project tip, so the diff
        # that caused those failures is gone. Naming them would send the next
        # attempt after a problem that is no longer there.
        from code_gantry.state import evidence_surviving_a_revision

        previous = {"id": "s", "suite_failing_paths": ["spec/b_spec.rb"]}
        assert evidence_surviving_a_revision(previous, keep_branch=False) == {}

    def test_nothing_recorded_stays_nothing(self):
        from code_gantry.state import evidence_surviving_a_revision

        assert evidence_surviving_a_revision({"id": "s"}, keep_branch=True) == {}
        assert evidence_surviving_a_revision(None, keep_branch=True) == {}

    def test_the_planner_cannot_author_it(self):
        # Machinery-recorded, like `excerpt_base_sha`. A model naming the spec
        # it wants run is a claim; the suite already said which one failed.
        from code_gantry.config import PLANNER_WRITABLE_FIELDS

        assert "suite_failing_paths" not in PLANNER_WRITABLE_FIELDS


class TestApprovalResetsTheReworkBudget:
    def test_the_counter_is_cleared_on_approval(self):
        from code_gantry.state import clear_rework_after_approval

        state = {"rework_attempt": 2}
        merged = {**state, **clear_rework_after_approval(state)}
        assert merged["rework_attempt"] == 0

    def test_it_only_happens_once_per_revision(self):
        # The bound. Without it: rework, approve, red suite, refund, rework,
        # approve, red suite … a cycle that never reaches the planner and pays
        # for a full suite run every lap.
        #
        # Asserted on the merged state rather than the update, because an
        # empty update is how this says "no change" — the same convention every
        # node here returns.
        from code_gantry.state import clear_rework_after_approval

        state = {"rework_attempt": 2}
        state = {**state, **clear_rework_after_approval(state)}
        state = {**state, "rework_attempt": 2}
        merged = {**state, **clear_rework_after_approval(state)}
        assert merged["rework_attempt"] == 2

    def test_a_revision_makes_it_available_again(self):
        from code_gantry.state import clear_rework_after_approval, fresh_stage_fields

        state = {"rework_attempt": 2}
        state = {**state, **clear_rework_after_approval(state)}
        state = {**state, **fresh_stage_fields(), "rework_attempt": 2}
        merged = {**state, **clear_rework_after_approval(state)}
        assert merged["rework_attempt"] == 0
