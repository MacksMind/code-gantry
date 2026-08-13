"""Paths the operator says a model may never write by hand.

Scope answers "may this stage touch this file". This answers a different
question that scope cannot: a file that is *generated* and must be produced by
the tool that owns it. A lockfile is the case that produced the rule — a
hand-written one can be unsatisfiable, and the failure surfaces later, in an
install that cannot succeed, in a container that then never comes back.

The refusal carries the operator's own sentence rather than one of ours. Why a
generated file is off-limits and what to run instead are properties of the
project, and a message written here would be a Ruby hint shipped to every
project's executor.
"""

from __future__ import annotations

import pytest

from orchestrator.edittools import Edit, FileEditor
from orchestrator.repotools import ToolError


def _editor(tmp_path, **kwargs) -> FileEditor:
    (tmp_path / "Gemfile.lock").write_text("GEM\n  specs:\n")
    (tmp_path / "app.rb").write_text("x = 1\n")
    return FileEditor(repo=tmp_path, edit_files=["**/*"], **kwargs)


NO_DIRECT_EDIT = [("Gemfile.lock", "resolve it with bundle_install instead")]


def test_edit_is_refused_and_the_reason_is_the_operators(tmp_path):
    editor = _editor(tmp_path, no_direct_edit=NO_DIRECT_EDIT)
    with pytest.raises(ToolError) as raised:
        editor.edit("Gemfile.lock", [Edit(old_string="GEM", new_string="X")])
    assert "resolve it with bundle_install instead" in str(raised.value)
    assert (tmp_path / "Gemfile.lock").read_text() == "GEM\n  specs:\n"


def test_create_file_and_delete_file_are_refused_too(tmp_path):
    """Otherwise the rule is a suggestion: delete and recreate reaches it."""
    editor = _editor(tmp_path, no_direct_edit=NO_DIRECT_EDIT)
    with pytest.raises(ToolError):
        editor.delete_file("Gemfile.lock")
    with pytest.raises(ToolError):
        editor.create_file("Gemfile.lock", "GEM\n")
    assert (tmp_path / "Gemfile.lock").exists()


def test_the_refusal_is_recorded_with_its_own_kind(tmp_path):
    """A refused call must reach the ledger, and say which rule refused it.

    A ledger that records only some refusals lists only the ones somebody
    remembered, and `_refusal_kind` cannot recover this bucket from the text:
    the message is the operator's and says whatever they wrote.
    """
    editor = _editor(tmp_path, no_direct_edit=NO_DIRECT_EDIT)
    with pytest.raises(ToolError) as raised:
        editor.edit("Gemfile.lock", [Edit(old_string="GEM", new_string="X")])
    assert raised.value.kind == "no direct edit"


def test_other_paths_are_untouched_by_the_rule(tmp_path):
    editor = _editor(tmp_path, no_direct_edit=NO_DIRECT_EDIT)
    editor.edit("app.rb", [Edit(old_string="x = 1", new_string="x = 2")])
    assert (tmp_path / "app.rb").read_text() == "x = 2\n"


def test_no_declarations_is_todays_behaviour(tmp_path):
    """A project that declares none must edit exactly as it always has."""
    editor = _editor(tmp_path)
    editor.edit("Gemfile.lock", [Edit(old_string="GEM", new_string="X")])
    assert (tmp_path / "Gemfile.lock").read_text().startswith("X")


def test_a_glob_covers_a_family(tmp_path):
    """The declaration is a glob, so one entry covers a directory of them."""
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "a.lock").write_text("a\n")
    editor = FileEditor(
        repo=tmp_path,
        edit_files=["**/*"],
        no_direct_edit=[("vendor/**/*.lock", "generated; run the installer")],
    )
    with pytest.raises(ToolError) as raised:
        editor.edit("vendor/a.lock", [Edit(old_string="a", new_string="b")])
    assert "generated; run the installer" in str(raised.value)


