"""A resume refuses when the plan has moved under it.

The run reads its plan documents once, at `plan_sha`, and every later read goes
through `git show <plan_sha>:<path>` — so the reviewer judges a diff against the
same text the planner drew it from, and nothing is substituted mid-run. A resume
inherits that sha in `**saved` and re-reads nothing.

That is exactly wrong across a fold. A fold moves content *out* of the progress
log, which is read live from the worktree, and *into* the plan documents and the
repository's agent-facing docs, which are pinned. Resumed, the run sees neither
copy: the live log no longer carries it and the pinned documents never did. From
the planner's side a fold is indistinguishable from deleting the log.

Refusing is the whole answer rather than re-reading, because anything already
derived — the stage in `current`, and every stage sitting in `stage_queue` from
a batched derivation — was drawn against the old text and is not made valid by
loading the new. A fresh run is the boundary that re-reads, and it is cheap
here: the work is on the project branch, not in the checkpoint.

The progress log is excluded by name. It changes on every landing, so including
it would refuse every resume.
"""

from pathlib import Path

import pytest

from code_gantry.config import parse_config
from code_gantry.gitops import Git
from code_gantry.preflight import _plan_unmoved


def commit(repo, run_git, files: dict[str, str], message="plan") -> str:
    for name, body in files.items():
        path = Path(repo) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    run_git(repo, "add", "-A")
    run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", message)
    return Git(repo).head_sha()


def cfg_for(repo, **over):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "project_branch": "proj",
        "plan_root": "docs/plan.md",
        "plan_addendum_path": "docs/progress.md",
        "full_test_command": "true",
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.6-sol"},
    }
    data.update(over)
    return parse_config(data)


@pytest.fixture
def planned(repo, run_git):
    """A repo on the project branch, whose plan root links a child and a log."""
    run_git(repo, "checkout", "-q", "-b", "proj")
    sha = commit(
        repo,
        run_git,
        {
            "docs/plan.md": "# Plan\nsee [gems](gems.md) and [progress](progress.md)\n",
            "docs/gems.md": "# Gems\nstep one\n",
            "docs/progress.md": "# Progress\nnothing yet\n",
            "AGENTS.md": "# Conventions\nrun the suite with bin/rspec\n",
        },
    )
    return sha


class TestUnmoved:
    def test_passes_when_nothing_has_been_committed_since(self, repo, planned):
        check = _plan_unmoved(cfg_for(repo), Git(repo), planned)
        assert check.ok
        assert planned[:12] in check.detail

    def test_passes_when_the_branch_moved_but_no_pinned_document_did(
        self, repo, run_git, planned
    ):
        # The ordinary case: the run itself lands stages on the project branch.
        commit(repo, run_git, {"app.py": "def hello():\n    return 2\n"}, "a stage")
        assert _plan_unmoved(cfg_for(repo), Git(repo), planned).ok

    def test_passes_when_only_the_progress_log_changed(self, repo, run_git, planned):
        # It is read live from the worktree on every planner call, so it is the
        # one plan document that is allowed to move under a resume — and it
        # moves on every landing, so refusing over it would refuse everything.
        commit(repo, run_git, {"docs/progress.md": "# Progress\none stage\n"})
        assert _plan_unmoved(cfg_for(repo), Git(repo), planned).ok


