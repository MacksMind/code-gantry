"""Project configuration: schema, loading, and structural validation.

Two things make this file the centre of the design's safety story.

**The declarative/executable partition.** The planner may author declarative
fields — instruction text, globs, prose constraints, regexes. It may never
author an executable one. That is enforced here, as an allowlist on what the
planner's structured output is permitted to contain, not as a comment
somewhere. A field the planner may not set should be impossible for it to
return.

**The denylist.** Some commands are refused regardless of operator approval,
because a human skims a sixty-line YAML once, motivated to start a run.

Structural validation is everything checkable from the file alone. The checks
that execute something — clean tree, test command actually passes, Aider's
flags, endpoint reachability — live in `preflight`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError


StageKind = Literal["agent", "script"]

# Stage ids name directories and git branches, so they must not contain
# separators, traversal, or anything git rejects in a ref.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Fields the planner is allowed to author. Everything else in Stage is
# operator-only. `nodes` and `planner` both import this; it is the single
# definition of the partition.
# `kind` is deliberately absent: a `script` stage needs an operator-authored
# `command`, and with no static stage list there is nowhere for the operator to
# put one. Every planner-derived stage is an `agent` stage; mechanical
# transforms are expressed as an instruction to write and run a script, which
# Aider does inside its own edit loop.
PLANNER_WRITABLE_FIELDS = frozenset(
    {
        "id",
        "instruction",
        "edit_files",
        "read_files",
        # A path and two integers. Declarative in the strongest sense: there is
        # nothing here a planner could turn into an instruction to run.
        "read_excerpts",
        "constraints",
        "acceptance",
        "forbidden_patterns",
        "must_not_remain",
        "test_paths",
        # Declarative, and one-way: it can raise the requirement, never waive
        # the operator's. What counts as a test file stays in operator config,
        # so this asks for coverage without reaching anything executable.
        "require_new_tests",
    }
)

# Refused regardless of operator approval. Each entry is (pattern, why).
#
# These are the moves that are catastrophic rather than merely wrong, and that
# a tired operator would not notice in a config review. Deliberately narrow:
# a denylist that fires on legitimate commands gets disabled.
DENYLIST: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bgit\s+push\b"),
        "the orchestrator must never push; the outer merge is the operator's",
    ),
    (
        re.compile(r"\bgit\s+(checkout|switch)\b"),
        "changing branches from inside a command would break the stage's "
        "branch identity; the orchestrator manages checkout itself",
    ),
    (
        re.compile(r"\bgit\s+merge\b"),
        "merging from inside a command bypasses the review gate",
    ),
    (
        re.compile(r"\bgit\s+(reset|clean)\b"),
        "resetting from inside a command would destroy the stage baseline the "
        "scope guard and the reviewer both measure against",
    ),
    (
        re.compile(r"\bgem\s+install\b"),
        "installing outside the bundle mutates the host's global gems; use "
        "bundle install",
    ),
    (
        re.compile(r"\b(cap|kubectl|terraform|serverless|fly|heroku)\b"),
        "deployment is out of scope and must not happen unattended",
    ),
    (
        re.compile(r"\bsudo\b"),
        "nothing here needs elevated privileges",
    ),
    (
        re.compile(r"\brm\s+-[a-zA-Z]*[rf]"),
        "recursive or forced deletion is never needed in a declared command",
    ),
)


class ConfigError(Exception):
    """Carries every problem found, not just the first, so one `validate` run
    is enough to fix the file."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("\n".join(f"- {p}" for p in problems))


class _Strict(BaseModel):
    """Rejects unknown keys. A misspelled config key that silently does
    nothing is worse than an error — the operator would believe a gate was
    active when it was not."""

    model_config = ConfigDict(extra="forbid")


class _EndpointConfig(_Strict):
    """Where a model role is reached, and how that address is supplied.

    `api_base_env` exists because a hostname is an infrastructure fact rather
    than a project decision. `projects/<slug>/config.yaml` is a tracked file
    that gets hashed for approval; a Spark hostname, an internal gateway, or a
    private port has no business in it.

    Naming a variable rather than interpolating one into a string is
    deliberate. General `${VAR}` substitution would reach command fields too,
    and then `test_command: "${CMD}"` would let the denylist scan a harmless
    literal while something else entirely ran. Restricting the mechanism to a
    single non-command field keeps every command in an approved config exactly
    what the operator read.
    """

    api_base: str | None = None
    api_base_env: str | None = None

    def resolve_api_base(self) -> str | None:
        """The address, reading the environment if that is where it lives.

        Deliberately not resolved at load time: `status` and `resume` load a
        config to read a report, and failing that on a machine that never
        exports the variable would be gratuitous. Preflight checks it, so a
        missing export fails `validate` rather than stage 1.
        """
        if self.api_base_env:
            value = os.environ.get(self.api_base_env)
            if not value:
                raise KeyError(
                    f"api_base_env names {self.api_base_env}, which is not set "
                    "in the environment"
                )
            return value
        return self.api_base


