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

from orchestrator.config import ExecutorConfig
from orchestrator.executortools import dispatch, openai_tool_schemas
from orchestrator.openaiclient import (
    TokenUsage,
    describe_call,
    extract_usage,
    merge_usage,
    refusal,
    tool_request,
    transport_errors,
)
from orchestrator.retry import Backoff, with_provider_retry


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
        self.text: str = ""
        self.failure: str = ""
        self.peak_prompt_tokens: int = 0
        # The first turn of a cycle, kept apart from the total. Summed
        # usage cannot answer whether the static prefix survived from the
        # previous stage: within one attempt the conversation grows and
        # every later turn re-reads what the first one wrote, so a run
        # with no cross-stage reuse at all still reports 90%-plus.
        self.first_prompt_tokens: int = 0
        self.first_cached_tokens: int = 0


def _reasoning_param(cfg: ExecutorConfig) -> dict:
    """Effort, only when the operator chose one.

    Absent by default so a model that does not take the parameter is not sent
    it, and so no default of ours overrides a provider's.
    """
    effort = getattr(cfg, "reasoning_effort", None)
    return {"reasoning": {"effort": effort}} if effort else {}


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
        self._client = client if client is not None else build_openai_client(cfg)

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
            from orchestrator.planner import _render_call

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
        return openai_tool_schemas(semantic, self.project_tools)

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

        extra: dict = {
            # GPT-5.6 caches at breakpoints and does not fall back to the
            # longest matching prefix, so the opt-in is required rather than
            # helpful. The marks themselves go on the tool results below.
            "prompt_cache_options": {"mode": "explicit"},
            **_reasoning_param(self.cfg),
        }
        if cache_key:
            extra["prompt_cache_key"] = cache_key

        for _ in range(self._max_turns()):
            try:
                response = with_provider_retry(
                    lambda: self._client.responses.create(
                        model=self.cfg.model,
                        # A plain list on the wire. `conversation` is a
                        # `Transcript` — a list subclass that mirrors itself to
                        # disk — and what the SDK does with a subclass is its
                        # business rather than a fact we should be relying on.
                        # Copying it costs one shallow list per HTTP call.
                        input=list(conversation),
                        tools=tools,
                        **extra,
                    ),
                    retry_on=transport_errors(),
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
            except Exception as e:  # noqa: BLE001 - any failure ends the cycle
                out.failure = f"the executor call failed: {e}"
                return out

            out.turns += 1
            turn_usage = extract_usage(getattr(response, "usage", None))
            out.usage = merge_usage(out.usage, turn_usage)
            # A high-water mark of one context, not a sum: what the operator
            # needs for stage sizing is how much the model actually held.
            out.peak_prompt_tokens = max(
                out.peak_prompt_tokens, turn_usage.prompt_tokens
            )
            if out.turns == 1:
                out.first_prompt_tokens = turn_usage.prompt_tokens
                out.first_cached_tokens = turn_usage.cached_tokens

            declined = refusal(response)
            if declined:
                out.failure = f"the executor refused to answer: {declined}"
                return out

            requests = [
                item
                for item in (getattr(response, "output", None) or [])
                if getattr(item, "type", "") == "function_call"
            ]
            if not requests:
                out.stopped = True
                out.text = _final_text(response)
                return out

            # The whole turn back, then its results — every output item, not
            # just the calls, because each `function_call` declares the
            # reasoning item as required and echoing one without it is
            # rejected.
            conversation.extend(getattr(response, "output", None) or [])
            for item in requests:
                name, args = tool_request(item)
                out.calls.append(describe_call(name, args))
                conversation.append(
                    {
                        "type": "function_call_output",
                        "call_id": item.call_id,
                        "output": [
                            {
                                "type": "input_text",
                                "text": dispatch(
                                    name,
                                    args,
                                    reader,
                                    editor,
                                    semantic,
                                    project_tools=self.project_tools,
                                    runner=self.runner,
                                ),
                                # Marks accumulate rather than move, so every
                                # turn extends the cached prefix instead of
                                # restarting it.
                                "prompt_cache_breakpoint": {"mode": "explicit"},
                            }
                        ],
                    }
                )
            logged = self._log_new_calls(reader, editor, logged)

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
