"""Paths the scope gate must not fail a stage over.

Some files rewrite themselves as a side effect of running the suite, and no
instruction to the executor can prevent it. On this project VCR cassettes are
configured to re-record when they are more than six months old, so a stage that
merely *runs* a spec touching an expired cassette produces a diff entry it never
authored. `commit_all` then sweeps it onto the stage branch and the scope gate,
correctly comparing the diff against `edit_files`, fails the stage.

That is the incident behind `order-edit-item-personalization-explicit-scope`,
which cost three redraws, 884 seconds of planning and a `restart` verdict on an
approved diff. The first failure was a cassette, and every attempt that ran the
same spec re-created it.

So the operator declares which paths are expected to move on their own. This is
project knowledge in the strongest sense — that cassettes auto-refresh at six
months is a fact about one repository's VCR configuration — so there is no
default, and a project that declares nothing behaves exactly as before.

Deliberately narrower than `edit_files`: an exemption says "changing this is not
evidence the executor wandered", not "this is part of the stage". It cannot
exempt a plan document, because that check runs first and is not the planner's
to widen either.
"""

import pytest

from test_config import as_test_tools


def _ctx(tmp_path, edit_files, exempt=()):
    from types import SimpleNamespace

    from code_gantry.config import Stage, parse_config

    cfg = parse_config(as_test_tools({
        "target_repo": str(tmp_path), "base_ref": "main", "project_branch": "p",
        "plan_root": "docs/PLAN.md", "full_test_command": "true",
        "scope_exempt_globs": list(exempt),
        "executor": {"model": "m"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.6-sol"},
    }))
    return SimpleNamespace(
        cfg=cfg,
        stage=Stage(id="s", instruction="do it", edit_files=list(edit_files)),
        plan=None,
    )


class TestAnExemptPathIsNotAScopeViolation:
    def test_without_an_exemption_it_fails(self, tmp_path):
        from code_gantry.verify import out_of_scope_paths

        ctx = _ctx(tmp_path, ["app/**"])
        assert out_of_scope_paths(
            ["app/a.rb", "spec/vcr/Thing/example.yml"], ctx
        ) == ["spec/vcr/Thing/example.yml"]

    def test_with_an_exemption_it_does_not(self, tmp_path):
        from code_gantry.verify import out_of_scope_paths

        ctx = _ctx(tmp_path, ["app/**"], ["spec/vcr/**"])
        assert out_of_scope_paths(
            ["app/a.rb", "spec/vcr/Thing/example.yml"], ctx
        ) == []

    def test_it_does_not_excuse_anything_else(self, tmp_path):
        # An exemption is a named allowance, not a general softening.
        from code_gantry.verify import out_of_scope_paths

        ctx = _ctx(tmp_path, ["app/**"], ["spec/vcr/**"])
        assert out_of_scope_paths(
            ["config/routes.rb", "spec/vcr/x.yml"], ctx
        ) == ["config/routes.rb"]

    def test_declaring_nothing_behaves_as_before(self, tmp_path):
        from code_gantry.verify import out_of_scope_paths

        ctx = _ctx(tmp_path, ["app/**"])
        assert out_of_scope_paths(["lib/x.rb"], ctx) == ["lib/x.rb"]


class TestItIsProjectKnowledge:
    def test_there_is_no_default(self):
        # A default naming `spec/vcr` would ship one project's VCR
        # configuration to every other project's scope gate.
        from code_gantry.config import ProjectConfig

        assert ProjectConfig.model_fields["scope_exempt_globs"].default_factory() == []
