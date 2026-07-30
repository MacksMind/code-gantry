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
from orchestrator.preflight import check_executor_endpoint, run_preflight

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
            check_aider=False,
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
            check_aider=False,
            check_models=False,
            check_approval=False,
            check_endpoint=False,
        )
        failed = [c for c in checks if not c.ok and "test_command" in c.name]
        assert failed, "the failing command should have produced a check"
        assert "9 examples, 3 failures" in failed[0].detail


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
            cfg, check_aider=False, check_models=False,
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
            cfg, check_aider=False, check_models=False,
            check_approval=False, check_endpoint=False,
        )
        assert marker.read_text().split() == ["a", "b"]
