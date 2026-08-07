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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator import gates
from orchestrator.commands import CommandResult, CommandRunner
from orchestrator.config import ProjectConfig, Stage
from orchestrator.gitops import Git, GitError
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
    "--no-suggest-shell-commands",
    "--edit-format",
    "--model-metadata-file",
    "--chat-history-file",
    "--input-history-file",
    "--llm-history-file",
    "--reasoning-effort",
    "--no-check-model-accepts-settings",
    "--cache-prompts",
    "--cache-keepalive-pings",
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
_COST_SESSION = re.compile(
    r"Cost:\s*\$[\d.]+\s*message,\s*\$([\d.]+)\s*session", re.IGNORECASE
)
_CACHE_TOKENS = re.compile(
    r"([\d,.]+)([km]?)\s+cache\s+(write|hit)", re.IGNORECASE
)


def _scaled(amount: str, scale: str) -> float:
    try:
        value = float(amount.replace(",", ""))
    except ValueError:  # pragma: no cover - defensive
        return 0.0
    return value * {"k": 1_000, "m": 1_000_000}.get(scale.lower(), 1)


def cost_from_log(log: str) -> float:
    """What the attempt spent, from Aider's own report.

    Nothing priced the executor before this, because nothing needed to: a local
    model is free, and the measured economics the design rests on — the planner
    at 91% of tokens against the executor's 2.2% of prompt volume — took that
    for granted. A hosted executor invalidates both figures and no existing
    record would show it.

    Absent for an unpriced model rather than wrong: Aider returns the tokens
    report alone unless litellm knows `input_cost_per_token`, which is why no
    log in this project has ever carried a `Cost:` line. Zero therefore means
    "cost nothing", which for a local endpoint is the truth. An operator who
    wants a notional figure can put pricing in `model_metadata_file`, which is
    already passed through.

    The session figure, and the largest one. Both numbers Aider prints are
    cumulative within an invocation, so the session total after the last
    exchange is what the attempt cost.
    """
    return max(
        (float(m) for m in _COST_SESSION.findall(log or "")),
        default=0.0,
    )


def cache_tokens_from_log(log: str) -> dict[str, int]:
    """Cache writes and hits, when the provider reported any.

    This is what makes prompt caching a measurement rather than a belief. Aider
    adds these fields to the tokens line only when the provider actually
    cached, so their absence is a real answer: either caching is off, or it is
    on and achieving nothing.

    Summed rather than maxed, unlike the context figure. That one is a
    high-water mark of a single context; these accumulate across the exchanges
    of an attempt, and what an operator wants is the total written and the
    total reused.
    """
    totals = {"write": 0, "hit": 0}
    for amount, scale, kind in _CACHE_TOKENS.findall(log or ""):
        totals[kind.lower()] += int(_scaled(amount, scale))
    return totals


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
    # What the attempt spent, when the model was priced. Zero for a local
    # endpoint, which is the truth rather than a missing reading.
    cost_usd: float = 0.0
    # Cache writes and hits, when the provider reported any. The evidence that
    # `cache_prompts` is doing something, rather than the assumption that it is.
    cache_tokens: dict = field(default_factory=lambda: {"write": 0, "hit": 0})
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
    # Files Aider attached because the message or the model's reply named them.
    # An attach on the reply costs that reply its edits, and nothing else in
    # this result distinguishes that from a model that produced nothing.
    attached_files: list[str] = field(default_factory=list)


# Aider's own normalisation, lifted from `get_file_mentions` in the installed
# 0.86.2 source rather than recalled. Both are applied to a whitespace-delimited
# word before it is compared against a repository path: sentence punctuation
# comes off the end, quotes and emphasis off both ends.
_MENTION_TAIL = ",.!;:?"
_MENTION_WRAP = "\"'`*_"


def _shield_word(word: str, candidates: set[str]) -> str:
    """One word, with its punctuation put back around the rewritten path."""
    if not word:
        return word
    body = word.rstrip(_MENTION_TAIL)
    tail = word[len(body) :]
    inner = body.lstrip(_MENTION_WRAP)
    lead = body[: len(body) - len(inner)]
    core = inner.rstrip(_MENTION_WRAP)
    trail = inner[len(core) :]
    if core not in candidates:
        return word
    return f"{lead}./{core}{trail}{tail}"


def shield_path_mentions(
    text: str, tracked: Iterable[str], exempt: Iterable[str] = ()
) -> str:
    """Rewrite words Aider would read as a file request into words it will not.

    Keeping the conventions document out of `--message` fixed the document that
    broke a run. It did not fix the mechanism, because the mechanism is not
    about that document: the planner writes prose, prose names files, and
    `check_for_file_mentions` runs on the message regardless of who wrote it.
    Measured across this project's run history, 1,713 files were attached this
    way — roughly 30.8M tokens — led by a 453,480-byte lint-exclusion list that
    attached on all 117 executor inputs naming it, taking one stage from ~20k
    tokens a message to 137k.

    Nothing showed it. `base_coder.py:919` calls the scan on the message and
    discards its return, so unlike the reply scan it emits no "I added these
    files" line into the history, and nothing tells the planner that naming a
    file has a price. A guard is the only thing that can see it.

    `./` is the whole trick. Aider compares the word against the repo-relative
    path verbatim, so the prefix defeats the comparison, while the executor
    reads the same file it always did and the sentence is otherwise untouched.
    Two things are deliberately left alone: fenced blocks, which are quoted from
    the repository rather than authored and whose contents the executor is told
    to treat as current; and a bare unique basename, which Aider also matches
    but which `./` cannot fix without either naming a file that does not exist
    or expanding the path the planner chose. That residual is real and small —
    on the same history the full-path form outnumbered it 71 inputs to 5.
    """
    candidates = set(tracked) - set(exempt)
    if not candidates:
        return text

    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        out.append(
            "".join(
                piece if i % 2 else _shield_word(piece, candidates)
                for i, piece in enumerate(re.split(r"(\s+)", line))
            )
        )
    return "\n".join(out)


