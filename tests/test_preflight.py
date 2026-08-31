"""Preflight checks that need something real to talk to.

The rest of preflight is covered through the integration suite and the smoke
test, which drive it as `validate` and `run` actually do. What needs isolating
here is endpoint verification, because the interesting cases — a mistyped model
id, an endpoint that is simply down — are awkward to stage end to end.

The stand-in serves llama-swap's real `/v1/models` shape, including the alias
field, since a configured alias is a name that legitimately will not appear as
an `id`.
"""

import json
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from test_config import runner_script, as_test_tools

from code_gantry.config import parse_config
from code_gantry.flake import FLAKES_FILENAME, recent_flakes
from code_gantry.preflight import (
    PREFLIGHT_SUITE_LOG,
    check_executor_endpoint,
    run_preflight,
)

MODELS = {
    "object": "list",
    "data": [
        {"id": "qwen3-coder-next", "object": "model", "owned_by": "llama-swap"},
        {
            "id": "qwen3-embedding",
            "object": "model",
            "owned_by": "llama-swap",
            "meta": {"llamaswap": {"aliases": ["qwen3-embedding:latest"]}},
        },
    ],
}


class _Handler(BaseHTTPRequestHandler):
    payload = MODELS
    status = 200

    def do_GET(self):  # noqa: N802
        raw = json.dumps(self.payload).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture
