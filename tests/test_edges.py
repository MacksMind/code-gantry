"""Graph wiring: the edge table, routing, and the recursion ceiling."""

import pytest

from code_gantry.driver import (
    EDGES,
    ENTRY_POINTS,
    NODES,
    _next,
    default_max_steps,
)


def _hops_each_node_can_return() -> dict[str, set[str]]:
    """Every `next_hop` a node function can produce, read off its source.

    The edge table and the nodes are two statements of the same thing, and
    nothing compared them: `execute` has returned `_escalate(...)` on a refused
    commit since the day that branch was written, `EDGES` never listed it, and
    both halves had a green unit test — `test_commit_refused` asserts the node
    returns `escalate` and `test_edges_match_the_spec` asserts `escalate` is
    not reachable from `execute`. Two tests contradicting each other, neither
    driving the driver, for as long as both have existed. It surfaced when a
    pre-commit hook refused a stage's work 16 landings into an overnight run
    and the run died with a `RuntimeError` instead of the escalation the node
    had carefully written.

    Resolved to a fixpoint over module-level calls, so a hop returned by a
    helper counts against the node that calls it.
    """
    import ast
    import pathlib as _p

    tree = ast.parse((_p.Path(__file__).resolve().parents[1]
                      / "src" / "code_gantry" / "nodes.py").read_text())
    funcs = {
        n.name: n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def literals(fn):
        found = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if not (isinstance(k, ast.Constant) and k.value == "next_hop"):
                        continue
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        found.add(v.value)
                    elif isinstance(v, ast.IfExp):
                        for branch in (v.body, v.orelse):
                            if isinstance(branch, ast.Constant):
                                found.add(branch.value)
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.slice, ast.Constant)
                        and t.slice.value == "next_hop"
                        and isinstance(n.value, ast.Constant)
                    ):
                        found.add(n.value.value)
        return found

    def callees(fn):
        return {
            n.func.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in funcs
        }

    hops = {name: literals(fn) for name, fn in funcs.items()}
    changed = True
    while changed:
        changed = False
        for name, fn in funcs.items():
            before = len(hops[name])
            for c in callees(fn):
                hops[name] |= hops[c]
            changed = changed or len(hops[name]) != before
    return hops


class TestTheTableMatchesTheNodes:
    def test_every_layer_a_node_escalates_with_is_a_declared_one(self):
        """`resume_entry_point` routes on `failure_layer`, so an unregistered
        one is not a type error — it is a resume that silently falls through to
        `stage_has_work` and re-enters somewhere nobody chose. Derived from the
        source for the same reason `EDGES` is: two statements of one thing with
        nothing comparing them is how `execute`'s escalation went unlisted for
        as long as it existed. Caught this test's own author adding
        `branch_moved` and `tree_dirty` to `finalize` and to neither set."""
        import ast
        import pathlib as _p
        import typing

        from code_gantry.state import FailureLayer

        tree = ast.parse((_p.Path(__file__).resolve().parents[1]
                          / "src" / "code_gantry" / "nodes.py").read_text())
        used = {
            n.args[0].value
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "_escalate"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
        }
        assert used, "the parse found no _escalate call, so it is not testing anything"
        assert used <= set(typing.get_args(FailureLayer)), sorted(
            used - set(typing.get_args(FailureLayer))
        )

    def test_no_node_can_return_a_hop_the_table_forbids(self):
        hops = _hops_each_node_can_return()
        illegal = {
            node: sorted(hops.get(node, set()) - set(targets) - {"end"})
            for node, targets in EDGES.items()
        }
        assert {k: v for k, v in illegal.items() if v} == {}