class ExecutorConfig(_EndpointConfig):
    model: str
    # Which editor runs a stage. `aider` drives the subprocess; `openai` drives
    # the in-process loop against the Responses API.
    #
    # Defaulting to `aider` while both exist is the whole point of having the
    # switch: the new path can be run against a real project, on real stages,
    # before anything is deleted. A rewrite of the component whose failure
    # modes are the best documented in this repository should not become the
    # only option on the strength of its unit tests.
    provider: Literal["aider", "openai"] = "aider"
    api_key_env: str | None = None
    # Passed to aider as `--lint-cmd`, which is narrower than it reads. Aider's
    # linter calls `filename_to_lang` first and returns before consulting this
    # command whenever the file's language cannot be named — grep-ast has no
    # parser for ERB, YAML, Haml or Markdown, so those are never linted at all,
    # whatever this says. Prefixing a language does not help either: the lookup
    # is `self.languages.get(lang)` with `lang` still None.
    #
    # So it suits a formatter for a recognised source language — `rubocop -a`,
    # `ruff check --fix`, which is what `discover` infers — and cannot carry a
    # guarantee that has to hold for every file. Trailing whitespace was one
    # such guarantee; it lives in `advance` instead.
    lint_command: str | None = None
    # Aider's default varies by model and is usually right for hosted ones. A
    # local model frequently cannot produce a valid diff — the first real run
    # ended in "the LLM did not conform to the edit format" — and `whole` trades
    # tokens for reliability. Left unset so Aider's per-model default applies.
    edit_format: str | None = None
    # A path to Aider's own metadata JSON, passed through unchanged. litellm has
    # no entry for a local model id, so without this Aider guesses at the context
    # window and warns about it. A path rather than the values themselves: the
    # numbers describe the endpoint, not the project, and they are Aider's
    # schema to define rather than ours to mirror.
    model_metadata_file: str | None = None
    # Repo map off by default: stages declare the files they need, and an
    # unscoped map swamps a local model's context before the task is stated.
    map_tokens: int = 0
    # Aider's own default is off, and so is this. Caching is a property of the
    # endpoint rather than of the work: against a local server that prices
    # nothing and caches nothing it buys nothing and adds a keepalive ping
    # loop; against a hosted model it is most of the saving. The operator knows
    # which they have — this file already carries every other fact about where
    # the executor runs.
    # Passed to Aider as `--reasoning-effort`, which it forwards as the API
    # parameter of the same name. Unset by default: a local model that does not
    # reason has nothing to do with it, and Aider omits the flag entirely.
    reasoning_effort: str | None = None
    cache_prompts: bool = False
    # Aider pings at five-minute intervals to hold the cache open. A stage's
    # attempts are separated by a scoped suite and sometimes a full one, which
    # is long enough for a window to lapse between the two attempts that would
    # have shared it. Meaningless without `cache_prompts`, and ignored there.
    cache_keepalive_pings: int = 0
    # Ceiling on the total size of a stage's `read_files`, in lines. Unset means
    # no ceiling, which is the old behaviour.
    #
    # `read_files` is reference material the planner chooses, and its natural
    # instinct is to pass every already-converted file as a worked example. That
    # set grows with every landed stage. Measured on the first real project: by
    # the twelfth stage it was sending 4,636 lines to change six, 69,000 tokens
    # a call, and the same stage converted four of six sites and then three of
    # six — the task lost inside the reference material. Attempts took 561s and
    # 584s against Aider's hardcoded, unreachable 600s request timeout, so
    # whether a stage landed or looked like it hung came down to the generation
    # rate that minute.
    #
    # The controlled comparison, from the same run: stage 10 revision 0 carried
    # 2,818 lines of reads and stalled six times over three hours. The planner's
    # redraw passed a single read file, 39k tokens, and it landed in 120s.
    #
    # `edit_files` is never trimmed — that is the task, not context.
    max_read_lines: int | None = None
    # Let Aider run the project's tests inside its own edit loop, and try to
    # repair what fails. Off by default, and the default is load-bearing: the
    # orchestrator already runs the tests at a layer that knows about the
    # stage's scope and about suite flakes, and Aider's loop knows neither. On
    # the first real stage it turned a 90-second edit into a 609-second attempt
    # spent trying to fix two order-dependent specs outside the stage's box.
    auto_test: bool = False
    # Aider's flag surface changes between releases. This is the escape hatch
    # for correcting it without waiting on a code change.
    extra_args: list[str] = []

    # --- the in-process executor ----------------------------------------
    #
    # Unread while `provider` is the subprocess editor. Declared here rather
    # than when the loop lands so that the client, its tests and the config
    # move together — a value that appears in the same commit as its first
    # reader has nowhere to be wrong yet.

    # Turns *within* one edit cycle. The editor and the reader refuse past
    # their own budgets and the model reads those refusals; this is the
    # backstop for one that ignores them and keeps asking, which would
    # otherwise hold a stage open until the request timeout. Same role as
    # `AnthropicPlanner._max_tool_turns`.
    max_model_turns: int = 20
    # A file holding the executor's system prompt, replacing the built-in one.
    #
    # A path rather than the text, for the reason that cost more than the rule
    # itself: config holding a copy of a document is a copy that drifts, and
    # the copy is the one the pipeline reads. Read at the run's sha, like every
    # other document.
    #
    # Replaces rather than appends. Appending would leave two statements of the
    # tool contract in one prompt with no way to tell which the model followed,
    # and an operator who wants the default plus additions can start from the
    # default — `orchestrator prompts executor` prints it.
    system_prompt_file: str | None = None
    # The same read budget the planner and reviewer carry, written here as its
    # own settings rather than shared: the three ask different questions, and
    # whoever decides the executor needs a different budget should be able to
    # say so without silently moving the other two.
    max_read_lines_per_call: int = 400
    max_read_lines_total: int = 6000
    max_read_calls: int = 60
    # Complete edit → lint → commit → test passes before the attempt gives up
    # and hands what it has to the gate. Deliberately low: an attempt is now a
    # whole loop, and `max_test_retries` still bounds the attempts, so three
    # here multiplies rather than adds.
    max_cycles: int = 3
    request_timeout_seconds: float = 900.0
    # See `PlannerConfig.transport_retry_seconds`. The wait is bounded by our
    # own wall clock rather than by an SDK retry counter, which is what makes
    # including 429 safe here.
    transport_retry_seconds: float = 3600.0
    transport_retry_max_delay_seconds: float | None = 300.0
    invalid_request_retry_seconds: float = 300.0
    invalid_request_initial_seconds: float = 120.0
    invalid_request_factor: float = 1.5


