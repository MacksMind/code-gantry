"""Landing through origin: pull --rebase, re-test if the tip moved, push.

The only push the pipeline makes, of the configured project branch, fast
forward only, and only with `remote_landing: true`. A bare repository stands
in for origin, and a second clone stands in for another bay.
"""

import subprocess

import pytest

from test_config import as_test_tools, minimal
from test_nodes import THE_ITEM, THE_OTHER_ITEM, make, planned_stage, with_stage

from code_gantry import nodes
from code_gantry.config import parse_config
from code_gantry.gitops import Git, GitError
from code_gantry.preflight import run_preflight


def sh(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(repo, tmp_path):
    """A bare origin holding main and the project branch, plus a second clone."""
    bare = tmp_path / "origin.git"
    sh(tmp_path, "init", "-q", "--bare", str(bare))
    sh(repo, "remote", "add", "origin", str(bare))
    sh(repo, "checkout", "-qb", "proj")
    sh(repo, "push", "-q", "origin", "main", "proj")
    sh(repo, "checkout", "-q", "main")
    other = tmp_path / "other"
    sh(tmp_path, "clone", "-q", str(bare), str(other))
    sh(other, "config", "user.email", "o@example.com")
    sh(other, "config", "user.name", "Other")
    sh(other, "config", "commit.gpgsign", "false")
    sh(other, "checkout", "-q", "proj")
    return bare, other


def other_lands(other, name="other.txt", text="theirs\n"):
    (other / name).write_text(text)
    sh(other, "add", "-A")
    sh(other, "commit", "-qm", "landed elsewhere")
    sh(other, "push", "-q", "origin", "proj")
    return sh(other, "rev-parse", "HEAD")


def origin_tip(bare):
    return sh(bare, "rev-parse", "proj")


def land(repo, tmp_path, **cfg_over):
    cfg, rt, state = make(repo, tmp_path, remote_landing=True, **cfg_over)
    state = with_stage(state, rt)
    (repo / "app.py").write_text("stage work\n")
    state = {**state, "review_summary": "fine", "review_record": "did it"}
    out = nodes.advance(state, rt)
    return cfg, rt, state, out


class TestTheFlag:
    def test_it_is_off_by_default(self):
        assert parse_config(as_test_tools(minimal())).remote_landing is False

    def test_without_it_nothing_is_pushed(self, repo, tmp_path, origin):
        bare, _ = origin
        before = origin_tip(bare)
        cfg, rt, state = make(repo, tmp_path)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        nodes.advance(state, rt)
        assert origin_tip(bare) == before


class TestAQuietOrigin:
    def test_the_landing_is_pushed_fast_forward(self, repo, tmp_path, origin):
        bare, _ = origin
        cfg, rt, state, out = land(repo, tmp_path)
        assert out["next_hop"] != "escalate"
        assert origin_tip(bare) == out["completed"][-1]["merge_sha"]
        assert origin_tip(bare) == rt.git.rev_parse("proj")

    def test_the_suite_runs_once_when_nothing_moved(self, repo, tmp_path, origin):
        counter = tmp_path / "suite-runs"
        land(repo, tmp_path, full_test_command=f"echo x >> {counter}")
        # `advance` itself runs no suite; the review gate did. Nothing moved,
        # so the publication ran none either.
        assert not counter.exists()

    def test_no_origin_at_all_lands_locally(self, repo, tmp_path):
        cfg, rt, state, out = land(repo, tmp_path)
        assert out["next_hop"] != "escalate"
        assert out["completed"][-1]["merge_sha"] == rt.git.rev_parse("proj")


class TestAnOriginThatMoved:
    def test_the_landing_is_rebased_onto_it_and_pushed(self, repo, tmp_path, origin):
        bare, other = origin
        theirs = other_lands(other)
        cfg, rt, state, out = land(repo, tmp_path)
        assert out["next_hop"] != "escalate"
        tip = origin_tip(bare)
        assert tip == rt.git.rev_parse("proj")
        assert rt.git.is_ancestor(theirs, tip), "their landing is below ours"
        assert out["completed"][-1]["merge_sha"] == tip, "the recorded sha is the rebased one"
        assert rt.ledger.views().state(THE_ITEM).sha == tip

    def test_the_suite_runs_again_on_the_rebased_tree(self, repo, tmp_path, origin):
        bare, other = origin
        other_lands(other)
        counter = tmp_path / "suite-runs"
        land(repo, tmp_path, full_test_command=f"echo x >> {counter}")
        assert counter.read_text().count("x") == 1

    def test_a_red_combined_tree_escalates_and_pushes_nothing(self, repo, tmp_path, origin):
        bare, other = origin
        before = origin_tip(bare)
        other_lands(other, name="poison.txt")
        cfg, rt, state, out = land(
            repo, tmp_path, full_test_command="test ! -e poison.txt"
        )
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "remote_landing"
        assert "red" in out["escalation_reason"]
        assert origin_tip(bare) == sh(other, "rev-parse", "HEAD"), "only their landing is on origin"
        # Landed locally all the same: the run's own record is complete.
        assert out["completed"][-1]["id"] == "extract"
        assert rt.ledger.views().state(THE_ITEM).state == "landed"

    def test_a_conflicting_rebase_escalates_and_keeps_the_local_landing(self, repo, tmp_path, origin):
        bare, other = origin
        other_lands(other, name="app.py", text="theirs\n")
        cfg, rt, state, out = land(repo, tmp_path)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "remote_landing"
        assert "rebasing" in out["escalation_reason"]
        assert rt.git.is_clean(), "the aborted rebase leaves a clean tree"
        assert rt.git.commit_subject("proj").startswith("[extract]")

    def test_a_refused_push_is_retried_after_pulling_again(self, repo, tmp_path, origin, monkeypatch):
        bare, other = origin
        cfg, rt, state = make(repo, tmp_path, remote_landing=True)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        real_push = rt.git.push
        calls = {"n": 0}

        def push_racing(branch, remote="origin"):
            calls["n"] += 1
            if calls["n"] == 1:
                other_lands(other)  # someone pushes between our pull and push
            return real_push(branch, remote)

        monkeypatch.setattr(rt.git, "push", push_racing)
        out = nodes.advance(state, rt)
        assert out["next_hop"] != "escalate"
        assert calls["n"] == 2
        assert origin_tip(bare) == rt.git.rev_parse("proj")


class TestPrecheckPullsFirst:
    def test_a_fresh_stage_starts_from_the_pulled_tip(self, repo, tmp_path, origin):
        bare, other = origin
        theirs = other_lands(other)
        cfg, rt, state = make(repo, tmp_path, remote_landing=True)
        state = {**state, "current": planned_stage()}
        out = nodes.precheck(state, rt)
        assert out.get("stage_branch")
        assert out["stage_start_sha"] == theirs

    def test_a_pull_that_conflicts_escalates_before_anything_is_cut(self, repo, tmp_path, origin):
        bare, other = origin
        other_lands(other, name="app.py", text="theirs\n")
        cfg, rt, state = make(repo, tmp_path, remote_landing=True)
        rt.git.checkout("proj")
        (repo / "app.py").write_text("ours\n")
        rt.git.commit_all("local divergence")
        state = {**state, "current": planned_stage()}
        out = nodes.precheck(state, rt)
        assert out["next_hop"] == "escalate"
        assert out["failure_layer"] == "remote_landing"
        assert not out.get("stage_branch")


class TestTheGitHelpers:
    def test_remote_has_branch_and_remote_exists(self, repo, tmp_path, origin):
        g = Git(repo)
        assert g.remote_exists()
        assert g.remote_has_branch("proj")
        assert not g.remote_has_branch("nope")

    def test_pull_rebase_reports_whether_the_tip_moved(self, repo, tmp_path, origin):
        bare, other = origin
        g = Git(repo)
        g.checkout("proj")
        assert g.pull_rebase("proj") is False
        other_lands(other)
        assert g.pull_rebase("proj") is True

    def test_push_refuses_a_non_fast_forward(self, repo, tmp_path, origin):
        bare, other = origin
        g = Git(repo)
        g.checkout("proj")
        (repo / "mine.txt").write_text("mine\n")
        g.commit_all("mine")
        other_lands(other)
        with pytest.raises(GitError):
            g.push("proj")
        assert origin_tip(bare) == sh(other, "rev-parse", "HEAD")


class TestTheLandingHoldsTheSuiteLock:
    """From the pull to the push, one landing at a time on the host, with the
    suite inside re-entering the same lock rather than waiting on it."""

    def _recording(self, monkeypatch):
        from code_gantry import hostlock

        seen = []
        real = hostlock.hold

        def hold(name, label, log=None, directory=None):
            seen.append((name, hostlock.held(name)))
            return real(name, label, log, directory)

        monkeypatch.setattr(hostlock, "hold", hold)
        return seen

    def test_the_lock_is_taken_once_for_the_publication_and_re_entered_by_the_suite(
        self, repo, tmp_path, origin, monkeypatch
    ):
        monkeypatch.setenv("CODE_GANTRY_LOCK_DIR", str(tmp_path / "locks"))
        seen = self._recording(monkeypatch)
        bare, other = origin
        other_lands(other)
        cfg, rt, state, out = land(repo, tmp_path, full_test_command="true")
        assert out["next_hop"] != "escalate"
        suite = [entry for entry in seen if entry[0] == cfg.full_test_lock]
        assert suite[0] == (cfg.full_test_lock, False), "the publication takes the lock first"
        assert (cfg.full_test_lock, True) in suite, "the re-test re-enters it"

    def test_no_lock_name_means_no_lock(self, repo, tmp_path, origin, monkeypatch):
        seen = self._recording(monkeypatch)
        cfg, rt, state, out = land(repo, tmp_path, full_test_lock=None)
        assert out["next_hop"] != "escalate"
        assert all(name != "full-suite" for name, _ in seen)


class TestALandingRecordsItsGreenSuite:
    """A stage reaches `advance` only after the full suite passed on its
    tree, and a publication that pulled a moved tip re-ran it on the rebased
    tree before pushing — so a publication that did not escalate means the
    pushed tree passed a full suite, whichever node ran it. The state here
    is what production hands `advance`; no digest is injected."""

    def _land_after_a_green_suite(self, repo, tmp_path, **cfg_over):
        cfg, rt, state = make(repo, tmp_path, remote_landing=True, **cfg_over)
        state = with_stage(state, rt)
        (repo / "app.py").write_text("stage work\n")
        state = {**state, "review_summary": "fine", "review_record": "did it"}
        return cfg, rt, state, nodes.advance(state, rt)

    def test_a_pushed_landing_is_recorded_green_on_its_tip(self, repo, tmp_path, origin):
        from code_gantry.ledger import SUITE_GREEN

        bare, other = origin
        cfg, rt, state, out = self._land_after_a_green_suite(repo, tmp_path)
        tip = out["completed"][-1]["merge_sha"]
        greens = [e for e in rt.ledger.events() if e.kind == SUITE_GREEN]
        assert [(e.sha, e.body["command"]) for e in greens] == [(tip, cfg.full_test_command)]

    def test_a_landing_onto_a_moved_tip_records_the_rebased_tip(self, repo, tmp_path, origin):
        from code_gantry.ledger import SUITE_GREEN

        bare, other = origin
        other_lands(other)
        cfg, rt, state, out = self._land_after_a_green_suite(repo, tmp_path, full_test_command="true")
        assert out["next_hop"] != "escalate"
        tip = origin_tip(bare)
        greens = [e for e in rt.ledger.events() if e.kind == SUITE_GREEN]
        assert [e.sha for e in greens] == [tip], "the tip the re-test passed on is the one recorded"

    def test_a_landing_with_no_origin_records_its_tip(self, repo, tmp_path):
        from code_gantry.ledger import SUITE_GREEN

        cfg, rt, state, out = self._land_after_a_green_suite(repo, tmp_path)
        greens = [e for e in rt.ledger.events() if e.kind == SUITE_GREEN]
        assert [e.sha for e in greens] == [rt.git.rev_parse("proj")]

    def test_a_landing_whose_re_test_was_red_records_nothing(self, repo, tmp_path, origin):
        from code_gantry.ledger import SUITE_GREEN

        bare, other = origin
        other_lands(other, name="poison.txt")
        cfg, rt, state, out = self._land_after_a_green_suite(repo, tmp_path, full_test_command="test ! -e poison.txt")
        assert out["next_hop"] == "escalate"
        assert not [e for e in rt.ledger.events() if e.kind == SUITE_GREEN]


class TestAFreshHostTakesBranchesFromOrigin:
    """A bay is cloned from its host's primary repo copy, so it holds only the
    branches that copy had locally. On a host that has never run the project
    both the base ref and the project branch can be absent, and only origin
    has them."""

    def bay(self, tmp_path, bare, name="bay1"):
        path = tmp_path / name
        sh(tmp_path, "clone", "-q", "--single-branch", "--branch", "main", str(bare), str(path))
        return path

    def checks(self, path, **over):
        fields = {
            "target_repo": str(path),
            "base_ref": "elsewhere",
            "project_branch": "proj",
            "full_test_command": "true",
            **over,
        }
        cfg = parse_config(minimal(**fields))
        return run_preflight(
            cfg,
            run_tests=False,
            check_models=False,
            check_approval=False,
            check_endpoint=False,
        )

    @pytest.fixture
    def bare_with_a_base(self, origin, repo):
        bare, other = origin
        sh(repo, "branch", "elsewhere", "main")
        sh(repo, "push", "-q", "origin", "elsewhere")
        return bare

    def named(self, checks, name):
        return [c for c in checks if name in c.name]

    def test_the_base_ref_is_fetched_and_the_check_line_says_so(self, tmp_path, bare_with_a_base):
        path = self.bay(tmp_path, bare_with_a_base)
        assert not Git(path).branch_exists("elsewhere")
        checks = self.checks(path, remote_landing=True)
        fetched = self.named(checks, "'elsewhere' fetched from origin")
        assert fetched and fetched[0].ok, [(c.name, c.detail) for c in checks]
        assert self.named(checks, "base_ref")[0].ok
        assert Git(path).rev_parse("elsewhere") == sh(bare_with_a_base, "rev-parse", "elsewhere")

    def test_the_project_branch_is_taken_from_origin_not_cut_from_the_base(
        self, tmp_path, bare_with_a_base, origin
    ):
        bare, other = origin
        theirs = other_lands(other)
        path = self.bay(tmp_path, bare_with_a_base)
        checks = self.checks(path, remote_landing=True)
        assert self.named(checks, "'proj' fetched from origin")[0].ok
        assert Git(path).rev_parse("proj") == theirs, "their landings are not discarded"

    def test_without_remote_landing_the_project_branch_is_left_to_this_host(
        self, tmp_path, bare_with_a_base, origin
    ):
        bare, other = origin
        other_lands(other)
        path = self.bay(tmp_path, bare_with_a_base)
        checks = self.checks(path)
        assert not self.named(checks, "'proj' fetched from origin")
        assert not Git(path).branch_exists("proj")
        assert self.named(checks, "base_ref")[0].ok, "the base is fetched either way"

    def test_a_base_ref_origin_does_not_have_either_still_fails(self, tmp_path, origin):
        bare, other = origin
        path = self.bay(tmp_path, bare)
        checks = self.checks(path, remote_landing=True)
        base = self.named(checks, "base_ref")[0]
        assert base.blocking
        assert not self.named(checks, "'elsewhere' fetched from origin")

    def test_branches_already_here_are_not_fetched(self, repo, tmp_path, bare_with_a_base):
        checks = self.checks(repo, base_ref="main", remote_landing=True)
        assert not self.named(checks, "fetched from origin")


class TestPreflightPullsFirst:
    """Under `remote_landing` a run's preflight brings the project branch to
    origin's tip before it asks anything about the tree, so the tip it asks
    about is the tip the run will use — and a tip another host landed green
    is not proven again here."""

    def cfg(self, repo, marker, **over):
        from test_config import minimal

        fields = {
            "target_repo": str(repo), "base_ref": "main", "project_branch": "proj",
            "full_test_command": f"echo x >> {marker}", "remote_landing": True, **over,
        }
        return parse_config(minimal(**fields))

    def preflight(self, cfg, ledger, *, pull=True):
        return run_preflight(
            cfg, check_models=False, check_approval=False, check_endpoint=False,
            ledger=ledger, pull=pull,
        )

    def test_a_tip_another_host_landed_green_is_pulled_and_not_proven_again(self, repo, tmp_path, origin):
        from code_gantry.ledger import open_ledger

        bare, other = origin
        theirs = other_lands(other)
        marker = tmp_path / "runs.txt"
        cfg = self.cfg(repo, marker)
        shared = tmp_path / "shared.db"
        open_ledger(shared, origin="other-host", actor="o").record_green(theirs, cfg.full_test_command)
        checks = self.preflight(cfg, open_ledger(shared, origin="this-host", actor="t"))
        pulled = next(c for c in checks if "is at origin's tip" in c.name)
        assert pulled.ok and f"pulled to {theirs[:12]}" in pulled.detail, pulled.detail
        assert Git(repo).rev_parse("proj") == theirs
        assert not marker.exists(), "the suite ran on a tip another host proved"
        suite = next(c for c in checks if "full_test_command passes" in c.name)
        assert "proven green by other-host" in suite.detail

    def test_already_at_the_tip_says_so_and_pulls_nothing(self, repo, tmp_path, origin):
        from code_gantry.ledger import open_ledger

        marker = tmp_path / "runs.txt"
        cfg = self.cfg(repo, marker)
        before = Git(repo).rev_parse("proj")
        checks = self.preflight(cfg, open_ledger(tmp_path / "l.db", origin="this-host"))
        pulled = next(c for c in checks if "is at origin's tip" in c.name)
        assert pulled.ok and f"already at {before[:12]}" in pulled.detail
        assert Git(repo).rev_parse("proj") == before

    def test_validate_never_pulls(self, repo, tmp_path, origin):
        from code_gantry.ledger import open_ledger

        bare, other = origin
        theirs = other_lands(other)
        cfg = self.cfg(repo, tmp_path / "runs.txt")
        checks = self.preflight(cfg, open_ledger(tmp_path / "l.db", origin="this-host"), pull=False)
        assert not [c for c in checks if "is at origin's tip" in c.name]
        assert Git(repo).rev_parse("proj") != theirs

    def test_without_remote_landing_nothing_is_pulled(self, repo, tmp_path, origin):
        from code_gantry.ledger import open_ledger

        bare, other = origin
        theirs = other_lands(other)
        cfg = self.cfg(repo, tmp_path / "runs.txt", remote_landing=False)
        checks = self.preflight(cfg, open_ledger(tmp_path / "l.db", origin="this-host"))
        assert not [c for c in checks if "is at origin's tip" in c.name]
        assert Git(repo).rev_parse("proj") != theirs

    def test_a_pull_that_conflicts_blocks_before_anything_runs(self, repo, tmp_path, origin):
        from code_gantry.ledger import open_ledger

        bare, other = origin
        other_lands(other, name="app.py", text="theirs\n")
        g = Git(repo)
        g.checkout("proj")
        (repo / "app.py").write_text("ours\n")
        g.commit_all("local divergence")
        marker = tmp_path / "runs.txt"
        cfg = self.cfg(repo, marker)
        checks = self.preflight(cfg, open_ledger(tmp_path / "l.db", origin="this-host"))
        pulled = next(c for c in checks if "is at origin's tip" in c.name)
        assert pulled.blocking and "pull --rebase" in pulled.detail
        assert not marker.exists()
        assert g.is_clean(), "the aborted rebase leaves a clean tree"
