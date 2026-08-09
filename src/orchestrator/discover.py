"""Repo inspection for `init`.

**Discovery splits along the same line as the planner's write permissions.**
Executable fields come from deterministic repo inspection, never from a model:
dependency manifests for framework and library versions, tool-version files for
the runtime, `bin/` and CI config for the canonical test invocation,
`docker-compose.yml` for services. Free, fast, reproducible, and auditable.

Every discovered field carries a provenance comment, so reviewing the draft is a
check of reasoning rather than a check of values — the operator can see *why*
`test_command` says what it says and judge whether the inference was right.

Nothing here writes a config the operator has not read: `init` emits a draft,
`approve` records that a human read it.
"""

from __future__ import annotations

from pathlib import Path

# Where a project's canonical test invocation tends to live, most specific
# first. A repo-local wrapper script beats a guess at the underlying tool.
_TEST_SCRIPT_CANDIDATES = (
    "bin/test",
    "bin/rspec",
    "script/test",
    "bin/ci",
    "Makefile",
)

_CI_CONFIG_GLOBS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".circleci/config.yml",
    ".gitlab-ci.yml",
)


def derive_target_repo(plan_doc: Path) -> Path | None:
    """Walk up from the plan document to the git root.

    The plan document must live inside the target repo: the repo copy is what
    the orchestrator operates against and what the planner revises. Deriving the
    repo from the document's location rather than asking makes that structural.
    """
    current = plan_doc.resolve().parent
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def draft_config(repo: Path, plan_rel: str) -> tuple[str, list[str]]:
    """Return (yaml text, notes for the operator)."""
    notes: list[str] = []
    lines: list[str] = []

    lines.append("# Drafted by `orchestrator init`. Read every line before approving —")
    lines.append("# each command here runs unattended, and the denylist is a backstop,")
    lines.append("# not a substitute for reading.")
    lines.append("")
    lines.append("# Documentation only: these commands assume this machine's Docker,")
    lines.append("# runtime, and paths. `validate` checks this host, not the config in")
    lines.append("# the abstract.")
    # Commented, not set. This file is committed to the repository it
    # describes and read by everyone who checks it out, so `init` writing one
    # machine's name into it is a real identifier reaching a tracked file
    # without anyone choosing to put it there. The field is documentation —
    # uncomment it if a reader should know which host these commands assume.
    lines.append(f'# host: "{_hostname()}"')
    lines.append("")
    # No `target_repo`. This file now lives in the repository it describes and
    # is committed there, so an absolute path would be one machine's home
    # directory written into a file other people check out — and it would be a
    # second name for something already known, free to disagree with where the
    # config actually is. Derived by walking up to the git root instead.
    lines.append("# target_repo is not set: it is the git repository this file")
    lines.append("# sits in, found by walking up. Set it only to point somewhere")
    lines.append("# else, such as a worktree.")
    lines.append("base_ref: main")
    lines.append("project_branch: refactor/CHANGE-ME")
    lines.append("")
    lines.append(f"plan_root: {plan_rel}")
    lines.append("")

    setup, setup_note = _discover_setup(repo)
    if setup:
        lines.append("# Runs before the executor and again before verify, so it must be")
        lines.append("# idempotent — `validate` checks that by running it twice.")
        lines.append(f'setup_command: "{setup}"')
        lines.append(f"# ^ {setup_note}")
        notes.append(f"setup_command inferred from {setup_note}")
    else:
        lines.append("# No container or service setup detected. Add one if the tests")
        lines.append("# need a database, a built image, or a loaded schema.")
        lines.append("# setup_command: \"\"")
        notes.append("no setup_command inferred — check whether the tests need one")
    lines.append("")

    test, test_note = _discover_test(repo)
    lines.append(f'test_command: "{test}"')
    lines.append(f"# ^ {test_note}")
    lines.append(f'full_test_command: "{test}"')
    lines.append("# ^ same by default. If the project has a faster subset for")
    lines.append("#   iteration, put that in test_command and leave the whole suite")
    lines.append("#   here — full_test_command gates every merge.")
    notes.append(f"test_command inferred from {test_note}")
    lines.append("")
    lines.append("# Optional. When set, per-stage iteration runs only the specs the")
    lines.append("# stage touched. {paths} is filled from the stage diff — this is how")
    lines.append("# the planner influences the test run without authoring shell.")
    lines.append(f'# scoped_test_command: "{test} {{paths}}"')
    lines.append("")
    lines.append("full_suite_on_approval: true")
    lines.append("")

    lines.append("executor:")
    lines.append("  # The id the API itself uses. No routing prefix: the executor")
    lines.append("  # calls the provider directly rather than through a router, so")
    lines.append("  # anything prepended here is sent as part of the model name.")
    lines.append('  model: "<model-id-from-/v1/models>"')
    lines.append("  # Both optional and both about where the endpoint is, not what")
    lines.append("  # it is. Omit api_base_env to talk to the provider directly;")
    lines.append("  # omit api_key_env only for an endpoint that serves without")
    lines.append("  # auth. A hostname is an infrastructure fact and this file is")
    lines.append("  # tracked, so the values live in the environment.")
    lines.append('  # api_base_env: "ORCHESTRATOR_EXECUTOR_API_BASE"')
    lines.append('  api_key_env: "OPENAI_API_KEY"')
    lines.append("")
    lines.append("planner:")
    lines.append('  model: "claude-opus-5"')
    lines.append('  api_key_env: "ANTHROPIC_API_KEY"')
    lines.append("  # The cached prefix expires after this. \"1h\" is the maximum")
    lines.append("  # Anthropic offers; a stage longer than the window means the")
    lines.append("  # planner's prefix is never reused, and a cache write costs more")
    lines.append("  # than no cache at all. Watch the percentage in report.md.")
    lines.append('  cache_ttl: "1h"')
    lines.append("  # Appended to the planner's system prompt, for policy specific to")
    lines.append("  # this project. Inline rather than a file path: approval hashes")
    lines.append("  # this file, and guidance elsewhere could change the planner's")
    lines.append("  # behaviour after you approved it.")
    lines.append("  # guidance: |")
    lines.append("  #   Prefer many small stages to few large ones.")
    lines.append("  #   Stage 4 needs AWS credentials this run does not have — defer")
    lines.append("  #   it and continue, rather than stopping the run.")
    lines.append("")
    lines.append("reviewer:")
    lines.append('  model: "gpt-5.6-sol"')
    lines.append('  api_key_env: "OPENAI_API_KEY"')
    lines.append("")

    lines.append("# Applied to every stage the planner derives. This is where the")
    lines.append("# executable half of a stage comes from — the planner cannot author")
    lines.append("# any of it.")
    lines.append("stage_defaults:")
    lines.append("  preconditions: []")
    lines.append("  context_commands: []")
    lint, lint_note = _discover_lint(repo)
    if lint:
        # In `checks`, not on the executor: the loop runs these after every
        # batch of edits and commits what they rewrite, so a fixing linter
        # corrects the model's work while the file is still in front of it.
        lines.append(f'  checks: ["{lint}"]')
        lines.append(f"  # ^ {lint_note}")
        notes.append(f"lint command inferred from {lint_note}, put in checks")
    else:
        lines.append("  checks: []")
    lines.append("  require_new_tests: false")
    lines.append("  review: true")
    lines.append("")

    lines.append("limits:")
    lines.append("  max_test_retries: 3            # per stage-revision")
    lines.append("  max_rework_retries: 2          # per stage-revision")
    lines.append("  max_planner_interventions: 12  # global across the run")
    lines.append("  max_stages: 60")
    lines.append("  command_timeout_seconds: 3600")
    lines.append("  wall_clock_hours: 14")
    lines.append("")

    for note in _stack_notes(repo):
        notes.append(note)

    notes.append("project_branch is a placeholder — set it before validating")
    return "\n".join(lines) + "\n", notes


