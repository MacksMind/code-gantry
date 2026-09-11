"""The ledger through the git remote: one ref per origin, never a branch.

A bare repository stands in for origin and a second clone for another host,
each with its own ledger and origin name.
"""

import subprocess

import pytest

from test_remote_landing import origin, sh  # noqa: F401 - the bare-origin fixture

from code_gantry.gitops import Git, GitError
from code_gantry.ledger import CLAIMED, LANDED, open_ledger
from code_gantry.ledgersync import FILE, REF_PREFIX, from_jsonl, ref_for, sync, to_jsonl


def plant(led, key):
    led.upsert_node(key, parent=None, position=0, kind="item", title=key)


@pytest.fixture
def hosts(repo, tmp_path, origin):
    bare, other = origin
    a = open_ledger(tmp_path / "a" / "ledger.db", origin="host-a", actor="a")
    b = open_ledger(tmp_path / "b" / "ledger.db", origin="host-b", actor="b")
    return Git(repo), a, Git(other), b, bare


class TestTheExchange:
    def test_a_host_sees_the_others_events_after_both_sync(self, hosts):
        git_a, a, git_b, b, bare = hosts
        plant(a, "p.001")
        a.append(CLAIMED, key="p.001", run_id="ra", stage_id="s")
        assert sync(a, git_a).pushed == 2
        report = sync(b, git_b)
        assert report.ingested == {"host-a": 2}
        assert b.views().state("p.001").state == "claimed"
        assert b.views().state("p.001").run_id == "ra"

    def test_a_second_sync_moves_nothing(self, hosts):
        git_a, a, git_b, b, bare = hosts
        plant(a, "p.001")
        sync(a, git_a)
        sync(b, git_b)
        again = sync(b, git_b)
        assert again.pushed is None and again.ingested == {"host-a": 0}
        assert sync(a, git_a).pushed is None

    def test_events_flow_both_ways_and_keep_their_origin(self, hosts):
        git_a, a, git_b, b, bare = hosts
        plant(a, "p.001")
        sync(a, git_a)
        sync(b, git_b)
        b.append(LANDED, key="p.001", sha="abc", run_id="rb", stage_id="s")
        sync(b, git_b)
        sync(a, git_a)
        landed = [e for e in a.events() if e.kind == LANDED]
        assert len(landed) == 1 and landed[0].origin == "host-b" and landed[0].seq == 1
        assert a.views().state("p.001").state == "landed"

    def test_the_ref_is_on_origin_and_is_not_a_branch(self, hosts):
        git_a, a, git_b, b, bare = hosts
        plant(a, "p.001")
        sync(a, git_a)
        refs = sh(bare, "for-each-ref", "--format=%(refname)")
        assert ref_for("host-a") in refs
        assert "refs/heads/host-a" not in refs
        assert sh(git_a.repo, "branch", "--list") .count("host-a") == 0

    def test_the_work_tree_and_head_are_untouched(self, hosts, repo):
        git_a, a, git_b, b, bare = hosts
        head = git_a.head_sha()
        (repo / "scratch.txt").write_text("uncommitted\n")
        plant(a, "p.001")
        sync(a, git_a)
        sync(a, git_a)
        assert git_a.head_sha() == head
        assert sh(repo, "status", "--porcelain") == "?? scratch.txt"

    def test_the_file_on_the_ref_is_the_log_in_order(self, hosts):
        git_a, a, git_b, b, bare = hosts
        plant(a, "p.001")
        a.append(CLAIMED, key="p.001", run_id="ra", stage_id="s")
        sync(a, git_a)
        text = git_a.ref_file(ref_for("host-a"), FILE)
        rows = from_jsonl(text)
        assert [r["seq"] for r in rows] == [1, 2]
        assert to_jsonl(rows) == text


class TestWhatItRefuses:
    def test_a_branch_cannot_be_pushed_through_the_ref_helpers(self, hosts):
        git_a, *_ = hosts
        with pytest.raises(GitError):
            git_a.push_ref("refs/heads/main")
        with pytest.raises(GitError):
            git_a.write_ref_file("refs/heads/sneaky", FILE, "x", "m")

    def test_a_read_only_ledger_and_a_repo_with_no_remote_are_skipped(self, repo, tmp_path):
        from code_gantry.ledger import read_ledger

        writer = open_ledger(tmp_path / "l.db", origin="h")
        plant(writer, "p.001")
        assert "reading only" in sync(read_ledger(tmp_path / "l.db"), Git(repo)).skipped
        assert "no remote" in sync(writer, Git(repo)).skipped


class TestTheSeams:
    def test_a_run_syncs_at_start_and_after_a_landing(self, repo, tmp_path, origin, monkeypatch):
        from test_nodes import make, with_stage

        from code_gantry import nodes

        bare, other = origin
        cfg, rt, state = make(repo, tmp_path, remote_landing=True)
        seen = []
        real = nodes._sync_ledger
        monkeypatch.setattr(nodes, "_sync_ledger", lambda rt_, where: seen.append(where) or real(rt_, where))
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "review_summary": "fine", "review_record": "did it"}
        out = nodes.advance(state, rt)
        assert out["next_hop"] != "escalate"
        assert "advance" in seen
        assert ref_for("test-host") in sh(bare, "for-each-ref", "--format=%(refname)")

    def test_without_remote_landing_nothing_is_exchanged(self, repo, tmp_path, origin):
        from test_nodes import make, with_stage

        from code_gantry import nodes

        bare, other = origin
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "review_summary": "fine", "review_record": "did it"}
        nodes.advance(state, rt)
        assert REF_PREFIX not in sh(bare, "for-each-ref", "--format=%(refname)")
