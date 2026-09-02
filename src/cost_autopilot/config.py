"""Model identifiers, and nothing else.

Every model id in this package lives here. No other module names a model; they
read these constants or the ladder built from `autopilot.toml`, which references
them by these names. That is the same rule project 1 follows, for the same
reason: a model id is a fact about the outside world that changes without
warning, and it should change in one reviewable place.

Prices do *not* live here. A price is policy that a human must be able to audit
and correct, so it belongs in `autopilot.toml` next to the ladder that uses it.
"""

import os

CHEAP_MODEL_ID = "gemini-3.5-flash-lite"
"""Rung 1. The cheapest published Gemini text model. Handles trivial work:
single-fact answers, one-line reformatting, short translations, field extraction.
"""

MID_MODEL_ID = "gemini-3.6-flash"
"""Rung 2. The default workhorse; the same model project 1 targets."""

TOP_MODEL_ID = "gemini-3.1-pro-preview"
"""Rung 3, the fallback of last resort and the counterfactual cost baseline.

This is *not* `gemini-3.6-pro`. As of 2026-09-02 the published Gemini API
pricing page lists no 3.6-generation Pro model; the Pro entry it does list is
`gemini-3.1-pro-preview`, at $2.00 per 1M input tokens and $12.00 per 1M output
tokens. The alternative check — asking the SDK for the model list at runtime —
needs an API key, which this repository does not have, so it was not run and
nothing here claims it was.

A preview id can be withdrawn. If a call to this rung returns a 404 for the
model, change this one line rather than any call site, and update the matching
`[[ladder.rung]]` price in `autopilot.toml` in the same commit.
"""

CHEAP_MODEL_ID_ENV_VAR = "CHEAP_MODEL_ID"
MID_MODEL_ID_ENV_VAR = "MID_MODEL_ID"
TOP_MODEL_ID_ENV_VAR = "TOP_MODEL_ID"

MODEL_ID_DEFAULTS: dict[str, str] = {
    CHEAP_MODEL_ID_ENV_VAR: CHEAP_MODEL_ID,
    MID_MODEL_ID_ENV_VAR: MID_MODEL_ID,
    TOP_MODEL_ID_ENV_VAR: TOP_MODEL_ID,
}
"""Environment variable name -> default model id.

`autopilot.toml` names a rung's model by *variable name*, never by model id, so
the configuration file stays free of vendor strings and a deployment can swap a
rung without editing committed configuration.
"""


class UnknownModelRefError(ValueError):
    """A ladder rung names a model reference this module does not define."""


def model_id_for_ref(ref: str) -> str:
    """Resolve a ladder rung's `model_ref` to the model id it should call.

    The environment wins over the default so a run can be pointed at another
    model without a commit, exactly as project 1 allows for its target and judge.

    Raises:
        UnknownModelRefError: `ref` is not one of the defined references. An
            unrecognised reference is never silently resolved to a default: a
            typo would then quietly route traffic to the wrong price.
    """
    if ref not in MODEL_ID_DEFAULTS:
        known = ", ".join(sorted(MODEL_ID_DEFAULTS))
        raise UnknownModelRefError(f"unknown model reference {ref!r}; known references: {known}")
    override = os.environ.get(ref, "").strip()
    return override or MODEL_ID_DEFAULTS[ref]