class PlannerConfig(_EndpointConfig):
    provider: Literal["anthropic"] = "anthropic"
    model: str
    api_key_env: str = "ANTHROPIC_API_KEY"
    # Was hard-coded `high`, which is a tuning decision about a deployment
    # sitting in code the operator who owns the deployment cannot reach.
    # `high` remains the default so nothing changes for a project that never
    # chose. Values are the installed SDK's — OutputConfigParam.effort is
    # Literal["low","medium","high","xhigh","max"].
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Anthropic's ephemeral cache lasts about five minutes by default. Between
    # two planner calls sits a whole stage — an executor attempt, a scoped
    # suite, a review, a full suite — which on a large project is comfortably
    # longer than that, so the prefix expires before it is ever reused. "1h"
    # buys a longer window at a higher write cost; leave unset to take the
    # provider default.
    cache_ttl: str | None = None
    # Operator prose appended to the planner's system prompt, fixed for the
    # run. Inline rather than a path to a file: approval hashes this file's
    # bytes, and guidance living elsewhere could be rewritten after approval to
    # change how the planner behaves without invalidating anything.
    guidance: str | None = None
    request_timeout_seconds: float = 900.0
    max_retries: int = 2
    # How long a network outage should be survivable, in seconds. Zero turns
    # retrying off rather than being an unsupported value found out about
    # during an outage.
    #
    # Not `max_retries`, which is the SDK's and stays small. Both SDKs clamp
    # every wait at MAX_RETRY_DELAY = 8s, so their schedule is linear after
    # four retries and spanning fifteen minutes costs 116 retries at best and
    # 154 at worst — and a count is not a clock, since the same setting honours
    # `retry-after` on a 429 and could then wait for hours. The decisive
    # difference is that SDK retries log at DEBUG, so fifteen minutes of them
    # look exactly like a hung process in `run.log`. Observed twice: Wi-Fi
    # dropped, planner and reviewer failed within two seconds of each other,
    # and a fourteen-hour run ended waiting for a human to notice.
    #
    # The two compose. `max_retries` handles sub-second blips fast; this waits
    # out real outages slowly and says so.
    transport_retry_seconds: float = 3600.0
    # Capped per wait, which matters more than the total. An hour of budget
    # doubling from 1s makes the last sleep 26 minutes, so a provider that
    # recovers a minute into it goes unnoticed for 25 more — an hour of
    # coverage with half an hour of latency. At 300s the same hour buys twenty
    # attempts and notices recovery within five minutes, and the extra
    # attempts are free because a failed request costs nothing.
    transport_retry_max_delay_seconds: float | None = 300.0
    # A separate, much smaller budget for a 400 the provider returns on a
    # request that is not malformed. Deliberately five minutes and not an
    # hour: a genuinely bad request must still reach a human quickly with its
    # own message, which is the only thing that keeps retrying a 400 from
    # being the mistake it looks like. Two waits, two minutes then three.
    invalid_request_retry_seconds: float = 300.0
    invalid_request_initial_seconds: float = 120.0
    invalid_request_factor: float = 1.5
    # What the planner may look at, and how much of it. Absent means no tools:
    # the planner is handed the plan and a directory listing and asked to
    # reason from them, which is how it invented spec paths and mis-counted
    # call sites on the first real project.
    #
    # A ceiling is still needed with the tools present. Unbounded context
    # degraded both latency and accuracy for the executor, and there is no
    # reason the planner is immune.
    repo_access: bool = False
    max_read_lines_per_call: int = 400
    max_read_lines_total: int = 3000
    max_read_calls: int = 25
    # Semantic search over a Qdrant index, when the project maintains one.
    # Endpoints come from the environment because they carry a host name, which
    # is an identifiable infrastructure value and does not belong in a tracked
    # file.
    semantic_search: dict | None = None


