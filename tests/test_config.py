"""Config loading and structural validation.

Structural validation is everything checkable without executing anything in
the target repo. The checks that shell out (clean tree, test command passes,
endpoints reachable) live in preflight and are tested separately.
"""

import textwrap

import pytest

from orchestrator.config import ConfigError, load_config, parse_config


def minimal(**overrides):
    """A structurally valid config dict, so each test can break one thing."""
    base = {
        "target_repo": "/tmp/some-app",
        "base_ref": "main",
        "branch": "refactor/thing",
        "test_command": "pytest",
        "executor": {"model": "openai/local", "api_base": "http://localhost:8080/v1"},
        "reviewer": {"model": "gpt-5.5"},
        "stages": [
            {
                "id": "first",
                "instruction": "do the thing",
                "edit_files": ["src/**"],
            }
        ],
    }
    base.update(overrides)
    return base


class TestDefaults:
    def test_minimal_config_parses(self):
        cfg = parse_config(minimal())
        assert cfg.branch == "refactor/thing"
        assert len(cfg.stages) == 1

    def test_stage_defaults_to_agent_kind(self):
        cfg = parse_config(minimal())
        assert cfg.stages[0].kind == "agent"

    def test_limits_have_defaults(self):
        cfg = parse_config(minimal())
        assert cfg.limits.max_test_retries == 3
        assert cfg.limits.max_rework_retries == 2
        assert cfg.limits.aider_timeout_seconds == 1800
        assert cfg.limits.command_timeout_seconds == 3600

    def test_rework_defaults_match_spec(self):
        cfg = parse_config(minimal())
        assert cfg.rework_strategy == "fresh"
        assert cfg.rework_reset is True

    def test_reviewer_provider_defaults_to_openai(self):
        cfg = parse_config(minimal())
        assert cfg.reviewer.provider == "openai"
        assert cfg.reviewer.api_key_env == "OPENAI_API_KEY"

    def test_unknown_reviewer_provider_rejected(self):
        # Only one implementation exists; a typo should not silently pass.
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(reviewer={"provider": "antropic", "model": "x"}))
        assert "provider" in str(e.value)

    def test_unknown_top_level_key_rejected(self):
        # A misspelled key that silently does nothing is worse than an error.
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(test_comand="pytest"))
        assert "test_comand" in str(e.value)


class TestReviewDefault:
    """`review` defaults to True, but to False for manual stages."""

    def test_agent_stage_is_reviewed(self):
        cfg = parse_config(minimal())
        assert cfg.stages[0].reviews_enabled is True

    def test_manual_stage_is_not_reviewed_by_default(self):
        cfg = parse_config(
            minimal(
                stages=[
                    {
                        "id": "bump",
                        "kind": "manual",
                        "human_steps": "bump the runtime",
                        "checks": ["true"],
                    }
                ]
            )
        )
        assert cfg.stages[0].reviews_enabled is False

    def test_manual_stage_can_opt_into_review(self):
        cfg = parse_config(
            minimal(
                stages=[
                    {
                        "id": "bump",
                        "kind": "manual",
                        "human_steps": "bump the runtime",
                        "checks": ["true"],
                        "review": True,
                    }
                ]
            )
        )
        assert cfg.stages[0].reviews_enabled is True


class TestKindWellFormedness:
    def test_agent_stage_requires_instruction(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(stages=[{"id": "a", "edit_files": ["x"]}]))
        assert "instruction" in str(e.value)

    def test_script_stage_requires_command(self):
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(stages=[{"id": "a", "kind": "script", "edit_files": ["x"]}])
            )
        assert "command" in str(e.value)

    def test_manual_stage_requires_human_steps(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(stages=[{"id": "a", "kind": "manual"}]))
        assert "human_steps" in str(e.value)

    def test_agent_stage_rejects_script_command(self):
        # Wrong-kind fields signal a misunderstanding; fail loudly.
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(
                    stages=[
                        {
                            "id": "a",
                            "instruction": "x",
                            "command": "./script",
                            "edit_files": ["x"],
                        }
                    ]
                )
            )
        assert "command" in str(e.value)


class TestScopeDeclaration:
    """The scope guard is only meaningful if edit_files is declared."""

    def test_agent_stage_requires_edit_files(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(stages=[{"id": "a", "instruction": "x"}]))
        assert "edit_files" in str(e.value)

    def test_script_stage_requires_edit_files(self):
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(stages=[{"id": "a", "kind": "script", "command": "./s"}])
            )
        assert "edit_files" in str(e.value)

    def test_manual_stage_does_not_require_edit_files(self):
        # A human bump legitimately touches whatever the change needs; the
        # scope guard is skipped for manual stages, so requiring globs here
        # would be theatre.
        cfg = parse_config(
            minimal(
                stages=[
                    {
                        "id": "a",
                        "kind": "manual",
                        "human_steps": "x",
                        "checks": ["true"],
                    }
                ]
            )
        )
        assert cfg.stages[0].edit_files == []


