"""What the progress log has accumulated since anyone last folded it.

Folding is the largest single lever on a run's bill and it is the one step
nothing in the loop performs. The log is spliced into the plan block, which
carries a cache breakpoint, so every landing invalidates that block and pays to
rewrite it — the log does not merely cost its own size, it drags the plan tree
through the cache with it. Measured between two folds: 292 bytes to 263KB over
66 landings, and an extra per-stage cost that grows with the gap rather than
with the log.

Nothing said so. The cost is invisible in behaviour — every stage lands, every
gate passes, the run simply gets more expensive — and the operator learns about
it by reading `stage-costs.md` afterwards, if at all. So preflight says it out
loud, at the one moment where acting on it is free: before a run or a resume,
when folding costs a commit and nothing is in flight.

A warning, never a refusal. Whether to fold is a judgement about what the work
has become — the same reason the loop does not do it unattended — and a run
that will not start until someone rewrites a plan is worse than an expensive
one. The accounting is what makes the warning actionable: a number an operator
can compare against the last time they looked.
"""

import subprocess

import pytest

from orchestrator.config import parse_config


def _cfg(repo, addendum="docs/progress_log.md"):
    return parse_config({
        "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
        "plan_root": "docs/PLAN.md", "test_command": "true",
        "plan_addendum_path": addendum,
        "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.6-sol"},
    })


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "target"
    (path / "docs").mkdir(parents=True)
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True,
                   capture_output=True)
    (path / "docs" / "PLAN.md").write_text("# Plan\n")
    return path


def _log(repo, entries):
    body = "# Plan addendum\n\nNotes for a later pass.\n\n" + "".join(entries)
    (repo / "docs" / "progress_log.md").write_text(body)


# The three writers in `addendum`, each with the marker it stamps. Written out
# here rather than imported so the test fails when a format changes, which is
# the event the counter cares about.
LANDED = "## What `stage-{}` landed\n\nIt did a thing.\n\n"
OBSERVED = "## A note\n\n- **observed** while planning `stage-{}`\n\n"
REVIEWED = (
    "## `app/models/thing.rb`\n\n"
    "- **observed** by the reviewer while landing `stage-{}`\n"
    "- **found** the count is wrong\n\n"
)


class TestWhenThereIsNothingToFold:
    def test_no_addendum_configured_is_not_a_warning(self, repo):
        from orchestrator.preflight import _unfolded_progress_check

        cfg = parse_config({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "p",
            "plan_root": "docs/PLAN.md", "test_command": "true",
            "executor": {"model": "m"}, "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6-sol"},
        })
        check = _unfolded_progress_check(cfg)
        assert check.ok

    def test_a_log_that_does_not_exist_yet_is_not_a_warning(self, repo):
        from orchestrator.preflight import _unfolded_progress_check

        assert _unfolded_progress_check(_cfg(repo)).ok

    def test_a_freshly_folded_log_is_not_a_warning(self, repo):
        # The header survives a fold; the entries do not. An empty log is the
        # state the warning exists to get the operator back to, so it must be
        # silent there or the warning means nothing.
        from orchestrator.preflight import _unfolded_progress_check

        _log(repo, [])
        check = _unfolded_progress_check(_cfg(repo))
        assert check.ok
        assert "nothing" in check.detail.lower()


class TestTheAccounting:
    def _check(self, repo, entries):
        from orchestrator.preflight import _unfolded_progress_check

        _log(repo, entries)
        return _unfolded_progress_check(_cfg(repo))

    def test_it_warns_without_blocking(self, repo):
        check = self._check(repo, [LANDED.format(1)])
        assert not check.ok
        assert not check.fatal, "a run must not refuse to start over this"

    def test_it_names_each_count_for_whoever_wrote_it(self, repo):
        """Two numbers, disjoint, each naming its author.

        They answer different questions. A reviewer summary is what a stage
        did, written after the diff by the only participant that saw it, and
        there is one per landing — so that count is also how many stages have
        gone by, which is what the cost grows with. A planner note is an
        observation about the plan itself, recorded whether or not the stage
        it was derived alongside ever landed.

        The first version said "3 entries and 2 landings", which put a subset
        beside its superset: the numbers could not be added, and neither said
        who wrote what, which is what tells an operator what folding one of
        them involves.
        """
        check = self._check(
            repo, [LANDED.format(1), OBSERVED.format(1), LANDED.format(2)]
        )
        assert "1 planner note" in check.detail
        assert "2 reviewer summaries" in check.detail

    def test_a_reviewer_note_is_neither_of_the_other_two(self, repo):
        """Three writers, not two.

        A reviewer finding — the code has a problem this stage did not cause —
        is filed under the file it is about and carries no plan citation, which
        is exactly what distinguishes it from a planner note. Counting by
        heading put it in with the planner's, so the one number an operator
        would read as "what the planner claimed about the plan" silently
        included the reviewer's corrections of those claims.
        """
        check = self._check(
            repo, [OBSERVED.format(1), REVIEWED.format(1), LANDED.format(1)]
        )
        assert "1 planner note" in check.detail
        assert "1 reviewer note" in check.detail
        assert "1 reviewer summary" in check.detail

    def test_a_kind_with_nothing_in_it_is_not_listed(self, repo):
        # Three zeroes would be three quarters of the line saying nothing.
        check = self._check(repo, [LANDED.format(1), LANDED.format(2)])
        assert check.detail.startswith("2 reviewer summaries unfolded")

    def test_the_singular_reads_correctly(self, repo):
        check = self._check(repo, [LANDED.format(1), OBSERVED.format(1)])
        assert "1 planner note and 1 reviewer summary" in check.detail

    def test_it_reports_the_size_the_planner_is_paying_for(self, repo):
        check = self._check(repo, [LANDED.format(n) for n in range(40)])
        # The bytes, because that is what the cache is billed in and what an
        # operator can compare against the last time they looked.
        assert "KB" in check.detail or "bytes" in check.detail

    def test_it_names_the_file_so_the_next_step_is_obvious(self, repo):
        check = self._check(repo, [LANDED.format(1)])
        assert "docs/progress_log.md" in check.detail

    def test_it_is_the_accounting_and_nothing_else(self, repo):
        """No explanation on the line.

        The first version spent four sentences saying why folding matters, on
        every start — read once and scrolled past thereafter, inside a block an
        operator is scanning for whatever is wrong. The reasoning is in the
        docstring, where the code beneath it keeps it honest; the line carries
        the numbers, which are the part that changes.
        """
        check = self._check(repo, [LANDED.format(1)])
        assert len(check.detail) < 160, check.detail
        assert "cache" not in check.detail.lower()


class TestItRunsWhereItCanBeActedOn:
    def test_preflight_includes_it(self, repo):
        """A check nothing calls is a check that does not exist.

        `_read_budget_check` and this one are both advisory, and both are only
        reachable through the list preflight assembles — which is exactly the
        sort of wiring that has been added correctly and left unreferenced
        here before.
        """
        import inspect

        from orchestrator import preflight

        src = inspect.getsource(preflight.run_preflight)
        assert "_unfolded_progress_check" in src