class ReviewerConfig(_EndpointConfig):
    provider: Literal["openai"] = "openai"
    model: str
    api_key_env: str = "OPENAI_API_KEY"
    # Unset by default, which is what it has always been: the reviewer never
    # sent a reasoning parameter, so it ran at whatever the provider chose.
    # Picking a default here would silently change the gate's behaviour on
    # every project that never asked. Values from the installed openai
    # package's ReasoningEffort.
    effort: (
        Literal["minimal", "none", "low", "medium", "high", "xhigh", "max"] | None
    ) = None
    # No `prompt_cache_retention`. It was a chat-completions field, unset by
    # default and measured to change nothing against gpt-5.6-sol; the reviewer
    # now calls the Responses API, where the lifetime comes from
    # `prompt_cache_options.ttl` — fixed at 30m and currently the only value
    # the provider supports. Nothing here for an operator to choose, so the
    # setting is gone rather than accepted and ignored.
    request_timeout_seconds: float = 600.0
    max_retries: int = 2
    # See `PlannerConfig.transport_retry_seconds`; same reasoning,
    # same failure — both clients died to the same disconnection.
    transport_retry_seconds: float = 3600.0
    # Capped per wait, which matters more than the total. An hour of budget
    # doubling from 1s makes the last sleep 26 minutes, so a provider that
    # recovers a minute into it goes unnoticed for 25 more — an hour of
    # coverage with half an hour of latency. At 300s the same hour buys twenty
    # attempts and notices recovery within five minutes, and the extra
    # attempts are free because a failed request costs nothing.
    transport_retry_max_delay_seconds: float | None = 300.0
    # A separate, much smaller budget for a 400 the provider returns on a
    # request that is not malformed. Deliberately five minutes and not an
    # hour: a genuinely bad request must still reach a human quickly with its
    # own message, which is the only thing that keeps retrying a 400 from
    # being the mistake it looks like. Two waits, two minutes then three.
    invalid_request_retry_seconds: float = 300.0
    invalid_request_initial_seconds: float = 120.0
    invalid_request_factor: float = 1.5
    # How many landed stages the reviewer is shown, most recent first. None
    # keeps all of them, which is the old behaviour and the right default for a
    # project with no progress log — there the history is the only account of
    # what has been done.
    #
    # Where a log exists, it is the better account and it rides in the cached
    # prefix. The history does not: it sits after the breakpoint and is
    # re-billed on every review, and across 164 stored verdicts on one run not
    # one cited an earlier stage. Set this where the log carries the record.
    history_stages: int | None = None
    # What the reviewer may look at, and how much of it. Without tools it can
    # only judge what the diff shows, and a diff does not always carry the fact
    # that decides it: a stage that deletes an `attr_accessible` declaration is
    # safe exactly when a permit list elsewhere covers the same attributes, and
    # that file is not in the diff. Across one run of 31 stages, 8 were
    # deletions of that shape — a quarter of the verdicts were approvals the
    # reviewer had no way to withhold.
    #
    # Deliberately its own settings rather than borrowed from the planner's,
    # though they start at the same numbers. These are per-role tuning: the two
    # ask different questions, and whoever decides the reviewer needs a
    # different budget should be able to say so in config without touching
    # code — and without a change to the planner's budget silently moving the
    # reviewer's.
    repo_access: bool = False
    max_read_lines_per_call: int = 400
    max_read_lines_total: int = 3000
    max_read_calls: int = 25
    # See `PlannerConfig.semantic_search`. Same shape, same reason for keeping
    # the endpoints in the environment.
    semantic_search: dict | None = None


class Limits(_Strict):
    max_test_retries: int = 3
    max_rework_retries: int = 2
    # Global across the run, not per-stage: per-stage caps let a pathological
    # project consume unbounded paid inference one stage at a time. Kept as an
    # absolute backstop; the rule below is what normally binds.
    max_planner_interventions: int = 12
    # Consecutive planner passes with nothing landing in between.
    #
    # A flat global cap needs a stage count nobody has — the orchestrator's
    # stages are not the plan document's stages, and the planner derives them
    # as it goes. An allowance that accrues per landed stage answers that, but
    # builds a reserve which is then spent all at once on the very stage it
    # should have caught.
    #
    # This measures being stuck directly. A run that keeps landing work can
    # continue as long as the wall clock allows; a run that has been round the
    # planner three times with nothing to show for it will not be unstuck by a
    # fourth.
    max_interventions_without_landing: int = 3
    max_stages: int = 60
    aider_timeout_seconds: int = 1800
    command_timeout_seconds: int = 3600
    wall_clock_hours: float = 14.0


class Excerpt(_Strict):
    """A numbered slice of a file, carried forward for the executor.

    The planner reads in slices — median forty-one lines, measured over 1,583
    calls — and then hands the executor whole-file globs it may not be able to
    have. This is the same information travelling the rest of the way.

    `note` is the planner's own reason for including it. The instruction can
    say the same thing, but a label attached to the lines survives the executor
    skimming, which the instruction does not.
    """

    path: str
    start: int = 1
    end: int = 0  # 0 means "to the end of the file"
    note: str = ""


class Stage(_Strict):
    """One unit of work — the shippable unit.

    Declarative fields may be authored by the planner. Executable and policy
    fields may not; they come from the config's stage defaults.
    """

    id: str
    kind: StageKind = "agent"

    # --- planner-writable (declarative) ---
    instruction: str | None = None
    edit_files: list[str] = []
    read_files: list[str] = []
    # Lines the planner already read and the executor will need. `read_files`
    # is whole files, so a file over `max_read_lines` is not trimmed to the part
    # that matters — it is dropped and reported as withheld, and the executor
    # gets nothing from it. Measured over two runs: the planner read
    # `order_controller.rb` twenty-seven times in slices of fifteen to
    # thirty-three lines, and that file cannot be a reference file at all.
    #
    # Declarative by construction — a path and two integers. Nothing here is
    # executed, and the orchestrator does the reading.
    read_excerpts: list[Excerpt] = []
    constraints: str | None = None
    acceptance: str | None = None
    forbidden_patterns: list[str] = []
    # Regexes that must not survive anywhere in `edit_files` once the stage is
    # done. The complement of `forbidden_patterns`, which reads added lines and
    # so can only see a construct arriving, never one left behind.
    must_not_remain: list[str] = []
    # Extra spec paths the planner expects to be affected beyond those the diff
    # reveals. Paths, never a command — see `scoped_test_command`.
    test_paths: list[str] = []

    # --- operator-only (executable) ---
    command: str | None = None
    preconditions: list[str] = []
    context_commands: list[str] = []
    setup_command: str | None = None
    test_command: str | None = None
    checks: list[str] = []

    # --- operator-only (policy) ---
    require_new_tests: bool = False
    require_scoped_tests: bool = False
    review: bool = True
    full_suite_on_approval: bool | None = None

    def effective_test_command(self, cfg: ProjectConfig) -> str | None:
        return self.test_command or cfg.test_command

    def effective_setup_command(self, cfg: ProjectConfig) -> str | None:
        return self.setup_command or cfg.setup_command

    def full_suite_required(self, cfg: ProjectConfig) -> bool:
        if self.full_suite_on_approval is not None:
            return self.full_suite_on_approval
        return cfg.full_suite_on_approval


