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
that execute something — clean tree, test command actually passes, endpoint
reachability — live in `preflight`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator, ValidationError

from code_gantry.globs import matches_any


# Stage ids name directories and git branches, so they must not contain
# separators, traversal, or anything git rejects in a ref.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_KEY_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

# Fields the planner is allowed to author. Everything else in Stage is
# operator-only. `nodes` and `planner` both import this; it is the single
# definition of the partition.
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
        # An adjective about the work. Nothing here can become an instruction
        # to run, which is the question to answer deliberately whenever a
        # field joins `Stage`. Read by nothing that decides anything — it is
        # recorded beside what the stage cost so the rating can be checked.
        "difficulty",
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
        "CodeGantry must never push; the outer merge is the operator's",
    ),
    (
        re.compile(r"\bgit\s+(checkout|switch)\b"),
        "changing branches from inside a command would break the stage's "
        "branch identity; CodeGantry manages checkout itself",
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

    # Derived from `max_read_lines_total` unless set, at eighty characters a
    # line. Shipped as a class-level constant first, which was wrong in the
    # one way that matters: every real config raises the line budget three to
    # seven times above its default, and the char ceiling stayed where the
    # default put it. Measured before it reached a run — the reviewer had
    # `max_read_lines_total: 20000` and an effective 240,000 characters, a
    # seventh of what the lines imply, so the ceiling added *underneath*
    # another would have bound first and silently, a read-budget refusal
    # reading the same whichever ceiling raised it.
    #
    # Eighty a line is generous for source on purpose: this exists for the
    # minified bundle and the one-row fixture, and must never be what stops
    # ordinary reading.
    max_read_chars_total: int | None = None

    @model_validator(mode="after")
    def _derive_char_budget(self):
        if self.max_read_chars_total is None:
            object.__setattr__(
                self, "max_read_chars_total", self.max_read_lines_total * 80
            )
        return self

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


class NoDirectEdit(_Strict):
    """A path a model may not author, and the operator's reason why.

    Two fields rather than a bare glob, because the refusal a model reads has
    to say what to do instead, and that sentence is project knowledge. "Run
    the installer" names a different command in every ecosystem, so a message
    written in our code would be one project's vocabulary shipped to all of
    them.

    Rendered verbatim into the refusal, so it is worth writing as an
    instruction — what to call, not what went wrong.
    """

    path_glob: str
    reason: str

    @model_validator(mode="after")
    def _both_present(self) -> NoDirectEdit:
        if not self.path_glob.strip():
            raise ValueError("no_direct_edit entry needs a path_glob")
        if not self.reason.strip():
            # A refusal with an empty reason is a dead end: the model is told
            # no and given nothing to do next, which is the shape that burns
            # an attempt asking the planner to widen a scope.
            raise ValueError(
                f"no_direct_edit entry for {self.path_glob!r} needs a reason; "
                "it is what the model is told to do instead"
            )
        return self


class ExecutorConfig(_EndpointConfig):
    model: str
    api_key_env: str | None = None
    # Extra request-body parameters, merged into every executor call.
    #
    # It exists because a gateway's controls are not always a model string. The
    # Pareto router's quality tier is `plugins: [{"id": "pareto-router",
    # "min_coding_score": 0.6}]` in the body, and naming that here would put one
    # deployment's vocabulary into a tool that is supposed to have none — the
    # same rule that keeps a framework's file extensions out of a prompt.
    #
    # Wide on purpose and bounded at the one place it must be: `tools` is the
    # capability partition, `model` decides who answers, `input` is the
    # conversation. `RESERVED_REQUEST_KEYS` refuses those at config load, which
    # is the first moment the question can be answered.
    request_extra: dict = Field(default_factory=dict)
    # Caching is a property of the
    # endpoint rather than of the work: against a local server that prices
    # nothing and caches nothing it buys nothing and adds a keepalive ping
    # loop; against a hosted model it is most of the saving. The operator knows
    # which they have — this file already carries every other fact about where
    # the executor runs.
    # The API parameter of the same name. Unset by default: a model that does
    # not reason has nothing to do with it, and the field is then omitted from
    # the request entirely rather than sent empty.
    reasoning_effort: str | None = None
    # Ceiling on the total size of a stage's `read_files`, in lines. Unset means
    # no ceiling, which is the old behaviour.
    #
    # `read_files` is reference material the planner chooses, and its natural
    # instinct is to pass every already-converted file as a worked example. That
    # set grows with every landed stage. Measured on the first real project: by
    # the twelfth stage it was sending 4,636 lines to change six, 69,000 tokens
    # a call, and the same stage converted four of six sites and then three of
    # six — the task lost inside the reference material. Attempts took 561s and
    # 584s against a 600s request timeout, so whether a stage landed or looked
    # like it hung came down to the generation rate that minute.
    #
    # The controlled comparison, from the same run: stage 10 revision 0 carried
    # 2,818 lines of reads and stalled six times over three hours. The planner's
    # redraw passed a single read file, 39k tokens, and it landed in 120s.
    #
    # `edit_files` is never trimmed — that is the task, not context.
    max_read_lines: int | None = None

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
    # A backstop against a model that ignores every refusal and keeps asking,
    # and nothing more. The real bounds are elsewhere and bind first: the read
    # budget refuses past its own ceiling, and `run_loop` stops the whole cycle
    # at `request_timeout_seconds` whatever the turn count.
    #
    # It was 20 and was silently deciding whether stages could be done at all.
    # Measured on one stage of a live run: four attempts made 85, 103, 76 and
    # 138 tool calls, every one a read, and each stopped at exactly 20 turns
    # having edited nothing — reported to the planner as "the attempt produced
    # no changes", which sent it to redraw a stage that was never the problem.
    # A ceiling that decides outcomes is not a backstop.
    max_model_turns: int = 200
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
    # default, which `prompts.py` builds.
    system_prompt_file: str | None = None
    # Same shape as the planner's and the reviewer's, and kept separate for the
    # same reason: the three ask different questions of the index and whoever
    # gives one a different collection should be able to say so here.
    #
    # The executor does *not* get semantic search as a tool. This is consulted
    # only when an edit fails to match, to locate the text it meant — see
    # `edittools.nearest_text`. The index lags the working tree by however many
    # edits and commits have happened since it was built, which is survivable
    # for something choosing where to look and not for something quoting bytes
    # exactly.
    semantic_search: dict | None = None
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
    # Paths a model may never write by hand, whatever a stage's scope allows.
    #
    # This is not scope narrowed. Scope asks whether a stage may touch a file;
    # this asks whether a *model* may author it at all, and the answer is no
    # for anything generated by a tool that owns it. A lockfile is the case
    # that produced it: a hand-written one can be unsatisfiable, and nothing
    # says so until an install fails — on this project, an install that then
    # killed the container and left an executor editing against a corpse for
    # 42 minutes.
    #
    # Empty by default, so a project that declares none behaves exactly as it
    # did. The remedy belongs to the operator because the tool that owns the
    # file is project knowledge, and it is rendered to the model verbatim.
    no_direct_edit: list[NoDirectEdit] = []


class PlannerConfig(_EndpointConfig):
    provider: Literal["anthropic"] = "anthropic"
    model: str
    # Optional in every role, not just the executor. Someone running three
    # local models should be able to say so in all three places, and an
    # operator naming no variable against a cloud endpoint has made a claim
    # rather than an omission — the request goes out and the provider answers.
    api_key_env: str | None = "ANTHROPIC_API_KEY"
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
    # What the planner may spend on one answer, thinking included. Not a model
    # limitation — the SDK takes a plain int and the real ceiling is enforced
    # server-side — and it was hardcoded, which made `_call_failure`'s advice
    # unfollowable: a truncated verdict tells the operator to "raise the
    # planner's max_tokens", and there was nothing to raise.
    #
    # Generous by default because it is a ceiling rather than an allocation.
    # 16,000 was not enough once already: stage instructions on a real project
    # reach 15,167 characters and a derivation died mid-string at 11,710 with
    # the reasoning already spent. The cost of headroom is nothing; the cost of
    # hitting it is a whole derivation discarded.
    #
    # 32,000 was not enough either, once batching landed. A derivation died
    # mid-JSON after 591 seconds and 27 reads, and the arithmetic is obvious in
    # hindsight: five stages is five instructions, plus reasoning at `xhigh`,
    # against a budget sized for one.
    #
    # 64,000 measured rather than recalled — the API accepts 64,000 and 128,000
    # for `claude-opus-5` and rejects 200,000 with "200000 > 128000, which is
    # the maximum". Half the ceiling leaves room for the next surprise.
    #
    # **This depends on `request_timeout_seconds` being set, in a way that is
    # invisible from here.** The installed SDK refuses a non-streaming request
    # whose budget implies a long generation — `3600 * max_tokens / 128_000 >
    # 600` — which caps it at 21,333, below even the old 32,000. The check runs
    # only when no explicit timeout is passed, and the planner always passes
    # one. Drop that and every call raises before it is sent.
    max_tokens: int = 64_000
    # How many stages one derivation may produce, the one being started
    # included. 1 is the default and turns batching off: a project opts in,
    # because the saving is real only where the work is homogeneous and the
    # cost of getting it wrong — a planner surveying as though it needs five —
    # falls on every stage.
    max_batch_stages: int = 1
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
    # Optional in every role, not just the executor. Someone running three
    # local models should be able to say so in all three places, and an
    # operator naming no variable against a cloud endpoint has made a claim
    # rather than an omission — the request goes out and the provider answers.
    api_key_env: str | None = "OPENAI_API_KEY"
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
    # A flat global cap needs a stage count nobody has — CodeGantry's
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
    command_timeout_seconds: int = 3600
    wall_clock_hours: float = 14.0


class LedgerConfig(_Strict):
    """The ledger that holds the plan — see `ledger.py`."""

    # Keys are `<key_prefix>.<nnn>`; a short project handle, fixed for its life.
    key_prefix: str
    # Fold when the projection is this fraction of the plan text or more.
    fold_ratio: float = 0.25
    # Longest a finding's prose is rendered to the planner.
    note_chars: int = 600


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
    # executed, and CodeGantry does the reading.
    read_excerpts: list[Excerpt] = []
    constraints: str | None = None
    acceptance: str | None = None
    # The planner's own estimate: "low", "medium" or "high". Empty on a stage
    # drawn before this existed, which matters because `current` is a dumped
    # `Stage` and `Stage` forbids extras — a resume must survive the field
    # appearing.
    difficulty: str = ""
    forbidden_patterns: list[str] = []
    # Regexes that must not survive anywhere in `edit_files` once the stage is
    # done. The complement of `forbidden_patterns`, which reads added lines and
    # so can only see a construct arriving, never one left behind.
    must_not_remain: list[str] = []
    # Extra spec paths the planner expects to be affected beyond those the diff
    # reveals. Paths, never a command — see `scoped_test_command`.
    test_paths: list[str] = []

    # --- machinery-recorded ---
    # The commit the planner's excerpt line numbers were chosen against, set by
    # `plan` on every stage it queues behind the one it starts.
    #
    # Deliberately not planner-writable, and the reason is the whole argument
    # for this field: a sha a model declares is a *claim* made before the work,
    # which is the class of thing that produced the prediction problem this
    # replaces. The run already knows which commit it derived against. Asking
    # for it back would be asking the planner to be right about something the
    # machinery cannot be wrong about.
    #
    # Empty on the stage being started now — it was derived against the tree as
    # it stands, so there is no window in which anything could have moved.
    excerpt_base_sha: str = ""
    # Specs the full suite failed on, after the reviewer had approved the diff.
    #
    # Recorded so the executor's inner loop runs them while it works. Handing
    # back "the suite was red" without them asks it to fix something it cannot
    # run, and `resolve_test_paths` builds the loop's command from the stage
    # alone. Machinery-recorded for the same reason as the sha above: the suite
    # already said which file failed, so asking a model to name it would be
    # taking a claim in place of a fact.
    #
    # Running is not editing. A spec outside `edit_files` still cannot be
    # modified, which leaves the executor fixing the code — the right outcome
    # when the failure is real.
    suite_failing_paths: list[str] = []

    # --- operator-only (executable) ---
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
        return self.test_command or cfg.full_test_command

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


# Tool names are identifiers on the wire and keys in a JSON schema.
_SAFE_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

# Refused as argv[0]. A declared command exists so that nothing interprets the
# arguments; putting a shell at the front hands that property straight back.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})

# `{name}` occupying an entire argv element. Anything embedded in a longer
# string is refused, so there is never a question of how a repeated value joins.
_PLACEHOLDER = re.compile(r"^\{([a-z][a-z0-9_]*)\}$")
# The same shape found anywhere inside a longer element. Only reached for
# elements that are not wholly a placeholder, so a bare `{gem}` never matches
# here. See the use site for why this is not simply "contains a brace".
_EMBEDDED_PLACEHOLDER = re.compile(r"(?<!\$)\{[a-z][a-z0-9_]*\}")

# The three roles that call tools. Named here rather than in `projecttools`
# because config is where the partition is decided and pinned; the runtime
# reads this set, it does not extend it.
Role = Literal["planner", "executor", "reviewer"]
ROLES: tuple[str, ...] = ("planner", "executor", "reviewer")


class ToolArgument(_Strict):
    """One value the model supplies when calling a declared tool.

    `repeated` is the difference between `bundle update rails` and
    `bundle update rails nokogiri`: a repeated argument expands in place into
    as many argv elements as it has values, which is the only expansion that
    needs no quoting rule.
    """

    name: str
    description: str
    repeated: bool = False
    # A ceiling on how many values a repeated argument may carry.
    #
    # This is what makes withholding a tool mean something. An operator who
    # declines to offer the full suite has decided the model must not run
    # everything; an uncapped repeated argument hands it back, because
    # enumerating every spec file is the same action spelled differently. Ask
    # what a stated boundary is made of.
    #
    # Set it from where legitimate use stops. Measured over 375 recorded suite
    # runs that named failing examples: median 2, p95 4, and 97.1% at five or
    # fewer — then a near-empty band before the mass-failure runs of 21 and up,
    # which are precisely the ones a model should not be answering one path at
    # a time.
    #
    # Disclosed to the model rather than enforced silently: a ceiling it cannot
    # see is one it can only discover by spending.
    max_values: int | None = None


class ProjectTool(_Strict):
    """A command the operator declares and the executor may call.

    CodeGantry ships eight tools and knows nothing about any project's
    toolchain. A migration needs more — resolve the manifest, precompile
    assets, run a generator — and every one of those is project knowledge,
    which belongs in config rather than in a Python feature named after
    whatever ecosystem prompted it. So this is a menu: the operator writes the
    command and the description, and the model reaches for it exactly as it
    reaches for `read_file`.

    **Nothing here gates which stage may call which tool, deliberately.** The
    scope gate already measures the outcome — a tool that writes a file the
    stage never declared fails it, from the tree rather than from a
    declaration. A per-stage permission in front of that would be a claim used
    to predict what the existing gate observes.

    `command` is argv and is spawned with `shell=False`. That is what makes it
    safe for the model to supply arguments at all: a value containing `;` or
    `&&` is one inert element that makes the underlying tool error, because
    nothing is there to interpret it.
    """

    name: str
    description: str
    command: list[str]
    arguments: list[ToolArgument] = []
    # Who may call it. The executor alone, until an operator says otherwise —
    # which is what every declaration written before this field existed meant,
    # so adding it moves nothing.
    #
    # The default is not timidity. The menu's original entries write:
    # `bundle install` rewrites the lockfile, `rails app:update` overwrites
    # templated config. The executor is the only role that runs inside the
    # quarantine a stage branch provides, and the scope gate measures what it
    # touched from the tree. A planner that dirtied the work tree mid-derivation
    # would be caught by the *next* stage's precheck, which refuses to cut a
    # branch over changes it cannot attribute — stopping a run on a stage with
    # nothing wrong with it.
    #
    # A read-only tool is the case this field exists for. The planner reads to
    # decide what to draw, and where a project keeps source the work tree does
    # not contain — a dependency installed into a container volume, say — no
    # built-in read tool can reach it and the planner is the role that most
    # needs it.
    roles: list[Role] = ["executor"]
    # None means the runner's own default, which is what every other declared
    # command already gets.
    timeout_seconds: int | None = None

    @model_validator(mode="after")
    def _check(self):
        problems = _tool_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self


def _tool_problems(tool: ProjectTool) -> list[str]:
    """Everything wrong with one declared tool, all at once."""
    problems: list[str] = []
    if not _SAFE_TOOL_NAME.match(tool.name or ""):
        problems.append(
            f"project_tools: {tool.name!r} is not a usable tool name — it must "
            "match [a-z][a-z0-9_]*, because it is sent to the provider as an "
            "identifier and used as a key in a JSON schema"
        )
    if not tool.command:
        problems.append(f"project_tools.{tool.name}: command must not be empty")
        return problems

    head = Path(tool.command[0]).name
    if head in _SHELLS:
        problems.append(
            f"project_tools.{tool.name}: command starts with {tool.command[0]!r}. "
            "A declared command is argv and is run without a shell, which is "
            "what makes it safe for the model to supply an argument — nothing "
            "interprets the value. Running it through a shell gives that back. "
            "Put the real program first, or wrap the shell script in a file and "
            "name the file."
        )

    # A tool nobody may call reads in config exactly like one that works, and
    # the symptom is a model never reaching for it — indistinguishable from a
    # description that did not persuade it.
    if not tool.roles:
        problems.append(
            f"project_tools.{tool.name}: roles must name at least one of "
            f"{', '.join(ROLES)}. A tool with no audience is never offered to "
            "anything, which is not distinguishable from one nobody chose to call."
        )
    if len(set(tool.roles)) != len(tool.roles):
        problems.append(f"project_tools.{tool.name}: a role is named twice")

    declared = [a.name for a in tool.arguments]
    for name in declared:
        if not _SAFE_TOOL_NAME.match(name or ""):
            problems.append(
                f"project_tools.{tool.name}: argument {name!r} must match "
                "[a-z][a-z0-9_]*"
            )
    if len(set(declared)) != len(declared):
        problems.append(f"project_tools.{tool.name}: two arguments share a name")

    for argument in tool.arguments:
        if argument.max_values is None:
            continue
        if not argument.repeated:
            problems.append(
                f"project_tools.{tool.name}: {argument.name!r} sets max_values "
                "but is not repeated. A single value has nothing to cap, so "
                "this reads as a limit and enforces nothing"
            )
        elif argument.max_values < 1:
            problems.append(
                f"project_tools.{tool.name}: {argument.name!r} sets "
                f"max_values {argument.max_values}, which no call can satisfy "
                "— a repeated argument already refuses an empty list"
            )

    used = []
    for element in tool.command:
        match = _PLACEHOLDER.match(element)
        if match:
            used.append(match.group(1))
            continue
        # An embedded placeholder is the trap: `--gems={names}` reads as
        # obviously intended and has no answer for how a repeated value joins,
        # so every answer is a quoting rule and quoting is what argv exists to
        # avoid.
        #
        # Matched as a placeholder rather than as "contains a brace". The
        # cruder test was exact while no declared command could contain shell
        # syntax, and stopped being so once one could: `sh` is refused as
        # argv[0] and allowed as a later element, which is how an operator
        # writes a tool that resolves something and then reads under it — with
        # the model's values arriving as positional parameters, so nothing is
        # interpolated. Every such script has braces (`|| { exit 1; }`,
        # `awk "{ print }"`), and refusing them rejected a working tool for
        # containing a character. `$` before the brace is excluded because
        # `${name}` is the shell expanding its own variable, not us expanding
        # ours.
        if _EMBEDDED_PLACEHOLDER.search(element):
            problems.append(
                f"project_tools.{tool.name}: {element!r} embeds a placeholder in "
                "a larger argument. A placeholder must be an entire argv "
                "element, so that a repeated value expands into elements "
                "rather than needing a rule for how to join it."
            )

    for name in used:
        if name not in declared:
            problems.append(
                f"project_tools.{tool.name}: command uses {{{name}}} but no "
                f"argument named {name!r} is declared"
            )
    for name in declared:
        if name not in used:
            problems.append(
                f"project_tools.{tool.name}: argument {name!r} is declared but "
                "the command never uses it, so the model would be asked for a "
                "value that reaches nothing"
            )
    return problems


class ProjectConfig(_Strict):
    # Documentation, not behaviour: these commands assume a particular
    # machine's Docker, Ruby, and paths. Recording it stops a future reader
    # running this config elsewhere and misreading the failures.
    host: str | None = None

    # Optional because the config now lives inside the repository it describes,
    # so the repository is wherever the config was read from — one fewer
    # absolute path in a file that is tracked and shared, and one that cannot
    # disagree with reality. Still settable: the tests build configs from no
    # file at all, and an operator may point at a worktree.
    target_repo: Path | None = None
    # Everything CodeGantry writes: runs, logs, the flake and cost
    # ledgers. Named here so CodeGantry's own tree
    # holds code and nothing else, and defaults to the config's directory,
    # which is inside the repo and gitignorable there.
    #
    # `~` and `${VAR}` are expanded, because this file is tracked in a shared
    # repository and cannot carry one machine's home directory. An unset
    # variable is an error rather than a directory literally named `${VAR}`: a
    # run that writes its whole record somewhere unintended is worse than one
    # that refuses to start.
    work_dir: Path | None = None
    # A `KEY=value` file holding the credentials `api_key_env` names, resolved
    # against this config's own directory so the answer does not depend on the
    # cwd a run was launched from. A path, never a value: this file is tracked
    # in the repository it describes, and the rule is `hold the path, not the
    # copy` — which for a secret is not an efficiency but the only permissible
    # shape.
    #
    # It belongs under `work_dir`, the one directory preflight refuses to run
    # without having proved is git-ignored. Parsed rather than sourced, and
    # applied with `setdefault` so the shell wins; see `envfile`.
    #
    # Optional. Without it credentials come from the launching shell, which is
    # a property of how a run was started that appears in no config, no log
    # and no artifact.
    env_file: Path | None = None
    # Repo-relative path of this config, when it was read from inside the
    # target repo. What `_is_plan_document` matches against; `None` when the
    # config lives elsewhere, which is the case every test builds.
    config_rel_path: str | None = None
    # Absolute path of the file this was read from, for the messages that tell
    # an operator what to run next. `config_rel_path` cannot serve: it is
    # `None` whenever the config lives outside the repo, which is where every
    # config lived until recently and where this one still lives.
    config_path: Path | None = None
    base_ref: str = "main"
    project_branch: str

    # Repo-relative path to the plan document. A document, not a directory:
    # pointing at `docs/` would sweep every runbook and ADR into every review
    # prompt.
    plan_root: str

    setup_command: str | None = None
    # The whole suite, taking no selection. A command rather than a declared
    # tool, and the difference is who calls it: no role ever runs the full
    # suite. The executor has no notion of `edit_files`, so faced with a red
    # spec outside the stage it will edit that spec, and a full run gives it
    # minutes per pass in which to do so. With no model in the path there is
    # nothing model-supplied to keep out of a shell, so this stays a string and
    # an operator's `a && b` keeps working.
    full_test_command: str | None = None
    # The same suite, taking a selection — named as an entry in
    # `project_tools`, because this one *is* called by a model.
    #
    # A name rather than a command, for the reason `plan_root` is a path rather
    # than a copy: which runner this project uses is the project's to say, and
    # a name in code would be one ecosystem's vocabulary shipped to every
    # other. Name it after what it is. `rspec` reads well in a Ruby project and
    # gives the model something better than a description: RSpec prints
    # `rspec ./spec/a_spec.rb:79` in its own failure output, so with a tool of
    # that name the printed line *is* the call — a name on the left and a
    # `paths` value on the right, nothing to translate.
    #
    # One declaration, two callers: the executor calls it to re-run the example
    # it was just shown, and the gate calls it to judge the stage. Both go
    # through `build_argv`, because a suite invoked one way in the loop and
    # another at the gate is how an exit code stops describing the artifacts.
    scoped_test_tool: str | None = None
    # Where the planner's append-only record of what git history shows was done
    # is kept. Commonly a subdirectory of the plan directory, so a later pass —
    # a human, or a tool outside this loop — can fold it into the plan
    # documents properly.
    #
    # Written by CodeGantry from the planner's structured output, never
    # by a stage. The scope guard treats it as a plan document precisely so an
    # executor cannot edit the record of its own work.
    plan_addendum_path: str | None = None
    # Where the project's test runner leaves its tally of warnings and
    # unexpected output. The path, not the copy: it is rewritten by every
    # suite run and read at prompt-build time, so a transcription here would
    # be the stale half of two records that disagree.
    #
    # Optional, and absent for a project whose runner writes no such file.
    test_warnings_path: str | None = None
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
    # recording how to reproduce it is what makes a flake permanent.
    # CodeGantry has excused the same handful of files all night and the only
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
    # Globs the `search` tool must never read, for every role.
    #
    # A hit in a vendored, minified or generated file is not actionable — no
    # stage can edit it — so the cost of reading it has no matching benefit.
    # Measured on one project: a single vendored editor bundle holds 240,181
    # characters across 108 lines, and a minified jQuery averages 23,157 per
    # line, so three hits return more text than an entire planner prompt.
    #
    # No default, deliberately. Which paths those are is a property of the
    # project, and a default naming `vendor/` or `public/` would be one
    # repository's shape shipped to every other one's planner.
    search_exclude_globs: list[str] = Field(default_factory=list)
    # Paths the scope gate must not fail a stage over.
    #
    # Some files rewrite themselves as a side effect of running the suite, and
    # no instruction to the executor can prevent it: a VCR cassette configured
    # to re-record after six months changes because a spec *ran*, not because
    # anything edited it. `commit_all` sweeps it onto the stage branch and the
    # gate — correctly comparing the diff against `edit_files` — fails a stage
    # that did nothing wrong. Measured: three redraws, 884 seconds of planning
    # and a `restart` verdict on an approved diff, all opened by one cassette.
    #
    # An exemption says "changing this is not evidence the executor wandered".
    # It is not `edit_files`: it grants no permission and belongs to the
    # operator rather than the planner, which cannot widen it.
    #
    # No default. That cassettes auto-refresh at six months is a fact about one
    # repository's VCR configuration.
    scope_exempt_globs: list[str] = Field(default_factory=list)
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
    ledger: LedgerConfig | None = None

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

    # Extra tools the executor may call, declared by the operator. Empty by
    # default: a project that needs none gets the eight built-ins and no new
    # surface at all.
    project_tools: list[ProjectTool] = []

    # Let the gate accept the executor loop's verdict when the command and the
    # tree are identical, instead of running the same thing again.
    #
    # Off, and the default is the point. The gate re-running is the one check
    # that would catch a loop testing the wrong thing — measured on one run,
    # 23 of 35 stages had an inner loop that was either absent or aimed at a
    # file the stage was not editing, and every one of them reported green.
    # An executor whose test selection has not been watched for long is
    # exactly the case that check exists for.
    #
    # Worth turning on once a run's worth of stages shows the gate never
    # disagreeing with the loop. Until then the duplication is what buys the
    # right to trust it, and 14s a stage is a cheap price for that.
    #
    # Named per layer, because that condition is met at different times for
    # different layers and a single switch conflates them. Measured over 81
    # verdicts after the executor rewrite: the gate disagreed about `tests`
    # **12 times** and about `checks` **zero**. `checks` moved wholesale into
    # the loop and commits there, so the gate re-asks a question already
    # answered on the same bytes; `tests` diverges deliberately, because the
    # gate adds test files from the diff and its set is a superset. As one
    # boolean, the well-evidenced skip was unavailable without the contradicted
    # one — and the contradicted one is the expensive layer, so the switch
    # offered a minute a stage in exchange for the only check that catches a
    # loop testing the wrong thing.
    #
    # Empty is the default and stays the default. Layer names are the ones
    # `gate_records` uses: "checks", "tests", "patterns", "residue".
    trust_executor_gates: list[str] = []

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

    def resume_command(self, run_id: str = "", flags: str = "") -> str:
        """How to continue, spelled the way the CLI actually takes it.

        One place decides, because there are four sites that say this — the
        pause escalation, the progress-budget escalation, the report, and the
        `pause` command's own parting line — and every one of them still named
        a run id alone months after every command started with a config path.
        None of it was checkable: the strings are console output, so nothing
        fails when they go stale, and an operator following the message gets a
        usage error at whatever hour the run stopped.

        The run id is optional in the CLI and it is optional here, for the same
        reason: it defaults to the newest run in the config's work dir, which
        is nearly always the one that just stopped.
        """
        return self._command("resume", run_id, flags)

    def start_command(self, flags: str = "") -> str:
        """How to begin a new run — the answer whenever a resume is refused.

        Beside `resume_command` rather than spelled out at the one site that
        needs it, because that site is console output and console output is
        what goes stale unnoticed.
        """
        return self._command("run", "", flags)

    def _command(self, verb: str, run_id: str = "", flags: str = "") -> str:
        where = str(self.config_path) if self.config_path else "<config>"
        return " ".join(
            part for part in (f"code-gantry {verb}", where, run_id, flags) if part
        )

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

    def _test_tool(self, name: str | None) -> ProjectTool | None:
        """The declared tool a test-tool field names, or None.

        Resolved by lookup rather than by position or convention, so a name
        that matches nothing is a startup failure in `validate` instead of a
        stage that quietly runs everything.
        """
        if not name:
            return None
        return next((t for t in self.project_tools if t.name == name), None)

    def scoped_test_argv(self, paths: list[str]) -> list[str] | None:
        """The same suite, taking these.

        Built through `build_argv`, which is the path the executor's own tool
        call takes. One builder, so the string the gate records and the string
        the model caused are the same string whenever the selection is.
        Uncapped: `max_values` bounds the model's call, and this is the wider
        run that refusal tells the model is the pipeline's to make.
        """
        tool = self._test_tool(self.scoped_test_tool)
        if tool is None or not paths:
            return None
        from code_gantry.projecttools import build_argv

        slot = next((a.name for a in tool.arguments if a.repeated), None)
        if slot is None:
            return None
        return build_argv(tool, {slot: list(paths)}, capped=False)

    @property
    def scoped_test_command(self) -> str | None:
        """The scoped selection as a template, for logs and for `validate`.

        The placeholder is left where the operator put it, which is what makes
        this readable as "the same runner, taking these". Nothing runs it: the
        argv builder is the only path to an actual invocation, so this cannot
        drift into being a second way to spell the command.
        """
        tool = self._test_tool(self.scoped_test_tool)
        return " ".join(tool.command) if tool else None

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
            ("full_test_command", self.full_test_command),
            ("scoped_test_command", self.scoped_test_command),
        ):
            if command:
                out.append((label, command))
        for i, command in enumerate(self.stage_defaults.preconditions):
            out.append((f"stage_defaults.preconditions[{i}]", command))
        for i, command in enumerate(self.stage_defaults.context_commands):
            out.append((f"stage_defaults.context_commands[{i}]", command))
        for i, command in enumerate(self.stage_defaults.checks):
            out.append((f"stage_defaults.checks[{i}]", command))
        for tool in self.project_tools:
            # Joined for scanning only. The denylist reads shell-shaped strings
            # — `git push`, `rm -rf` — and an argv list would hide `["git",
            # "push"]` from every pattern in it. What actually runs is still
            # the list; this is the string the operator would have written.
            out.append((f"project_tools.{tool.name}", " ".join(tool.command)))
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


