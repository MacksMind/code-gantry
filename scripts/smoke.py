#!/usr/bin/env python3
"""End-to-end smoke test: the real CLI, a synthetic repo, stand-in models.

`pytest` covers every unit and drives the graph directly. This covers the one
thing it cannot: that the installed console script, run from a shell, against a
real git repository, completes a whole project unattended and leaves the
repository in the state the design promises.

Three things stand in for the outside world, and nothing else does:

- **All three models.** One HTTP server on localhost answering `/v1/messages`
  in Anthropic's wire format for the planner, `/v1/chat/completions` in
  OpenAI's for the reviewer, and `/v1/responses` for the executor. The real
  SDKs, the real parsing, the real defensive paths — only the inference is fake.

  The executor used to be a stub binary on PATH, back when it shelled out to a
  subprocess. It is in-process now and calls `responses.create`, so a binary on
  PATH stood in for nothing: the smoke test would have driven a code path
  production no longer takes, which is the "a test suite can be exercising the
  path you are about to delete" failure with the roles reversed. The stand-in is
  a stub endpoint that answers with a `function_call` to the real `edit` tool,
  so `executortools`, `edittools` and the dispatch loop all run for real.

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
# Live mode still stubs the executor, but names a real model so preflight's
# endpoint check verifies against the real server.
LIVE_EXECUTOR_MODEL = "qwen3-coder-next"

# What llama.cpp reports as n_ctx_slot for that model — the ceiling the
# executor's read and output budgets are sized against.
#
# The input budget is the slot size minus the output budget, deliberately: they
# share one window, so declaring the full slot as input invites an overflow at
# exactly the moment the model has most to say.
LIVE_CONTEXT_TOKENS = 262_144
LIVE_OUTPUT_TOKENS = 32_768

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

# What the stub executor implements, and where. Tests are appended to the
# *existing* test file rather than written to a new one: a real planner scopes a
# stage to the files it expects to change and says "add tests in the existing
# test file", so inventing `tests/test_multiply.py` violated that scope on every
# attempt — a stand-in ignoring its instructions rather than an orchestrator
# mis-scoping.
TEST_FILE = "tests/test_calc.py"

# Anchors the stub edits against. Both are lines the seeded repository already
# contains and neither stage removes, so the same two work for `multiply` and
# for `divide` after it — an anchored insertion, which is the shape a real stage
# produces, rather than a whole-file rewrite that would hide whether the edit
# tool located anything.
CALC_ANCHOR = "    return a + b\n"
TEST_ANCHOR = "    assert add(2, 2) == 4\n"

IMPLEMENTATIONS = {
    "multiply": (
        "\n\ndef multiply(a, b):\n    return a * b\n",
        "\n\ndef test_multiply():\n"
        "    from src.calc import multiply\n"
        "    assert multiply(3, 4) == 12\n"
        "    assert multiply(-2, 3) == -6\n"
        "    assert multiply(0, 5) == 0\n",
    ),
    "divide": (
        "\n\ndef divide(a, b):\n"
        "    if b == 0:\n"
        "        raise ValueError('divide by zero')\n"
        "    return a / b\n",
        "\n\ndef test_divide():\n"
        "    import pytest\n"
        "    from src.calc import divide\n"
        "    assert divide(8, 2) == 4\n"
        "    with pytest.raises(ValueError):\n        divide(1, 0)\n",
    ),
}


def stage_subject(text: str) -> str | None:
    """Which operation a stage instruction is about.

    Not "which is mentioned" — a real planner's constraints name what must NOT
    be built ("reject the stage if the diff adds division"), and a plain keyword
    match implemented the prohibition. Nor "which is mentioned most": a live
    planner produced a multiply stage naming both exactly five times, because
    every prohibition about division was matched by a requirement about
    multiplication.

    Position is the reliable signal. The prompt states the task before it
    constrains it, so whichever operation is named *first* is the subject.
    """
    lowered = text.lower()
    synonyms = {
        "multiply": ("multiply", "multiplication"),
        "divide": ("divide", "division"),
    }
    positions = {}
    for op, words in synonyms.items():
        found = [lowered.find(w) for w in words if lowered.find(w) >= 0]
        if found:
            positions[op] = min(found)
    return min(positions, key=positions.get) if positions else None


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
        elif "/responses" in self.path:
            # Both the executor and the reviewer speak the Responses API, on
            # the same path, so the model id is what tells them apart. Splitting
            # on the path alone sent the reviewer the executor's "Implemented,
            # with tests." and it failed to parse as a verdict — the stand-in
            # answering the wrong role rather than anything wrong upstream.
            if model == REVIEWER_MODEL:
                self._respond(self._reviewer_response(model))
            else:
                self._respond(self._executor(model, body))
        else:
            self._respond(self._openai(model))

    def _respond(self, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _reviewer_response(self, model: str) -> dict:
        """The reviewer's verdict, in the Responses shape rather than chat's.

        `reviewer.py` calls `responses.parse`, so the verdict has to arrive as
        output text that its schema accepts. The chat-completions branch below
        stays: `_openai` still serves anything that asks for it, and dropping it
        would remove coverage of a wire format the code still knows.
        """
        # Every required field of `ReviewVerdict`, built from the model rather
        # than from memory — a stand-in that drifts from the schema fails the
        # smoke test for a reason that has nothing to do with the pipeline.
        verdict = {
            "verdict": "approved",
            "summary": "Implements the stage as specified, with tests.",
            "record": "Adds the operation to src/calc.py and a test for it.",
            "issues": [],
        }
        return {
            "id": "resp_smoke_review",
            "object": "response",
            "model": model,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": json.dumps(verdict)}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 9_000,
                "output_tokens": 40,
                "input_tokens_details": {"cached_tokens": 8_600},
            },
        }

    def _executor(self, model: str, body: dict) -> dict:
        """The executor, in the Responses shape its client actually parses.

        Two turns, told apart by the conversation itself rather than by state
        the server keeps: the first asks for an `edit`, and once a
        `function_call_output` comes back the work is done and a plain message
        ends the loop. A stateless stand-in cannot be desynchronised by a retry.

        The tool call is real. `executortools` validates it, `edittools` applies
        it, and the scope guard judges the result — which is the point of
        replacing the old stub binary rather than deleting it: that stood
        outside every one of those, so the smoke test exercised a path
        production no longer takes.
        """
        items = body.get("input") or []
        text = " ".join(
            str(block.get("text", ""))
            for item in items
            if isinstance(item, dict)
            for block in (item.get("content") or [])
            if isinstance(block, dict)
        )
        answered = any(
            isinstance(i, dict) and i.get("type") == "function_call_output"
            for i in items
        )
        usage = {
            "input_tokens": 8_000,
            "output_tokens": 120,
            "input_tokens_details": {"cached_tokens": 7_400},
        }
        if answered:
            return {
                "id": "resp_smoke_done",
                "object": "response",
                "model": model,
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": "Implemented, with tests."}
                        ],
                    }
                ],
                "usage": usage,
            }

        op = stage_subject(text)
        calls = []
        if op:
            source, test_source = IMPLEMENTATIONS[op]
            # Appended, so the edit is an anchored insertion rather than a
            # whole-file rewrite — the shape a real stage produces.
            calls = [
                {
                    "type": "function_call",
                    "call_id": "call_smoke_1",
                    "name": "edit",
                    "arguments": json.dumps(
                        {
                            "path": "src/calc.py",
                            "edits": [
                                {"old_string": CALC_ANCHOR, "new_string": CALC_ANCHOR + source}
                            ],
                        }
                    ),
                },
                {
                    "type": "function_call",
                    "call_id": "call_smoke_2",
                    "name": "edit",
                    "arguments": json.dumps(
                        {
                            "path": TEST_FILE,
                            "edits": [
                                {
                                    "old_string": TEST_ANCHOR,
                                    "new_string": TEST_ANCHOR + test_source,
                                }
                            ],
                        }
                    ),
                },
            ]
        return {
            "id": "resp_smoke",
            "object": "response",
            "model": model,
            "output": calls
            or [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            # Not "already implemented": a stand-in that
                            # misreports why it did nothing wastes a debugging
                            # session. Say which operations it knows.
                            "text": "cannot tell which operation this stage is "
                            f"about; this stand-in implements only "
                            f"{sorted(IMPLEMENTATIONS)}",
                        }
                    ],
                }
            ],
            "usage": usage,
        }

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
            "record": "Adds the operation to src/calc.py and a test for it.",
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
    # `.code_gantry/` because the work dir defaults inside the repo, beside
    # the plan, and preflight blocks a run whose data directory is tracked.
    # Every real project needs this line; the smoke test is the one place that
    # proves a project set up from scratch actually starts.
    (repo / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.code_gantry/\n"
    )

    git(repo, "init", "-b", "main")
    # Persisted locally, not just passed per-invocation: the executor commits
    # its own work, and it would otherwise inherit the operator's global identity
    # and signing settings. An unattended run cannot answer a pinentry prompt,
    # so a signed commit is a hang, not an error.
    git(repo, "config", "user.name", "Smoke Test")
    git(repo, "config", "user.email", "smoke@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
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


# --- driving the CLI -----------------------------------------------------


def cli(work: Path, env: dict, *args: str, expect: int = 0) -> str:
    """Invoke the installed console script and return its combined output."""
    done = subprocess.run(
        [sys.executable, "-m", "code_gantry.cli", *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
    )
    output = done.stdout + done.stderr
    if done.returncode != expect:
        fail(
            f"`code-gantry {' '.join(args)}` exited {done.returncode}, "
            f"expected {expect}:\n{output}"
        )
    return output


def patch_config(config: Path, live: bool = False) -> None:
    """Do what an operator does after `init`: fill in what it could not infer.

    Each replacement asserts the placeholder was there. If `init`'s draft
    changes shape, this fails loudly rather than silently patching nothing and
    running against the wrong endpoint.

    In live mode the two paid models are left pointing at their real APIs, and
    the executor at the real endpoint — only the executor stays stubbed, so a
    live run
    exercises planning and review without generating any code.
    """
    text = config.read_text()

    def swap(old: str, new: str) -> None:
        nonlocal text
        if old not in text:
            fail(f"`init` no longer drafts {old!r}; scripts/smoke.py needs updating")
        text = text.replace(old, new, 1)

    swap("project_branch: refactor/CHANGE-ME", f"project_branch: {BRANCH}")
    # No `openai/` prefix. That was litellm routing, which the executor needed
    # while it ran as a subprocess; in-process it calls the SDK directly
    # and the model id is the endpoint's own.
    swap(
        'model: "<model-id-from-/v1/models>"',
        f'model: "{LIVE_EXECUTOR_MODEL if live else "local-model"}"',
    )
    # `init` drafts the executor's `api_base_env` commented out, because talking
    # to the provider directly is the common case. Uncommenting it is exactly
    # what an operator does when the executor lives on their own endpoint, and
    # here it is what points the executor at the stand-in server — so the form
    # covered is both the one `init` emits and the one this test needs.
    swap(
        f'  # api_base_env: "{EXECUTOR_API_BASE_VAR}"',
        f'  api_base_env: "{EXECUTOR_API_BASE_VAR}"',
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


def verify_outcome(work: Path, repo: Path, report: str, live: bool = False, main_at_start: str = "") -> None:
    """Assert the promises the design makes about the finished repository.

    Live mode asserts outcomes rather than counts. A real planner decides how
    many stages the plan needs and what to call them, so pinning either would
    be asserting the model's wording rather than the orchestrator's behaviour.
    """
    # Where the config says, not where a slug used to put it. The work dir
    # defaults beside the plan documents inside the target repo, which is the
    # layout every new project gets.
    project = repo / "docs" / ".code_gantry"
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
    # Against the sha as it stood when the run began, not a commit count. The
    # config lives in the repo now and an operator commits it to their default
    # branch before starting, so "main has exactly one commit" stopped being
    # true for a reason that has nothing to do with the tool.
    check(
        git(repo, "rev-parse", "main") == main_at_start,
        "main is untouched",
        "the tool never merges the project branch; that is the operator's job",
    )
    # Since the branch point, not from the root. The repo now carries the
    # config as an ordinary tracked file, so the absolute count includes
    # commits that have nothing to do with stages.
    commits = int(git(repo, "rev-list", "--count", f"{main_at_start}..{BRANCH}"))
    if live:
        check(
            commits > 1,
            f"{BRANCH} carries a squashed commit per landed stage",
            git(repo, "log", "--oneline", BRANCH),
        )
    else:
        check(
            commits == len(STAGES),
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
        help="Use the real planner, reviewer, and endpoint. The executor stays "
        "stubbed, so this exercises planning and review without generating "
        "code. Costs money.",
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

    root = Path(tempfile.mkdtemp(prefix="code-gantry-smoke-"))
    print(f"sandbox: {root}\n")

    try:
        repo = build_repo(root)
        (root / "bin").mkdir(parents=True, exist_ok=True)
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
        # No `--slug`: the config names its own work dir, and where the
        # config goes is the second argument rather than a project name.
        config = repo / "docs" / "code_gantry.yaml"
        cli(work, env, "init", str(repo / "docs" / "plan.md"), str(config))
        check(config.is_file(), "drafted a config")
        patch_config(config, live=args.live)

        # Commit it. The config now lives in the target repo, so an uncommitted
        # one leaves the tree dirty — and a run must begin from a known state
        # or its diffs mean nothing. This is also what gives the run a config
        # sha to record, which is what replaced `code-gantry approve`.
        for argv in (["add", "-A"], ["commit", "-qm", "code_gantry config"]):
            subprocess.run(["git", "-C", str(repo), *argv], check=True,
                           capture_output=True)

        print("\nconfig outside the repo is warned about, not blocked")
        # The pre-relocation layout. It has no commit to cite, so there is
        # nothing to check — and refusing would strand every project that has
        # not moved its config in yet.

        print("\nvalidate")
        checks = cli(work, env, "validate", str(config))
        check("[FAIL]" not in checks, "no blocking problems", checks)
        check(
            f"executor endpoint resolves from {EXECUTOR_API_BASE_VAR}" in checks
            and "127.0.0.1" not in checks.split("endpoint resolves")[1][:200],
            "resolved the executor endpoint from the environment",
            checks,
        )
        expected_model = LIVE_EXECUTOR_MODEL if args.live else "local-model"
        check(
            f"endpoint offers '{expected_model}'" in checks,
            "verified the model id against the endpoint's /v1/models",
            checks,
        )


        print("\nrun")
        main_at_start = git(repo, "rev-parse", "main")
        report = cli(work, env, "run", str(config), "--run-id", RUN_ID)
        check("complete" in report.lower(), "the run completed", report[-2000:])

        verify_outcome(work, repo, report, live=args.live,
                       main_at_start=main_at_start)

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