class StageDefaults(_Strict):
    """Operator-authored executable and policy fields applied to every
    planner-derived stage.

    The planner invents *what* the work is; it cannot invent *how* to run
    anything. Since there is no static stage list, this is where the executable
    half of a stage comes from.
    """

    preconditions: list[str] = []
    context_commands: list[str] = []
    checks: list[str] = []
    require_new_tests: bool = False
    require_scoped_tests: bool = False
    review: bool = True


class ProjectConfig(_Strict):
    # Documentation, not behaviour: these commands assume a particular
    # machine's Docker, Ruby, and paths. Recording it stops a future reader
    # running this config elsewhere and misreading the failures.
    host: str | None = None

    target_repo: Path
    base_ref: str = "main"
    project_branch: str

    # Repo-relative path to the plan document. A document, not a directory:
    # pointing at `docs/` would sweep every runbook and ADR into every review
    # prompt.
    plan_root: str

    setup_command: str | None = None
    test_command: str | None = None
    full_test_command: str | None = None
    # Where the planner's append-only record of what git history shows was done
    # is kept. Commonly a subdirectory of the plan directory, so a later pass —
    # a human, or a tool outside this loop — can fold it into the plan
    # documents properly.
    #
    # Written by the orchestrator from the planner's structured output, never
    # by a stage. The scope guard treats it as a plan document precisely so an
    # executor cannot edit the record of its own work.
    plan_addendum_path: str | None = None
    # Documents the repository already keeps for whoever works in it: how to
    # run the suite, what the container does, which conventions bite. They are
    # maintained because humans and interactive sessions read them, and the
    # planner could not — so the same facts had to be hand-copied into
    # `planner.guidance`, and a hand copy drifts. Observed: `AGENTS.md`
    # recorded that editing the Gemfile reinstalls the bundle automatically,
    # the guidance said nothing, and the plan asserted the opposite across five
    # items nobody drew because they read as blocked.
    #
    # Unset means the conventional names — a project that keeps one has almost
    # always called it one of these. An explicit empty list is different: it
    # means the operator looked and decided there is none.
    #
    # Read once at the plan sha and frozen, like the plan itself, and protected
    # by the scope guard for the same reason: a document the planner draws
    # conventions from must not be editable by the executor those conventions
    # govern.
    agent_context: list[str] | None = None
    # Optional. `{paths}` is filled by the orchestrator from the stage diff.
    scoped_test_command: str | None = None
    # Used instead of `scoped_test_command` when any of those paths is a
    # directory rather than a file. A directory can hold hundreds of files, and
    # running it serially costs minutes on every attempt and every re-run; a
    # single file is not worth starting workers for. Both strings are yours —
    # this only chooses between them, on a fact about the filesystem.
    directory_test_command: str | None = None
    # What Aider runs inside its own edit loop, when `executor.auto_test` is on.
    # Separate from the above because the two have opposite needs from the same
    # runner: verify *parses* the output to find which files failed, so it needs
    # the full failed-examples block, while Aider's output lands in the model's
    # context — a directory-scoped run put 138k-152k tokens into a single
    # request, most of it passing-example lines, a profile and a deprecation
    # tally. Quiet it here, not there.
    #
    # Keep the failure detail. Our parsers want only filenames, but the model
    # cannot fix a failure it cannot see. Drop the noise around the traces, not
    # the traces.
    #
    # Falls back to `scoped_test_command` when unset.
    auto_test_command: str | None = None

    full_suite_on_approval: bool = True

    # When a suite goes red, re-run only the files that failed rather than the
    # whole suite; a file that passes whole and standalone counts as green.
    # Needs `scoped_test_command` to have somewhere to put the paths, and a
    # pattern that can find them in this runner's output.
    flake_rerun_failed_files: bool = True
    # Regex, applied per line against the test command's output; group 1 must
    # capture a repo-relative file path. There is deliberately no default:
    # which lines of which runner name a failing file is a property of the
    # project, not of this tool, and a Ruby-shaped default in the code would be
    # exactly the kind of project knowledge that does not belong here. Unset,
    # the re-run falls back to running the whole suite again.
    failed_file_pattern: str | None = None
    # Regex, group 1 capturing the ordering seed a runner reports. Same
    # reasoning as above about defaults, and one more: excusing a flake without
    # recording how to reproduce it is what makes a flake permanent. The
    # orchestrator has excused the same handful of files all night and the only
    # record of *which ordering* did it lives in an output nobody keeps.
    #
    # Matched after each failing file, not once for the whole run: a parallel
    # runner is many independent orderings, and the seed that matters is the
    # one belonging to the worker that failed. Fourteen were printed on the run
    # that motivated this.
    seed_pattern: str | None = None
    # Above this many, a red suite is a broken stage rather than a flake, and
    # re-running to prove it is minutes spent on a foregone conclusion.
    flake_rerun_max_files: int = 5
    # How many times to re-run the failing files alone before believing them.
    # Two, making three strikes with the group run that started it: a spec on
    # the first real target failed in the suite, failed again alone, and then
    # passed on the next full run — so one isolated attempt called it real and
    # cost a stage that was innocent. The isolated run is cheap next to the
    # suite (49 seconds against four minutes), so the second costs little.
    flake_rerun_attempts: int = 2

    # What counts as a test file for `require_new_tests`.
    test_file_patterns: list[str] = [
        "**/test_*.py",
        "**/*_test.py",
        "**/*_test.go",
        "**/*_test.rb",
        "**/*_spec.rb",
        "**/*.test.ts",
        "**/*.test.tsx",
        "**/*.test.js",
        "**/*.spec.ts",
        "**/*.spec.js",
        "test/**",
        "tests/**",
        "spec/**",
    ]

    executor: ExecutorConfig
    planner: PlannerConfig
    reviewer: ReviewerConfig
    limits: Limits = Limits()

    stage_defaults: StageDefaults = StageDefaults()

    # Whether to commit what a `checks` command changed. Most linters only
    # report, and for those this is a no-op — there is nothing uncommitted to
    # find. It exists for the ones that also fix: `rubocop -A`, `eslint --fix`,
    # `gofmt -w`.
    #
    # Defaulting on, because the failure it prevents is not a style question.
    # The executor commits its own work before verify starts, so nothing else
    # in the loop commits what a check wrote. Left in the tree it survives the
    # stage: swept up silently if the stage lands, and orphaned if the stage is
    # blocked or reworked away — at which point the *next* stage's precheck
    # refuses to cut a branch over changes it cannot attribute, and the run
    # stops. That happened, and it would have recurred on every blocked stage.
    #
    # Turn it off for a check that writes something which should not be part of
    # the stage — a coverage report a `.gitignore` has missed, say. The tree
    # will then be dirty when the stage ends, which is a problem this cannot
    # solve on the operator's behalf.
    checks_commit_changes: bool = True

    # A reviewer leaves comments on the work in front of it. It does not ask for
    # the work again, and an author who cannot see their own diff is not in a
    # position to amend it — so a rejected attempt stays on the branch and the
    # executor is shown what the stage has changed so far, for the three tries
    # `max_rework_retries` allows.
    #
    # This defaulted to True on the reasoning that each attempt should produce
    # one clean single-purpose diff. Stages land by squash merge, so that buys
    # less than it sounds like: the diff anyone ever sees is the net one either
    # way. What it costs became clear when a rejection said the behaviour and
    # scope were correct and only an explanatory comment contradicted the code —
    # resetting rebuilt a correct spec from nothing in order to change one
    # sentence, using a model that had failed four times that morning to
    # reproduce ten lines byte-for-byte.
    #
    # Kept as an option because a stage whose approach is wrong is better off
    # starting over, and only an operator watching a particular project can say
    # how often that is the case.
    rework_reset: bool = False
    # Read by the planner only; see `effective_operations_context`.
    operations_context: list[str] | None = None

    @property
    def plan_root_path(self) -> Path:
        return self.target_repo / self.plan_root

    @property
    def effective_operations_context(self) -> list[str]:
        """Documents only the planner is given.

        The conventions half of a repository's agent-facing docs goes to all
        three participants: the planner draws against it, the executor writes
        code that has to obey it, and the reviewer judges whether the code did.
        The operational half — build, test, deploy, the container's behaviour —
        is the planner's alone. It is what tells it that a Gemfile edit
        reinstalls the bundle, which is the fact the whole mechanism was built
        for; and it is exactly what the executor must not be handed, because it
        runs no commands and a page of them invites it to narrate ones it never
        ran.

        No default. A project that keeps one document for both audiences names
        it in `agent_context` and leaves this empty, which is what every
        project did before the split existed.
        """
        return list(self.operations_context or [])

    @property
    def effective_agent_context(self) -> list[str]:
        """The agent-context documents to read, defaults applied.

        `None` and `[]` differ: unset means nobody chose, so try the names a
        project almost always uses; empty means the operator looked and decided
        there is none. Collapsing them would make "we have no such file" say
        the same thing as "nobody thought about it".
        """
        if self.agent_context is None:
            return ["AGENTS.md", "CLAUDE.md"]
        return list(self.agent_context)

    @property
    def stage_branch_namespace(self) -> str:
        """Child branches live in a namespace *beside* the project branch, not
        under it.

        Git refs are filesystem paths, so `refs/heads/upgrade/rails-5` (a file)
        and `refs/heads/upgrade/rails-5/stage-001-x` (a directory) cannot
        coexist — git refuses with "cannot lock ref". Appending `-stage` makes
        it a sibling path component, which keeps the group greppable
        (`git branch --list 'upgrade/rails-5-stage/*'`) and deletable together
        while remaining a legal ref.
        """
        return f"{self.project_branch}-stage"

    def stage_branch(self, index: int, stage_id: str) -> str:
        return f"{self.stage_branch_namespace}/{index:03d}-{stage_id}"

    def all_commands(self) -> list[tuple[str, str]]:
        """Every executable string in the config, as (where, command).

        Used by the denylist check and by preflight. If a new executable field
        is added and not listed here, the denylist silently stops covering it —
        so this is deliberately exhaustive rather than reflective.
        """
        out: list[tuple[str, str]] = []
        for label, command in (
            ("setup_command", self.setup_command),
            ("test_command", self.test_command),
            ("full_test_command", self.full_test_command),
            ("scoped_test_command", self.scoped_test_command),
            ("directory_test_command", self.directory_test_command),
            ("auto_test_command", self.auto_test_command),
            ("executor.lint_command", self.executor.lint_command),
        ):
            if command:
                out.append((label, command))
        for i, command in enumerate(self.stage_defaults.preconditions):
            out.append((f"stage_defaults.preconditions[{i}]", command))
        for i, command in enumerate(self.stage_defaults.context_commands):
            out.append((f"stage_defaults.context_commands[{i}]", command))
        for i, command in enumerate(self.stage_defaults.checks):
            out.append((f"stage_defaults.checks[{i}]", command))
        return out

    def stage_from_planner(self, fields: dict) -> Stage:
        """Build a Stage from planner output, merging in operator-only fields.

        The planner's fields are filtered against the allowlist rather than
        trusted, so even a schema failure upstream cannot introduce an
        executable field.
        """
        safe = {k: v for k, v in fields.items() if k in PLANNER_WRITABLE_FIELDS}
        defaults = self.stage_defaults
        # OR rather than override, so the planner can only tighten this. An
        # operator who requires tests everywhere must not have that waived by a
        # model that judged one stage exempt.
        wants_tests = bool(safe.pop("require_new_tests", False))
        return Stage(
            **safe,
            preconditions=list(defaults.preconditions),
            context_commands=list(defaults.context_commands),
            checks=list(defaults.checks),
            require_new_tests=defaults.require_new_tests or wants_tests,
            require_scoped_tests=defaults.require_scoped_tests,
            review=defaults.review,
        )


