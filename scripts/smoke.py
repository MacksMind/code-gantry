#!/usr/bin/env python3
"""End-to-end smoke test: the real CLI, a synthetic repo, stand-in models.

`pytest` covers every unit and drives the graph directly. This covers the one
thing it cannot: that the installed console script, run from a shell, against a
real git repository, completes a whole project unattended and leaves the
repository in the state the design promises.

Three things stand in for the outside world, and nothing else does:

- **The executor.** A fake `aider` on PATH that appends a function and a test.
  It speaks the flag surface the real one does, so `preflight`'s
  `aider --help` check is exercised rather than skipped.
- **The planner and reviewer.** One HTTP server on localhost answering
  `/v1/messages` in Anthropic's wire format and `/v1/chat/completions` in
  OpenAI's, with usage figures in both. The real SDKs, the real parsing, the
  real defensive paths — only the inference is fake.

Everything else is real: git, the branch topology, the squash merges, the
checkpointer, the verify layers, the subprocess runner, the report.

    uv run python scripts/smoke.py            # ~15s, no network, no API keys
    uv run python scripts/smoke.py --keep     # leave the sandbox for poking

Exit 0 means every assertion below held.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Claimed at startup, not fixed: a hardcoded port collides with a stand-in left
# running by an earlier session, and the resulting bind error is a confusing way
# to learn that.
PORT = 0
BRANCH = "feature/calculator"
RUN_ID = "smoke"
SLUG = "plan"

# The variable `init` drafts for the executor's endpoint. A hostname is an
# infrastructure fact, so the drafted config names a variable rather than
# carrying an address.
EXECUTOR_API_BASE_VAR = "ORCHESTRATOR_EXECUTOR_API_BASE"

# The models `init` drafts. Kept here so a change to the draft fails loudly in
# patch_config rather than silently pointing a live run at nothing.
PLANNER_MODEL = "claude-opus-5"
REVIEWER_MODEL = "gpt-5.6-sol"
# Live mode still uses the fake Aider, but names a real model so preflight's
# endpoint check verifies against the real server.
LIVE_EXECUTOR_MODEL = "qwen3-coder-next"

# The two stages the stand-in planner derives, in order. Each is exactly what a
# real planner may return: declarative fields only, no command anywhere.
STAGES = [
    {
        "id": "add-multiply",
        "instruction": "Add a multiply function to src/calc.py, with tests.",
        "edit_files": ["src/**", "tests/**"],
        "read_files": [],
        "constraints": "Pure functions only; no I/O.",
        "acceptance": "multiply(3, 4) == 12",
        "forbidden_patterns": [],
        "test_paths": [],
    },
    {
        "id": "add-divide",
        "instruction": (
            "Add a divide function to src/calc.py, with tests, raising on "
            "divide-by-zero."
        ),
        "edit_files": ["src/**", "tests/**"],
        "read_files": [],
        "constraints": "Pure functions only; no I/O.",
        "acceptance": "divide(8, 2) == 4, and divide(1, 0) raises",
        "forbidden_patterns": [],
        "test_paths": [],
    },
]

FAKE_AIDER = '''#!/usr/bin/env python3
"""A stand-in for Aider: implements whichever operation the message names."""
import pathlib
import sys

if "--help" in sys.argv:
    # preflight parses this. The real flag surface, so a renamed flag in
    # executor.AIDER_FLAGS fails the smoke test rather than passing it.
    print(
        "--message --yes-always --no-stream --model --openai-api-base "
        "--test-cmd --auto-test --lint-cmd --map-tokens --file --read"
    )
    sys.exit(0)

message = sys.argv[sys.argv.index("--message") + 1] if "--message" in sys.argv else ""
calc = pathlib.Path("src/calc.py")
body = calc.read_text()

# Tests are appended to the *existing* test file rather than written to a new
# one. A real planner scopes a stage to the files it expects to change and says
# "add tests in the existing test file"; inventing tests/test_multiply.py
# violated that scope on every attempt, which is a stand-in that ignores its
# instructions rather than an orchestrator that mis-scoped.
TEST_FILE = "tests/test_calc.py"

IMPLEMENTATIONS = {
    "multiply": (
        "\\n\\ndef multiply(a, b):\\n    return a * b\\n",
        "\\n\\ndef test_multiply():\\n"
        "    from src.calc import multiply\\n"
        "    assert multiply(3, 4) == 12\\n"
        "    assert multiply(-2, 3) == -6\\n"
        "    assert multiply(0, 5) == 0\\n",
    ),
    "divide": (
        "\\n\\ndef divide(a, b):\\n"
        "    if b == 0:\\n"
        "        raise ValueError('divide by zero')\\n"
        "    return a / b\\n",
        "\\n\\ndef test_divide():\\n"
        "    import pytest\\n"
        "    from src.calc import divide\\n"
        "    assert divide(8, 2) == 4\\n"
        "    with pytest.raises(ValueError):\\n        divide(1, 0)\\n",
    ),
}

# Which operation is this stage about? Not "which is mentioned" — a real
# planner's constraints name what must NOT be built ("reject the stage if the
# diff adds division"), and a plain keyword match implemented the prohibition.
# Frequency separates the subject from the prohibitions cleanly: the stage's own
# operation is named throughout, the forbidden ones once or twice in passing.
lowered = message.lower()
SYNONYMS = {"multiply": ("multiply", "multiplication"), "divide": ("divide", "division")}
counts = {
    op: sum(lowered.count(word) for word in words) for op, words in SYNONYMS.items()
}
ranked = sorted(counts.items(), key=lambda kv: -kv[1])

subject = None
if ranked[0][1] > 0 and ranked[0][1] > ranked[1][1]:
    subject = ranked[0][0]

named = [subject] if subject and f"def {subject}" not in body else []

if named:
    op = named[0]
    source, test_source = IMPLEMENTATIONS[op]
    calc.write_text(body + source)
    tests = pathlib.Path(TEST_FILE)
    tests.write_text(tests.read_text() + test_source)
    print(f"aider: implemented {op}, with tests in {TEST_FILE}")
    sys.exit(0)

if subject:
    print(f"aider: {subject} is already implemented; nothing to do")
    sys.exit(0)

# Report the counts. Saying "already implemented" here would be a lie — the
# real situation is that no operation clearly dominates the instruction, and a
# stand-in that misreports why it did nothing wastes a debugging session.
print(
    f"aider: cannot tell which operation this stage is about (mention counts: "
    f"{counts}); this stand-in implements only {sorted(IMPLEMENTATIONS)}",
    file=sys.stderr,
)
sys.exit(1)
'''

PLAN_DOC = """# Calculator capability plan

