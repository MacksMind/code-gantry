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
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from orchestrator.config import parse_config
from orchestrator.flake import FLAKES_FILENAME, recent_flakes
from orchestrator.preflight import (
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
        {
            "target_repo": "/tmp/app",
            "base_ref": "main",
            "project_branch": "proj",
            "plan_root": "docs/plan.md",
            "test_command": "true",
            "executor": {"model": model, "api_base": api_base},
            "planner": {"model": "claude-opus-5"},
            "reviewer": {"model": "gpt-5.5"},
        }
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
            {
                "target_repo": "/tmp/app",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "docs/plan.md",
                "test_command": "true",
                "executor": {"model": "openai/m", "api_base_env": "SOME_UNSET_BASE"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.5"},
            }
        )
        assert check_executor_endpoint(cfg) == []


class TestEndpointEnvironmentChecks:
    def test_an_unexported_address_is_blocking(self, repo, monkeypatch):
        # A real repo: the repo checks short-circuit everything below them, so a
        # bare tmp_path would prove nothing about the endpoint check.
        monkeypatch.delenv("SPARK_BASE", raising=False)
        cfg = parse_config(
            {
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "test_command": "true",
                "executor": {"model": "openai/m", "api_base_env": "SPARK_BASE"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.5"},
            }
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
            {
                "target_repo": "/tmp/app",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "docs/plan.md",
                "test_command": "true",
                "executor": {
                    "model": "openai/qwen3-coder-next",
                    "api_base_env": "SECRET_BASE",
                },
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
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
            {
                "target_repo": "/tmp/app",
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "docs/plan.md",
                "test_command": "true",
                "executor": {"model": "openai/m", "api_base_env": "SECRET_BASE"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
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
        cfg = parse_config(
            {
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                # Prints its verdict, then 4000 characters of noise, then fails.
                "test_command": (
                    "echo '9 examples, 3 failures'; "
                    "for i in $(seq 1 200); do echo 'DEPRECATION WARNING: something'; done; "
                    "exit 1"
                ),
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        checks = run_preflight(
            cfg,
            check_models=False,
            check_approval=False,
            check_endpoint=False,
        )
        failed = [c for c in checks if not c.ok and "test_command" in c.name]
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
        return parse_config(
            {
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "test_command": (
                    "echo 'rspec ./spec/features/a_spec.rb:40'; "
                    "echo '9 examples, 1 failure'; exit 1"
                ),
                "scoped_test_command": (
                    "echo {paths}" if scoped_ok else "echo {paths}; exit 1"
                ),
                "failed_file_pattern": r"^rspec \./(\S+?\.rb)",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )

    def _check(self, repo, scoped_ok: bool):
        checks = run_preflight(
            self._cfg(repo, scoped_ok),
            check_models=False,
            check_approval=False, check_endpoint=False,
        )
        return next(c for c in checks if "test_command passes" in c.name)

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
        assert any(c.ok for c in checks if "test_command passes" in c.name)
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
        cfg = parse_config({**cfg.model_dump(mode="json"), "test_command": "true",
                            "full_test_command": "true"})
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


class TestSuitesAreNotRunTwice:
    """`validate` runs test_command and full_test_command.

    When they are the same string that is the same suite twice, which on the
    first real project is 23 minutes to learn one thing.
    """

    def test_an_identical_pair_runs_once(self, repo):
        marker = repo / "runs.txt"
        command = f"echo x >> {marker}"
        cfg = parse_config(
            {
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "test_command": command,
                "full_test_command": command,
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        run_preflight(
            cfg, check_models=False,
            check_approval=False, check_endpoint=False,
        )
        assert marker.read_text().count("x") == 1

    def test_a_differing_pair_still_runs_both(self, repo):
        marker = repo / "runs.txt"
        cfg = parse_config(
            {
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "test_command": f"echo a >> {marker}",
                "full_test_command": f"echo b >> {marker}",
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        run_preflight(
            cfg, check_models=False,
            check_approval=False, check_endpoint=False,
        )
        assert marker.read_text().split() == ["a", "b"]

    def test_the_skipped_twin_inherits_the_verdict(self, repo):
        """Not re-running is a saving, not an acquittal.

        The first real `validate` printed `[FAIL] test_command` and, two lines
        later, `[ok] full_test_command passes on a clean tree`, for one red
        suite run once. Reporting the deduplicated twin as a pass is worse
        than running it twice: it manufactures evidence of green from a run
        that was red.
        """
        command = "echo '9 examples, 3 failures'; exit 1"
        cfg = parse_config(
            {
                "target_repo": str(repo),
                "base_ref": "main",
                "project_branch": "proj",
                "plan_root": "PLAN.md",
                "test_command": command,
                "full_test_command": command,
                "executor": {"model": "m"},
                "planner": {"model": "claude-opus-5"},
                "reviewer": {"model": "gpt-5.6-sol"},
            }
        )
        checks = run_preflight(
            cfg, check_models=False,
            check_approval=False, check_endpoint=False,
        )
        twin = [c for c in checks if "full_test_command" in c.name]
        assert twin, "the deduplicated twin should still be reported"
        assert not twin[0].ok, "a red suite cannot pass under a second label"
        assert "9 examples, 3 failures" in twin[0].detail


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
        from orchestrator.preflight import _credential_check

        assert _credential_check("planner", self._client()).ok

    def test_an_expired_key_fails_fatally(self):
        from orchestrator.preflight import _credential_check

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
        from orchestrator.preflight import _credential_check

        exc = type("E", (Exception,), {"status_code": 400})(
            "Could not finish the message because max_completion_tokens"
        )
        check = _credential_check("reviewer", self._client(exc))
        assert check.ok
        assert "the key is live" in check.detail

    def test_a_rate_limit_does_not_block_a_run(self):
        from orchestrator.preflight import _credential_check

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
        from orchestrator.gitops import Git
        from orchestrator.preflight import _read_budget_check

        cfg = parse_config({
            "target_repo": str(repo), "base_ref": "main", "project_branch": "proj",
            "plan_root": "PLAN.md", "test_command": "true",
            "executor": {"model": "m", "max_read_lines": cap},
            "planner": {"model": "claude-opus-5"}, "reviewer": {"model": "gpt-5.5"},
        })
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
        from orchestrator.preflight import _ripgrep_check

        check = _ripgrep_check()
        assert check.ok
        assert check.detail.endswith("rg")

    def test_it_fails_with_a_fix_when_rg_is_absent(self, monkeypatch):
        import orchestrator.preflight as pf

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

        from orchestrator.runlog import RunLog

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
        from orchestrator.runlog import RunLog

        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        RunLog(run_dir / "run.log", echo=None).close()
        assert not (run_dir / "run.json").exists()
