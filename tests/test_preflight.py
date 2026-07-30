"""Preflight checks.

"Failing fast beats failing on stage 6" is the whole reason this exists, so
the tests are mostly about catching the conditions that would otherwise waste
a long run.
"""

import os
import stat

from orchestrator.commands import CommandRunner
from orchestrator.config import parse_config
from orchestrator.gitops import Git
from orchestrator.preflight import Check, check_aider_flags, format_checks, run_preflight


def cfg_with(repo, **over):
    data = {
        "target_repo": str(repo),
        "base_ref": "main",
        "branch": "refactor/thing",
        "test_command": "true",
        "executor": {"model": "m"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": [{"id": "s1", "instruction": "do it", "edit_files": ["app.py"]}],
    }
    data.update(over)
    return parse_config(data)


def preflight(repo, cfg, **kw):
    kw.setdefault("check_aider", False)
    kw.setdefault("check_reviewer", False)
    return run_preflight(
        cfg, CommandRunner(cwd=repo, timeout=60), **kw
    )


def named(checks, fragment):
    return next(c for c in checks if fragment in c.name)


class TestRepoChecks:
    def test_clean_repo_passes(self, repo):
        checks = preflight(repo, cfg_with(repo))
        assert not any(c.blocking for c in checks)

    def test_dirty_tree_blocks(self, repo):
        (repo / "app.py").write_text("uncommitted\n")
        checks = preflight(repo, cfg_with(repo))
        assert named(checks, "working tree is clean").blocking

    def test_missing_repo_blocks(self, tmp_path):
        cfg = cfg_with(tmp_path / "nope")
        checks = preflight(tmp_path, cfg)
        assert checks[0].blocking

    def test_non_git_directory_blocks(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        checks = preflight(plain, cfg_with(plain))
        assert any(c.blocking for c in checks)

    def test_unknown_base_ref_blocks(self, repo):
        checks = preflight(repo, cfg_with(repo, base_ref="nonexistent"))
        assert named(checks, "base_ref").blocking

    def test_stops_early_when_the_repo_is_unusable(self, tmp_path):
        # Running environment checks against a broken repo produces confusing
        # secondary failures.
        plain = tmp_path / "plain"
        plain.mkdir()
        checks = preflight(plain, cfg_with(plain, test_command="exit 1"))
        assert not any("test_command" in c.name for c in checks)


class TestBranchSafety:
    def test_new_branch_is_fine(self, repo):
        checks = preflight(repo, cfg_with(repo))
        assert not named(checks, "is new").blocking

    def test_existing_branch_not_checked_out_blocks(self, repo):
        # Reusing another run's branch by accident would mix two units of work.
        Git(repo).create_branch("refactor/thing", base="main")
        Git(repo).checkout("main")
        checks = preflight(repo, cfg_with(repo))
        assert named(checks, "safe to use").blocking

    def test_existing_branch_already_checked_out_is_fine(self, repo):
        Git(repo).create_branch("refactor/thing", base="main")
        checks = preflight(repo, cfg_with(repo))
        assert not named(checks, "safe to use").blocking

    def test_head_must_match_base_ref_before_cutting(self, repo, run_git):
        # Otherwise the run silently starts from somewhere other than base_ref.
        run_git(repo, "checkout", "-q", "-b", "elsewhere")
        (repo / "app.py").write_text("divergent\n")
        run_git(repo, "commit", "-aqm", "divergent")
        checks = preflight(repo, cfg_with(repo))
        assert named(checks, "HEAD matches").blocking

    def test_resume_requires_being_on_the_run_branch(self, repo):
        checks = preflight(repo, cfg_with(repo), for_resume=True)
        assert named(checks, "on branch").blocking

    def test_resume_does_not_require_a_clean_tree(self, repo):
        # A run paused at a manual stage is resumed because a human just did
        # work, and that work is normally uncommitted. Requiring clean here
        # would make manual stages unusable.
        Git(repo).create_branch("refactor/thing", base="main")
        (repo / "human-work.txt").write_text("bumped the runtime\n")
        checks = preflight(repo, cfg_with(repo), for_resume=True)
        assert not any(c.blocking for c in checks)


class TestEnvironmentChecks:
    def test_failing_setup_blocks(self, repo):
        checks = preflight(repo, cfg_with(repo, setup_command="exit 1"))
        assert named(checks, "setup_command").blocking

    def test_red_test_suite_blocks(self, repo):
        # A target repo that is already red makes every later verdict
        # meaningless.
        checks = preflight(repo, cfg_with(repo, test_command="exit 1"))
        assert named(checks, "test_command").blocking

    def test_full_test_command_is_checked_too(self, repo):
        checks = preflight(repo, cfg_with(repo, full_test_command="exit 1"))
        assert named(checks, "full_test_command").blocking

    def test_tests_can_be_skipped(self, repo):
        checks = preflight(repo, cfg_with(repo, test_command="exit 1"), run_tests=False)
        assert not any(c.blocking for c in checks)

    def test_warns_when_the_test_command_dirties_the_tree(self, repo):
        # Un-ignored caches fail the scope guard on every stage, and make `run`
        # refuse to start after a `validate`. Naming the paths once here is far
        # cheaper than discovering it mid-run.
        checks = preflight(repo, cfg_with(repo, test_command="touch cache.junk"))
        check = named(checks, "leaves the tree clean")
        assert not check.ok
        assert not check.blocking
        assert "cache.junk" in check.detail

    def test_no_warning_when_the_tree_stays_clean(self, repo):
        assert named(preflight(repo, cfg_with(repo)), "leaves the tree clean").ok

    def test_setup_failure_stops_before_the_tests(self, repo):
        checks = preflight(
            repo, cfg_with(repo, setup_command="exit 1", test_command="true")
        )
        assert not any("test_command passes" in c.name for c in checks)


class TestAiderFlagCheck:
    def _fake_aider(self, tmp_path, monkeypatch, help_text, exit_code=0):
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "aider"
        script.write_text(f"#!/bin/sh\ncat <<'EOF'\n{help_text}\nEOF\nexit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    def test_all_flags_present_passes(self, repo, tmp_path, monkeypatch):
        from orchestrator.executor import AIDER_FLAGS

        self._fake_aider(tmp_path, monkeypatch, "\n".join(AIDER_FLAGS))
        checks = check_aider_flags(CommandRunner(cwd=repo, timeout=60))
        assert not any(c.blocking for c in checks)

    def test_a_renamed_flag_is_caught(self, repo, tmp_path, monkeypatch):
        # This is the point: a renamed flag must fail validation, not surface
        # as an opaque aider usage error on stage 1.
        from orchestrator.executor import AIDER_FLAGS

        partial = [f for f in AIDER_FLAGS if f != "--map-tokens"]
        self._fake_aider(tmp_path, monkeypatch, "\n".join(partial))
        checks = check_aider_flags(CommandRunner(cwd=repo, timeout=60))
        failing = named(checks, "still accepts")
        assert failing.blocking
        assert "--map-tokens" in failing.detail

    def test_missing_aider_is_caught(self, repo, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        checks = check_aider_flags(CommandRunner(cwd=repo, timeout=60))
        assert checks[0].blocking

    def test_skipped_when_no_agent_stages_exist(self, repo):
        # A run of only script and manual stages needs no aider at all — and
        # aider is not installed here, so the check would fail if it ran.
        cfg = cfg_with(
            repo,
            stages=[
                {"id": "s", "kind": "script", "command": "true", "edit_files": ["app.py"]}
            ],
        )
        checks = run_preflight(
            cfg,
            CommandRunner(cwd=repo, timeout=60),
            check_aider=True,
            check_reviewer=False,
        )
        assert not any("aider" in c.name for c in checks)


class TestPreconditionChecks:
    def test_unmet_precondition_is_a_warning_not_a_block(self, repo):
        # Stage 5's precondition may well be satisfied by stage 4.
        cfg = cfg_with(
            repo,
            stages=[
                {
                    "id": "s1",
                    "instruction": "x",
                    "edit_files": ["app.py"],
                    "preconditions": ["false"],
                }
            ],
        )
        checks = preflight(repo, cfg)
        check = named(checks, "precondition")
        assert not check.ok
        assert not check.blocking

    def test_met_precondition_passes(self, repo):
        cfg = cfg_with(
            repo,
            stages=[
                {
                    "id": "s1",
                    "instruction": "x",
                    "edit_files": ["app.py"],
                    "preconditions": ["true"],
                }
            ],
        )
        assert named(preflight(repo, cfg), "precondition").ok


class TestReviewerCheck:
    def test_missing_api_key_blocks(self, repo, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        checks = run_preflight(
            cfg_with(repo),
            CommandRunner(cwd=repo, timeout=60),
            check_aider=False,
            check_reviewer=True,
        )
        assert named(checks, "OPENAI_API_KEY").blocking

    def test_present_api_key_passes(self, repo, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        checks = run_preflight(
            cfg_with(repo),
            CommandRunner(cwd=repo, timeout=60),
            check_aider=False,
            check_reviewer=True,
        )
        assert not any(c.blocking for c in checks)


class TestFormatting:
    def test_marks_pass_fail_and_warn_distinctly(self):
        text = format_checks(
            [
                Check("a", True),
                Check("b", False),
                Check("c", False, fatal=False),
            ]
        )
        assert "[ok  ] a" in text
        assert "[FAIL] b" in text
        assert "[warn] c" in text

    def test_includes_detail_indented(self):
        text = format_checks([Check("a", False, "because reasons")])
        assert "because reasons" in text