def test_it_is_refused_before_scope_is_consulted(tmp_path):
    """Two rules, and the one naming the real cause has to win.

    A generated file is very often *in* a stage's scope — this one was, on
    every stage that touched the Gemfile. If scope answered first the model
    would be told the path is out of bounds, which is false and sends it to
    ask the planner to widen a list that is already wide enough.
    """
    editor = FileEditor(
        repo=tmp_path,
        edit_files=["app.rb"],
        no_direct_edit=NO_DIRECT_EDIT,
    )
    (tmp_path / "Gemfile.lock").write_text("GEM\n")
    with pytest.raises(ToolError) as raised:
        editor.edit("Gemfile.lock", [Edit(old_string="GEM", new_string="X")])
    assert "bundle_install" in str(raised.value)
    assert "scope" not in str(raised.value)


class TestItReachesTheEditor:
    """The journey, not its endpoints.

    A value computed correctly and written correctly has been lost in transit
    four separate times here — dropped by a schema that did not declare the
    key, zeroed by a reset spread over the top of it, omitted from the
    artifact meant to prove it existed. This declaration crosses from YAML
    through `ExecutorConfig` into a dataclass that is not a pydantic model, so
    it crosses a schema boundary and gets an end-to-end test rather than two
    unit tests that both pass while nothing arrives.
    """

    def test_a_declaration_in_config_refuses_a_write(self, repo):
        from orchestrator.executorloop import build_loop_parts
        from test_executor_loop import build

        cfg, stage = build(
            repo,
            executor={
                "model": "m",
                "no_direct_edit": [
                    {"path_glob": "Gemfile.lock", "reason": "run the installer"}
                ],
            },
        )
        _, editor, _sem = build_loop_parts(stage, cfg, repo)
        (repo / "Gemfile.lock").write_text("GEM\n")
        with pytest.raises(ToolError) as raised:
            editor.edit("Gemfile.lock", [Edit(old_string="GEM", new_string="X")])
        assert "run the installer" in str(raised.value)

    def test_a_project_declaring_none_still_writes(self, repo):
        from orchestrator.executorloop import build_loop_parts
        from test_executor_loop import build

        cfg, stage = build(repo)
        _, editor, _sem = build_loop_parts(stage, cfg, repo)
        assert editor.no_direct_edit == []


class TestTheExecutorIsToldUpFront:
    """Declared, not discovered.

    A refusal costs a call and arrives after the model has chosen its move —
    and this one refuses a path the stage's scope permits, which reads as a
    contradiction from where the model sits. Generated from the config so a
    project declaring none reads exactly the prompt it read before.
    """

    def test_a_project_declaring_none_gets_no_paragraph(self, repo):
        from orchestrator.prompts import _executor_system_prompt
        from test_executor_loop import build

        cfg, _stage = build(repo)
        assert "not yours to author" not in _executor_system_prompt(cfg)

    def test_the_declaration_and_its_reason_both_appear(self, repo):
        from orchestrator.prompts import _executor_system_prompt
        from test_executor_loop import build

        cfg, _stage = build(
            repo,
            executor={
                "model": "m",
                "no_direct_edit": [
                    {"path_glob": "Gemfile.lock", "reason": "run the installer"}
                ],
            },
        )
        text = _executor_system_prompt(cfg)
        assert "Gemfile.lock" in text
        assert "run the installer" in text

    def test_no_project_vocabulary_is_hardcoded(self):
        """The rule that keeps project knowledge out of model-facing strings.

        The paragraph around the operator's list must not name a framework, a
        manifest or a command — those are the operator's to supply, and a
        default written here ships one ecosystem to every project.
        """
        import inspect

        from orchestrator import prompts

        source = inspect.getsource(prompts._no_direct_edit_block)
        for word in ("Gemfile", "bundle", "lockfile", "gem", "npm", "yarn"):
            assert word.lower() not in source.lower(), word