def _git_root(start: Path) -> Path | None:
    """The repository `start` is in, or None.

    Walked rather than shelled out to: this runs during config parsing, which
    the tests call thousands of times, and `git rev-parse` is a process each.
    """
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _expanded(value: str, field: str) -> Path:
    """A path from a shared, tracked file: `~` and `${VAR}` resolved."""
    text = os.path.expanduser(str(value))
    missing = [
        name for name in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)
        if name not in os.environ
    ]
    if missing:
        raise ConfigError(
            [f"{field}: {', '.join(missing)} is not set in the environment"]
        )
    return Path(os.path.expandvars(text))


def parse_config(data: dict, source: Path | str | None = None) -> ProjectConfig:
    """`source` is the file it was read from, which locates the repo.

    Passed rather than stored on the config so that building one in a test, or
    from a dict, stays a pure function of its argument.
    """
    if not isinstance(data, dict):
        raise ConfigError(["config must be a YAML mapping"])

    data = dict(data)
    for field in ("target_repo", "work_dir", "env_file"):
        if data.get(field) is not None:
            data[field] = _expanded(data[field], field)
    # The enclosing git repository, found by walking up. Not a fixed depth:
    # the config sits beside the plan documents it belongs with, and a plan
    # root is commonly several directories down.
    if source is not None:
        source = Path(source).resolve()
        data.setdefault("config_path", source)
        root = _git_root(source.parent)
        if root is not None:
            data.setdefault("target_repo", root)
        repo = data.get("target_repo")
        if repo is not None and source.is_relative_to(Path(repo).resolve()):
            data.setdefault("config_rel_path", str(source.relative_to(Path(repo).resolve())))
        # Against the config's directory, not the cwd. A relative path decided
        # by the launch command appears in no config, no log and no artifact —
        # which is how a 1.76MB rate table landed in a tracked directory and
        # took a run's prompt past its ceiling.
        if data.get("env_file") is not None:
            declared = Path(data["env_file"])
            if not declared.is_absolute():
                data["env_file"] = source.parent / declared
    if data.get("target_repo") is None:
        raise ConfigError(
            ["target_repo is not set and the config was not read from a file, "
             "so the repository it describes cannot be derived"]
        )

    try:
        cfg = ProjectConfig.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_pydantic_errors(e)) from e

    if cfg.work_dir is None:
        # Beside the plan documents, because they are the same project: the
        # plan says what the migration is, the run data says what happened to
        # it, and a later reader wants them in one place. `plan_root` is a
        # document, so its directory is the project's.
        cfg = cfg.model_copy(
            update={
                "work_dir": cfg.target_repo / Path(cfg.plan_root).parent
                / ".code_gantry"
            }
        )

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

    return parse_config(data, source=path)


