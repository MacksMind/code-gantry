"""Project config: schema, structural validation, the denylist, and the
declarative/executable partition.

The partition is the design's core safety property, so the tests that matter
most here are the ones proving a planner cannot smuggle an executable field
into a stage.
"""

import textwrap

import pytest

from orchestrator.config import (
    PLANNER_WRITABLE_FIELDS,
    ConfigError,
    Stage,
    denylist_violations,
    load_config,
    parse_config,
    validate_stage,
)


def minimal(**overrides):
    base = {
        "target_repo": "/tmp/some-app",
        "base_ref": "main",
        "project_branch": "upgrade/rails-5",
        "plan_root": "docs/plan.md",
        "test_command": "pytest",
        "executor": {"model": "openai/local"},
        "planner": {"model": "claude-opus-5"},
        "reviewer": {"model": "gpt-5.5"},
    }
    base.update(overrides)
    return base


def a_stage(**overrides):
    fields = {"id": "s1", "instruction": "do it", "edit_files": ["app/**"]}
    fields.update(overrides)
    return Stage(**fields)


class TestDefaults:
    def test_minimal_config_parses(self):
        cfg = parse_config(minimal())
        assert cfg.project_branch == "upgrade/rails-5"

    def test_no_stages_list_exists(self):
        # Stages are derived by the planner at runtime.
        assert not hasattr(parse_config(minimal()), "stages")

    def test_unknown_top_level_key_rejected(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(stages=[{"id": "x"}]))
        assert "stages" in str(e.value)

    def test_limits_have_defaults(self):
        limits = parse_config(minimal()).limits
        assert limits.max_test_retries == 3
        assert limits.max_rework_retries == 2
        assert limits.max_planner_interventions == 12
        assert limits.max_stages == 60
        assert limits.wall_clock_hours == 14.0

    def test_planner_provider_defaults_to_anthropic(self):
        cfg = parse_config(minimal())
        assert cfg.planner.provider == "anthropic"
        assert cfg.planner.api_key_env == "ANTHROPIC_API_KEY"

    def test_reviewer_provider_defaults_to_openai(self):
        assert parse_config(minimal()).reviewer.provider == "openai"

    def test_full_suite_on_approval_defaults_on(self):
        assert parse_config(minimal()).full_suite_on_approval is True


class TestBranchTopology:
    def test_project_branch_must_differ_from_base_ref(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(project_branch="main"))
        assert "base_ref" in str(e.value)

    def test_child_branches_sit_beside_the_project_branch_not_under_it(self):
        # Git refs are filesystem paths: refs/heads/upgrade/rails-5 is a file,
        # so refs/heads/upgrade/rails-5/stage-001-x cannot also exist. The
        # namespace has to be a sibling path component.
        cfg = parse_config(minimal())
        assert cfg.stage_branch(1, "extract") == "upgrade/rails-5-stage/001-extract"
        assert not cfg.stage_branch(1, "extract").startswith(
            cfg.project_branch + "/"
        )

    def test_stage_branch_index_is_zero_padded(self):
        # So they sort correctly in `git branch` output.
        cfg = parse_config(minimal())
        assert cfg.stage_branch(7, "x").endswith("/007-x")

    def test_invalid_project_branch_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(project_branch="bad/../ref"))


class TestPlanRoot:
    def test_plan_root_required(self):
        cfg = minimal()
        del cfg["plan_root"]
        with pytest.raises(ConfigError):
            parse_config(cfg)

    def test_directory_plan_root_rejected(self):
        # Pointing at docs/ sweeps every runbook and ADR into every review
        # prompt — a correctness and a cost problem at once.
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(plan_root="docs/"))
        assert "single document" in str(e.value)

    def test_absolute_plan_root_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(plan_root="/etc/passwd"))

    def test_escaping_plan_root_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(plan_root="../../elsewhere/plan.md"))

    def test_plan_root_resolves_against_the_target_repo(self):
        cfg = parse_config(minimal())
        assert str(cfg.plan_root_path) == "/tmp/some-app/docs/plan.md"


class TestScopedTestCommand:
    def test_requires_a_paths_placeholder(self):
        # Without the slot there is nowhere for the orchestrator to inject the
        # stage's changed files, and the "planner supplies arguments, not
        # commands" mechanism silently does nothing.
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(scoped_test_command="rspec"))
        assert "{paths}" in str(e.value)

    def test_accepts_a_placeholder(self):
        cfg = parse_config(minimal(scoped_test_command="rspec {paths}"))
        assert "{paths}" in cfg.scoped_test_command