Bring the calculator up to a usable set of operations. The details are in
[the detail document](plan_detail.md).

## Stage shape

One operation per stage, each landing with its own tests. A stage is done when
its tests pass and the whole suite is still green.
"""

PLAN_DETAIL = """# Detail

Stage 1: multiplication.

Stage 2: division, raising `ValueError` on divide-by-zero.
"""


# --- the stand-in models -------------------------------------------------


class ModelStub(BaseHTTPRequestHandler):
    """Both paid models, in their own wire formats, on one port.

    The planner's answer is derived from **what has landed on the project
    branch**, not from a call counter. A counter would hand out a different
    stage on every retry or rework, and the resulting confusion would look
    like an orchestrator bug. This way the stand-in is idempotent: called
    twice for the same repository state, it says the same thing.
    """

    repo: Path  # set on the class before serving

    def do_GET(self) -> None:  # noqa: N802
        """`/v1/models`, which preflight reads to verify the model id.

        Answering it in llama-swap's shape means the smoke test covers the
        endpoint check rather than having to disable it.
        """
        payload = {
            "object": "list",
            "data": [{"id": "local-model", "object": "model", "owned_by": "llama-swap"}],
        }
        self._respond(payload)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "stub")

        if "/messages" in self.path:
            self._respond(self._anthropic(model))
        else:
            self._respond(self._openai(model))

    def _respond(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _anthropic(self, model: str) -> dict:
        # Cache figures are deliberately high: report.md warns below 50%, and a
        # warning firing here would mean the report logic changed.
        return {
            "id": "msg_smoke",
            "type": "message",
            "role": "assistant",
            "model": model,
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": json.dumps(self._plan())}],
            "usage": {
                "input_tokens": 12_000,
                "output_tokens": 300,
                "cache_read_input_tokens": 11_200,
            },
        }

    def _openai(self, model: str) -> dict:
        verdict = {
            "verdict": "approved",
            "summary": "Implements the stage as specified, with tests.",
            "issues": [],
        }
        return {
            "id": "chatcmpl-smoke",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(verdict)},
                }
            ],
            "usage": {
                "prompt_tokens": 9_000,
                "completion_tokens": 40,
                "total_tokens": 9_040,
                "prompt_tokens_details": {"cached_tokens": 8_600},
            },
        }

    def _plan(self) -> dict:
        landed = self._landed_calc()
        for index, stage in enumerate(STAGES):
            operation = stage["id"].removeprefix("add-")
            if f"def {operation}" not in landed:
                return {
                    "verdict": "next_stage",
                    "reasoning": f"{operation} is not on the project branch yet",
                    "status_entry": (
                        f"Goal: land {operation}. Expected: one commit adding "
                        f"the function and its tests. Actual: pending."
                    ),
                    "stage": stage,
                    "revision_mode": None,
                }
        return {
            "verdict": "project_complete",
            "reasoning": "every operation in the plan is on the project branch",
            "status_entry": "Goal: none remaining. Expected and actual agree.",
            "stage": None,
            "revision_mode": None,
        }

    def _landed_calc(self) -> str:
        """src/calc.py as it stands on the project branch, or on base."""
        for ref in (BRANCH, "main"):
            done = subprocess.run(
                ["git", "-C", str(self.repo), "show", f"{ref}:src/calc.py"],
                capture_output=True,
                text=True,
            )
            if done.returncode == 0:
                return done.stdout
        return ""

    def log_message(self, *args) -> None:  # noqa: D102 - silence the access log
        pass


def serve(repo: Path) -> HTTPServer:
    global PORT
    ModelStub.repo = repo
    server = HTTPServer(("127.0.0.1", 0), ModelStub)
    PORT = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _await_port()
    print(f"stand-in models on 127.0.0.1:{PORT}")
    return server


def _await_port(attempts: int = 50) -> None:
    """Wait for the socket, without speaking HTTP.

    A health-check request would be answered like any other request, and — when
    the stand-in planner was counter-driven — silently consumed the first
    stage. That cost an hour of debugging a bug that was in the test, not the
    orchestrator. So: TCP only, never a request.
    """
    for _ in range(attempts):
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.1):
                return
        except OSError:
            continue
    fail(f"the stand-in model server never came up on port {PORT}")


# --- the synthetic repo --------------------------------------------------


def build_repo(root: Path) -> Path:
    repo = root / "app"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "docs").mkdir()

    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    (repo / "docs" / "plan.md").write_text(PLAN_DOC)
    (repo / "docs" / "plan_detail.md").write_text(PLAN_DETAIL)
    # Without this the caches pytest writes fail the scope guard on every
    # stage — which is precisely what preflight's tidiness check warns about.
    (repo / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")

    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "Add a calculator with one operation")
    return repo


def git(repo: Path, *args: str) -> str:
    """Run git with identity and signing forced off.

    Signing in particular: an unattended run cannot answer a pinentry prompt,
    and neither can this script.
    """
    done = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Smoke Test",
            "-c",
            "user.email=smoke@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        fail(f"git {' '.join(args)} failed:\n{done.stderr}")
    return done.stdout.strip()


def git_config(repo: Path, key: str) -> str | None:
    """A config value, or None if unset.

    Separate from `git()` because `git config --get` exits 1 for an unset key,
    and unset is the answer this script is usually hoping for.
    """
    done = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", key],
        capture_output=True,
        text=True,
    )
    return done.stdout.strip() if done.returncode == 0 else None


def write_fake_aider(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    aider = bin_dir / "aider"
    aider.write_text(FAKE_AIDER)
    aider.chmod(0o755)


# --- driving the CLI -----------------------------------------------------


def cli(work: Path, env: dict, *args: str, expect: int = 0) -> str:
    """Invoke the installed console script and return its combined output."""
    done = subprocess.run(
        [sys.executable, "-m", "orchestrator.cli", *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    output = done.stdout + done.stderr
    if done.returncode != expect:
        fail(
            f"`orchestrator {' '.join(args)}` exited {done.returncode}, "
            f"expected {expect}:\n{output}"
        )
    return output


def patch_config(config: Path, live: bool = False) -> None:
    """Do what an operator does after `init`: fill in what it could not infer.

    Each replacement asserts the placeholder was there. If `init`'s draft
    changes shape, this fails loudly rather than silently patching nothing and
    running against the wrong endpoint.

    In live mode the two paid models are left pointing at their real APIs, and
    the executor at the real endpoint — only Aider stays fake, so a live run
    exercises planning and review without generating any code.
    """
    text = config.read_text()

    def swap(old: str, new: str) -> None:
        nonlocal text
        if old not in text:
            fail(f"`init` no longer drafts {old!r}; scripts/smoke.py needs updating")
        text = text.replace(old, new, 1)

    swap("project_branch: refactor/CHANGE-ME", f"project_branch: {BRANCH}")
    swap(
        'model: "openai/<model-id-from-/v1/models>"',
        f'model: "openai/{LIVE_EXECUTOR_MODEL if live else "local-model"}"',
    )
    # The executor's address is left exactly as drafted — `api_base_env`,
    # resolved from the environment at run time. That is the form `init` emits,
    # so it is the form worth covering.
    if f'api_base_env: "{EXECUTOR_API_BASE_VAR}"' not in text:
        fail(
            f"`init` no longer drafts api_base_env: {EXECUTOR_API_BASE_VAR}; "
            "scripts/smoke.py needs updating"
        )

    if not live:
        # The planner's base has no /v1: the Anthropic SDK appends it.
        swap(
            f'model: "{PLANNER_MODEL}"',
            f'model: "{PLANNER_MODEL}"\n  api_base: "http://127.0.0.1:{PORT}"',
        )
        swap(
            f'model: "{REVIEWER_MODEL}"',
            f'model: "{REVIEWER_MODEL}"\n  api_base: "http://127.0.0.1:{PORT}/v1"',
        )
    else:
        for model in (PLANNER_MODEL, REVIEWER_MODEL):
            if f'model: "{model}"' not in text:
                fail(
                    f"`init` no longer drafts {model!r}; scripts/smoke.py needs "
                    "updating"
                )

    swap("max_stages: 60", "max_stages: 6")
    config.write_text(text)


# --- assertions ----------------------------------------------------------

CHECKS = 0


def check(condition: bool, description: str, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {description}")
        return
    print(f"  FAIL {description}")
    if detail:
        for line in detail.strip().splitlines():
            print(f"         {line}")
    sys.exit(1)


def fail(message: str) -> None:
    print(f"\nsmoke test aborted: {message}")
    sys.exit(1)


def verify_outcome(work: Path, repo: Path, report: str, live: bool = False) -> None:
    """Assert the promises the design makes about the finished repository.

    Live mode asserts outcomes rather than counts. A real planner decides how
    many stages the plan needs and what to call them, so pinning either would
    be asserting the model's wording rather than the orchestrator's behaviour.
    """
    project = work / "projects" / SLUG
    run_dir = project / "runs" / RUN_ID

    print("\nthe report")
    if not live:
        for stage in STAGES:
            check(stage["id"] in report, f"names stage {stage['id']}")
    check("approved" in report, "records the reviewer's verdict")
    check(
        "cache" not in report.lower() or "%" in report,
        "reports cache figures when it mentions caching",
    )

    print("\nthe repository")
    leftovers = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    branches = leftovers.splitlines()
    check(
        not [b for b in branches if b.startswith(f"{BRANCH}-stage/")],
        "every child branch was deleted after merging",
        f"branches: {branches}",
    )
    check(
        git(repo, "rev-list", "--count", "main") == "1",
        "main is untouched",
        "the tool never merges the project branch; that is the operator's job",
    )
    commits = int(git(repo, "rev-list", "--count", BRANCH))
    if live:
        check(
            commits > 1,
            f"{BRANCH} carries a squashed commit per landed stage",
            git(repo, "log", "--oneline", BRANCH),
        )
    else:
        check(
            commits == 1 + len(STAGES),
            f"{BRANCH} carries one squashed commit per stage",
            git(repo, "log", "--oneline", BRANCH),
        )
    landed = git(repo, "show", f"{BRANCH}:src/calc.py")
    for stage in STAGES:
        operation = stage["id"].removeprefix("add-")
        check(f"def {operation}" in landed, f"{operation} landed on the branch")
    check(
        git(repo, "status", "--porcelain") == "",
        "the working tree is clean at the end",
    )
    check(
        git_config(repo, "gc.auto") is None,
        "gc.auto was restored to unset",
        f"gc.auto is {git_config(repo, 'gc.auto')!r}; left disabled, the "
        "repository never garbage-collects again",
    )

    print("\nthe project directory")
    check((project / "approval.json").is_file(), "approval.json was recorded")
    snapshot = sorted(p.name for p in (project / "plan-snapshot").iterdir())
    check(
        len([n for n in snapshot if n.endswith(".md")]) == 2,
        "the plan snapshot holds both documents",
        f"snapshot: {snapshot}",
    )
    status = (project / "status.md").read_text()
    check(
        status.count("## ") >= (2 if live else len(STAGES) + 1),
        "status.md has an append-only entry per planner call",
        f"{status.count('## ')} entries",
    )

    print("\nthe run directory")
    for name in ("report.md", "run.log", "state.db", "run.json"):
        check((run_dir / name).is_file(), f"{name} was written")
    attempts = sorted(p.name for p in (run_dir / "stages").iterdir())

    # A planner call that *derives* a stage has no stage to be attributed to
    # yet, so its artifact lands under a `plan` pseudo-id. One per derivation,
    # plus the final one that declares the project complete.
    derivations = [name for name in attempts if "-plan-rev-" in name]
    check(
        len(derivations) >= 2 if live else len(derivations) == len(STAGES) + 1,
        "one planner directory per derivation, plus the completion call",
        f"derivations: {derivations}",
    )
    check(
        (run_dir / "stages" / derivations[0] / "planner.json").is_file(),
        f"{derivations[0]}/planner.json",
    )

    executions = [name for name in attempts if "-plan-rev-" not in name]
    check(
        len(executions) >= 1 if live else len(executions) == len(STAGES),
        "one attempt directory per stage executed",
        f"attempts: {attempts}",
    )
    for name in ("prompt.md", "executor.log", "verify.log", "review.json"):
        check((run_dir / "stages" / executions[0] / name).is_file(), f"{executions[0]}/{name}")


# --- main ----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="Leave the sandbox in place for inspection."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use the real planner, reviewer, and endpoint. Aider stays fake, so "
        "this exercises planning and review without generating code. Costs money.",
    )
    args = parser.parse_args()

    if args.live:
        missing = [
            name
            for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", EXECUTOR_API_BASE_VAR)
            if not os.environ.get(name)
        ]
        if missing:
            fail("live mode needs these exported: " + ", ".join(missing))

    root = Path(tempfile.mkdtemp(prefix="orchestrator-smoke-"))
    print(f"sandbox: {root}\n")

    try:
        repo = build_repo(root)
        write_fake_aider(root / "bin")
        # In live mode nothing talks to the stand-in: both paid models go to
        # their real APIs and the executor endpoint check goes to the real one.
        server = None if args.live else serve(repo)

        work = root / "work"
        work.mkdir()

        env = {
            **os.environ,
            "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
        }
        if not args.live:
            # The stand-in accepts anything; the two paid clients refuse to
            # build without a key, which is itself worth exercising. The
            # executor deliberately gets none — a local endpoint serves without
            # auth, so this exercises the placeholder path.
            env.update(
                {
                    "ANTHROPIC_API_KEY": "smoke-planner-key",
                    "OPENAI_API_KEY": "smoke-reviewer-key",
                    EXECUTOR_API_BASE_VAR: f"http://127.0.0.1:{PORT}/v1",
                }
            )

        print("init")
        cli(work, env, "init", str(repo / "docs" / "plan.md"), "--slug", SLUG)
        config = work / "projects" / SLUG / "config.yaml"
        check(config.is_file(), "drafted a config")
        patch_config(config, live=args.live)

        print("\nrun, before approval")
        refused = cli(work, env, "run", SLUG, expect=1)
        check(
            "refus" in refused.lower(),
            "refuses to start without an approval",
            refused,
        )

        print("\nvalidate")
        checks = cli(work, env, "validate", SLUG)
        check("[FAIL]" not in checks, "no blocking problems", checks)
        check("aider still accepts the flags we build" in checks, "checked aider's flags")
        check(
            f"executor endpoint resolves from {EXECUTOR_API_BASE_VAR}" in checks,
            "resolved the executor endpoint from the environment",
            checks,
        )
        expected_model = LIVE_EXECUTOR_MODEL if args.live else "local-model"
        check(
            f"endpoint offers '{expected_model}'" in checks,
            "verified the model id against the endpoint's /v1/models",
            checks,
        )

        print("\napprove")
        approved = cli(work, env, "approve", SLUG)
        check("python -m pytest" in approved, "printed the commands it will run")

        print("\nrun")
        report = cli(work, env, "run", SLUG, "--run-id", RUN_ID)
        check("complete" in report.lower(), "the run completed", report[-2000:])

        verify_outcome(work, repo, report, live=args.live)

        print(f"\n{CHECKS} checks passed")
        if server is not None:
            server.shutdown()
        return 0
    finally:
        if args.keep:
            print(f"\nsandbox kept at {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