class TestVerifiability:
    """Every stage must have some way to be verified."""

    def test_stage_with_no_test_command_and_no_checks_rejected(self):
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(
                    test_command=None,
                    stages=[{"id": "a", "instruction": "x", "edit_files": ["y"]}],
                )
            )
        assert "a" in str(e.value)

    def test_global_test_command_satisfies_stage(self):
        cfg = parse_config(minimal())
        assert cfg.stages[0].effective_test_command(cfg) == "pytest"

    def test_stage_test_command_overrides_global(self):
        cfg = parse_config(
            minimal(
                stages=[
                    {
                        "id": "a",
                        "instruction": "x",
                        "edit_files": ["y"],
                        "test_command": "pytest tests/unit",
                    }
                ]
            )
        )
        assert cfg.stages[0].effective_test_command(cfg) == "pytest tests/unit"

    def test_checks_alone_satisfy_a_greenfield_stage(self):
        # No suite exists yet; checks carry the verification.
        cfg = parse_config(
            minimal(
                test_command=None,
                stages=[
                    {
                        "id": "bootstrap",
                        "instruction": "create the package",
                        "edit_files": ["src/**"],
                        "checks": ["python -c 'import thing'"],
                        "require_new_tests": True,
                    }
                ],
            )
        )
        assert cfg.stages[0].effective_test_command(cfg) is None
        assert cfg.stages[0].require_new_tests is True


class TestForbiddenPatterns:
    def test_invalid_regex_rejected(self):
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(
                    stages=[
                        {
                            "id": "a",
                            "instruction": "x",
                            "edit_files": ["y"],
                            "forbidden_patterns": ["unclosed(["],
                        }
                    ]
                )
            )
        assert "forbidden_patterns" in str(e.value)

    def test_valid_regex_accepted(self):
        cfg = parse_config(
            minimal(
                stages=[
                    {
                        "id": "a",
                        "instruction": "x",
                        "edit_files": ["y"],
                        "forbidden_patterns": [r"ActiveRecord::Migration\["],
                    }
                ]
            )
        )
        assert cfg.stages[0].forbidden_patterns == [r"ActiveRecord::Migration\["]


class TestStageIdentity:
    def test_duplicate_stage_ids_rejected(self):
        # Run directories are named per stage; collisions would overwrite logs.
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(
                    stages=[
                        {"id": "dup", "instruction": "x", "edit_files": ["y"]},
                        {"id": "dup", "instruction": "x", "edit_files": ["y"]},
                    ]
                )
            )
        assert "dup" in str(e.value)

    def test_empty_stage_list_rejected(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(stages=[]))
        assert "stages" in str(e.value)

    def test_stage_id_must_be_path_safe(self):
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(
                    stages=[
                        {"id": "bad/id", "instruction": "x", "edit_files": ["y"]}
                    ]
                )
            )
        assert "bad/id" in str(e.value)


class TestBranchSafety:
    def test_branch_must_differ_from_base_ref(self):
        # Committing to base_ref is exactly what safety requirements forbid.
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(branch="main", base_ref="main"))
        assert "base_ref" in str(e.value)

    def test_branch_required(self):
        cfg = minimal()
        del cfg["branch"]
        with pytest.raises(ConfigError) as e:
            parse_config(cfg)
        assert "branch" in str(e.value)


class TestReferenceDocs:
    def test_missing_reference_doc_rejected(self, tmp_path):
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(
                    target_repo=str(tmp_path),
                    reference_docs=["docs/nope.md"],
                )
            )
        assert "nope.md" in str(e.value)

    def test_reference_doc_resolved_against_target_repo(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "plan.md").write_text("the plan")
        cfg = parse_config(
            minimal(target_repo=str(tmp_path), reference_docs=["docs/plan.md"])
        )
        assert cfg.reference_doc_paths()[0].read_text() == "the plan"

    def test_absolute_reference_doc_path_allowed(self, tmp_path):
        doc = tmp_path / "elsewhere.md"
        doc.write_text("shared plan")
        cfg = parse_config(minimal(reference_docs=[str(doc)]))
        assert cfg.reference_doc_paths()[0] == doc


class TestLoadFromFile:
    def test_loads_yaml(self, tmp_path):
        path = tmp_path / "run.yaml"
        path.write_text(
            textwrap.dedent(
                """
                target_repo: /tmp/some-app
                branch: refactor/thing
                test_command: pytest
                executor:
                  model: openai/local
                reviewer:
                  model: gpt-5.5
                stages:
                  - id: only
                    instruction: do it
                    edit_files:
                      - "src/**"
                """
            )
        )
        cfg = load_config(path)
        assert cfg.stages[0].id == "only"

    def test_missing_file_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(tmp_path / "absent.yaml")

    def test_malformed_yaml_is_a_config_error(self, tmp_path):
        path = tmp_path / "run.yaml"
        path.write_text("stages: [unclosed")
        with pytest.raises(ConfigError):
            load_config(path)

    def test_non_mapping_yaml_is_a_config_error(self, tmp_path):
        path = tmp_path / "run.yaml"
        path.write_text("- just\n- a\n- list\n")
        with pytest.raises(ConfigError):
            load_config(path)
