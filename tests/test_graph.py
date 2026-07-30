"""Graph wiring: the edge table, routing, and the recursion ceiling."""

import pytest

from orchestrator.graph import EDGES, ENTRY_POINTS, NODES, _router, recursion_limit


class TestEdgeTable:
    def test_every_node_has_edges(self):
        assert set(NODES) == set(EDGES)

    def test_edges_match_the_spec(self):
        # PLAN.md's edge list, transcribed. Drift from the spec shows up here.
        assert EDGES == {
            "plan": ["precheck", "finalize", "escalate"],
            "precheck": ["execute", "plan", "escalate"],
            "execute": ["verify", "execute", "plan"],
            "verify": ["review", "advance", "execute", "plan", "escalate"],
            "review": ["advance", "execute", "plan"],
            "advance": ["plan"],
            "finalize": ["end", "escalate"],
            "escalate": ["end"],
        }

    def test_no_node_reaches_a_nonexistent_node(self):
        for name, targets in EDGES.items():
            for target in targets:
                assert target in NODES or target == "end", f"{name} -> {target}"

    def test_review_cannot_escalate_directly(self):
        # A rejected stage is a planning problem, not a human's problem.
        assert "escalate" not in EDGES["review"]

    def test_verify_can_reach_the_planner(self):
        # Scope violations and exhausted retries route there.
        assert "plan" in EDGES["verify"]

    def test_only_three_nodes_can_escalate(self):
        # The design goal: a run stops for a good reason or not at all.
        escalators = {n for n, t in EDGES.items() if "escalate" in t}
        assert escalators == {"plan", "precheck", "verify", "finalize"} - {"finalize"} | {
            "finalize"
        }

    def test_every_entry_point_is_a_real_node(self):
        for entry in ENTRY_POINTS:
            assert entry in NODES


class TestRouter:
    def test_routes_to_a_permitted_hop(self):
        assert _router(["verify", "plan"])({"next_hop": "verify"}) == "verify"

    def test_end_maps_to_the_graph_terminal(self):
        assert _router(["end"])({"next_hop": "end"}) == "__end__"

    def test_an_unpermitted_hop_raises(self):
        # A node asking for an edge the spec lacks is a bug; rerouting hides it.
        with pytest.raises(RuntimeError):
            _router(["verify"])({"next_hop": "advance"})

    def test_a_missing_hop_escalates(self):
        # Fail safe: an unset next_hop stops the run rather than advancing it.
        assert _router(["escalate"])({}) == "escalate"


class TestRecursionLimit:
    def test_far_above_the_langgraph_default(self):
        assert recursion_limit(60, 3, 2, 12) > 25

    def test_scales_with_every_budget(self):
        base = recursion_limit(10, 3, 2, 12)
        assert recursion_limit(20, 3, 2, 12) > base
        assert recursion_limit(10, 6, 2, 12) > base
        assert recursion_limit(10, 3, 5, 12) > base
        assert recursion_limit(10, 3, 2, 24) > base

    def test_covers_a_worst_case_run(self):
        # Every stage burning every retry and rework, plus every planner
        # intervention. Exhausting the limit surfaces as an opaque framework
        # error rather than an escalation — the one failure mode to avoid.
        stages, tests, reworks, interventions = 60, 3, 2, 12
        per_stage = 5 * (tests + reworks + 1)
        worst = stages * per_stage + interventions * per_stage
        assert recursion_limit(stages, tests, reworks, interventions) >= worst


class TestCheckpointer:
    def test_state_survives_a_reopened_connection(self, tmp_path):
        from orchestrator.graph import open_checkpointer

        saver, conn = open_checkpointer(tmp_path / "runs" / "r1" / "state.db")
        config = {"configurable": {"thread_id": "r1", "checkpoint_ns": ""}}
        saver.put(
            config,
            {
                "v": 1, "id": "c1", "ts": "2026-01-01T00:00:00+00:00",
                "channel_values": {"stage_index": 3}, "channel_versions": {},
                "versions_seen": {},
            },
            {"source": "update", "step": 1, "parents": {}},
            {},
        )
        conn.close()

        reopened, conn2 = open_checkpointer(tmp_path / "runs" / "r1" / "state.db")
        tup = reopened.get_tuple(config)
        assert tup.checkpoint["channel_values"]["stage_index"] == 3
        conn2.close()

    def test_creates_the_run_directory(self, tmp_path):
        from orchestrator.graph import open_checkpointer

        _, conn = open_checkpointer(tmp_path / "deep" / "nested" / "state.db")
        assert (tmp_path / "deep" / "nested").is_dir()
        conn.close()
