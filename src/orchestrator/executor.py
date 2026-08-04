"""The executor: Aider for agent stages, a declared command for script stages.

Aider is invoked as a subprocess in headless mode, not imported as a library.
The orchestrator hands it a task and waits; Aider owns its own
edit → lint → test → fix cycle inside a single attempt.

Two things worth knowing:

- **File scoping is mandatory, not an optimisation.** Handing a local model an
  unscoped repository means the repo map alone consumes the context window
  before the task is stated. Every agent stage declares what it may edit and
  what it needs to read.
- **Flag names are unverified against a live Aider.** The spec says to check
  them, and `preflight` does exactly that by parsing `aider --help`, because
  Aider's CLI surface changes between releases. `executor.extra_args` is the
  escape hatch when it has moved again.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.commands import CommandResult, CommandRunner
from orchestrator.config import ProjectConfig, Stage
from orchestrator.gitops import GitError
from orchestrator.globs import matches_any

# The flags we build. preflight checks each of these against `aider --help`
# so a release that renamed one fails validation instead of stage 1.
AIDER_FLAGS = [
    "--message",
    "--yes-always",
    "--no-stream",
    "--model",
    "--openai-api-base",
    "--test-cmd",
    "--auto-test",
    "--lint-cmd",
    "--map-tokens",
    "--file",
    "--read",
    "--no-gitignore",
    "--no-show-model-warnings",
    "--no-detect-urls",
    "--edit-format",
    "--model-metadata-file",
    "--chat-history-file",
    "--input-history-file",
    "--llm-history-file",
]

# A command, not a browser name: Python's webbrowser module treats an entry
# containing %s as a command line to run, so this consumes the URL and does
# nothing. Aider pairs --yes-always with prompts like "Open documentation url
# for more info?", and an unattended overnight run must not answer yes to that
# dozens of times.
NO_BROWSER = "/usr/bin/true %s"

# Aider makes its own commits, in a subprocess we do not drive. The
# orchestrator's own commits already pass `-c commit.gpgsign=false`, but that
# does nothing for Aider's, which inherit the operator's global git config.
# With signing on, every attempt tries to reach a GPG agent: fine while the
# passphrase is cached, and over a fourteen-hour run it will not stay cached —
# after which each attempt either fails or waits on a pinentry dialog nobody is
# there to answer.
#
# Injected through git's own environment-variable config mechanism rather than
# by editing the operator's config or the target repo's. There is nothing to
# remember to restore, nothing left behind if the run dies, and no change to
# how that repository behaves for anyone else. Aider's commits are squashed
# away on merge in any case, so nothing signed is being lost.
GIT_CONFIG_OVERRIDES = (
    ("commit.gpgsign", "false"),
    ("tag.gpgsign", "false"),
)


def _git_config_env() -> dict[str, str]:
    env = {"GIT_CONFIG_COUNT": str(len(GIT_CONFIG_OVERRIDES))}
    for index, (key, value) in enumerate(GIT_CONFIG_OVERRIDES):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env

# Aider exits 0 when the model's reply could not be turned into an edit. The
# attempt failed, and saying so here — rather than letting it surface two gates
# later as "the attempt produced no changes" — is the difference between telling
# the model its output was the wrong shape and telling it, falsely, that it
# produced nothing.
UNAPPLIED_EDIT_MARKERS = (
    "did not conform to the edit format",
    "reflections allowed, stopping",
)

# ...but Aider prints those markers when *any* block fails, including when
# others applied and were committed. An 18-site sweep applied 11 of them,
# committed three files, and was recorded as a failed attempt — sending the run
# round execute -> execute -> execute without ever reaching verify, landing more
# edits each pass and never testing them. Whether the attempt did *enough* is
# the scope guard's, the suite's and the reviewer's question; the wrapper's job
# is only to say whether it did anything.
APPLIED_EDIT_MARKER = "Applied edit to"

# Aider's client library requires *some* key for an `openai/`-prefixed model,
# even when the endpoint it is pointed at serves without auth. The `sk-` prefix
# satisfies any naive format check along the way.
PLACEHOLDER_API_KEY = "sk-no-key-required"


def _absolute(path: Path | str) -> str:
    """Any path handed to Aider, made absolute first.

    Aider runs with its working directory set to the target repository, so a
    relative path resolves *inside the repository under test*. That is how the
    history files, having just been moved out of the repo, landed straight back
    in it — as `projects/…` at the repo root, failing the scope gate exactly as
    before. Resolution happens here, against the orchestrator's own cwd, which
    is what run-relative paths like `projects/<slug>/…` are relative to.
    """
    return str(Path(path).expanduser().resolve())


_TOKENS_SENT = re.compile(r"Tokens:\s*([\d,.]+)([km]?)\s+sent", re.IGNORECASE)


def context_tokens_from_log(log: str) -> int:
    """How much context the executor actually held, from Aider's own report.

    Stage sizing is guesswork without this. Two stages that each edited "one
    file" differed by 3.4x in what the executor loaded — 14k tokens for a small
    leaf controller against 47k for a 1,935-line one — so a file count, which
    is what a planner can see, does not describe the constraint that decides
    whether a batch fits.

    The largest report wins. Aider prints one per exchange and a reflection
    produces several; what bounds the next stage is the high-water mark, not
    the last thing it happened to say.
    """
    biggest = 0
    for amount, scale in _TOKENS_SENT.findall(log or ""):
        try:
            value = float(amount.replace(",", ""))
        except ValueError:  # pragma: no cover - defensive
            continue
        value *= {"k": 1_000, "m": 1_000_000}.get(scale.lower(), 1)
        biggest = max(biggest, int(value))
    return biggest


@dataclass
class ExecutionResult:
    ok: bool
    log: str = ""
    timed_out: bool = False
    # Peak context Aider reported for this attempt, or 0 if it never said.
    context_tokens: int = 0
    results: list[CommandResult] = field(default_factory=list)
    # Aider ran and exited cleanly, but produced no edit because it could not
    # parse the model's reply. A different failure from a crash, and one the
    # retry should be told about precisely.
    unapplied_edit: bool = False
    # Reference files withheld to keep inside `max_read_lines`. Reported rather
    # than dropped quietly: a stage that behaves differently because it was
    # shown less than it declared must say so, or the next person debugging it
    # is reading a prompt the executor never received.
    dropped_reads: list[str] = field(default_factory=list)


def build_aider_argv(
    stage: Stage,
    cfg: ProjectConfig,
    prompt: str,
    history_dir: Path | None = None,
) -> list[str]:
    """Assemble the Aider invocation.

    The API key is deliberately absent: it goes through the environment, so it
    never appears in `ps` output or in our own run log.

    `history_dir` relocates Aider's own scratch files. It writes
    `.aider.chat.history.md` and `.aider.input.history` into the repository
    root by default, which no stage declares and which therefore fails the
    scope gate on the first attempt of every project. Pointing them at the
    attempt directory keeps the target repository clean — it receives product
    code and plan revisions, nothing else — and makes the model's actual
    conversation a per-attempt artifact worth reading afterwards.
    """
    ex = cfg.executor
    argv = [
        "aider",
        "--message",
        prompt,
        "--yes-always",
        "--no-stream",
        "--model",
        ex.model,
        # Aider adds `.aider*` to .gitignore by default. No stage declares that
        # file, so it fails the scope gate on the first attempt of every
        # project. The orchestrator owns this repository's git; Aider does not
        # need to tidy it.
        "--no-gitignore",
        # A model warning becomes "Open documentation url for more info?",
        # which --yes-always answers for us.
        "--no-show-model-warnings",
        # And so does any other URL Aider happens to see. The one it sees most
        # is its own: this repository is large enough that the startup banner
        # prints a mono-repo warning citing aider.chat/docs/faq.html, Aider
        # offers to add that URL to the chat, and --yes-always accepts — so it
        # scrapes its own documentation over the network, unattended, bounded
        # only by the attempt timeout. Two of the nine attempt timeouts so far
        # end on "Scraping https://aider.chat/docs/faq.html…" and nothing else.
        #
        # NO_BROWSER does not help: that stops a browser opening, not a fetch.
        "--no-detect-urls",
    ]

    if ex.edit_format:
        argv += ["--edit-format", ex.edit_format]

    if ex.model_metadata_file:
        argv += ["--model-metadata-file", _absolute(ex.model_metadata_file)]

    if history_dir is not None:
        argv += [
            "--chat-history-file", _absolute(history_dir / "aider-chat.md"),
            "--input-history-file", _absolute(history_dir / "aider-input.txt"),
            "--llm-history-file", _absolute(history_dir / "aider-llm.txt"),
        ]

    api_base = ex.resolve_api_base()
    if api_base:
        argv += ["--openai-api-base", api_base]

    test_command = _auto_test_command(stage, cfg) if ex.auto_test else None
    if test_command:
        argv += ["--test-cmd", test_command, "--auto-test"]

    if ex.lint_command:
        argv += ["--lint-cmd", ex.lint_command]

    argv += ["--map-tokens", str(ex.map_tokens)]

    for glob in stage.edit_files:
        argv += ["--file", glob]
    # Conventions first, and through `--read` rather than the prompt. Aider
    # scans the *user message* for anything path-shaped and offers to attach
    # it, which `--yes-always` accepts; the repository's agent-facing document
    # is dense with paths, so putting its text in `--message` attached
    # `config/routes.rb`, `db/structure.sql` and the rest — 258,854 tokens
    # against a 229,376 limit, the request refused, and every attempt exiting
    # in three seconds having written nothing. `check_for_file_mentions` runs
    # on the message and on the model's reply and nowhere else, so a file
    # supplied here is rendered as context and never scanned. There is no flag
    # to turn the behaviour off — `--detect-urls` covers URLs only.
    #
    # Outside `max_read_lines`, unlike the stage's own reference files. That
    # budget exists to stop the planner's per-stage choices swamping the task;
    # these are the operator's standing context, the same on every stage, and
    # letting a large one evict the file the stage actually needs would trade
    # the wrong thing away.
    for path in _existing_agent_context(cfg):
        argv += ["--read", path]
    for glob in _within_read_budget(stage.read_files, cfg):
        argv += ["--read", glob]

    argv += list(ex.extra_args)
    return argv


class Executor:
    def __init__(self, cfg: ProjectConfig, runner: CommandRunner):
        self.cfg = cfg
        self.runner = runner

    def gather_context(
        self, stage: Stage
    ) -> tuple[list[tuple[str, str]], list[CommandResult]]:
        """Run the stage's context commands and collect their stdout.

        These are declared by the operator, never produced by a model. Their
        output flows into the prompt; nothing flows the other way.
        """
        collected: list[tuple[str, str]] = []
        results: list[CommandResult] = []
        for command in stage.context_commands:
            result = self.runner.run(command)
            results.append(result)
            collected.append((command, result.output))
        return collected, results

    def run_agent_stage(
        self, stage: Stage, prompt: str, history_dir: Path | None = None
    ) -> ExecutionResult:
        try:
            env = self._executor_env()
            argv = build_aider_argv(stage, self.cfg, prompt, history_dir=history_dir)
        except KeyError as e:
            # A missing key or endpoint variable. Failing here beats letting
            # Aider fail opaquely on auth, or calling the wrong endpoint.
            return ExecutionResult(ok=False, log=str(e.args[0]))
        kept = set(_within_read_budget(stage.read_files, self.cfg))
        result = self.runner.run_argv(
            argv,
            timeout=self.cfg.limits.aider_timeout_seconds,
            env=env,
        )
        outcome = _classify_execution(result)
        outcome.dropped_reads = [p for p in stage.read_files if p not in kept]
        return outcome

    def run_script_stage(self, stage: Stage) -> ExecutionResult:
        result = self.runner.run(stage.command or "")
        return ExecutionResult(
            ok=result.ok,
            log=result.output,
            timed_out=result.timed_out,
            results=[result],
        )

    def _executor_env(self) -> dict[str, str]:
        """Environment additions for the Aider subprocess.

        Aider talks to the local endpoint through the OpenAI-compatible
        variables, so a configured key env var is mapped onto OPENAI_API_KEY
        rather than passed as a flag: a key in argv shows up in `ps` output and
        in our own run log.

        `api_key_env` is optional because a local endpoint — llama-swap,
        llama.cpp, Ollama — serves without auth, so there is no key for an
        operator to name. Aider's client still refuses to make the call with
        none set at all, so a placeholder is supplied here. That is a
        client-side requirement, not the server's.
        """
        env: dict[str, str] = {"BROWSER": NO_BROWSER, **_git_config_env()}
        name = self.cfg.executor.api_key_env
        api_base = self.cfg.executor.resolve_api_base()

        if name:
            if name not in os.environ:
                raise KeyError(
                    f"executor.api_key_env names {name}, which is not set in "
                    "the environment"
                )
            env["OPENAI_API_KEY"] = os.environ[name]
        elif api_base:
            # Only when an api_base redirects us somewhere local. With no
            # api_base this is the real OpenAI endpoint, where a placeholder
            # would turn a legible "you set no key" into a puzzling 401.
            env["OPENAI_API_KEY"] = PLACEHOLDER_API_KEY

        if api_base:
            env["OPENAI_API_BASE"] = api_base
        return env


def _classify_execution(result) -> ExecutionResult:
    """Did the attempt apply anything, and did the editor complain?

    Both can be true at once, and that case is the common one on a multi-site
    stage: some blocks match, some do not. Only "complained and applied
    nothing" is a failed attempt.
    """
    complained = any(marker in result.output for marker in UNAPPLIED_EDIT_MARKERS)
    applied = APPLIED_EDIT_MARKER in result.output
    unapplied = result.ok and complained and not applied
    return ExecutionResult(
        ok=result.ok and not unapplied,
        log=result.output,
        timed_out=result.timed_out,
        results=[result],
        unapplied_edit=unapplied,
        context_tokens=context_tokens_from_log(result.output),
    )


def _read_lines(path: str, cfg: ProjectConfig) -> int | None:
    """Lines in a reference file, or None when it cannot be counted.

    None covers a glob, a path outside the repo, a binary blob — anything whose
    size is not a plain fact. Callers must not substitute a number for it.
    """
    if any(ch in path for ch in "*?["):
        return None
    try:
        target = cfg.target_repo / path
        if not target.is_file():
            return None
        return sum(1 for _ in target.open("rb"))
    except OSError:  # pragma: no cover - unreadable file behaves as uncountable
        return None


def _existing_agent_context(cfg: ProjectConfig) -> list[str]:
    """The agent-facing documents that are actually present.

    The defaults name two and most projects keep one, so an unconditional pass
    would hand Aider a path that does not resolve — which it reports as a
    warning and then offers to create, a prompt `--yes-always` would accept.

    Not deduplicated by content the way the planner's copy is. That dedup
    exists because both documents are rendered into one prompt; here they are
    file arguments, and Aider is the thing that decides what to do with two
    paths naming the same bytes.
    """
    root = Path(cfg.target_repo)
    return [p for p in cfg.effective_agent_context if (root / p).is_file()]


class ExcerptError(Exception):
    """A declared range could not be read.

    Loud, and it did not used to be. The old policy skipped an unreadable range
    on the reasoning that the instruction is the authority and an excerpt is
    only help — which held exactly as long as the instruction also carried the
    code. It no longer does: the planner authors none, so the excerpt *is* the
    code, and skipping one hands the executor an instruction referring to lines
    it was never shown. A payload that fails must fail the stage.
    """


def resolve_excerpts(
    stage,
    cfg: ProjectConfig,
    git=None,
    sha: str = "",
) -> list[tuple[str, str]]:
    """Read each declared range, returning (label, numbered text) pairs.

    Numbered, for the same reason the planner's own reads are: a line the
    executor is told to match is checkable against a number and not against a
    recollection.

    Read at `sha` when one is given, and the caller in the loop always gives
    one. Line numbers are the least stable identifier there is, and the state a
    range was chosen against is not the state it is read against: on a rework
    the executor's own prior attempt has already moved the lines. Reading at the
    stage's start sha puts the executor on the same baseline as the reviewer's
    diff and the planner's revision block, so all three describe one tree.

    Charged against `max_read_lines`, the same budget whole reference files
    come out of — this exists so a large file can contribute the part that
    matters, not so it can contribute more than a small one. Ranges are clipped
    rather than dropped, because a clipped range still carries its beginning,
    where a dropped file carries nothing.
    """
    budget = cfg.executor.max_read_lines
    remaining = None
    if budget is not None:
        # One budget for all reference material, not one each. Excerpts exist
        # so a file too large to send whole can still contribute the part that
        # matters — not so a stage can carry twice what the operator allowed by
        # splitting it across two fields. Reference files are counted first
        # because they were already chosen and trimmed by the time we get here.
        kept = _within_read_budget(stage.read_files, cfg)
        spent = sum(n for n in (_read_lines(p, cfg) for p in kept) if n)
        remaining = max(budget - spent, 0)
    out: list[tuple[str, str]] = []

    for ex in getattr(stage, "read_excerpts", []) or []:
        if remaining is not None and remaining <= 0:
            break
        if git is not None and sha:
            # `git show <sha>:<path>` on a symlink returns the link's *target* —
            # a path, not the file it names — so an excerpt of one would be a
            # numbered line of nonsense presented as the code to edit. Ask
            # before reading rather than guessing from the content.
            if git.is_symlink(sha, ex.path):
                raise ExcerptError(
                    f"{ex.path!r} is a symlink at {sha[:12]}; an excerpt of it "
                    "would carry the link's target, not the file. Point the "
                    "range at the file it resolves to."
                )
            try:
                body = git.show_file(sha, ex.path).splitlines()
            except GitError as exc:
                raise ExcerptError(
                    f"cannot read {ex.path!r} at {sha[:12]} for an excerpt of "
                    f"lines {ex.start}-{ex.end or 'end'}: {exc}"
                ) from exc
        else:
            target = Path(cfg.target_repo) / ex.path
            try:
                body = target.read_text(errors="replace").splitlines()
            except OSError as exc:
                raise ExcerptError(
                    f"cannot read {ex.path!r} for an excerpt of lines "
                    f"{ex.start}-{ex.end or 'end'}: {exc}"
                ) from exc
        first = max(ex.start or 1, 1)
        last = min(ex.end or len(body), len(body))
        if first > last:
            continue
        chosen = body[first - 1 : last]
        if remaining is not None and len(chosen) > remaining:
            chosen = chosen[:remaining]
        if not chosen:
            continue
        if remaining is not None:
            remaining -= len(chosen)
        label = f"{ex.path}:{first}-{first + len(chosen) - 1}"
        # Say so when the range is not the range that was asked for. The
        # executor is the participant that would otherwise act on a partial
        # quotation believing it whole, and since the planner no longer writes
        # code there is nothing else in the prompt to contradict it. Kept in
        # the label rather than a log line because the label travels with the
        # lines into the prompt, and the operator's copy is the artifact.
        if len(chosen) < last - first + 1:
            label += f" (clipped from {first}-{last} by max_read_lines)"
        if ex.note:
            label += f" — {ex.note}"
        numbered = "\n".join(
            f"{first + i:>5}  {line}" for i, line in enumerate(chosen)
        )
        out.append((label, numbered))
    return out


def _within_read_budget(read_files: list[str], cfg: ProjectConfig) -> list[str]:
    """Trim reference files to `max_read_lines`, largest first.

    Largest first because dropping the biggest recovers the most context per
    file lost, and because the small ones are likelier to be the base class or
    the routes file the stage actually needs — the big ones are the worked
    examples that accumulate as a run proceeds.

    Order is preserved among the survivors; only membership changes.

    A file whose size cannot be established is kept. The alternative is to
    invent a number for it, and inventing zero admits anything while inventing
    a large one drops the routes file that was declared as a glob. This is the
    same rule the auto-test paths follow: act on evidence, not on its absence.
    """
    budget = cfg.executor.max_read_lines
    if budget is None or not read_files:
        return list(read_files)

    sizes = {p: _read_lines(p, cfg) for p in read_files}
    total = sum(n for n in sizes.values() if n is not None)
    if total <= budget:
        return list(read_files)

    # Drop measurable files, biggest first, until the rest fit.
    dropped: set[str] = set()
    for path, _ in sorted(
        ((p, n) for p, n in sizes.items() if n is not None),
        key=lambda item: item[1],
        reverse=True,
    ):
        if total <= budget:
            break
        dropped.add(path)
        total -= sizes[path] or 0
    return [p for p in read_files if p not in dropped]


def _runnable(path: str, stage: Stage, cfg: ProjectConfig) -> bool:
    """Is this declared path worth putting in front of the inner loop?

    Dropped only on positive evidence that it is not: a readable repository
    that does not contain it, and a stage that cannot create it. A stage can
    create it if the path is inside what it is allowed to write, or if it is
    obliged to add tests and so may write specs it was not handed by name.

    The repository check is deliberately a precondition rather than an
    assumption. If `target_repo` cannot be read there is no evidence either
    way, and inventing some by treating every path as absent would silently
    switch the inner loop off for a whole project on the strength of a check
    that never ran.
    """
    if not cfg.target_repo.is_dir():
        return True
    if (cfg.target_repo / path).exists():
        return True
    return stage.require_new_tests or matches_any(path, stage.edit_files)


def _auto_test_command(stage: Stage, cfg: ProjectConfig) -> str | None:
    """The command Aider runs itself, after applying its edits.

    Built from the stage's declared `test_paths` and from nothing else. Never
    the project's full suite: Aider has no notion of `edit_files`, so faced with
    a red spec outside the stage it will edit that spec, and a full suite gives
    it three and a half minutes per pass to do so. Scoped, the inner loop is
    seconds and confined to the specs the stage claims to prove.

    Resolution differs from the verify layer's on purpose. A glob is a question
    about files that exist, so an unmatched one is dropped — left in, it reaches
    the runner as a literal and kills the loop. A plain path that does not exist
    yet is kept *only when this stage could plausibly create it*: either it is
    inside `edit_files`, or the stage is required to add tests. Aider runs this
    after its edits, so a spec the stage was told to write will be there.

    Kept unconditionally, as it was, a path the stage cannot create is a command
    that can never pass. Aider reads the runner's "no such file" as a failing
    test and spends its reflections repairing a file that will never exist.
    Observed live: a planner that cannot grep the spec tree declared
    `spec/requests/godata_spec.rb` and `spec/controllers/godata_controller_spec.rb`
    for a repository containing no godata specs at all, and the attempt hung on
    a 77,000-token fix. Verify dropped the same two paths and ran the whole
    suite, which passed — so the edit was right the entire time and only the
    inner loop was chasing a phantom.

    No runnable paths means no inner loop, rather than one that cannot pass.
    """
    template_base = cfg.auto_test_command or cfg.scoped_test_command
    if not template_base:
        return None

    paths: list[str] = []
    for raw in stage.test_paths:
        path = (raw or "").strip()
        if not path:
            continue
        if any(ch in path for ch in "*?["):
            paths.extend(
                sorted(str(m.relative_to(cfg.target_repo)) for m in cfg.target_repo.glob(path))
            )
        elif _runnable(path, stage, cfg):
            paths.append(path)

    if not paths:
        return None

    template = template_base
    if (
        cfg.auto_test_command is None
        and cfg.directory_test_command
        and any((cfg.target_repo / p).is_dir() for p in paths)
    ):
        template = cfg.directory_test_command
    return template.format(paths=" ".join(paths))
