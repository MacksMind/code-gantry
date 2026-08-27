"""The executor's side of the Responses API.

Structured like `reviewer.py`, because the hard-won parts of that file are
properties of the API rather than of the question being asked, and both were
learned from live calls rather than from documentation:

- **A reasoning model's tool call must be echoed back with its reasoning item.**
  Sending the `function_call` alone is rejected — "was provided without its
  required 'reasoning' item" — so the whole `output` list goes back, not just
  the calls. No stub can show this, because a stub has no reasoning item to
  omit.
- **Cache breakpoints accumulate rather than move.** A request writes only its
  latest four marks, but matching considers up to the latest eighty in the
  conversation, so marking every tool result extends the cached prefix instead
  of restarting it. Without it the loop re-sends every earlier result at full
  price and cost grows with the square of the turn count.

What differs from the reviewer is the shape of an answer. The reviewer parses a
verdict; the executor has no structured output at all. Its work is the edits it
made, and a turn with no `function_call` items is how it says it has finished —
which is deliberately *not* a claim of success. "I am done" and "this needs a
file outside my scope so I have stopped" are the same signal here, and the
gates decide which happened. Prefer the fact to the label.
"""

from __future__ import annotations

import os

from code_gantry.config import ExecutorConfig
from code_gantry.dialects import without_cache_markers
from code_gantry.gateway import gateway_body, gateway_effort_body
from code_gantry.executortools import REPLAN_TOOL, dispatch, openai_tool_schemas
from code_gantry.openaiclient import (
    TokenUsage,
    describe_call,
    extract_usage,
    merge_usage,
    refusal,
    tool_request,
)
from code_gantry.retry import Backoff, is_stale_cache_rejection, with_provider_retry


class ExecutorTurn:
    """One exchange with the model, and what it did.

    Not a dataclass with a verdict, because there is no verdict. The loop reads
    `stopped` to know the model has finished asking for things, and everything
    else is accounting.
    """

    def __init__(self) -> None:
        self.usage = TokenUsage()
        self.calls: list[str] = []
        self.turns: int = 0
        self.stopped: bool = False
        # Set by `request_replan`, and the one thing here the model asserts
        # rather than the gates measure. That looks like a contradiction of the
        # note above — "I am done" and "I have stopped" being one signal, with
        # the gates deciding which — and it is not, because this claims nothing
        # about the work. It says the *stage* is wrong or too small; every gate
        # still runs, nothing lands, nothing is approved. What the gates cannot
        # supply is why: measured on one run, an executor that correctly found
        # nothing left to do reached the planner as "the attempt reproduced the
        # previous diff", and one whose change broke files outside its scope
        # reached the executor again as "tests failed".
        self.replan_kind: str = ""
        self.replan_reason: str = ""
        self.text: str = ""
        self.failure: str = ""
        # Turns that ended with no tool calls and no text. Counted rather than
        # flagged, because one is a hiccup that the nudge below resolves and
        # two in a row is the model's answer. `stopped` cannot carry this: it
        # is true for a model that finished and for one that gave up, and the
        # wire renders those identically — `stop_reason` is the same either
        # way, and the only difference is that one response carries content.
        self.empty_finishes: int = 0
        # How the last turn of this cycle ended, as the wire described it.
        # The counter above is the *consequence* — this is the cause, and
        # until it was recorded the executor could say a turn had ended and
        # not why. Same shape the planner and reviewer write.
        self.turn_end: dict | None = None
        # The first turn of a cycle, kept apart from the total. Summed
        # usage cannot answer whether the static prefix survived from the
        # previous stage: within one attempt the conversation grows and
        # every later turn re-reads what the first one wrote, so a run
        # with no cross-stage reuse at all still reports 90%-plus.
        self.first_prompt_tokens: int = 0
        self.first_cached_tokens: int = 0
        # What actually answered, per turn. A count rather than a name because
        # a router resolves per request: `openrouter/pareto-code` was measured
        # answering one call shape as `openai/gpt-5.6-sol` and another as
        # `x-ai/grok-4.6`, and nothing forbids that happening inside a single
        # attempt. Against a pinned model this is one key with the turn count
        # on it, which is exactly the uninteresting answer that makes the
        # interesting one legible.
        self.served_models: dict[str, int] = {}

    def note_served(self, model) -> None:
        """Record which model answered a turn.

        Silent on an absent name rather than counting `""`: a provider that
        does not echo the model is a provider we cannot attribute, and an empty
        key would render as a model whose name is nothing.
        """
        if not model:
            return
        self.served_models[model] = self.served_models.get(model, 0) + 1