class TestMoved:
    def test_refuses_when_a_plan_document_changed(self, repo, run_git, planned):
        commit(repo, run_git, {"docs/gems.md": "# Gems\nstep one, folded\n"})
        check = _plan_unmoved(cfg_for(repo), Git(repo), planned)
        assert not check.ok
        assert check.blocking
        assert "docs/gems.md" in check.detail

    def test_refuses_when_the_plan_root_changed(self, repo, run_git, planned):
        commit(
            repo,
            run_git,
            {"docs/plan.md": "# Plan\nsee [gems](gems.md) and [progress](progress.md)\nmore\n"},
        )
        check = _plan_unmoved(cfg_for(repo), Git(repo), planned)
        assert not check.ok
        assert "docs/plan.md" in check.detail

    def test_refuses_when_a_child_is_added(self, repo, run_git, planned):
        commit(
            repo,
            run_git,
            {
                "docs/plan.md": (
                    "# Plan\nsee [gems](gems.md), [routes](routes.md) "
                    "and [progress](progress.md)\n"
                ),
                "docs/routes.md": "# Routes\nnew stream\n",
            },
        )
        check = _plan_unmoved(cfg_for(repo), Git(repo), planned)
        assert not check.ok
        assert "docs/routes.md" in check.detail

    def test_refuses_when_an_agent_context_document_changed(self, repo, run_git, planned):
        # The other half of a fold: the durable part of a log entry moves into
        # the repository's own agent-facing document, which is pinned at the
        # same sha as the plan.
        commit(repo, run_git, {"AGENTS.md": "# Conventions\nrun bin/parallel_rspec\n"})
        check = _plan_unmoved(cfg_for(repo), Git(repo), planned)
        assert not check.ok
        assert "AGENTS.md" in check.detail

    def test_follows_a_symlinked_agent_document_to_its_target(
        self, repo, run_git, planned
    ):
        # `CLAUDE.md -> AGENTS.md` is the usual shape, and a symlink's blob is
        # its target path — which does not change when the target's contents
        # do. Comparing blobs alone would call this unmoved.
        (Path(repo) / "CLAUDE.md").symlink_to("AGENTS.md")
        run_git(repo, "add", "-A")
        run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "link")
        started = Git(repo).head_sha()
        commit(repo, run_git, {"AGENTS.md": "# Conventions\nchanged\n"})

        cfg = cfg_for(repo, agent_context=["CLAUDE.md"])
        check = _plan_unmoved(cfg, Git(repo), started)
        assert not check.ok
        assert "AGENTS.md" in check.detail

    def test_names_the_fresh_run_as_the_way_forward(self, repo, run_git, planned):
        commit(repo, run_git, {"docs/gems.md": "# Gems\nfolded\n"})
        check = _plan_unmoved(cfg_for(repo), Git(repo), planned)
        assert "code-gantry run" in check.detail


class TestWiring:
    """The recorded sha has to reach the check, which is where values go missing."""

    def test_run_preflight_asks_when_given_a_recorded_plan_sha(
        self, repo, run_git, planned
    ):
        from code_gantry.preflight import run_preflight

        commit(repo, run_git, {"docs/gems.md": "# Gems\nfolded\n"})
        checks = run_preflight(
            cfg_for(repo),
            run_tests=False,
            check_models=False,
            check_endpoint=False,
            for_resume=True,
            recorded_plan_sha=planned,
        )
        moved = [c for c in checks if "plan" in c.name and c.blocking]
        assert moved, [c.name for c in checks]

    def test_run_preflight_does_not_ask_on_a_fresh_run(self, repo, run_git, planned):
        from code_gantry.preflight import run_preflight

        commit(repo, run_git, {"docs/gems.md": "# Gems\nfolded\n"})
        checks = run_preflight(
            cfg_for(repo),
            run_tests=False,
            check_models=False,
            check_endpoint=False,
        )
        assert not [c for c in checks if c.blocking]

    def test_resume_hands_the_checkpoints_plan_sha_to_preflight(
        self, repo, run_git, planned, tmp_path, monkeypatch
    ):
        """`cli.resume` reads `plan_sha` off the checkpoint and passes it on.

        The hop that has actually lost a value here before: `resume_fields` said
        for months that it was "what a resume merges over the saved checkpoint"
        while nothing merged it, and every test asserting what the delta
        *contained* stayed green.
        """
        import yaml

        from code_gantry import cli
        from code_gantry.preflight import Check

        config_path = Path(repo) / "docs" / "code_gantry.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "base_ref": "main",
                    "project_branch": "proj",
                    "plan_root": "docs/plan.md",
                    "plan_addendum_path": "docs/progress.md",
                    "full_test_command": "true",
                    "executor": {"model": "m"},
                    "planner": {"model": "claude-opus-5"},
                    "reviewer": {"model": "gpt-5.6-sol"},
                }
            )
        )

        seen = {}

        def fake_preflight(cfg, runner=None, **kw):
            seen.update(kw)
            # Blocking, so the command stops before it needs a real run.
            return [Check("stop here", False, "")]

        monkeypatch.setattr(cli, "run_preflight", fake_preflight)
        monkeypatch.setattr(cli, "_resolve_run_id", lambda project, run_id: "run-1")
        monkeypatch.setattr(
            cli, "_load_state", lambda paths, run_id: {"plan_sha": planned, "base_sha": planned}
        )
        monkeypatch.setattr(cli, "problem_resuming", lambda path, saved: "")

        from click.testing import CliRunner

        result = CliRunner().invoke(cli.main, ["resume", str(config_path)])
        assert seen.get("recorded_plan_sha") == planned, result.output
