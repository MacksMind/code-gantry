"""What a gateway needs that a first-party endpoint does not.

One place, because all three roles talk to the same gateway when they are
pointed at it, and a behaviour re-implemented per role is a behaviour that
drifts. `for_role` in `projecttools` is here for the same reason: one function
evaluated three times rather than three filters free to disagree.

Everything here is decided from the **endpoint**, never declared. Which API
accepts these fields is a property of the provider — the same category as
`prompt_cache_options` being OpenAI's — and getting it wrong is not a missed
optimisation but a 400 on every call, because a first-party endpoint rejects
an argument it does not recognise. An operator chooses OpenRouter by pointing
`api_base` at it; nobody should have to know these fields exist.
"""

from __future__ import annotations

from urllib.parse import urlparse


def is_openrouter(api_base) -> bool:
    """Whether this endpoint is OpenRouter, by host.

    Matched on the host rather than on the string appearing anywhere in the
    URL: a path that happens to contain the name is a different service.
    """
    if not api_base:
        return False
    host = (urlparse(str(api_base)).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def gateway_body(cfg, session_id: str = "", declared: dict | None = None) -> dict:
    """The body fields a gateway call carries, merged into one `extra_body`.

    One dict, not several kwargs: `extra_body` given twice in a single splat
    means the later silently replaces the earlier, and none of these are
    parameters the SDKs declare — handed over as keywords they are a
    `TypeError` on every call, which is how one of them reached production.

    **`provider.require_parameters`** routes only to providers that can honour
    what the request carries. Not a provider list: `provider.only` pins one
    upstream and discards the fallback that is the reason to use a gateway.
    Measured, and the tell was a token count rather than an answer — a planner
    call reached a provider without structured-output support carrying 107
    input tokens against 6,929 on every call that parsed, because
    `output_format` serialises an 18,400-character schema and 107 tokens is
    the bare prompt. The parameter was stripped in transit, and prose is a
    well-formed answer to a prompt with no schema attached.

    **`session_id`** is the gateway's sticky-routing key, not a router control:
    a session goes back to the provider holding the warm cache, and one model
    can be served by several providers with separate caches. So a pinned model
    needs it as much as a routed one. Scoped per run by the caller — keyed on
    anything longer-lived, two runs inside the idle window inherit each other's
    routing and a fresh run cannot re-ask.

    An operator's own declared fields win outright: this is a floor, not a
    ceiling.
    """
    declared = dict(declared or {})
    if not is_openrouter(_api_base(cfg)):
        return {"extra_body": declared} if declared else {}
    body: dict = {"provider": {"require_parameters": True}}
    body.update(declared)
    if session_id:
        body["session_id"] = session_id
    return {"extra_body": body}


def gateway_effort_body(cfg, level) -> dict:
    """Reasoning effort as the gateway spells it, or nothing off-gateway.

    Measured against all three endpoints on 2026-08-22, because recall and the
    docs were both wrong about it:

        spelling                 Anthropic direct   OR -> claude   OR -> gemini
        output_config            OK                 OK             404
        extra_body.reasoning     400                OK             OK

    `output_config` is Anthropic-native and survives the gateway only where
    the upstream *is* Anthropic. OpenRouter's model listing agrees — nothing on
    it declares `output_config` and everything declares `reasoning`. Combined
    with `require_parameters`, which exists so a provider cannot silently drop
    structured output, a parameter no provider can honour excludes every
    provider and the answer is 404 `No endpoints found that can handle the
    requested parameters`: four attempts a second, three stages drawn, no model
    ever reached.

    So the spelling follows the *route*, not the model family, which is why it
    lives here and not on the dialect.
    """
    if not level or not is_openrouter(_api_base(cfg)):
        return {}
    return {"reasoning": {"effort": level}}


def _api_base(cfg):
    resolve = getattr(cfg, "resolve_api_base", None)
    return resolve() if callable(resolve) else getattr(cfg, "api_base", None)


def _probe(cfg) -> str:
    """One throwaway call, to see which model a policy resolves to.

    There is no cheaper way. OpenRouter documents no endpoint that reports what
    a router *would* pick, and its Auto Router "ranks candidates from scratch on
    each turn", so the answer exists only once a request has been made. The
    reply is discarded; only `response.model` is read.

    Always on the Responses surface, whatever the run will use afterwards.
    Measured 2026-08-21: `min_coding_score` separates cleanly there — 0.3 to
    `google/gemini-3.7-flash`, 0.9 to `openai/gpt-5.6-sol`, three times each —
    and has no effect at all through Messages, where both scores returned the
    High tier. Probing on the wrong surface would answer a question about a
    tier we did not ask for.
    """
    from code_gantry.dialects import RESPONSES

    client = RESPONSES.client(cfg)
    # The operator's own routing fields go with it — the score is the whole
    # point of the probe, and a probe sent without it would answer about a
    # tier nobody asked for.
    body = gateway_body(cfg, "", getattr(cfg, "request_extra", None))
    response = client.responses.create(
        model=cfg.model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": "."}]}],
        max_output_tokens=16,
        # The shape of the work, not just a text completion.
        #
        # `provider.require_parameters` filters providers by the parameters
        # *in the request*. A bare prompt carries almost none, so it would
        # happily resolve to a model that cannot do strict function calling or
        # honour the configured effort — and Pareto picks on coding score,
        # which says nothing about capability. The failure would land on stage
        # one, as a router-chosen model that cannot run the workload it was
        # chosen for.
        #
        # One representative strict tool rather than the real menu: the filter
        # reads which parameters are present, not what the schemas contain, and
        # the menu is thousands of tokens on a call whose reply is discarded.
        tools=_PROBE_TOOLS,
        **RESPONSES.effort(getattr(cfg, "reasoning_effort", None)),
        **RESPONSES.cache_options(),
        **body,
    )
    return getattr(response, "model", "") or ""


# Shaped like the executor's own: strict, with every property required and
# `additionalProperties: false`, which is what strict mode demands.
_PROBE_TOOLS = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a range of lines from a file.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    }
]


