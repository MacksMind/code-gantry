"""Graph wiring: edges, checkpointing, and the resume path.

The end-to-end runs live in test_integration; this file pins the structural
claims — that the declared edges match PLAN.md, that state survives a fresh
process, and that a gated run re-enters at verify rather than redoing the
stage.
"""

import pytest

from orchestrator.graph import EDGES, NODES, _entry, _router, recursion_limit


class TestEdgeTable:
    def test_every_node_has_edges(self):
        assert set(NODES) == set(EDGES)

    def test_edges_match_the_spec(self):
        # PLAN.md's edge list, transcribed. If the implementation drifts from
        # the spec, this is where it shows up.
        assert EDGES == {
            "precheck": ["execute", "gate", "escalate"],
            "execute": ["verify", "execute", "escalate"],
            "gate": ["end"],
            "verify": ["review", "advance", "execute", "escalate"],
            "review": ["advance", "execute", "escalate"],
            "advance": ["precheck", "finalize"],
            "finalize": ["end", "escalate"],
            "escalate": ["end"],
        }

    def test_no_node_can_reach_a_nonexistent_node(self):
        for name, targets in EDGES.items():
            for target in targets:
                assert target in NODES or target == "end", f"{name} -> {target}"


class TestRouter:
    def test_routes_to_a_permitted_hop(self):
        assert _router(["verify", "escalate"])({"next_hop": "verify"}) == "verify"

    def test_end_maps_to_the_graph_terminal(self):
        assert _router(["end"])({"next_hop": "end"}) == "__end__"

    def test_an_unpermitted_hop_raises(self):
        # A node asking for an edge the spec does not have is a bug; silently
        # rerouting would hide it.
        with pytest.raises(RuntimeError):
            _router(["verify"])({"next_hop": "advance"})

    def test_a_missing_hop_escalates(self):
        # Fail safe: an unset next_hop stops the run rather than advancing it.
        assert _router(["escalate"])({}) == "escalate"


class TestEntryPoint:
    def test_a_new_run_starts_at_precheck(self):
        assert _entry({"status": "running"}) == "precheck"

    def test_a_gated_run_resumes_at_verify(self):
        # The human has done the work; the orchestrator's job on resume is to
        # confirm it landed green, not to re-run the stage.
        assert _entry({"status": "awaiting_human"}) == "verify"

    def test_an_escalated_run_starts_at_precheck(self):
        assert _entry({"status": "escalated"}) == "precheck"


class TestRecursionLimit:
    def test_scales_with_stages_and_retries(self):
        assert recursion_limit(1, 3, 2) > 25
        assert recursion_limit(10, 3, 2) > recursion_limit(1, 3, 2)

    def test_leaves_headroom_for_a_worst_case_run(self):
        # 6 stages each burning every retry and rework must not hit the limit
        # and surface as an opaque framework error instead of an escalation.
        stages, tests, reworks = 6, 3, 2
        worst_case_steps = stages * (1 + (tests + reworks + 1) * 3 + 1)
        assert recursion_limit(stages, tests, reworks) >= worst_case_steps


class TestCheckpointer:
    def test_state_survives_a_reopened_connection(self, tmp_path):
        from orchestrator.graph import open_checkpointer

        saver, conn = open_checkpointer(tmp_path / "runs" / "r1" / "state.db")
        config = {"configurable": {"thread_id": "r1", "checkpoint_ns": ""}}
        saver.put(
            config,
            {"v": 1, "id": "c1", "ts": "2026-01-01T00:00:00+00:00",
             "channel_values": {"stage_index": 3}, "channel_versions": {},
             "versions_seen": {}},
            {"source": "update", "step": 1, "parents": {}},
            {},
        )
        conn.close()

        reopened, conn2 = open_checkpointer(tmp_path / "runs" / "r1" / "state.db")
        tup = reopened.get_tuple(config)
        assert tup is not None
        assert tup.checkpoint["channel_values"]["stage_index"] == 3
        conn2.close()

    def test_creates_the_run_directory(self, tmp_path):
        from orchestrator.graph import open_checkpointer

        _, conn = open_checkpointer(tmp_path / "deep" / "nested" / "state.db")
        assert (tmp_path / "deep" / "nested").is_dir()
        conn.close()
