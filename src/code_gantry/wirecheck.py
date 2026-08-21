"""Whether each role's client can speak the wire its model wants.

`dialects.py` says which wire a model should be called on. Until every role
can speak both, a configured model can want one wire while the role calling it
speaks the other — and that mismatch is silent by construction. On Responses
our OpenAI-dialect cache marks mean nothing to a Gemini model, so the cached
region pins at whatever the provider chose for itself, the bill grows with the
conversation, and no log line names a cause. Finding it once took an afternoon
of probes and three refuted theories.

Reported as a warning rather than a blocker. The call still works and the loss
is fine-grained cache control, not correctness — and a gate that refuses a
working configuration is one people learn to switch off.

This table is a statement about the *clients*, not about the models, so it
stops being true the moment a role learns a second wire. That is exactly when
this file should shrink.
"""

from __future__ import annotations

from code_gantry.dialects import MESSAGES, RESPONSES, dialect_for

# Roles that still speak exactly one wire. The executor is absent because it
# no longer does: its client, tool schemas, call, echo, results and stop
# detection all come from the dialect its model asks for, so it has nothing to
# mismatch against. This table shrinks as roles are ported, and empties when
# the last one is — which is the point of it existing rather than a table of
# what each role is.
ROLE_WIRE = {
    "planner": MESSAGES,    # anthropic.Anthropic().messages.parse
    "reviewer": RESPONSES,  # OpenAI().responses.parse
}


def wire_mismatches(cfg) -> list[str]:
    """One line per role whose model wants a wire the role cannot speak."""
    out: list[str] = []
    for role, spoken in ROLE_WIRE.items():
        endpoint = getattr(cfg, role, None)
        model = getattr(endpoint, "model", "") if endpoint else ""
        try:
            wanted = dialect_for(model)
        except ValueError:
            # A routing policy. It resolves per run, so it cannot be classified
            # here — refusing to answer is right, reporting it as wrong is not.
            continue
        if wanted is spoken:
            continue
        out.append(
            f"{role}.model is {model!r}, which caches best on the {wanted.name} "
            f"API, but the {role} client speaks {spoken.name}. The call works; "
            f"what is lost is cache control — the marks this role sends are "
            f"not this model's vocabulary, so its cached prefix will not grow "
            f"with the conversation and cost rises with turn count."
        )
    return out
