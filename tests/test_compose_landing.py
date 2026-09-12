"""Landing through a composing bay.

A stage that is approved is squashed to one candidate commit on the base it
was cut from and pushed as its own branch. The project branch is moved by
nobody until a bay holds the landing semaphore and composes what is pending.
A bare repository stands in for origin.
"""

import pytest

from test_config import as_test_tools, minimal
from test_nodes import THE_ITEM, THE_OTHER_ITEM, make, with_stage
from test_mesh import FakeDaemon, daemon_state  # noqa: F401
from test_remote_landing import origin, origin_tip, other_lands, sh  # noqa: F401

from code_gantry import lander, nodes
from code_gantry.config import ConfigError, parse_config
from code_gantry.gitops import Git, GitError


@pytest.fixture
def a_daemon(daemon_state):
    """A daemon that grants the landing semaphore. The whole path is
    exercised rather than patched out, because "may this bay land" is the
    question the composition turns on."""
    from code_gantry import mesh

    daemon = FakeDaemon(mesh.socket_path())
    yield daemon
    daemon.stop()


def compose_cfg(**over):
    return {"remote_landing": True, "compose_landings": True, **over}


def finish_a_stage(repo, tmp_path, **over):
    """A stage pushed as a candidate, with the bay held back from composing
    it: these are the tests about what pushing a candidate does."""
    from unittest import mock

    cfg, rt, state = make(repo, tmp_path, **compose_cfg(**over))
    state = with_stage(state, rt)
    (repo / "app.py").write_text("stage work\n")
    state = {**state, "review_summary": "fine", "review_record": "did it"}
    with mock.patch.object(nodes, "_compose_if_free"):
        out = nodes.advance(state, rt)
    return cfg, rt, state, out


class TestTheFlag:
    def test_it_is_off_by_default(self):
        assert parse_config(as_test_tools(minimal())).compose_landings is False

    def test_it_needs_somewhere_to_push(self):
        # A candidate nobody else can fetch can be composed by nobody but
        # the bay that wrote it, which is the arrangement it replaces.
        with pytest.raises(ConfigError) as e:
            parse_config(as_test_tools(minimal(compose_landings=True)))
        assert "remote_landing" in str(e.value)


class TestWhatAFinishedStageDoes:
    def test_the_project_branch_is_not_moved(self, repo, tmp_path, origin):
        bare, _ = origin
        before = origin_tip(bare)
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        assert origin_tip(bare) == before, "a bay moved the project branch by itself"
        assert rt.git.rev_parse("proj") == before

    def test_the_stage_is_pushed_as_one_commit_on_its_base(self, repo, tmp_path, origin):
        bare, _ = origin
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        branch = state["stage_branch"]
        pushed = sh(bare, "rev-parse", branch)
        assert sh(bare, "rev-parse", f"{branch}^") == state["stage_start_sha"]
        assert sh(bare, "show", f"{pushed}:app.py") == "stage work"

    def test_the_candidate_is_recorded_as_pending(self, repo, tmp_path, origin):
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        [candidate] = rt.views().pending_candidates()
        assert candidate.branch == state["stage_branch"]
        assert candidate.base == state["stage_start_sha"]
        assert candidate.stage_id == "extract"
        assert THE_ITEM in candidate.landing["keys"]

    def test_nothing_is_recorded_as_landed(self, repo, tmp_path, origin):
        # Nothing is on the project branch, so nothing has landed. A key
        # marked landed here would be a claim about a tree no one holds.
        # (`with_stage` stands in for precheck and writes no claim, so what
        # is asserted is that the key did not move to landed.)
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        assert rt.views().state(THE_ITEM).state != "landed"
        assert [e.kind for e in rt.ledger.events() if e.kind == "landed"] == []

    def test_the_bay_ends_on_the_project_branch_with_a_clean_tree(self, repo, tmp_path, origin):
        # The next stage is cut from the same base as this one, not stacked
        # on it: two stages drawn against one plan are independent, and a
        # rebase would invalidate the base the ledger recorded.
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        assert rt.git.current_branch() == "proj"
        assert rt.git.is_clean()

    def test_the_local_stage_branch_is_gone(self, repo, tmp_path, origin):
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        assert not rt.git.branch_exists(state["stage_branch"])

    def test_the_candidate_carries_the_landing_message(self, repo, tmp_path, origin):
        bare, _ = origin
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        assert "[extract]" in sh(bare, "log", "-1", "--format=%s", state["stage_branch"])

    def test_a_neighbour_landing_first_changes_nothing_about_the_candidate(self, repo, tmp_path, origin):
        # The candidate is built against its own base, so it neither
        # conflicts with nor reverts what landed while it was in flight.
        bare, other = origin
        other_lands(other)
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        branch = state["stage_branch"]
        changed = sh(bare, "diff", "--name-only", f"{branch}^", branch).split()
        assert changed == ["app.py"]

    def test_a_stage_that_changed_nothing_pushes_no_candidate(self, repo, tmp_path, origin):
        cfg, rt, state = make(repo, tmp_path, **compose_cfg())
        state = with_stage(state, rt)
        state = {**state, "review_summary": "fine", "review_record": "did it"}
        nodes.advance(state, rt)
        assert rt.views().pending_candidates() == []