def resolve_policy(cfg, ask=_probe, log=None):
    """A routing policy, resolved to the model it picks today.

    Returns `cfg` untouched for a concrete model, which must not pay for a call
    that could only confirm itself.

    A failed probe also returns `cfg` untouched, and that branch matters more
    than the happy one: the router still answers, so what is lost is a warm
    prefix and the ability to choose a dialect — not the run. Refusing to start
    would turn an optimisation into a new way to fail.

    Resolving once is not pinning. What per-request routing costs is the
    cross-stage cache, any attribution of an outcome to a model, and the
    dialect — `dialect_for` refuses a policy outright rather than guess, and it
    resolved to three different families in one day.

    Called per stage, by `precheck`. It was called once per run, and the run
    was the wrong unit: the answer was re-sampled only when a human restarted,
    so one run held one model for 30 stages and another for the 11 after a
    resume — a sampling frequency set by operational accidents rather than by
    anything about the work. Per stage follows a price move mid-run, gives
    every stage one model to attribute its cost and its rework to, and lets a
    model that is serving badly stop at the next stage instead of lasting the
    run. Not per attempt and not per turn: those are one conversation, and a
    turn served by a different model reads nothing of the prefix the others
    built.
    """
    from code_gantry.dialects import dialect_for

    try:
        dialect_for(getattr(cfg, "model", ""))
    except ValueError:
        pass  # a policy: the only case worth probing
    else:
        return cfg
    try:
        resolved = ask(cfg)
    except Exception as e:  # noqa: BLE001 - any failure leaves the policy alone
        if log:
            log(f"[preflight] could not resolve {cfg.model!r} ({e}); the router "
                "will pick per request, the prefix will not stay warm, and the "
                "wire cannot be chosen from the model")
        return cfg
    if not resolved or resolved == cfg.model:
        return cfg
    if log:
        log(f"[precheck] {cfg.model} resolved to {resolved} for this stage")
    return cfg.model_copy(update={"model": resolved})