class TestEdgeTable:
    def test_every_node_has_edges(self):
        assert set(NODES) == set(EDGES)

    def test_edges_match_the_spec(self):
        # PLAN.md's edge list, transcribed. Drift from the spec shows up here.
        assert EDGES == {
            # `plan` reaches itself: a spec that fails validation is redrawn
            # rather than escalated, and the redraw is another planner call.
            "plan": ["precheck", "verify", "finalize", "escalate", "plan"],
            "precheck": ["execute", "plan", "escalate"],
            # Reaches itself only when the executor failed to run at all,
            # so there is nothing for a gate to look at. Escalates when a
            # commit hook refuses the work. See EDGES.
            "execute": ["verify", "plan", "execute", "escalate"],
            "verify": ["review", "advance", "execute", "plan", "escalate"],
            "review": ["advance", "execute", "plan", "escalate"],
            "advance": ["plan", "precheck", "escalate"],
            "finalize": ["end", "escalate"],
            "escalate": ["end"],
        }

    def test_no_node_reaches_a_nonexistent_node(self):
        for name, targets in EDGES.items():
            for target in targets:
                assert target in NODES or target == "end", f"{name} -> {target}"

    def test_a_rejected_stage_still_goes_to_the_planner(self):
        # The original of this test banned `escalate` from `review` outright,
        # on the true premise that a rejected stage is a planning problem
        # rather than a human's. But `review` had grown a second exit — the
        # full suite killed by a signal after approval, where the work is
        # correct and the environment is gone — and the ban made that a crash
        # rather than the escalation it was written as. The doctrine is about
        # rejections, so this asserts what it actually meant.
        assert {"execute", "plan"} <= set(EDGES["review"])

    def test_verify_can_reach_the_planner(self):
        # Scope violations and exhausted retries route there.
        assert "plan" in EDGES["verify"]

    def test_plan_can_re_enter_at_verify(self):
        # An `extend` revision leaves the stage's work standing, so the next
        # question is whether it now passes — not what the executor would write
        # a second time.
        assert "verify" in EDGES["plan"]

    def test_only_the_nodes_that_should_can_escalate(self):
        """The design goal: a run stops for a good reason or not at all.

        `advance` joined the set, and it is worth being explicit that this is
        not a widening of when a run may give up. Every other escalator stops
        because something is wrong; `advance` stops only because the operator
        asked, and only at the instant a stage has been squash-merged and the
        tree is clean. It is the safest stopping point in the graph, and it had
        to be added to the table because a node routing outside its edges
        raises rather than rerouting.

        `execute` and `review` joined it for the same reason and not by the
        same route: they had *already* been escalating in `nodes.py` — a hook
        refusing the commit, a full suite killed by a signal after approval —
        and the table's omission turned each into a `RuntimeError`. So this is
        not a widening either. It is the table catching up with two exits that
        have been written, commented and unit-tested for as long as they have
        existed. The version of this test that banned them read as a design
        constraint and was really a description of a bug.

        What remains banned is the thing the doctrine is actually about, and
        it is asserted in `test_a_rejected_stage_still_goes_to_the_planner`
        rather than here: a rejection is the planner's problem, and neither
        new edge carries one.
        """
        escalators = {n for n, t in EDGES.items() if "escalate" in t}
        assert escalators == {
            "plan", "precheck", "execute", "verify", "review", "advance", "finalize"
        }

    def test_every_entry_point_is_a_real_node(self):
        for entry in ENTRY_POINTS:
            assert entry in NODES


class TestRouter:
    """The same four rules, now that the router is ours rather than a callback.

    `_router(allowed)` returned a closure LangGraph called per node; `_next`
    takes the node and the table directly, so the error can name which node
    misrouted — which the closure could not, and which is the first thing
    anyone reading that traceback wants.
    """

    def test_routes_to_a_permitted_hop(self):
        assert _next("x", {"next_hop": "verify"}, {"x": ["verify", "plan"]}) == "verify"

    def test_end_is_the_terminal(self):
        # No longer LangGraph's `__end__` sentinel; the loop returns instead.
        assert _next("x", {"next_hop": "end"}, {"x": ["end"]}) == "end"

    def test_an_unpermitted_hop_raises(self):
        # A node asking for an edge the spec lacks is a bug; rerouting hides it.
        with pytest.raises(RuntimeError, match="'x'"):
            _next("x", {"next_hop": "advance"}, {"x": ["verify"]})

    def test_a_missing_hop_escalates(self):
        # Fail safe: an unset next_hop stops the run rather than advancing it.
        assert _next("x", {}, {"x": ["escalate"]}) == "escalate"


class TestTheStepCeiling:
    def test_it_is_generous(self):
        # It was sized to defeat LangGraph's 25-super-step default. The default
        # is gone; the arithmetic stays, because the reasoning behind it was
        # about this project's shape rather than about the framework.
        assert default_max_steps(60, 3, 2, 12) > 25

    def test_scales_with_every_budget(self):
        base = default_max_steps(10, 3, 2, 12)
        assert default_max_steps(20, 3, 2, 12) > base
        assert default_max_steps(10, 6, 2, 12) > base
        assert default_max_steps(10, 3, 5, 12) > base
        assert default_max_steps(10, 3, 2, 24) > base

    def test_covers_a_worst_case_run(self):
        # Every stage burning every retry and rework, plus every planner
        # intervention. Exhausting it is an escalation now rather than an
        # opaque framework error — see `test_driver.py` — but the ceiling
        # still has to sit above a legitimate worst case, or the escalation
        # would fire on a run that was working.
        stages, tests, reworks, interventions = 60, 3, 2, 12
        per_stage = 5 * (tests + reworks + 1)
        worst = stages * per_stage + interventions * per_stage
        assert default_max_steps(stages, tests, reworks, interventions) >= worst


class TestTheExecutorCanBeRetriedDirectly:
    """A crash, not a design question — this one stopped a live run.

    `nodes.execute` has always been able to reach `_retry_or_plan`, which sets
    `next_hop="execute"`, and the edge spec has always forbidden it. The
    subprocess editor made the path almost unreachable: it returned `ok=False`
    only for a missing credential or a malformed argv. The in-process executor
    returns it whenever the API call itself fails, so the first rejected
    request took the run down with `node routed to 'execute'`.
    """

    def test_execute_may_reach_itself(self):
        from code_gantry.driver import EDGES

        assert "execute" in EDGES["execute"]

    def test_it_is_still_the_only_node_besides_plan_that_does(self):
        # The self-loop is a licence for one case, not a general one. Every
        # other node still has to hand control somewhere else.
        from code_gantry.driver import EDGES

        looping = {name for name, hops in EDGES.items() if name in hops}
        assert looping == {"plan", "execute"}