def dead_port():
    """A port with nothing on it, obtained by binding and releasing."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def cfg_for(model, api_base):
    return parse_config(
        as_test_tools({
            "target_repo": "/tmp/app",
            "base_ref": "main",
            "project_branch": "proj",
            "plan_root": "docs/plan.md",
            "full_test_command": "true",
            "executor": {"model": model, "api_base": api_base},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.5"},
        })
    )


class TestExecutorEndpoint:
    def test_a_listed_model_passes(self, endpoint):
        checks = check_executor_endpoint(cfg_for("openai/qwen3-coder-next", endpoint))
        assert all(c.ok for c in checks), [c.detail for c in checks if not c.ok]

    def test_the_provider_prefix_is_not_sent_to_the_server(self, endpoint):
        # `openai/` is a litellm routing prefix. Comparing the whole string
        # against the server's ids would reject every correct config.
        checks = check_executor_endpoint(cfg_for("openai/qwen3-coder-next", endpoint))
        assert not any("openai/qwen3-coder-next" in (c.detail or "") for c in checks if not c.ok)

    def test_an_alias_counts_as_present(self, endpoint):
        # llama-swap lets a model answer to names that are not its id.
        checks = check_executor_endpoint(
            cfg_for("openai/qwen3-embedding:latest", endpoint)
        )
        assert all(c.ok for c in checks)

    def test_a_mistyped_model_is_blocking(self, endpoint):
        checks = check_executor_endpoint(cfg_for("openai/qwen3-codr-next", endpoint))
        blocking = [c for c in checks if c.blocking]
        assert blocking, "a typo that fails every stage must fail validation"
        # The available names belong in the message; that is what makes it fixable.
        assert "qwen3-coder-next" in blocking[0].detail

    def test_an_unreachable_endpoint_is_blocking(self, dead_port):
        checks = check_executor_endpoint(
            cfg_for("openai/whatever", f"http://127.0.0.1:{dead_port}/v1")
        )
        assert [c for c in checks if c.blocking]

    def test_an_unparseable_listing_warns_rather_than_blocks(self, endpoint, monkeypatch):
        # Not every OpenAI-compatible server implements /v1/models the same way.
        # Refusing to run over that would be worse than not checking.
        monkeypatch.setattr(_Handler, "payload", {"unexpected": "shape"})
        checks = check_executor_endpoint(cfg_for("openai/anything", endpoint))
        assert not any(c.blocking for c in checks)
        assert any(not c.ok for c in checks)

    def test_no_api_base_means_nothing_to_check(self):
        cfg = cfg_for("gpt-4o", None)
        assert check_executor_endpoint(cfg) == []

    def test_an_unresolvable_api_base_env_does_not_raise(self, monkeypatch):
        # The env-var check reports that separately; this must not blow up.
        monkeypatch.delenv("SOME_UNSET_BASE", raising=False)
        cfg = parse_config(
            as_test_tools({
                "target_repo": "/tmp/app",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "docs/plan.md",
                "full_test_command": "true",
                "executor": {"model": "openai/m", "api_base_env": "SOME_UNSET_BASE"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.5"},
            })
        )
        assert check_executor_endpoint(cfg) == []


class TestEndpointEnvironmentChecks:
    def test_an_unexported_address_is_blocking(self, repo, monkeypatch):
        # A real repo: the repo checks short-circuit everything below them, so a
        # bare tmp_path would prove nothing about the endpoint check.
        monkeypatch.delenv("SPARK_BASE", raising=False)
        cfg = parse_config(
            as_test_tools({
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "full_test_command": "true",
                "executor": {"model": "openai/m", "api_base_env": "SPARK_BASE"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.5"},
            })
        )
        checks = run_preflight(
            cfg,
            run_tests=False,
            check_models=False,
            check_approval=False,
            check_endpoint=False,
        )
        named = [c for c in checks if "SPARK_BASE" in c.name]
        assert named and named[0].blocking


class TestEndpointRedaction:
    """An address from the environment stays out of the output.

    The whole reason `api_base_env` exists is that a hostname is an
    infrastructure fact that should not be committed. Printing the resolved
    value to the terminal — and from there into logs, transcripts, and
    screenshots — gives most of that back. The variable name is what an
    operator needs to fix a problem; the value is not.

    A literal `api_base` is different: the operator wrote it into the config
    themselves, so echoing it reveals nothing they did not already choose.
    """

    def test_a_resolved_address_is_not_printed(self, endpoint, monkeypatch):
        monkeypatch.setenv("SECRET_BASE", endpoint)
        cfg = parse_config(
            as_test_tools({
                "target_repo": "/tmp/app",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "docs/plan.md",
                "full_test_command": "true",
                "executor": {
                    "model": "openai/qwen3-coder-next",
                    "api_base_env": "SECRET_BASE",
                },
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )
        checks = check_executor_endpoint(cfg)
        rendered = "\n".join(f"{c.name} {c.detail}" for c in checks)
        assert endpoint not in rendered
        assert "SECRET_BASE" in rendered

    def test_an_unreachable_address_is_not_printed_either(self, dead_port, monkeypatch):
        # The failure path is where a URL is most tempting to include.
        address = f"http://127.0.0.1:{dead_port}/v1"
        monkeypatch.setenv("SECRET_BASE", address)
        cfg = parse_config(
            as_test_tools({
                "target_repo": "/tmp/app",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "docs/plan.md",
                "full_test_command": "true",
                "executor": {"model": "openai/m", "api_base_env": "SECRET_BASE"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )
        checks = check_executor_endpoint(cfg)
        rendered = "\n".join(f"{c.name} {c.detail}" for c in checks)
        assert f"127.0.0.1:{dead_port}" not in rendered
        assert "SECRET_BASE" in rendered

    def test_a_literal_api_base_is_still_shown(self, endpoint):
        # The operator wrote it in the config they approved.
        checks = check_executor_endpoint(cfg_for("openai/qwen3-coder-next", endpoint))
        assert any(endpoint in (c.detail or "") for c in checks)


class TestFailureOutputKeepsTheVerdict:
    """A failing command's own summary must survive truncation.

    Preflight kept the last 2000 characters, which is wrong for any tool that
    prints something after its result. The real target's `bin/rspec` tallies
    deprecation warnings at the end, so a failed full-suite run reported
    nothing but deprecation noise — the "N examples, M failures" line had been
    pushed out of the window entirely, and the operator could not tell whether
    one spec had failed or two hundred.
    """

    def test_the_head_of_the_output_is_kept(self, repo):
        _commit_a_plan(repo)
        cfg = parse_config(
            as_test_tools({
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                # Prints its verdict, then 4000 characters of noise, then fails.
                "full_test_command": (
                    "echo '9 examples, 3 failures'; "
                    "for i in $(seq 1 200); do echo 'DEPRECATION WARNING: something'; done; "
                    "exit 1"
                ),
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )
        checks = run_preflight(
            cfg,
            check_models=False,
            check_approval=False,
            check_endpoint=False,
        )
        failed = [c for c in checks if not c.ok and "full_test_command" in c.name]
        assert failed, "the failing command should have produced a check"
        assert "9 examples, 3 failures" in failed[0].detail


class TestPreflightExcusesAFlakeTheRunWouldExcuse:
    """Preflight failed the run on a file the pipeline forgives every time.

    Observed: `run` was launched, preflight ran the suite, and one feature spec
    failed under fourteen parallel workers sharing four browsers. That file
    passes standalone and had been excused as a flake twenty-one times — the
    most-excused file in the project. The pipeline's flake gate re-runs a
    failing file alone and excuses it; preflight ran the same suite with no such
    gate, so it failed hard on precisely the failure the run itself treats as
    noise, and the operator had to skip preflight's tests to get started.

    Both use `flake.adjudicate`, and preflight already holds everything it
    needs to call it.
    """

    def _cfg(self, repo, scoped_ok: bool):
        _commit_a_plan(repo)
        return parse_config(
            as_test_tools({
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "full_test_command": (
                    "echo 'rspec ./spec/features/a_spec.rb:40'; "
                    "echo '9 examples, 1 failure'; exit 1"
                ),
                "scoped_test_command": (
                    "true {paths}"
                    if scoped_ok
                    else runner_script(repo.parent, "exit 1", "red_runner") + " {paths}"
                ),
                "failed_file_pattern": r"^rspec \./(\S+?\.rb)",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )

    def _check(self, repo, scoped_ok: bool):
        checks = run_preflight(
            self._cfg(repo, scoped_ok),
            check_models=False,
            check_approval=False, check_endpoint=False,
        )
        return next(c for c in checks if "full_test_command passes" in c.name)

    def test_a_file_that_passes_alone_does_not_block_the_run(self, repo):
        check = self._check(repo, scoped_ok=True)
        assert check.ok
        assert not check.blocking
        assert "spec/features/a_spec.rb" in check.detail

    def test_the_excusal_is_recorded_where_the_count_lives(self, repo, tmp_path):
        # The ledger earns its keep by being countable — the spec that caused
        # this was identifiable as noise because it had twenty-one entries. An
        # excusal preflight makes and does not write down undercounts the next
        # one.
        project_dir = tmp_path / "proj"
        checks = run_preflight(
            self._cfg(repo, scoped_ok=True),
            project_dir=project_dir,
            check_models=False,
            check_approval=False, check_endpoint=False,
        )
        assert any(c.ok for c in checks if "full_test_command passes" in c.name)
        entry = recent_flakes(project_dir / FLAKES_FILENAME)[0]
        assert entry["file"] == "spec/features/a_spec.rb"
        # Its own field, rather than a sentinel standing in for a stage that
        # does not exist — and no run id, because there is not one yet.
        assert entry["origin"] == "preflight"
        assert entry["stage_id"] is None
        assert entry["run_id"] is None

    def test_the_suite_output_is_kept_when_the_run_is_let_through(
        self, repo, tmp_path
    ):
        """The one check with no artifact behind it was the one that forgives.

        Observed live: preflight excused a red suite naming `(unnamed)` — the
        extraction found no locator at all — and the output that would have
        said why was parsed and dropped. `last-run.out` held 63 lines after the
        run header and not one `Failed examples`, so the two live explanations
        (the failure produced no locators, or the pattern stopped matching) were
        indistinguishable an hour later. A gate that can wave a red suite
        through has to leave the bytes it decided on.
        """
        project_dir = tmp_path / "proj"
        run_preflight(
            self._cfg(repo, scoped_ok=True),
            project_dir=project_dir,
            check_models=False,
            check_approval=False, check_endpoint=False,
        )
        kept = (project_dir / PREFLIGHT_SUITE_LOG).read_text()
        assert "rspec ./spec/features/a_spec.rb:40" in kept, "the runner's own output"
        assert "9 examples, 1 failure" in kept

    def test_a_green_preflight_writes_nothing(self, repo, tmp_path):
        # Bounded by only writing what needs explaining. A full suite is
        # thousands of lines and this file sits beside a 14MB `last-run.out`.
        project_dir = tmp_path / "proj"
        cfg = self._cfg(repo, scoped_ok=True)
        cfg = parse_config(as_test_tools({**cfg.model_dump(mode="json"), "full_test_command": "true",
                            "full_test_command": "true"}))
        run_preflight(
            cfg, project_dir=project_dir, check_models=False,
            check_approval=False, check_endpoint=False,
        )
        assert not (project_dir / PREFLIGHT_SUITE_LOG).exists()

    def test_a_file_that_fails_alone_still_blocks(self, repo):
        # The whole point of adjudicating rather than ignoring: a real red
        # suite must still stop the run before it spends a planner call.
        check = self._check(repo, scoped_ok=False)
        assert not check.ok
        assert check.blocking


def _commit_a_plan(repo):
    """The shared `repo` fixture has no plan root, and preflight blocks on that.

    Any test meaning to reach `_environment_checks` has to supply one. Before
    the suites moved behind the blocking check they were reached regardless, so
    a dozen tests here were exercising a preflight that had already failed — a
    path no caller takes, since all three exit on a blocking check. That is the
    shape `CLAUDE.md` records about test helpers standing in for nodes: the
    stand-in was laxer than the thing.

    Idempotent, and takes no fixture, so a `_cfg` builder can call it and a test
    may call that builder more than once.
    """
    if (repo / "PLAN.md").exists():
        return
    (repo / "PLAN.md").write_text("# Plan\n")
    subprocess.run(["git", "add", "PLAN.md"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-qm", "plan"], cwd=repo, check=True,
                   capture_output=True)


class TestTheSuitesGoLast:
    """A cheap check must not be answered after an expensive one.

    Measured 2026-08-18: a run was launched without credentials in the shell,
    and preflight ran `setup_command`, `full_test_command` and `full_test_command` —
    5m30s of green RSpec — before reaching `_model_checks`, whose first act is
    `env_var not in os.environ`. The answer was available before the function
    did anything.

    `CLAUDE.md` already carries the rule, written about a startup question asked
    from inside `verify`. It did not catch this because a rule is checked
    against new work and nothing re-reads the code that predates it. So this
    asserts the behaviour rather than the order: whatever the sequence, a
    blocking failure must not cost a suite.
    """

    def _cfg(self, repo, marker):
        return parse_config(
            as_test_tools({
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "full_test_command": f"echo ran >> {marker}",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )

    def test_a_missing_credential_costs_no_suite(self, repo, monkeypatch):
        # Both cleared deliberately. A test that asks whether a variable is set
        # and does not clear it is asking about the developer's shell, which is
        # how a preflight test once failed for everyone who had configured the
        # tool.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _commit_a_plan(repo)
        marker = repo / "runs.txt"

        checks = run_preflight(
            self._cfg(repo, marker),
            check_models=True,
            check_approval=False,
            check_endpoint=False,
        )

        # The credential has to be the *only* blocker, or this passes on
        # whatever else was already failing and says nothing about ordering.
        blocking = [c for c in checks if c.blocking]
        assert [c.name for c in blocking] == [
            "ANTHROPIC_API_KEY is set",
            "OPENAI_API_KEY is set",
        ]
        assert not marker.exists(), "no suite may run once something blocks"

    def test_the_skip_is_reported_rather_than_silent(self, repo, monkeypatch):
        # A check that renders as nothing reads the same as one that passed.
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        _commit_a_plan(repo)
        marker = repo / "runs.txt"

        checks = run_preflight(
            self._cfg(repo, marker),
            check_models=True,
            check_approval=False,
            check_endpoint=False,
        )

        skipped = [c for c in checks if c.name == "setup and the test suites"]
        assert len(skipped) == 1
        assert not skipped[0].ok, "not a pass"
        assert not skipped[0].blocking, "and not a second failure to chase"
        assert "not run" in skipped[0].detail

    def test_a_clean_preflight_still_runs_them(self, repo, monkeypatch):
        # The other half: the skip must be reachable only by blocking, or this
        # would have quietly retired preflight's most valuable check.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        _commit_a_plan(repo)
        marker = repo / "runs.txt"

        run_preflight(
            self._cfg(repo, marker),
            check_models=False,
            check_approval=False,
            check_endpoint=False,
        )

        assert marker.exists(), "a green preflight runs the suites"


class TestTheSuiteRunsOnce:
    """One everything-command, so one run.

    `validate` used to iterate two labels — `test_command` and
    `full_test_command` — and deduplicate by command text, because a project
    pointing both at the same script is the sensible default and running a
    full suite twice is 23 minutes to learn one thing. The dedup went out with
    the second name. What is still worth pinning is the property it protected:
    preflight runs the suite exactly once, and reports what that run said.
    """

    def test_the_suite_runs_once(self, repo):
        marker = repo / "runs.txt"
        command = f"echo x >> {marker}"
        cfg = parse_config(
            as_test_tools({
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "full_test_command": command,
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )
        _commit_a_plan(repo)
        run_preflight(
            cfg, check_models=False,
            check_approval=False, check_endpoint=False,
        )
        assert marker.read_text().count("x") == 1

    def test_a_red_suite_is_reported_red_and_carries_the_output(self, repo):
        """A verdict must come from the run it claims to describe.

        This began as a dedup bug: the first real `validate` printed
        `[FAIL] test_command` and, two lines later,
        `[ok] full_test_command passes on a clean tree`, for one red suite run
        once — evidence of green manufactured from a run that failed. There is
        no twin to inherit a verdict now, so what is left to hold is the
        simpler half: preflight is the only gate that can wave a red
        repository through, and its verdict carries the bytes it decided on.
        """
        command = "echo '9 examples, 3 failures'; exit 1"
        cfg = parse_config(
            as_test_tools({
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "full_test_command": command,
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )
        _commit_a_plan(repo)
        checks = run_preflight(
            cfg, check_models=False,
            check_approval=False, check_endpoint=False,
        )
        reported = [c for c in checks if "full_test_command" in c.name]
        assert reported, "the suite's verdict should be reported"
        assert not reported[0].ok, "a red suite cannot be reported as a pass"
        assert "9 examples, 3 failures" in reported[0].detail


class TestCredentialsAreActuallyTested:
    """Building a client proves a variable is set. It proves nothing else.

    A key with a short expiry died mid-session. Preflight had said "planner
    client builds" and passed; every planner call then returned 401 as a
    generic blocked verdict, and the failure was mistaken for the planner
    declining to use its tools. The local executor endpoint had always been
    called for real; the two paid credentialed services were taken on trust.
    """

    def _client(self, exc=None):
        class Inner:
            class messages:
                @staticmethod
                def create(**kw):
                    if exc:
                        raise exc

        return type("C", (), {"_client": Inner, "cfg": type("X", (), {"model": "m"})})()

    def test_a_live_key_passes(self):
        from code_gantry.preflight import _credential_check

        assert _credential_check("planner", self._client()).ok

    def test_an_expired_key_fails_fatally(self):
        from code_gantry.preflight import _credential_check

        exc = type("E", (Exception,), {"status_code": 401})("API key is invalid.")
        check = _credential_check("planner", self._client(exc))
        assert not check.ok
        assert check.fatal, "a dead key must stop the run before stage one"
        assert "credentials problem" in check.detail

    def test_any_other_answer_counts_as_authenticated(self):
        # What is being tested is authentication, not a useful completion. A
        # 400 about a token budget means the request was accepted, parsed and
        # answered — which is everything this needs to know. Chasing a clean
        # 200 across providers means tracking each one's parameter spellings,
        # and a check that breaks when a vendor renames a field gets skipped.
        from code_gantry.preflight import _credential_check

        exc = type("E", (Exception,), {"status_code": 400})(
            "Could not finish the message because max_completion_tokens"
        )
        check = _credential_check("reviewer", self._client(exc))
        assert check.ok
        assert "the key is live" in check.detail

    def test_a_rate_limit_does_not_block_a_run(self):
        from code_gantry.preflight import _credential_check

        exc = type("E", (Exception,), {"status_code": 429})("slow down")
        assert _credential_check("planner", self._client(exc)).ok


class TestFilesTooLargeToEverBeReference:
    """`read_files` is capped in total, so a file over the cap is unreachable.

    Not a tail that got trimmed to fit — a structural impossibility, in every
    stage, for any planner, in any combination. Observed: a 1,443-line model
    declared against a 1,200-line budget, withheld identically on six
    consecutive stages and logged each time as though it were a sizing
    decision. Nothing could learn from it because nothing was variable.

    A warning, not a failure. The budget exists because unbounded context
    degraded the executor, so an operator may well accept that large files are
    unreachable — the point is that they decide it once, knowingly.
    """

    def _repo(self, tmp_path, run_git, files):
        repo = tmp_path / "target"
        repo.mkdir()
        run_git(repo, "init", "-q", "-b", "main")
        run_git(repo, "config", "user.email", "t@example.com")
        run_git(repo, "config", "user.name", "T")
        run_git(repo, "config", "commit.gpgsign", "false")
        for name, lines in files.items():
            (repo / name).write_text("x\n" * lines)
        (repo / "PLAN.md").write_text("# Plan")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "init")
        return repo

    def _check(self, repo, cap):
        from code_gantry.gitops import Git
        from code_gantry.preflight import _read_budget_check

        cfg = parse_config(as_test_tools({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "proj",
            "plan_root": "PLAN.md", "full_test_command": "true",
            "executor": {"model": "m", "max_read_lines": cap},
            "planner": {"model": "claude-opus-5"}, "reviewer": {"model": "gpt-5.5"},
        }))
        return _read_budget_check(cfg, Git(repo))

    def test_it_warns_without_blocking(self, tmp_path, run_git):
        repo = self._repo(tmp_path, run_git, {"big.rb": 300})
        check = self._check(repo, 100)
        assert not check.ok
        assert not check.blocking, "an operator may accept this; it must not refuse to start"

    def test_a_repo_that_fits_says_so(self, tmp_path, run_git):
        repo = self._repo(tmp_path, run_git, {"small.rb": 10})
        assert self._check(repo, 100).ok

    def test_no_ceiling_is_not_a_problem(self, tmp_path, run_git):
        repo = self._repo(tmp_path, run_git, {"big.rb": 5000})
        assert self._check(repo, None).ok

    def test_it_names_the_files_closest_to_the_line(self, tmp_path, run_git):
        # Not the largest. The biggest file over the cap is usually a fixture
        # nobody would cite; the ones just over it are the plausible references
        # and the ones a new ceiling would recover.
        repo = self._repo(
            tmp_path, run_git,
            {"just_over.rb": 110, "enormous_fixture.txt": 9000},
        )
        detail = self._check(repo, 100).detail
        assert "just_over.rb (110)" in detail
        assert detail.index("just_over.rb") < detail.index("enormous_fixture.txt")
        assert "past 110" in detail

    def test_binary_files_are_skipped(self, tmp_path, run_git):
        repo = self._repo(tmp_path, run_git, {"code.rb": 10})
        (repo / "blob.bin").write_bytes(b"\0" * 400_000)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-qm", "blob")
        assert self._check(repo, 100).ok, "a binary blob is not a candidate reference"


class TestRipgrepIsPresent:
    """The one external binary the pipeline itself requires.

    Everything else it runs — the suite, the linter, the setup command — the
    operator named in config, so a missing one fails as that command failing.
    `rg` is ours, called from inside a tool loop, where a `FileNotFoundError`
    would reach the model as an exception rather than a result.
    """

    def test_it_reports_where_rg_was_found(self):
        from code_gantry.preflight import _ripgrep_check

        check = _ripgrep_check()
        assert check.ok
        assert check.detail.endswith("rg")

    def test_it_fails_with_a_fix_when_rg_is_absent(self, monkeypatch):
        import code_gantry.preflight as pf

        monkeypatch.setattr(pf.shutil, "which", lambda _: None)
        check = pf._ripgrep_check()
        assert not check.ok
        assert "brew install ripgrep" in check.detail


class TestTheRunLogExistsBeforePreflight:
    """The file an operator tails must say something while the slow part runs.

    The startup banner goes to stdout, which on this project is redirected into
    a file appended across every run — so telling this run's lines from the
    last one's means counting. The run's *own* log held nothing until preflight
    returned, and on a real project that is three minutes of test suites. An
    empty file is what a hung run looks like too.
    """

    def test_the_log_carries_a_start_line_and_the_pid(self, tmp_path):
        # Driven through the helper rather than the CLI so the assertion is
        # about the contract — a line, before anything slow, naming the run.
        import os

        from code_gantry.runlog import RunLog

        path = tmp_path / "run.log"
        log = RunLog(path, echo=None)
        log.record(f"=== r1 — 2026-08-08T03:00:00+00:00 pid {os.getpid()} ===")
        log("[preflight] starting (running the suites, which take minutes)")
        log.close()

        text = path.read_text()
        assert str(os.getpid()) in text
        assert "[preflight] starting" in text
        assert "take minutes" in text

    def test_a_refused_start_leaves_no_resumable_run(self, tmp_path):
        # The reason creating the directory early is safe: `_locate_run` keys
        # on `run.json`, which is written only once preflight has passed.
        from code_gantry.runlog import RunLog

        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        RunLog(run_dir / "run.log", echo=None).close()
        assert not (run_dir / "run.json").exists()


def _with_executor_key(cfg, env_var: str = "OPENROUTER_API_KEY"):
    """The same config with the executor naming a key of its own."""
    return cfg.model_copy(
        update={"executor": cfg.executor.model_copy(update={"api_key_env": env_var})}
    )


class TestEveryRoleProvesItsKey:
    """The executor's credentials were never checked, and looked as if they were.

    The planner and the reviewer each make a real authenticated call at
    preflight. The executor got an unauthenticated `GET /models` — the endpoint
    answers and the model id is offered — which proves the endpoint exists and
    nothing about whether we may call it.

    It looked covered because of an accident: `resolve_policy` made a real
    completion on the executor's endpoint, and its line printed among the
    preflight output. But it fired only when the configured model was a
    *routing policy*; a concrete model returned early without probing, so those
    configs never had the check at all. Moving that probe to stage start took
    the accident with it, which is the right time to notice it was load-bearing.

    Finding out at stage one is expensive in a way preflight exists to prevent:
    the plan derives, a branch is cut, and the first attempt dies on auth.
    """

    def test_all_three_roles_are_checked(self, monkeypatch):
        from code_gantry import preflight

        monkeypatch.setattr(preflight, "_build_planner", lambda cfg: object())
        monkeypatch.setattr(preflight, "_build_reviewer", lambda cfg: object())
        monkeypatch.setattr(preflight, "_build_executor", lambda cfg: object())
        monkeypatch.setattr(preflight, "_ping", lambda client: None)
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
            monkeypatch.setenv(var, "k")

        cfg = _with_executor_key(cfg_for("openrouter/pareto-code", ""))
        names = [c.name for c in preflight._model_checks(cfg)]
        for role in ("planner", "reviewer", "executor"):
            assert f"{role} credentials work" in names, f"{role} proves nothing"

    def test_a_missing_executor_key_is_named_before_anything_runs(self, monkeypatch):
        from code_gantry import preflight

        monkeypatch.setattr(preflight, "_build_planner", lambda cfg: object())
        monkeypatch.setattr(preflight, "_build_reviewer", lambda cfg: object())
        monkeypatch.setattr(preflight, "_ping", lambda client: None)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        cfg = _with_executor_key(cfg_for("openrouter/pareto-code", ""))
        failed = [c for c in preflight._model_checks(cfg) if not c.ok]
        assert any("OPENROUTER_API_KEY" in c.name for c in failed)


class TestDeclaredChecksRunAtPreflight:
    """`stage_defaults.checks` are host commands, and nothing proved one ran.

    `_environment_checks` covered `setup_command` and the two test commands.
    On the project this was found on all three went through `docker compose`,
    so a `checks` entry running on the *host* was first invoked by stage 000:
    `bin/rubocop` could not materialise its bundle on a newly-provisioned
    machine and exited 1 in a fifth of a second, and because `_layer_checks`
    routes an ordinary non-zero back to the executor as "a required check
    failed", two planner revisions and three attempts were spent telling a
    model its work was wrong by an environment that was never there.

    The second entry never ran at all, for the whole life of the run, because
    the gate stops at the first failure. That is why these assert the whole
    list rather than the verdict.
    """

    def _cfg(self, repo, *, checks, suite_marker):
        return parse_config(
            as_test_tools({
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "full_test_command": f"echo suite >> {suite_marker}",
                "stage_defaults": {"checks": checks},
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            })
        )

    def _run(self, repo, *, checks, suite_marker, run_tests=True):
        _commit_a_plan(repo)
        return run_preflight(
            self._cfg(repo, checks=checks, suite_marker=suite_marker),
            check_models=False,
            check_approval=False,
            check_endpoint=False,
            run_tests=run_tests,
        )

    def test_a_declared_check_is_run(self, repo):
        # The gap itself. Before this, nothing here invoked the entry at all.
        ran = repo / "ran.txt"
        self._run(repo, checks=[f"echo a >> {ran}"], suite_marker=repo / "suite.txt")
        assert ran.exists(), "preflight must invoke the declared checks"

    def test_every_entry_runs_even_once_one_has_failed(self, repo):
        # The half that stayed invisible. `CommandRunner.run_all` stops at the
        # first failure, which is right for a stage and wrong here: an operator
        # repairing a machine wants every broken entry in one pass.
        ran = repo / "ran.txt"
        checks = self._run(
            repo,
            checks=["exit 1", f"echo second >> {ran}"],
            suite_marker=repo / "suite.txt",
        )
        assert ran.exists(), "a later entry must still be proven"
        names = [c.name for c in checks if c.name.startswith("check runs:")]
        assert len(names) == 2, names

    def test_a_failing_check_blocks_and_costs_no_suite(self, repo):
        suite = repo / "suite.txt"
        checks = self._run(repo, checks=["exit 1"], suite_marker=suite)

        failed = [c for c in checks if c.name.startswith("check runs:")]
        assert failed and failed[0].blocking
        assert not suite.exists(), "no suite may run once a check blocks"

        # Said out loud: a check that renders as nothing reads like a pass.
        skipped = [c for c in checks if c.name == "the test suites"]
        assert len(skipped) == 1
        assert not skipped[0].ok and not skipped[0].blocking
        assert "not run" in skipped[0].detail

    def test_skipping_the_suites_does_not_skip_the_checks(self, repo):
        # `--skip-preflight-tests` buys back minutes of suite. These are
        # seconds, and they answer a question about the host, which is not
        # what that flag offers to skip.
        ran = repo / "ran.txt"
        self._run(
            repo,
            checks=[f"echo a >> {ran}"],
            suite_marker=repo / "suite.txt",
            run_tests=False,
        )
        assert ran.exists()

    def test_an_autocorrecting_check_is_caught_and_attributed(self, repo):
        # Useful checks often fix as well as report. Rewriting a tree no stage
        # has touched would carry those bytes into the first stage's diff with
        # nothing to attribute them to.
        checks = self._run(
            repo, checks=["echo x >> app.py"], suite_marker=repo / "suite.txt"
        )
        dirty = [c for c in checks if c.name == "checks leave the tree clean"]
        assert len(dirty) == 1
        assert dirty[0].blocking
        assert "app.py" in dirty[0].detail

    def test_a_clean_check_leaves_no_such_complaint(self, repo):
        # The other direction: the tidiness check must not fire on a check
        # that merely wrote outside the repo.
        checks = self._run(
            repo,
            checks=[f"echo a >> {repo.parent / 'outside.txt'}"],
            suite_marker=repo / "suite.txt",
        )
        assert not [c for c in checks if c.name == "checks leave the tree clean"]

    def test_no_declared_checks_says_nothing(self, repo):
        checks = self._run(repo, checks=[], suite_marker=repo / "suite.txt")
        assert not [c for c in checks if c.name.startswith("check runs:")]