EMPTY_FINISH_PROMPT = (
    "That turn ended with no tool call and nothing said, which leaves no way "
    "to tell whether you finished or stopped. Say which. If the work is done, "
    "say what you changed. If you cannot do it — the stage contradicts itself, "
    "or needs a file you may not touch — call `{replan}` and say why. "
    "Otherwise carry on."
).format(replan=REPLAN_TOOL["name"])


def request_extra(cfg: ExecutorConfig) -> dict:
    """Operator-declared request parameters, as SDK kwargs.

    Empty when nothing was declared, for the reason `_reasoning_param` gives
    just below: a model that does not take a parameter should not be sent one,
    and no default of ours should override a provider's.

    Wrapped in `extra_body` because these are body fields the SDK has no named
    argument for — a router's `plugins` is not part of the Responses schema and
    would be dropped rather than sent if handed over as a keyword.
    """
    extra = getattr(cfg, "request_extra", None) or {}
    return {"extra_body": dict(extra)} if extra else {}



def _dialect(cfg):
    """The wire this role's configured model wants.

    Falls back to what this client actually speaks when the model names a
    routing policy — a policy resolves per run and cannot be classified, and a
    run must not fail here over it. `wirecheck` reports the case where the two
    genuinely disagree.
    """
    from code_gantry.dialects import RESPONSES, dialect_for

    try:
        return dialect_for(getattr(cfg, "model", ""))
    except ValueError:
        return RESPONSES


def _status_of(failure) -> int | None:
    """The HTTP status a provider failure carries, if it carries one.

    The same reading `with_provider_retry` takes by default, and spelled once:
    both SDKs put it in the same place, and two copies of that fact is how a
    classifier comes to disagree with the retry that runs beside it.
    """
    return getattr(failure, "status_code", None)


def _reasoning_param(cfg: ExecutorConfig) -> dict:
    """Effort, only when the operator chose one.

    Absent by default so a model that does not take the parameter is not sent
    it, and so no default of ours overrides a provider's.
    """
    effort = getattr(cfg, "reasoning_effort", None)
    return {"reasoning": {"effort": effort}} if effort else {}


def request_extras(cfg, session_id: str = "", cache_key: str | None = None) -> dict:
    """Every top-level keyword the executor's call carries beyond the basics.

    One function because it is one decision, and because the two outages it
    exists to prevent were both a keyword the endpoint does not take —
    `session_id` to `responses.create`, then `prompt_cache_key` to
    `messages.create`. Each was pinned afterwards by a test that rebuilt this
    dict by hand, so each test could only see the keys whoever wrote it
    remembered. A copy of an assembly is not a check on it.

    Assembled here, the loop adds nothing of its own and the seam test reads
    what production reads. That is the same answer as the transcript being a
    `list` subclass and `executor-loop.json`'s writer walking
    `dataclasses.fields`: make the recording a property of the only thing that
    can change it.
    """
    wire = _dialect(cfg)
    level = getattr(cfg, "reasoning_effort", None) or getattr(cfg, "effort", None)
    # Effort is spelled for the route on this wire, not for the model. See
    # `gateway_effort_body` for the measurement; the short version is that
    # `output_config` 404s through the gateway for anything whose upstream is
    # not Anthropic, and `require_parameters` turns that into every provider
    # being excluded rather than one parameter being ignored.
    via_body = gateway_effort_body(cfg, level) if wire.effort_in_gateway_body else {}
    declared = dict(request_extra(cfg).get("extra_body") or {})
    return {
        # GPT-5.6 caches at breakpoints and does not fall back to the longest
        # matching prefix, so the opt-in is required rather than helpful. The
        # marks themselves go on the tool results. Spelled by the dialect the
        # model's family wants: the wire is a property of the model, not of
        # this file.
        **wire.cache_options(),
        # Spelled by the dialect for the same reason, and it is the one that
        # was not: `prompt_cache_key` is a Responses parameter, and hardcoded
        # at the call site it reached `messages.create()` and ended a run.
        **wire.cache_key_param(cache_key),
        **({} if via_body else wire.effort(level)),
        # Last, but it cannot reach anything above it: the reserved keys are
        # refused at config load, so this adds and never replaces. One body for
        # both, built by `merged_body` — these were two separate splats and the
        # second silently replaced the first.
        # An operator's own declared fields win outright, so ours go under.
        **gateway_body(cfg, session_id, {**via_body, **declared}),
    }