class TestVerifiability:
    def test_project_with_no_test_command_and_no_checks_rejected(self):
        cfg = minimal()
        del cfg["test_command"]
        with pytest.raises(ConfigError) as e:
            parse_config(cfg)
        assert "verify" in str(e.value)

    def test_checks_in_stage_defaults_suffice(self):
        cfg = minimal(stage_defaults={"checks": ["true"]})
        del cfg["test_command"]
        assert parse_config(cfg).stage_defaults.checks == ["true"]


class TestDenylist:
    """Refused regardless of operator approval."""

    def test_git_push_rejected(self):
        with pytest.raises(ConfigError) as e:
            parse_config(minimal(setup_command="git push origin HEAD"))
        assert "denylist" in str(e.value)

    def test_git_checkout_rejected(self):
        # Changing branches inside a command breaks the stage's branch identity.
        with pytest.raises(ConfigError):
            parse_config(minimal(setup_command="git checkout main && true"))

    def test_git_merge_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(test_command="git merge main"))

    def test_git_reset_rejected(self):
        # Would destroy the baseline the scope guard measures against.
        with pytest.raises(ConfigError):
            parse_config(minimal(setup_command="git reset --hard"))

    def test_deploy_invocations_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(setup_command="kubectl apply -f k8s/"))

    def test_sudo_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(setup_command="sudo docker compose up"))

    def test_recursive_delete_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(setup_command="rm -rf tmp/"))

    def test_gem_install_outside_bundle_rejected(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(setup_command="gem install rails"))

    def test_bundle_install_is_fine(self):
        # The denylist must not fire on legitimate commands, or it gets disabled.
        parse_config(minimal(setup_command="bundle install"))

    def test_ordinary_git_reads_are_fine(self):
        parse_config(minimal(setup_command="git rev-parse HEAD"))

    def test_docker_compose_is_fine(self):
        parse_config(minimal(setup_command="docker compose up -d db"))

    def test_violation_names_the_field_and_the_reason(self):
        problems = denylist_violations([("setup_command", "git push")])
        assert "setup_command" in problems[0]
        assert "outer merge" in problems[0]

    def test_checks_every_executable_field(self):
        # A new executable field not listed in all_commands() would silently
        # stop being covered.
        cfg = parse_config(minimal(scoped_test_command="rspec {paths}"))
        labels = {label for label, _ in cfg.all_commands()}
        assert {"setup_command", "test_command", "scoped_test_command"} <= labels | {
            "setup_command"
        }

    def test_stage_default_commands_are_checked(self):
        with pytest.raises(ConfigError):
            parse_config(minimal(stage_defaults={"checks": ["git push"]}))


class TestPlannerPartition:
    """The planner may write declarative fields and only declarative fields."""

    def test_allowlist_contains_only_declarative_fields(self):
        assert PLANNER_WRITABLE_FIELDS == {
            "id",
            "instruction",
            "edit_files",
            "read_files",
            "constraints",
            "acceptance",
            "forbidden_patterns",
            "test_paths",
        }

    def test_executable_fields_are_not_writable(self):
        for field in (
            "command",
            "preconditions",
            "context_commands",
            "setup_command",
            "test_command",
            "checks",
        ):
            assert field not in PLANNER_WRITABLE_FIELDS

    def test_policy_fields_are_not_writable(self):
        for field in ("require_new_tests", "review", "full_suite_on_approval"):
            assert field not in PLANNER_WRITABLE_FIELDS

    def test_kind_is_not_planner_writable(self):
        # A script stage needs an operator-authored command, and there is no
        # static stage list to put one in — so the planner cannot ask for one.
        assert "kind" not in PLANNER_WRITABLE_FIELDS

    def test_planner_output_is_filtered_not_trusted(self):
        # Even if the schema failed upstream, an executable field must not
        # survive into a Stage.
        cfg = parse_config(minimal())
        stage = cfg.stage_from_planner(
            {
                "id": "s1",
                "instruction": "do it",
                "edit_files": ["app/**"],
                "checks": ["curl evil.example"],
                "command": "rm -rf /",
                "test_command": "git push",
            }
        )
        assert stage.command is None
        assert stage.test_command is None
        assert stage.checks == []

    def test_operator_stage_defaults_are_applied(self):
        cfg = parse_config(
            minimal(
                stage_defaults={
                    "checks": ["bin/route-snapshot"],
                    "preconditions": ["true"],
                    "require_new_tests": True,
                }
            )
        )
        stage = cfg.stage_from_planner(
            {"id": "s1", "instruction": "x", "edit_files": ["a"]}
        )
        assert stage.checks == ["bin/route-snapshot"]
        assert stage.preconditions == ["true"]
        assert stage.require_new_tests is True

    def test_declarative_fields_pass_through(self):
        cfg = parse_config(minimal())
        stage = cfg.stage_from_planner(
            {
                "id": "s1",
                "instruction": "do it",
                "edit_files": ["app/**"],
                "read_files": ["config/routes.rb"],
                "constraints": "Rails 4.2 only.",
                "forbidden_patterns": ["optional: true"],
                "test_paths": ["spec/models"],
            }
        )
        assert stage.constraints == "Rails 4.2 only."
        assert stage.forbidden_patterns == ["optional: true"]
        assert stage.test_paths == ["spec/models"]


