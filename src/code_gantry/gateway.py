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


def _api_base(cfg):
    resolve = getattr(cfg, "resolve_api_base", None)
    return resolve() if callable(resolve) else getattr(cfg, "api_base", None)