def _hostname() -> str:
    import socket

    return socket.gethostname()


def _discover_setup(repo: Path) -> tuple[str | None, str]:
    compose = None
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml"):
        if (repo / name).is_file():
            compose = name
            break
    if not compose:
        return None, ""

    services = _compose_services(repo / compose)
    backing = [s for s in services if s in ("postgres", "db", "mysql", "redis", "mongo")]
    parts = ["docker compose build"]
    if backing:
        parts.insert(0, f"docker compose up -d {' '.join(backing)}")
    return " && ".join(parts), (
        f"{compose}"
        + (f"; backing services {', '.join(backing)}" if backing else "; no backing services detected")
    )


def _compose_services(path: Path) -> list[str]:
    """Top-level service names, read textually.

    Deliberately not a YAML parse: compose files use anchors and merge keys that
    a naive load mangles, and all that is needed here is a list of names to put
    in a comment the operator will read.
    """
    services: list[str] = []
    in_services = False
    for raw in path.read_text().splitlines():
        if raw.startswith("services:"):
            in_services = True
            continue
        if in_services:
            if raw and not raw[0].isspace():
                break
            if raw.startswith("  ") and not raw.startswith("    ") and raw.strip().endswith(":"):
                services.append(raw.strip().rstrip(":"))
    return services