_ATTACH_QUESTION = "Add file to the chat?"


def attached_by_mention(chat_history: str) -> list[str]:
    """Files Aider attached because something named them, in order, deduped.

    Read from the chat history because it is the only record: the confirmation
    is a prompt_toolkit call that never reaches the captured stdout, and the
    scan on the *message* discards its own return value, so unlike the scan on
    the reply it does not even leave an "I added these files" line behind. 344
    attempts went by without this being visible anywhere an operator looked.

    Only an accepted attach counts. A declined one adds nothing, so Aider does
    not take the early return and the reply's edits apply normally — counting
    it would report a loss that did not happen.
    """
    found: list[str] = []
    lines = chat_history.splitlines()
    for i, line in enumerate(lines):
        if _ATTACH_QUESTION not in line or not line.rstrip().endswith(": y"):
            continue
        if i == 0:
            continue
        named = lines[i - 1].lstrip("> ").strip()
        if named and named not in found:
            found.append(named)
    return found


def build_aider_argv(
    stage: Stage,
    cfg: ProjectConfig,
    prompt: str,
    history_dir: Path | None = None,
    tracked: list[str] | None = None,
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
    message = prompt
    if tracked:
        # Files already handed to Aider are excluded from
        # `get_addable_relative_files`, so it cannot re-add them and marking
        # them up would only clutter the sentence naming the stage's own work.
        scoped = list(stage.edit_files) + list(stage.read_files)
        exempt = [p for p in tracked if matches_any(p, scoped)]
        exempt += _existing_agent_context(cfg)
        message = shield_path_mentions(prompt, tracked, exempt)

    argv = [
        "aider",
        "--message",
        message,
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
        # Aider's own system prompt asks the model to "suggest any shell
        # commands the user might want to run", listing "if you added a test,
        # suggest how to run it" among the examples (`coders/shell.py`). On a
        # stage that requires tests the model complies, names the test runner,
        # and Aider's scan of that reply attaches the runner and returns before
        # applying the edits the same reply carried. The suggestion is declined
        # anyway — that confirm is `explicit_yes_required`, which --yes-always
        # answers no — and the run's own --test-cmd already runs the suite.
        #
        # Removed rather than argued with. A line in our prompt asking the
        # model not to suggest commands would be asking for restraint against
        # an instruction that is not ours, and would lose.
        "--no-suggest-shell-commands",
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

    test_command = (
        gates.resolve_test_command(stage, cfg, for_loop=True) if ex.auto_test else None
    )
    if test_command:
        argv += ["--test-cmd", test_command, "--auto-test"]

    if ex.lint_command:
        argv += ["--lint-cmd", ex.lint_command]

    argv += ["--map-tokens", str(ex.map_tokens)]

    # Keepalive only alongside caching: pinging every five minutes to hold open
    # a cache that was never enabled is pure cost for nothing.
    if ex.reasoning_effort:
        # And tell Aider not to second-guess it. Aider checks its own model
        # metadata for `supports_reasoning_effort` and silently drops the flag
        # when the answer is no — which it was for a model whose provider
        # accepts the setting on a live call and names the valid values in the
        # error for an invalid one. Stale metadata is the wrong authority once
        # the operator has stated a value.
        argv += [
            "--reasoning-effort", ex.reasoning_effort,
            "--no-check-model-accepts-settings",
        ]

    if ex.cache_prompts:
        argv += ["--cache-prompts"]
        if ex.cache_keepalive_pings:
            argv += ["--cache-keepalive-pings", str(ex.cache_keepalive_pings)]

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
    def __init__(
        self, cfg: ProjectConfig, runner: CommandRunner, git: Git | None = None
    ):
        self.cfg = cfg
        self.runner = runner
        self.git = git

    def _tracked_paths(self) -> list[str] | None:
        """What the repository currently tracks, for the mention shield.

        Degrades to no shielding rather than failing the stage: this guard
        exists to save tokens, and a run whose git cannot list its own files
        has a larger problem than an over-attached prompt.
        """
        if self.git is None:
            return None
        try:
            return self.git.tracked_paths_now()
        except GitError:
            return None

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
            argv = build_aider_argv(
                stage,
                self.cfg,
                prompt,
                history_dir=history_dir,
                tracked=self._tracked_paths(),
            )
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
        outcome.attached_files = self._attachments(history_dir)
        return outcome

    def _attachments(self, history_dir: Path | None) -> list[str]:
        """What Aider attached, from the chat history it wrote for us.

        Best effort by design. `history_dir` is optional, the file may not
        exist if Aider died early, and none of that should fail an attempt that
        otherwise worked — this reports on the attempt, it does not judge it.
        """
        if history_dir is None:
            return []
        try:
            return attached_by_mention(
                (history_dir / "aider-chat.md").read_text(errors="replace")
            )
        except OSError:
            return []

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
        cost_usd=cost_from_log(result.output),
        cache_tokens=cache_tokens_from_log(result.output),
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