def denylist_violations(commands: list[tuple[str, str]]) -> list[str]:
    """Refused regardless of operator approval."""
    problems = []
    for where, command in commands:
        for pattern, why in DENYLIST:
            if pattern.search(command):
                problems.append(
                    f"{where}: command {command!r} matches the denylist "
                    f"(/{pattern.pattern}/) — {why}. This is refused regardless "
                    "of approval."
                )
    return problems


def parse_config(data: dict) -> ProjectConfig:
    if not isinstance(data, dict):
        raise ConfigError(["config must be a YAML mapping"])

    try:
        cfg = ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_pydantic_errors(e)) from e

    problems = _structural_problems(cfg)
    if problems:
        raise ConfigError(problems)
    return cfg


def load_config(path: Path | str) -> ProjectConfig:
    path = Path(path)
    try:
        raw = path.read_text()
    except OSError as e:
        raise ConfigError([f"cannot read config {path}: {e}"]) from e

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError([f"{path} is not valid YAML: {e}"]) from e

    if not isinstance(data, dict):
        raise ConfigError([f"{path} must contain a YAML mapping at the top level"])

    return parse_config(data)


def _glob_could_match_a_test(glob: str, test_patterns: list[str]) -> bool:
    """Could a file written under `glob` be recognised as a test?

    Deliberately permissive. `spec/**` is how a planner usually grants room for
    a spec, and demanding it name the exact file would reject the common,
    correct form. The question is whether there is *anywhere* to put one, not
    whether the planner predicted its name.
    """
    from orchestrator.globs import glob_to_regex, matches_any

    if matches_any(glob, test_patterns):
        return True
    # A prefix like `spec/**` cannot be matched against a pattern directly, so
    # ask whether a plausible file beneath it would be.
    stem = glob.split("*")[0].rstrip("/")
    if not stem:
        return False
    return any(
        glob_to_regex(pattern).match(f"{stem}/x_spec.rb")
        or glob_to_regex(pattern).match(f"{stem}/x/x_spec.rb")
        or glob_to_regex(pattern).match(f"{stem}/test_x.py")
        for pattern in test_patterns
    )