class OpenAIExecutorModel:
    """Drives one edit cycle: the model calls tools until it stops."""

    def __init__(
        self,
        cfg: ExecutorConfig,
        client=None,
        log=None,
        tool_log=None,
        project_tools=None,
        runner=None,
    ):
        self.cfg = cfg
        self.log = log
        # Operator-declared tools and the runner that executes them. Both come
        # from the `ProjectConfig`, which this class does not otherwise see —
        # it is built from `cfg.executor` alone. Passed rather than reached
        # for, because a client that could find the project config could find
        # anything in it.
        self.project_tools = list(project_tools or [])
        self.runner = runner
        # Its own file, like the other two roles. This one has never been in
        # the run log at all and could not be: sixty calls a cycle would drown
        # a timeline, which is why `nodes.execute` reports counts. The full
        # exchange with results is still the per-attempt transcript; this is
        # the one-line-per-call view, in the same file the planner and reviewer
        # write to, so one `tail -f` shows every role.
        self.tool_log = tool_log
        # Constant for the life of a run, so it is bound here rather than
        # handed over on every call — the same reasoning that puts the tool
        # menu and the runner on the instance. Assigned by `Executor`, which is
        # the only thing that knows the project identity this is derived from.
        self.session_id: str | None = None
        # Built by the dialect the model's family wants, so this role speaks
        # whichever wire its model caches best on. Injected clients are left
        # alone — the tests supply their own.
        self._client = client if client is not None else _dialect(cfg).client(cfg)

    @staticmethod
    def _watermark(reader, editor) -> tuple[int, int]:
        """Where each ledger stands, as one value the caller cannot mis-shape.

        Derived from the ledgers rather than written out at the call site, for
        the reason the pair exists at all: a second place that knows the shape
        is a second place to get it wrong.
        """
        return (
            len(getattr(reader, "calls", []) or []),
            len(getattr(editor, "calls", []) or []),
        )

    def _log_new_calls(self, reader, editor, seen: tuple[int, int]) -> tuple[int, int]:
        """Emit the calls made since `seen`; return the new watermark.

        Two ledgers, because reads and edits are recorded by different objects
        — `_count_tool_use` merges the same pair for the summary line. Order
        within a turn is reads then edits rather than the true interleaving,
        which the split ledgers cannot recover; the turn boundary is preserved
        and that is what a reader following along actually needs.

        **One watermark per ledger.** This was a single index into the two
        concatenated, and a concatenation's middle moves: every read appended
        during a turn pushes the whole editor half one place right, so the next
        slice began inside edits already logged. It re-printed those and
        skipped the reads that displaced them. Measured over one run of 992
        calls, `tools.log` claimed 479 edits against 117, 267 reads against
        455, and none of the 37 `git_diff` calls — eleven consecutive
        `edit(config/routes.rb)` lines for two real edits, which reads as a
        model thrashing on a file. Nothing was wrong with either ledger, and
        the summary line beside it in `run.log` was exact the whole time,
        because it counts the pair rather than slicing them joined.
        """
        reads, edits = seen
        if self.tool_log:
            from code_gantry.planner import _render_call

            new = list((getattr(reader, "calls", []) or [])[reads:]) + list(
                (getattr(editor, "calls", []) or [])[edits:]
            )
            for call in new:
                self.tool_log(f"[execute] {_render_call(call)}")
        return self._watermark(reader, editor)

    def _max_turns(self) -> int:
        """Backstop, not the real ceiling.

        The editor and the reader refuse past their own budgets and the model
        reads those refusals. This catches a model that ignores them and keeps
        asking, which would otherwise hold a stage open until the request
        timeout.
        """
        return max(getattr(self.cfg, "max_model_turns", 20), 1)

    def _tools(self, semantic) -> list:
        """The menu actually sent to the provider.

        Its own method so a test can assert what is sent rather than what is
        held — this seam has broken twice, both times with the constructor
        taking the argument and nothing carrying it further.
        """
        from code_gantry.executortools import tool_schemas

        return _dialect(self.cfg).tool_schemas(
            tool_schemas(semantic, self.project_tools)
        )

    def _send(self, conversation, tools, extra):
        """One request, waited out on the wire it actually goes out on.

        `retry_on` used to come from `openaiclient.transport_errors()` at the
        call site — OpenAI's classes, whatever wire the model resolved to. An
        `anthropic.APIStatusError` is not an `openai.APIStatusError`, so on the
        Messages wire the tuple matched nothing and every failure propagated on
        its first raise: no backoff, no log line, four attempts in four
        seconds. Asking the dialect is what keeps the question and the client
        answering it in one place.
        """
        wire = _dialect(self.cfg)
        return with_provider_retry(
            lambda: wire.send(self._client, self.cfg, conversation, tools, extra),
            retry_on=wire.transport_errors(),
            transient=Backoff(
                budget_seconds=self.cfg.transport_retry_seconds,
                max_delay_seconds=self.cfg.transport_retry_max_delay_seconds,
            ),
            spurious=Backoff(
                budget_seconds=self.cfg.invalid_request_retry_seconds,
                initial_seconds=self.cfg.invalid_request_initial_seconds,
                factor=self.cfg.invalid_request_factor,
            ),
            log=self.log,
        )

    def run(
        self,
        conversation: list,
        reader,
        editor,
        semantic=None,
        cache_key: str | None = None,
    ) -> ExecutorTurn:
        """Let the model work until it stops calling tools.

        `conversation` is mutated in place and handed back to the caller, so a
        later cycle can append feedback and continue rather than restarting —
        which is what keeps the cached prefix intact across cycles within one
        attempt.
        """
        out = ExecutorTurn()
        tools = self._tools(semantic)
        # Both ledgers, cumulative across the turns of one attempt — and one
        # watermark each, because they grow independently.
        logged = self._watermark(reader, editor)

        extra: dict = request_extras(self.cfg, self.session_id, cache_key)

        # Whether this attempt has given up on the provider's cache. Local to
        # the attempt rather than to the turn: a handle that is dead for this
        # turn is dead for the next one, and re-marking would pay the same
        # rejection once per turn.
        cold = False

        for _ in range(self._max_turns()):
            try:
                response = self._send(conversation, tools, extra)
            except Exception as e:  # noqa: BLE001 - any failure ends the cycle
                if cold or not is_stale_cache_rejection(_status_of(e), str(e)):
                    out.failure = f"the executor call failed: {e}"
                    return out
                # The conversation was never the problem: what was refused is a
                # pointer to a cache the provider had built for our marked
                # prefix and then dropped. So the same context goes back out
                # whole, with nothing marked — the first turn again, at the
                # price of a first turn.
                cold = True
                conversation[:] = without_cache_markers(conversation)
                extra = request_extras(self.cfg, self.session_id, None)
                if self.log:
                    self.log(
                        "the provider's cache reference had expired; resending "
                        f"the whole context uncached: {e}"
                    )
                try:
                    response = self._send(conversation, tools, extra)
                except Exception as again:  # noqa: BLE001 - the cold send is the last word
                    out.failure = f"the executor call failed: {again}"
                    return out

            out.turns += 1
            out.turn_end = _dialect(self.cfg).turn_end(response).as_record()
            # Through the wire it arrived on. This was `extract_usage`,
            # OpenAI's reader, applied to every response — so a
            # Messages attempt lost `cache_read_input_tokens` and
            # `cache_creation_input_tokens`, which have no counterpart
            # there, and reported 0% cached while the provider's own
            # logs showed the cache working.
            turn_usage = _dialect(self.cfg).usage(getattr(response, "usage", None))
            # The peak rides on `usage`: `extract_usage` sets it from the one
            # reading and `merge_usage` maxes it while everything else sums, so
            # a high-water mark of one context — what the operator needs for
            # stage sizing — comes out of the same merge as the bill. This used
            # to be a `max` written out here, from before `TokenUsage` had the
            # field, and keeping both would be one number in two places.
            out.usage = merge_usage(out.usage, turn_usage)
            out.note_served(getattr(response, "model", None))
            if out.turns == 1:
                out.first_prompt_tokens = turn_usage.prompt_tokens
                out.first_cached_tokens = turn_usage.cached_tokens

            declined = _dialect(self.cfg).refusal(response)
            if declined:
                out.failure = f"the executor refused to answer: {declined}"
                return out

            wire = _dialect(self.cfg)
            requests = wire.tool_calls(response)
            if not requests:
                closing = wire.final_text(response)
                if not closing.strip() and not out.empty_finishes:
                    # Measured over one run's 76 attempts: 6 ended here with
                    # nothing at all — no text, no calls, no edits — and were
                    # recorded `ok: True` with an empty log, one of them after
                    # 96 searches and $0.35. The scope gate then reported "the
                    # attempt produced no changes", which reads as a badly
                    # drawn stage and sends the planner to redraw one that was
                    # never the problem.
                    #
                    # Asked here because this is where the question can first
                    # be answered, and asked *once*: a turn against a wasted
                    # attempt is cheap, and a model that answers silence twice
                    # has said what it means. The response itself is not
                    # appended — an assistant message whose content is empty
                    # is not a message the next request can carry — so this
                    # user turn is also the only record that it happened.
                    out.empty_finishes += 1
                    conversation.append(
                        {
                            "role": "user",
                            "content": [{"type": "input_text", "text": EMPTY_FINISH_PROMPT}],
                        }
                    )
                    continue
                out.stopped = True
                out.text = closing
                if not closing.strip():
                    out.empty_finishes += 1
                else:
                    # The turn that ends the attempt, in the record of the
                    # attempt. This branch used to return without appending,
                    # so `executor-conversation.jsonl` ended on the last tool
                    # call and never held what the model said to finish —
                    # a hole at exactly the last item, in a transcript whose
                    # whole design is that appending is the only way to record
                    # so that nothing can forget.
                    #
                    # Only when it said something. A message with no content
                    # is not one the next request can carry, and the nudge
                    # above already leaves a record of that case.
                    #
                    # The second effect is deliberate: cycles within an
                    # attempt share this conversation, so a rework now opens
                    # with the model's own account of what it did in front of
                    # it, rather than a gate failure attached to a
                    # conversation that ends mid-tool-call.
                    wire.append_model_turn(conversation, response)
                return out

            # The model's turn back, in whichever shape this wire wants — the
            # whole output list on Responses, because each `function_call`
            # declares its reasoning item as required; one assistant message
            # of blocks on Messages, thinking included, because a dropped
            # thinking block breaks the turn it belongs to.
            wire.append_model_turn(conversation, response)
            answers: list[tuple[str, str]] = []
            for req in requests:
                name, args = req["name"], req["args"]
                out.calls.append(describe_call(name, args))
                # Answered here rather than in `dispatch`, which is text in and
                # text out for every other tool. A control signal returned as a
                # string would have to be recognised by matching that string —
                # a classifier over rendered text, which is how three different
                # refusal causes became indistinguishable once they rendered
                # the same sentence. The reply is still appended, because a
                # tool call with no result leaves the conversation malformed
                # for the provider.
                if name == REPLAN_TOOL["name"]:
                    out.replan_kind = str(args.get("kind") or "")
                    out.replan_reason = str(args.get("reason") or "")
                answers.append(
                    (
                        req["id"],
                        dispatch(
                            name,
                            args,
                            reader,
                            editor,
                            semantic,
                            project_tools=self.project_tools,
                            runner=self.runner,
                        ),
                    )
                )
            # Marked, so the cached prefix follows the conversation: on
            # Responses marks accumulate and every turn extends it; on Messages
            # only the last one counts, so it moves.
            wire.append_tool_results(
                conversation, answers, cache=not cold,
                ttl=getattr(self.cfg, "cache_ttl", None),
            )
            logged = self._log_new_calls(reader, editor, logged)

            # The model has handed the stage back, so there is nothing further
            # to ask it. `stopped` is true in the sense the loop reads it —
            # finished asking for things — and the reason it stopped travels
            # beside it rather than being inferred from the absence of calls.
            if out.replan_kind:
                out.stopped = True
                return out

        # Ran out of turns with the model still asking for things. Not a
        # failure of the work — whatever it committed stands and the gates will
        # judge it — but the loop must not treat this as "finished".
        out.stopped = False
        return out