def _discover_test(repo: Path) -> tuple[str, str]:
    for candidate in _TEST_SCRIPT_CANDIDATES:
        path = repo / candidate
        if path.is_file():
            if candidate == "Makefile":
                if "test:" in path.read_text():
                    return "make test", "a `test` target in Makefile"
                continue
            return candidate, f"the repo-local script {candidate}"

    for pattern in _CI_CONFIG_GLOBS:
        for path in sorted(repo.glob(pattern)):
            command, line = _test_line_from_ci(path)
            if command:
                rel = path.relative_to(repo).as_posix()
                return command, f"{rel}:{line}"

    if (repo / "Gemfile").is_file():
        return "bundle exec rspec", "a Gemfile, with no wrapper script found"
    if (repo / "pyproject.toml").is_file():
        return "pytest", "a pyproject.toml, with no wrapper script found"
    if (repo / "package.json").is_file():
        return "npm test", "a package.json, with no wrapper script found"

    # No manifest, but a conventional test layout is still a strong signal.
    for directory in ("tests", "test", "spec"):
        path = repo / directory
        if not path.is_dir():
            continue
        if any(path.rglob("test_*.py")) or any(path.rglob("*_test.py")):
            return "python -m pytest", f"python tests found under {directory}/"
        if any(path.rglob("*_spec.rb")):
            return "bundle exec rspec", f"ruby specs found under {directory}/"

    return "CHANGE-ME", "nothing recognisable — set this by hand"


def _test_line_from_ci(path: Path) -> tuple[str | None, int]:
    """The first plausible test invocation in a CI config, with its line number.

    Textual on purpose: a CI file's structure varies by provider, and the goal
    is a citable starting point for the operator, not a faithful parse.
    """
    markers = ("rspec", "pytest", "go test", "npm test", "yarn test", "bin/test")
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        stripped = raw.strip().lstrip("-").strip()
        if not stripped or stripped.startswith("#"):
            continue
        if any(marker in stripped for marker in markers):
            if stripped.startswith("run:"):
                stripped = stripped[4:].strip()
            if "|" in stripped or stripped.endswith(":"):
                continue
            return stripped, number
    return None, 0


def _discover_lint(repo: Path) -> tuple[str | None, str]:
    if (repo / ".rubocop.yml").is_file():
        return "bundle exec rubocop -a", "a .rubocop.yml"
    if (repo / "ruff.toml").is_file() or (repo / ".ruff.toml").is_file():
        return "ruff check --fix", "a ruff config"
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file() and "[tool.ruff" in pyproject.read_text():
        return "ruff check --fix", "a [tool.ruff] section in pyproject.toml"
    return None, ""


def _stack_notes(repo: Path) -> list[str]:
    """Version facts worth telling the operator, from manifests only."""
    notes = []
    for name in (".tool-versions", ".ruby-version", ".python-version", ".nvmrc"):
        path = repo / name
        if path.is_file():
            notes.append(f"runtime pinned by {name}: {path.read_text().strip()[:80]}")

    gemfile = repo / "Gemfile"
    if gemfile.is_file():
        for line in gemfile.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("ruby ", "gem 'rails'", 'gem "rails"')):
                notes.append(f"Gemfile: {stripped[:80]}")
    return notes