def a_candidate(repo, tmp_path, *, name, content=None, path="app.py", land=False, keys=None, files=None, **over):
    """One finished stage, pushed as a candidate.

    `land` is whether the bay is allowed to go on and compose, which it
    ordinarily offers to do the moment it has pushed. The tests that are
    about composing hold it back so they can say when, and with what.
    """
    from unittest import mock

    cfg, rt, state = make(repo, tmp_path, **compose_cfg(**over))
    state = with_stage(state, rt, id=name, **({"plan_keys": keys} if keys else {}))
    for where, what in (files or {path: content}).items():
        (repo / where).write_text(what)
    state = {**state, "review_summary": "fine", "review_record": "did it"}
    if land:
        nodes.advance(state, rt)
    else:
        with mock.patch.object(nodes, "_compose_if_free"):
            nodes.advance(state, rt)
    return cfg, rt, state


class TestComposingWhatIsPending:
    def test_two_candidates_land_together_on_one_suite(self, repo, tmp_path, origin, a_daemon):
        bare, _ = origin
        counter = tmp_path / "suites"
        a_candidate(repo, tmp_path, name="first", content="one\n",
                    full_test_command=f"echo x >> {counter}")
        cfg, rt, _ = a_candidate(repo, tmp_path, name="second", content="two\n", path="other.py",
                                 full_test_command=f"echo x >> {counter}")
        counter.write_text("")

        outcome = lander.compose(rt)
        assert outcome is not None and outcome.sha
        assert {c.stage_id for c in outcome.landed} == {"first", "second"}
        assert counter.read_text().count("x") == 1, "one composition, one suite"
        assert sh(bare, "show", f"proj:app.py") == "one"
        assert sh(bare, "show", f"proj:other.py") == "two"

    def test_nothing_is_pending_afterwards(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, _ = a_candidate(repo, tmp_path, name="first", content="one\n")
        lander.compose(rt)
        assert rt.views().pending_candidates() == []

    def test_the_landed_keys_are_recorded_against_the_composed_commit(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, _ = a_candidate(repo, tmp_path, name="first", content="one\n")
        outcome = lander.compose(rt)
        landed = rt.views().state(THE_ITEM)
        assert landed.state == "landed"
        assert landed.sha == outcome.sha, "a key landed against a tree nobody holds"

    def test_the_branches_it_landed_are_taken_off_the_remote(self, repo, tmp_path, origin, a_daemon):
        bare, _ = origin
        cfg, rt, state = a_candidate(repo, tmp_path, name="first", content="one\n")
        assert sh(bare, "branch", "--list", state["stage_branch"])
        lander.compose(rt)
        assert sh(bare, "branch", "--list", state["stage_branch"]) == ""

    def test_the_composed_tip_is_recorded_green(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, _ = a_candidate(repo, tmp_path, name="first", content="one\n",
                                 full_test_command="true")
        outcome = lander.compose(rt)
        assert rt.views().proven_green(outcome.sha, "true")

    def test_a_bay_that_cannot_have_the_semaphore_composes_nothing(self, repo, tmp_path, origin):
        # With no daemon nobody may land: one host cannot decide alone that
        # it is the only one moving the project branch.
        bare, _ = origin
        before = origin_tip(bare)
        cfg, rt, _ = a_candidate(repo, tmp_path, name="first", content="one\n")
        assert lander.compose(rt) is None
        assert origin_tip(bare) == before

    def test_with_nothing_pending_it_does_nothing(self, repo, tmp_path, origin):
        cfg, rt, state = make(repo, tmp_path, **compose_cfg())
        assert lander.compose(rt) is None


class TestWhenTheCompositionIsRed:
    def _poisoned(self, repo, tmp_path):
        """Two candidates: one harmless, one that only fails in company."""
        suite = "test ! -e poison.txt"
        a_candidate(repo, tmp_path, name="good", content="fine\n",
                    full_test_command=suite)
        cfg, rt, state = a_candidate(repo, tmp_path, name="bad", content="x\n",
                                     path="poison.txt", full_test_command=suite)
        return cfg, rt, state

    def test_the_guilty_candidate_is_removed_and_the_rest_land(self, repo, tmp_path, origin, a_daemon):
        bare, _ = origin
        cfg, rt, state = self._poisoned(repo, tmp_path)
        outcome = lander.compose(rt)

        assert [c.stage_id for c in outcome.landed] == ["good"]
        assert [c.stage_id for c, _ in outcome.rejected] == ["bad"]
        assert sh(bare, "show", "proj:app.py") == "fine"

    def test_the_rejected_candidate_keeps_its_branch_and_its_reason(self, repo, tmp_path, origin, a_daemon):
        bare, _ = origin
        cfg, rt, state = self._poisoned(repo, tmp_path)
        lander.compose(rt)

        [rejection] = rt.views().rejected.values()
        assert rejection.candidate.stage_id == "bad"
        assert "red" in rejection.reason
        assert sh(bare, "branch", "--list", rejection.candidate.branch), (
            "the work was thrown away with the rejection"
        )

    def test_the_guilty_keys_are_not_landed(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, state = self._poisoned(repo, tmp_path)
        lander.compose(rt)
        landed = [e for e in rt.ledger.events() if e.kind == "landed" and e.stage_id == "bad"]
        assert landed == []

    def test_a_candidate_that_will_not_replay_is_rejected_rather_than_resolved(self, repo, tmp_path, origin, a_daemon):
        # Two stages editing the same lines. A conflict is a disagreement
        # about the work, and no merge strategy is entitled to settle it.
        bare, other = origin
        a_candidate(repo, tmp_path, name="first", content="theirs\n")
        cfg, rt, state = a_candidate(repo, tmp_path, name="second", content="mine\n")
        outcome = lander.compose(rt)

        assert len(outcome.landed) == 1
        assert len(outcome.rejected) == 1
        assert "replay" in outcome.rejected[0][1]


class TestWhenTheBranchItselfIsRed:
    def test_nothing_is_rejected_for_a_tip_that_was_already_broken(self, repo, tmp_path, origin, a_daemon):
        # A red tip looks exactly like a bad first candidate. Rejecting the
        # stage would send a person to read the wrong diff, and then do it
        # again to the next stage, and the next.
        bare, other = origin
        cfg, rt, state = a_candidate(repo, tmp_path, name="first", content="one\n",
                                     full_test_command="test ! -e poison.txt")
        other_lands(other, name="poison.txt", text="the branch was broken\n")

        outcome = lander.compose(rt)
        assert outcome.escalation is not None
        assert "red before anything was composed" in outcome.escalation["escalation_reason"]
        assert outcome.rejected == [], "a good stage was blamed for a broken branch"
        assert rt.views().pending_candidates(), "the candidate was thrown away"

    def test_a_composition_that_will_not_go_green_asks_for_a_person(self, repo, tmp_path, origin, a_daemon):
        suite = "test ! -e poison.txt"
        a_candidate(repo, tmp_path, name="bad-one", content="x\n", path="poison.txt",
                    full_test_command=suite)
        cfg, rt, state = a_candidate(repo, tmp_path, name="bad-two", content="y\n", path="poison2.txt",
                                     full_test_command=suite, limits={"max_compose_suites": 1})
        outcome = lander.compose(rt)
        assert outcome.escalation is not None
        assert "still red" in outcome.escalation["escalation_reason"]


class TestWhenABayOffersToLand:
    """Landing a stage is two halves of `advance` and nothing else's
    business: make the candidate, then land what is pending if this bay can
    have the semaphore."""

    def test_finishing_a_stage_offers_to_land_what_is_pending(self, repo, tmp_path, origin, a_daemon):
        bare, _ = origin
        before = origin_tip(bare)
        cfg, rt, state = a_candidate(repo, tmp_path, name="first", content="one\n", land=True)
        assert origin_tip(bare) != before, "finishing a stage landed nothing"
        assert sh(bare, "show", "proj:app.py") == "one"

    def test_it_is_offered_from_advance_and_from_nowhere_else(self):
        # Parsed rather than asserted in prose: the second half of a
        # landing belongs to the node that does the first half.
        import ast
        from pathlib import Path

        tree = ast.parse(Path("src/code_gantry/nodes.py").read_text())
        callers = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Name)
                and c.func.id == "_compose_if_free"
                for c in ast.walk(node)
            )
        ]
        assert callers == ["advance"], callers


class TestWhatTheRecordClaims:
    """A record published from a run is a claim in every later prompt, and
    `completed` is the cacheable prefix of the paid ones. A candidate is not
    a landing: it is a commit on a branch of its own that no composition has
    taken yet, and may never take."""

    def test_a_candidate_is_not_recorded_as_a_landing(self, repo, tmp_path, origin):
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        [entry] = out["completed"]
        assert entry["merge_sha"] is None, "a stage claimed to have landed on the project branch"
        assert entry["candidate_sha"], "the candidate went unrecorded"

    def test_the_planner_is_told_it_is_a_candidate_and_not_a_landing(self, repo, tmp_path, origin):
        from code_gantry.prompts import build_planner_messages

        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        messages = build_planner_messages(
            cfg=cfg, plan_text="p", projection="", completed=out["completed"],
            current_stage=None, failure=None, opening_failure=None, gate_history=[],
            revision=0, interventions_used=0, interventions_max=5, layout="",
        )
        text = "\n".join(
            block.get("text", "")
            for message in messages
            for block in (message.get("content") or [])
            if isinstance(block, dict)
        )
        assert "Landed as" not in text
        assert "pushed as candidate" in text.lower()

    def test_a_run_that_ends_is_not_told_the_branch_moved_under_it(self, repo, tmp_path, origin):
        # Every bay moves the project branch when it holds the landing
        # semaphore, so a tip that differs from anything this run produced
        # is the arrangement working rather than something outside it.
        cfg, rt, state, out = finish_a_stage(repo, tmp_path)
        final = nodes.finalize({**state, "completed": out["completed"]}, rt)
        assert final.get("failure_layer") != "branch_moved", final
        assert final["status"] == "complete"


class TestReworkingARejectedCandidate:
    """A rejection is work that was finished and is now red beside what
    landed while it waited. A bay looking for something to do takes that
    before anything the planner would draw: it is closer to done, and it is
    holding plan keys while it waits."""

    def _rejected(self, repo, tmp_path, origin):
        """One landed stage and one rejected by it."""
        suite = "test ! -e poison.txt"
        a_candidate(repo, tmp_path, name="good", content="fine\n", full_test_command=suite)
        # Real work *and* the thing that only fails in company, so the
        # rework has something left to be about once the poison is gone.
        cfg, rt, state = a_candidate(repo, tmp_path, name="bad",
                                     files={"other.py": "the work\n", "poison.txt": "x\n"},
                                     full_test_command=suite, keys=[THE_OTHER_ITEM])
        lander.compose(rt)
        return cfg, rt, state

    def test_the_keys_it_was_holding_are_given_back(self, repo, tmp_path, origin, a_daemon):
        # Otherwise no bay can ever take it: a claim never takes what
        # another run holds, and the run that made the candidate is holding
        # these while working something else entirely.
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        [rejection] = rt.views().rejected.values()
        for key in rejection.candidate.landing["keys"]:
            assert rt.views().state(key).state == "open"

    def test_a_bay_takes_the_rework_before_anything_drawn(self, repo, tmp_path, origin, a_daemon, monkeypatch):
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        drawn = []
        monkeypatch.setattr(nodes, "_take_derived", lambda *a, **k: drawn.append(True))
        out = nodes._take_rework(rt, state)
        assert out is not None
        assert out["current"]["id"] == "bad"
        assert out["next_hop"] == "precheck"
        assert drawn == [], "a drawn stage was taken while rework was waiting"

    def test_it_starts_on_the_new_tip_with_the_work_re_applied(self, repo, tmp_path, origin, a_daemon):
        # The base it was built against is not what the branch holds any
        # more, and the failure it has to answer is against the tree as it
        # is now. The branch is put on the new tip and the work comes back
        # uncommitted, so what the executor gets is a working tree.
        bare, _ = origin
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        out = nodes._take_rework(rt, state)

        assert out["stage_start_sha"] == origin_tip(bare)
        assert rt.git.head_sha() == origin_tip(bare), "the branch carries a commit already"
        assert (repo / "app.py").read_text() == "fine\n", "what landed is not there"
        assert (repo / "poison.txt").exists(), "the stage's own work was not re-applied"
        assert (repo / "other.py").read_text() == "the work\n"

    def test_nothing_is_left_half_done_for_a_later_command(self, repo, tmp_path, origin, a_daemon):
        # A `git rebase` that conflicts leaves an operation in progress for
        # somebody to continue. This must leave an ordinary working tree.
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        nodes._take_rework(rt, state)
        assert not (repo / ".git" / "CHERRY_PICK_HEAD").exists()
        assert not (repo / ".git" / "rebase-merge").exists()
        rt.git.commit_all("the executor's first cycle")

    def test_the_executor_is_told_both_what_changed_and_what_is_still_to_do(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        out = nodes._take_rework(rt, state)
        summary = out["last_failure"]["summary"]
        assert out["last_failure"]["layer"] == "composition"
        assert "could not be landed" in summary
        assert "red" in summary, "what the composition found is missing"
        assert "instruction is unchanged" in summary, "the original scope was not carried over"
        assert out["current"]["instruction"] == state["current"]["instruction"] if state.get("current") else True

    def test_two_bays_never_rework_one_branch(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        assert nodes._take_rework(rt, state) is not None
        other, other_state = make(repo, tmp_path, **compose_cfg())[1], state
        assert rt.views().rework_waiting() == [], "the rejection was still on offer"

    def test_a_rework_that_cannot_even_be_set_up_is_left_for_a_person(self, repo, tmp_path, origin, a_daemon, monkeypatch):
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        monkeypatch.setattr(
            rt.git, "reset_branch_to",
            lambda *a, **k: (_ for _ in ()).throw(GitError("the branch is gone")),
        )
        assert nodes._take_rework(rt, state) is None
        [rejection] = rt.views().rejected.values()
        assert rejection.taken_run, "it went back on offer to the next bay to ask"
        assert "could not be set up" in rejection.reason

    def test_coming_back_makes_it_a_candidate_again(self, repo, tmp_path, origin, a_daemon):
        # The rework rejoins the ordinary path: `advance` squashes it to a
        # candidate and offers to land, whether this bay can or not.
        cfg, rt, state = self._rejected(repo, tmp_path, origin)
        out = nodes._take_rework(rt, state)
        state = {**state, **out, "review_summary": "fixed", "review_record": "removed the poison"}
        (repo / "poison.txt").unlink()
        nodes.advance(state, rt)

        assert rt.views().rejected == {}, "it is still recorded as needing rework"
        assert [c.stage_id for c in rt.views().pending_candidates()] == [], "it never got composed"
        assert sh(origin[0], "show", "proj:app.py") == "fine", "the earlier landing was lost"
        assert sh(origin[0], "show", "proj:other.py") == "the work", "the reworked stage did not land"


class TestWhatMakesSomethingACandidate:
    """A branch at origin is not a candidate. The ledger says what is, and a
    rejection takes it off that list — so a branch a bay is part-way through
    reworking is inert however long the work takes, and a composition that
    runs in the middle of one does not see it."""

    def test_a_branch_being_reworked_is_not_composed(self, repo, tmp_path, origin, a_daemon):
        bare, _ = origin
        suite = "test ! -e poison.txt"
        a_candidate(repo, tmp_path, name="good", content="fine\n", full_test_command=suite)
        cfg, rt, state = a_candidate(repo, tmp_path, name="bad",
                                     files={"other.py": "the work\n", "poison.txt": "x\n"},
                                     full_test_command=suite, keys=[THE_OTHER_ITEM])
        lander.compose(rt)

        # Mid-rework: taken, rebased, not yet finished. The branch is still
        # on the remote, holding the pre-rework work.
        out = nodes._take_rework(rt, state)
        assert out is not None
        assert sh(bare, "branch", "--list", out["stage_branch"]), "the branch left the remote"

        tip = origin_tip(bare)
        again = lander.compose(rt)
        assert again is None, "a composition picked up a branch somebody is working on"
        assert origin_tip(bare) == tip

    def test_the_remote_branch_is_the_backup_while_the_rework_runs(self, repo, tmp_path, origin, a_daemon):
        # Deleting it would make the remote match the ledger, and would put
        # the only copy of the work in one bay's checkout until the rework
        # finishes. The ledger already answers what is a candidate, so the
        # branch is worth more as the copy that survives the bay.
        bare, _ = origin
        suite = "test ! -e poison.txt"
        a_candidate(repo, tmp_path, name="good", content="fine\n", full_test_command=suite)
        cfg, rt, state = a_candidate(repo, tmp_path, name="bad",
                                     files={"other.py": "the work\n", "poison.txt": "x\n"},
                                     full_test_command=suite, keys=[THE_OTHER_ITEM])
        lander.compose(rt)
        [rejection] = rt.views().rejected.values()
        nodes._take_rework(rt, state)
        assert sh(bare, "rev-parse", rejection.candidate.branch) == rejection.candidate.sha


class TestAReworkThatConflicts:
    """A candidate can fail to land two ways at once: its changes will not
    apply beside what landed, and what it does is wrong beside what landed.
    Neither is a defect in the stage as it was drawn, and the stage is still
    the thing to do."""

    def _conflicting(self, repo, tmp_path, origin):
        # Two stages editing the same lines. The first lands; the second
        # cannot be replayed onto it.
        a_candidate(repo, tmp_path, name="first", content="theirs\n")
        cfg, rt, state = a_candidate(repo, tmp_path, name="second", content="mine\n",
                                     keys=[THE_OTHER_ITEM])
        lander.compose(rt)
        return cfg, rt, state

    def test_the_conflict_is_left_in_the_tree_to_be_worked_on(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, state = self._conflicting(repo, tmp_path, origin)
        out = nodes._take_rework(rt, state)
        assert out is not None, "a conflicting candidate was refused instead of taken"
        assert "<<<<<<<" in (repo / "app.py").read_text(), "there is nothing to resolve"
        assert "conflict markers" in out["last_failure"]["summary"]
        assert "app.py" in out["last_failure"]["summary"]

    def test_it_is_an_ordinary_tree_with_no_operation_in_progress(self, repo, tmp_path, origin, a_daemon):
        cfg, rt, state = self._conflicting(repo, tmp_path, origin)
        nodes._take_rework(rt, state)
        assert not (repo / ".git" / "CHERRY_PICK_HEAD").exists()
        # And the ordinary path works from here: resolve, commit, carry on.
        (repo / "app.py").write_text("theirs\nmine\n")
        rt.git.commit_all("resolved")
        assert rt.git.is_clean()

    def test_resolving_it_lands_the_stage(self, repo, tmp_path, origin, a_daemon):
        bare, _ = origin
        cfg, rt, state = self._conflicting(repo, tmp_path, origin)
        out = nodes._take_rework(rt, state)
        (repo / "app.py").write_text("theirs\nmine\n")
        state = {**state, **out, "review_summary": "resolved", "review_record": "took both"}
        nodes.advance(state, rt)
        assert sh(bare, "show", "proj:app.py") == "theirs\nmine"
        assert rt.views().rejected == {}
        assert rt.views().pending_candidates() == []


class TestACandidateFromBeforeItCarriedItsStage:
    def test_it_is_recovered_from_the_drawn_record(self, repo, tmp_path, origin, a_daemon):
        # Without this the rejection is offered forever and can never be
        # taken, and its branch sits on the remote with nothing that will
        # ever pick it up.
        from code_gantry.ledger import CANDIDATE_REJECTED, STAGE_DERIVED
        from test_nodes import planned_stage

        cfg, rt, state = make(repo, tmp_path, **compose_cfg())
        fields = planned_stage(id="old", plan_keys=[THE_ITEM])
        event = rt.ledger.append(
            STAGE_DERIVED, stage_id="old", run_id="r0", fields=fields,
            keys=[THE_ITEM], findings=[], rank=0,
        )
        rt.ledger.append(
            nodes.CANDIDATE_PUSHED, stage_id="old", run_id="r0", sha="deadbeef",
            branch="proj-stage/000-old", base=rt.git.rev_parse("proj"),
            landing={"keys": [THE_ITEM], "held": [], "derived_id": event.derived_id},
        )
        rt.ledger.append(
            CANDIDATE_REJECTED, stage_id="old", run_id="r0",
            branch="proj-stage/000-old", reason="red", sha=rt.git.rev_parse("proj"),
        )

        [rejection] = rt.views().rejected.values()
        assert rejection.candidate.fields == {}, "this test is no longer about the old shape"
        taken = nodes._claim_rework(rt, state)
        assert taken is not None
        assert taken[1].id == "old"


class TestWhatTheExecutorIsActuallyHanded:
    """`review_feedback` is the executor's channel and `last_failure` is the
    planner's. Writing the rework into the second alone left a bay resolving
    conflict markers it had never been told were there — it managed, from
    the tree, which is how a channel that has gone missing stays missing."""

    def test_the_rework_reaches_the_executor_not_only_the_planner(self, repo, tmp_path, origin, a_daemon):
        a_candidate(repo, tmp_path, name="first", content="theirs\n")
        cfg, rt, state = a_candidate(repo, tmp_path, name="second", content="mine\n",
                                     keys=[THE_OTHER_ITEM])
        lander.compose(rt)
        out = nodes._take_rework(rt, state)

        [feedback] = out["review_feedback"]
        assert "conflict markers" in feedback
        assert "app.py" in feedback
        assert "instruction is unchanged" in feedback

    def test_it_reaches_the_prompt_the_executor_is_sent(self, repo, tmp_path, origin, a_daemon):
        from code_gantry.prompts import build_executor_messages

        a_candidate(repo, tmp_path, name="first", content="theirs\n")
        cfg, rt, state = a_candidate(repo, tmp_path, name="second", content="mine\n",
                                     keys=[THE_OTHER_ITEM])
        lander.compose(rt)
        out = nodes._take_rework(rt, state)

        stage = nodes.Stage(**out["current"])
        messages = build_executor_messages(
            stage=stage, cfg=cfg, prompt="do the stage",
            feedback=out["review_feedback"], failure_layer=out["failure_layer"],
        )
        text = "\n".join(
            block.get("text", "")
            for message in messages
            for block in (message.get("content") or [])
            if isinstance(block, dict)
        )
        assert "conflict markers" in text, "the executor is sent no word of the conflicts"
        # The stage's prompt is rendered by the node and passed in; what
        # matters here is that the rework is added to it rather than put in
        # place of it.
        assert "do the stage" in text, "the rework displaced the stage's own prompt"