def _final_text(response) -> str:
    """The model's closing message, for the attempt log."""
    parts: list[str] = []
    for item in getattr(response, "output", None) or []:
        for part in getattr(item, "content", None) or []:
            text = getattr(part, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def build_openai_client(cfg: ExecutorConfig):
    """The SDK client, pointed wherever the operator pointed it.

    A key comes from the environment by name, never from config: config is
    committed and hashed for approval, and a key in either place is a key in
    the repository.
    """
    from openai import OpenAI

    kwargs: dict = {}
    name = getattr(cfg, "api_key_env", None)
    if name:
        if name not in os.environ:
            raise KeyError(
                f"{name} is not set; the executor cannot authenticate. It is "
                "named in the project config and read from the environment so "
                "that no key is ever written to a file that gets committed."
            )
        kwargs["api_key"] = os.environ[name]

    api_base = cfg.resolve_api_base() if hasattr(cfg, "resolve_api_base") else None
    if api_base:
        kwargs["base_url"] = api_base

    timeout = getattr(cfg, "request_timeout_seconds", None)
    if timeout:
        kwargs["timeout"] = timeout

    # Retrying is ours: `with_provider_retry` bounds the wait by our own wall
    # clock and distinguishes a transient status from a spurious 400, which the
    # SDK's own counter cannot do.
    kwargs["max_retries"] = 0

    return OpenAI(**kwargs)