def validate_stage(stage: Stage, cfg: ProjectConfig) -> list[str]:
    """Well-formedness of a single stage, planner-derived or otherwise.

    Runs before anything acts on a planner-produced spec: a bad stage should
    fail here, cheaply, rather than as a confusing verify failure.
    """
    problems: list[str] = []
    where = f"stage {stage.id!r}"

    if not _SAFE_ID.match(stage.id):
        problems.append(
            f"stage id {stage.id!r} is not safe for a path or a git ref: use "
            "letters, digits, dot, dash, underscore"
        )

    if stage.kind == "agent" and not stage.instruction:
        problems.append(f"{where}: agent stages require an instruction")

    # The partition, one field further along. `PLANNER_WRITABLE_FIELDS` stops
    # the planner naming anything executable; it never stopped it writing the
    # code, and an instruction reading "replace this block with exactly this
    # block" is authored code travelling in a declarative field.
    #
    # A fence is the whole test, deliberately. Inline backticks are how a
    # property names an identifier — "every entry must name something that
    # exists" — and catching those would make the rule unusable and get it
    # routed around. A fenced block is the shape that carries a replacement.
    if stage.instruction and "```" in stage.instruction:
        problems.append(
            f"{where}: the instruction contains a fenced code block. The "
            "planner states the end state; the executor writes the code. Put "
            "existing code in `read_excerpts` as a path and a line range — a "
            "reference can only point at what is already there, which is what "
            "keeps an instruction from becoming a transcription job"
        )
    if stage.kind == "script" and not stage.command:
        problems.append(f"{where}: script stages require a command")
    if stage.kind == "agent" and stage.command:
        problems.append(
            f"{where}: `command` belongs to script stages; this stage is an "
            "agent stage"
        )

    if not stage.edit_files:
        problems.append(
            f"{where}: must declare edit_files — the scope guard is meaningless "
            "without it, and a stage that cannot name its files is too broad "
            "to be a stage"
        )
    elif stage.require_new_tests and not any(
        _glob_could_match_a_test(g, cfg.test_file_patterns) for g in stage.edit_files
    ):
        # A stage that cannot pass however well the executor performs.
        # `require_new_tests` fails it unless the diff touches a test file; the
        # scope guard fails it if the diff leaves `edit_files`. Demand a test
        # and forbid writing one and the executor writes the spec, scope
        # rejects it, and the loop spends retries — then an intervention —
        # discovering something checkable before it started.
        problems.append(
            f"{where}: require_new_tests is set but edit_files has nowhere to "
            f"put a test. Recognised test paths are "
            f"{', '.join(cfg.test_file_patterns)}; add the spec you expect to "
            "be written, or drop the requirement"
        )

    if not stage.effective_test_command(cfg) and not stage.checks:
        problems.append(
            f"{where}: needs a test command (its own or the project's) or at "
            "least one check — otherwise nothing verifies it"
        )

    for pattern in stage.forbidden_patterns:
        try:
            re.compile(pattern)
        except re.error as e:
            problems.append(
                f"{where}: forbidden_patterns entry {pattern!r} is not a valid "
                f"regex: {e}"
            )

    problems.extend(
        denylist_violations(
            [(f"{where}.command", stage.command)] if stage.command else []
        )
    )

    return problems


