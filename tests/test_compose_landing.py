"""Landing through a composing bay.

A stage that is approved is squashed to one candidate commit on the base it
was cut from and pushed as its own branch. The project branch is moved by
nobody until a bay holds the landing semaphore and composes what is pending.
A bare repository stands in for origin.
"""

import pytest

from test_config import as_test_tools, minimal
from test_nodes import THE_ITEM, make, with_stage
from test_remote_landing import origin, origin_tip, other_lands, sh  # noqa: F401

from code_gantry import nodes
from code_gantry.config import ConfigError, parse_config
from code_gantry.gitops import Git


def compose_cfg(**over):
    return {"remote_landing": True, "compose_landings": True, **over}


def finish_a_stage(repo, tmp_path, **over):
    cfg, rt, state = make(repo, tmp_path, **compose_cfg(**over))
    state = with_stage(state, rt)
    (repo / "app.py").write_text("stage work\n")
    state = {**state, "review_summary": "fine", "review_record": "did it"}
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
        assert THE_ITEM in candidate.keys

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