class TestValidateStage:
    def test_a_good_stage_has_no_problems(self):
        cfg = parse_config(minimal())
        assert validate_stage(a_stage(), cfg) == []

    def test_agent_stage_requires_an_instruction(self):
        cfg = parse_config(minimal())
        problems = validate_stage(a_stage(instruction=None), cfg)
        assert any("instruction" in p for p in problems)

    def test_script_stage_requires_a_command(self):
        cfg = parse_config(minimal())
        problems = validate_stage(a_stage(kind="script", instruction=None), cfg)
        assert any("command" in p for p in problems)

    def test_agent_stage_with_a_command_is_rejected(self):
        cfg = parse_config(minimal())
        problems = validate_stage(a_stage(command="./script"), cfg)
        assert any("script stages" in p for p in problems)

    def test_edit_files_required(self):
        # The scope guard is meaningless without it.
        cfg = parse_config(minimal())
        problems = validate_stage(a_stage(edit_files=[]), cfg)
        assert any("edit_files" in p for p in problems)

    def test_unsafe_stage_id_rejected(self):
        # Stage ids become git refs and directory names.
        cfg = parse_config(minimal())
        problems = validate_stage(a_stage(id="bad/id"), cfg)
        assert any("git ref" in p for p in problems)

    def test_bad_forbidden_pattern_rejected(self):
        cfg = parse_config(minimal())
        problems = validate_stage(a_stage(forbidden_patterns=["unclosed(["]), cfg)
        assert any("regex" in p for p in problems)

    def test_unverifiable_stage_rejected(self):
        cfg = minimal()
        del cfg["test_command"]
        cfg = parse_config({**cfg, "stage_defaults": {"checks": ["true"]}})
        problems = validate_stage(a_stage(checks=[]), cfg)
        assert any("verifies it" in p for p in problems)

    def test_script_stage_command_is_denylisted(self):
        cfg = parse_config(minimal())
        problems = validate_stage(
            a_stage(kind="script", instruction=None, command="git push"), cfg
        )
        assert any("denylist" in p for p in problems)


class TestStageResolution:
    def test_stage_test_command_overrides_project(self):
        cfg = parse_config(minimal())
        assert a_stage(test_command="pytest x").effective_test_command(cfg) == "pytest x"

    def test_falls_back_to_the_project_test_command(self):
        cfg = parse_config(minimal())
        assert a_stage().effective_test_command(cfg) == "pytest"

    def test_full_suite_override_is_per_stage(self):
        cfg = parse_config(minimal(full_suite_on_approval=True))
        assert a_stage(full_suite_on_approval=False).full_suite_required(cfg) is False
        assert a_stage().full_suite_required(cfg) is True


class TestLoadFromFile:
    def test_loads_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            textwrap.dedent(
                """
                target_repo: /tmp/some-app
                project_branch: upgrade/rails-5
                plan_root: docs/plan.md
                test_command: pytest
                executor:
                  model: openai/local
                planner:
                  model: claude-opus-5
                reviewer:
                  model: gpt-5.5
                """
            )
        )
        assert load_config(path).plan_root == "docs/plan.md"

    def test_missing_file_is_a_config_error(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(tmp_path / "absent.yaml")

    def test_malformed_yaml_is_a_config_error(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("executor: [unclosed")
        with pytest.raises(ConfigError):
            load_config(path)