def _format_pydantic_errors(e: ValidationError) -> list[str]:
    return [
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
        for err in e.errors()
    ]


def _structural_problems(cfg: ProjectConfig) -> list[str]:
    problems: list[str] = []

    if cfg.project_branch == cfg.base_ref:
        problems.append(
            f"project_branch {cfg.project_branch!r} must differ from base_ref "
            f"{cfg.base_ref!r}: the orchestrator must never commit to the "
            "branch it cuts from"
        )

    if cfg.project_branch.endswith("/") or ".." in cfg.project_branch:
        problems.append(
            f"project_branch {cfg.project_branch!r} is not a valid git ref"
        )

    if not cfg.plan_root:
        problems.append("plan_root is required: a project is defined by its plan")
    elif Path(cfg.plan_root).is_absolute() or ".." in Path(cfg.plan_root).parts:
        problems.append(
            f"plan_root {cfg.plan_root!r} must be a repo-relative path that does "
            "not escape the repo"
        )
    elif cfg.plan_root.endswith("/"):
        problems.append(
            f"plan_root {cfg.plan_root!r} looks like a directory. It must be a "
            "single document — pointing at a directory sweeps every runbook and "
            "ADR beside it into every review prompt"
        )

    for label in (
        "scoped_test_command",
        "directory_test_command",
        "auto_test_command",
    ):
        command = getattr(cfg, label)
        if command and "{paths}" not in command:
            problems.append(
                f"{label} must contain a {{paths}} placeholder — that is the "
                "slot the orchestrator fills with the stage's changed files"
            )

    if cfg.directory_test_command and not cfg.scoped_test_command:
        problems.append(
            "directory_test_command needs scoped_test_command: it is the "
            "variant used when the selection contains a directory, not a "
            "scoping mechanism on its own"
        )

    if cfg.failed_file_pattern:
        try:
            compiled = re.compile(cfg.failed_file_pattern, re.MULTILINE)
        except re.error as e:
            problems.append(f"failed_file_pattern is not a valid regex: {e}")
        else:
            if compiled.groups != 1:
                problems.append(
                    "failed_file_pattern must have exactly one capture group, "
                    "around the file path to re-run. With none it would match "
                    "and yield nothing, which looks identical to a test runner "
                    "we cannot read"
                )

    if cfg.seed_pattern:
        try:
            compiled = re.compile(cfg.seed_pattern, re.MULTILINE)
        except re.error as e:
            problems.append(f"seed_pattern is not a valid regex: {e}")
        else:
            if compiled.groups != 1:
                problems.append(
                    "seed_pattern must have exactly one capture group, around "
                    "the seed itself. A pattern that matches the line and "
                    "captures nothing records an excusal with no way to "
                    "reproduce it, which is the thing this exists to prevent"
                )

    if not cfg.test_command and not cfg.stage_defaults.checks:
        problems.append(
            "a project needs test_command, or checks in stage_defaults, or "
            "nothing will verify its stages"
        )

    for pattern in cfg.test_file_patterns:
        if not pattern:
            problems.append("test_file_patterns contains an empty pattern")

    for role, endpoint in (
        ("executor", cfg.executor),
        ("planner", cfg.planner),
        ("reviewer", cfg.reviewer),
    ):
        if endpoint.api_base and endpoint.api_base_env:
            problems.append(
                f"{role} sets both api_base and api_base_env. Pick one — "
                "silently preferring either would hide the mistake, and which "
                "endpoint gets called is not a detail to guess at"
            )

    problems.extend(denylist_violations(cfg.all_commands()))
    return problems
