"""Money, as integer micro-USD. No float ever touches an amount.

One micro-USD is one millionth of a dollar. Token prices are quoted per million
tokens at four or five significant figures, so cents are far too coarse to hold
a single call's cost and a binary float is the wrong shape for money at any
scale. Integers in the smallest useful unit are the standard answer, and the
rulebook requires them: "represent money as integer minor units ... never use
binary floating point for authoritative financial arithmetic".

Two consequences run through the whole package:

  - Every amount is named `*_micro_usd` and carries `CURRENCY` alongside it. A
    number without a unit in its name is not money here.
  - Every division rounds *up*. A tenth of a micro-USD charged to a caller is a
    tenth of a micro-USD; truncating it would make a million one-token calls
    free, and a ledger that under-reports spend is worse than no ledger.
"""

CURRENCY = "USD"
"""Every amount in this package is USD. Recorded on every ledger row so a reader
never has to assume."""

MICRO_USD_PER_USD = 1_000_000
TOKENS_PER_PRICE_UNIT = 1_000
"""Prices are quoted per 1,000 tokens, matching `autopilot.toml`."""


class MoneyError(ValueError):
    """An amount, token count, or price is not a usable non-negative integer."""


def _non_negative_int(value: object, *, field: str) -> int:
    """Reject anything that is not a non-negative `int`.

    `bool` is excluded explicitly: it is a subclass of `int` in Python, and
    `True` silently costing one micro-USD is exactly the class of bug this
    module exists to prevent.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError(
            f"{field} must be a non-negative integer, got {type(value).__name__}: {value!r}"
        )
    if value < 0:
        raise MoneyError(f"{field} must be a non-negative integer, got {value}")
    return value


def validate_micro_usd(value: object, *, field: str) -> int:
    """Validate an amount of money at a boundary (config, ledger row, CLI)."""
    return _non_negative_int(value, field=field)


def token_cost_micro_usd(*, tokens: object, price_micro_usd_per_1k: object) -> int:
    """Cost of `tokens` tokens at `price_micro_usd_per_1k`, rounded up.

    Pure integer arithmetic: `(tokens * price + 999) // 1000` is the ceiling of
    `tokens * price / 1000` without ever constructing the quotient as a float.

    Raises:
        MoneyError: either argument is not a non-negative integer.
    """
    token_count = _non_negative_int(tokens, field="tokens")
    price = _non_negative_int(price_micro_usd_per_1k, field="price_micro_usd_per_1k")
    numerator = token_count * price
    return -(-numerator // TOKENS_PER_PRICE_UNIT)


def completion_cost_micro_usd(
    *,
    input_tokens: object,
    output_tokens: object,
    input_price_micro_usd_per_1k: object,
    output_price_micro_usd_per_1k: object,
) -> int:
    """Total cost of one completion: the two legs priced separately, then summed.

    Each leg ceils on its own rather than the total ceiling once. That is the
    conservative reading of a two-part tariff, and it makes the ledger's
    per-request cost reproducible from the row's own four fields.
    """
    return token_cost_micro_usd(
        tokens=input_tokens, price_micro_usd_per_1k=input_price_micro_usd_per_1k
    ) + token_cost_micro_usd(
        tokens=output_tokens, price_micro_usd_per_1k=output_price_micro_usd_per_1k
    )


def format_micro_usd(amount_micro_usd: object) -> str:
    """Render an amount as `$D.dddddd`, for humans, without a float.

    Presentation only. The value keeps its full integer precision because the
    dollars and the millionths are split with `divmod`, never by division.
    """
    amount = _non_negative_int(amount_micro_usd, field="amount_micro_usd")
    dollars, micros = divmod(amount, MICRO_USD_PER_USD)
    return f"${dollars}.{micros:06d}"
