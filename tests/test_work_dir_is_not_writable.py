"""The executor's write tools refuse the work dir, the config and the conventions.

The scope gate reads `git diff`, which never shows the gitignored work dir, so
a write there would be invisible to every gate. The refusal has to sit on the
tool. `build_loop_parts` wires it; this drives the wiring, and a builder with
no guard is the falsification.
"""

import pytest

from test_config import as_test_tools

from code_gantry.config import Stage, parse_config
from code_gantry.edittools import FileEditor
from code_gantry.executorloop import build_loop_parts, protected_paths
from code_gantry.repotools import ToolError


def cfg_for(repo):
    return parse_config(
        as_test_tools({
            "target_repo": str(repo),
            "base_ref": "main",
            "project_branch": "proj",
            "plan_root": "PLAN.md",
            "full_test_command": "true",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.5"},
            "config_rel_path": "code_gantry.yaml",
        })
    )


class TestThePredicate:
    def test_names_the_work_dir_the_config_and_the_conventions(self, repo):
        cfg = cfg_for(repo)
        protected = protected_paths(cfg)
        work_dir = str(cfg.work_dir.relative_to(cfg.target_repo))
        assert protected(f"{work_dir}/ledger.db")
        assert protected(work_dir)
        assert protected("code_gantry.yaml")
        assert protected("AGENTS.md") and protected("CLAUDE.md")

    def test_leaves_ordinary_files_alone(self, repo):
        protected = protected_paths(cfg_for(repo))
        assert not protected("app.py")
        assert not protected("docs/PLAN.md")


class TestTheEditorRefuses:
    def _editor(self, repo, guarded=True):
        cfg = cfg_for(repo)
        stage = Stage(id="s", instruction="i", edit_files=["**"])
        if guarded:
            editor = build_loop_parts(stage, cfg, repo)[1]
        else:
            editor = FileEditor(repo=repo, edit_files=["**"])
        return cfg, editor

    def test_a_write_under_the_work_dir_is_refused(self, repo):
        cfg, editor = self._editor(repo)
        work_dir = str(cfg.work_dir.relative_to(cfg.target_repo))
        with pytest.raises(ToolError):
            editor.create_file(f"{work_dir}/notes.md", "fabricated")
        assert not (repo / work_dir / "notes.md").exists()

    def test_a_write_to_the_conventions_is_refused(self, repo):
        cfg, editor = self._editor(repo)
        with pytest.raises(ToolError):
            editor.create_file("AGENTS.md", "# rewritten")

    def test_without_the_guard_the_same_write_lands(self, repo):
        # The falsification: the refusal is the wiring, not the file system.
        cfg, editor = self._editor(repo, guarded=False)
        work_dir = str(cfg.work_dir.relative_to(cfg.target_repo))
        editor.create_file(f"{work_dir}/notes.md", "fabricated")
        assert (repo / work_dir / "notes.md").exists()