def _glob_could_match_a_test(glob: str, test_patterns: list[str]) -> bool:
    """Could a file written under `glob` be recognised as a test?

    Deliberately permissive. `spec/**` is how a planner usually grants room for
    a spec, and demanding it name the exact file would reject the common,
    correct form. The question is whether there is *anywhere* to put one, not
    whether the planner predicted its name.
    """
    from code_gantry.globs import glob_to_regex, matches_any

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

    if not stage.instruction:
        problems.append(f"{where}: a stage requires an instruction")

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
            f"{cfg.base_ref!r}: CodeGantry must never commit to the "
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

    for label in ("scoped_test_command",):
        command = getattr(cfg, label)
        if command and "{paths}" not in command:
            problems.append(
                f"{label} must contain a {{paths}} placeholder — that is the "
                "slot CodeGantry fills with the stage's changed files"
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

    if cfg.ledger is not None:
        if not _KEY_PREFIX.match(cfg.ledger.key_prefix):
            problems.append(
                f"ledger.key_prefix {cfg.ledger.key_prefix!r} must be letters, "
                "digits, '_' or '-', starting with a letter"
            )
        if cfg.ledger.fold_ratio <= 0:
            problems.append("ledger.fold_ratio must be positive")
        if cfg.ledger.note_chars < 80:
            problems.append("ledger.note_chars must be at least 80")

    problems.extend(_test_tool_problems(cfg))

    for pattern in cfg.test_file_patterns:
        if not pattern:
            problems.append("test_file_patterns contains an empty pattern")

    for role, endpoint in (
        ("executor", cfg.executor),
        ("planner", cfg.planner),
        ("reviewer", cfg.reviewer),
    ):
        problems.extend(_request_extra_problems(role, endpoint))
        if endpoint.api_base and endpoint.api_base_env:
            problems.append(
                f"{role} sets both api_base and api_base_env. Pick one — "
                "silently preferring either would hide the mistake, and which "
                "endpoint gets called is not a detail to guess at"
            )

    problems.extend(_tool_name_problems(cfg.project_tools))
    problems.extend(denylist_violations(cfg.all_commands()))
    return problems


RESERVED_REQUEST_KEYS = frozenset(
    {
        # Who answers. A router is chosen by naming one here as the model, not
        # by an operator swapping it underneath the call.
        "model",
        # The conversation. Replacing it would discard the prompt the whole
        # caching arrangement is built around.
        "input",
        "messages",
        # The capability partition. This is the safety story: the executor can
        # do exactly what its tool schemas allow, and a config key able to
        # extend that list would be a way around every guard in `config.py`.
        "tools",
        "tool_choice",
        # The cache controls the loop sets deliberately. GPT-5.6 does not fall
        # back to a longest matching prefix, so these are load-bearing rather
        # than advisory, and an operator overriding them would show up only as
        # an unexplained bill.
        "prompt_cache_options",
        "prompt_cache_key",
        # Derived from the project identity, like the two above. A constant in
        # config would put two projects, or two roles, on one session.
        "session_id",
    }
)


def _request_extra_problems(role: str, endpoint) -> list[str]:
    """Refuse a passthrough that would replace something we set on purpose.

    Asked at config load because that is where it can first be answered, and
    because the alternative — noticing at the call site — means noticing a
    changed tool list by its consequences.
    """
    extra = getattr(endpoint, "request_extra", None) or {}
    return [
        f"{role}.request_extra sets {key!r}, which CodeGantry sets itself. "
        "Extra parameters are merged into the request and may add to it, "
        "never replace what makes an attempt what it is — the model, the "
        "conversation, the tool schemas or the cache controls."
        for key in sorted(extra)
        if key in RESERVED_REQUEST_KEYS
    ]


def _tool_name_problems(tools: list[ProjectTool]) -> list[str]:
    """Declared tool names must be unique and must not shadow a built-in.

    Two tools with one name is not a merge — it is whichever the provider
    happens to pick, and the model cannot tell it got the wrong one. Checked
    here rather than on `ProjectTool` because it is a property of the set.
    """
    from code_gantry.projecttools import BUILTIN_TOOL_NAMES

    problems: list[str] = []
    seen: set[str] = set()
    for tool in tools:
        if tool.name in BUILTIN_TOOL_NAMES:
            problems.append(
                f"project_tools: {tool.name!r} is the name of a built-in tool. "
                "Two tools with one name is not a merge — the model cannot tell "
                "which one it reached."
            )
        if tool.name in seen:
            problems.append(f"project_tools: {tool.name!r} is declared twice")
        seen.add(tool.name)
    return problems


def _test_tool_problems(cfg: ProjectConfig) -> list[str]:
    """The two test selections, resolved against the declared tools.

    A reference rather than a command, so the failure modes are a reference's:
    a name matching nothing, and a tool whose shape cannot serve the role. Both
    are startup failures here rather than a stage discovering them, because the
    symptom otherwise is a command that runs everything or runs nothing and
    says neither.
    """
    problems: list[str] = []
    by_name = {t.name: t for t in cfg.project_tools}
    for field, name in (("scoped_test_tool", cfg.scoped_test_tool),):
        if not name:
            problems.append(
                "scoped_test_tool is not set. Without it every attempt of "
                "every stage runs the whole suite, which on a large project "
                "is the most expensive mistake this config can make"
            )
        elif name not in by_name:
            problems.append(
                f"{field} names {name!r}, which is not a declared project "
                f"tool. Declared: {', '.join(sorted(by_name)) or '(none)'}"
            )

    scoped = by_name.get(cfg.scoped_test_tool or "")
    if scoped is not None:
        if not any(a.repeated for a in scoped.arguments):
            problems.append(
                f"scoped_test_tool {scoped.name!r} declares no repeated "
                "argument, so there is nowhere to put the selection. That is "
                "what separates it from the full suite"
            )
        # No placeholder check here: `_tool_problems` already refuses an
        # argument the command never uses, with a better message, and a
        # repeated argument is required above — so a scoped tool without a
        # slot cannot be built. A second guard would be one nothing can reach.

    if not cfg.full_test_command and not cfg.stage_defaults.checks:
        problems.append(
            "a project needs full_test_command, or checks in stage_defaults, "
            "or nothing will verify its stages. scoped_test_tool alone is not "
            "enough: a stage whose diff scopes to no test would run nothing"
        )
    return problems
