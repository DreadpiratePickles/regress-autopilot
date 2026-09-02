"""Which cheap-routed requests get re-answered and judged, decided deterministically.

Validating every request would double the system's model spend, on a tool whose
purpose is to spend less. So a sample is validated, and the sampling rule is
recorded next to the result rather than left implicit.

The rule is a hash of the request id, not a random draw. Three consequences, all
of them the point:

  - The same request is always sampled or always skipped, so a re-run of the same
    workload inspects the same requests and two months are comparable.
  - Nothing has to be stored to remember what was sampled: the decision is
    recomputable from the id alone, by anyone, forever.
  - Sampling cannot correlate with the outcome. A random draw taken *after* the
    model answered could — accidentally or otherwise — end up favouring the calls
    that went well, and a regret figure computed from a biased sample is worse
    than no regret figure.

SHA-256 rather than Python's `hash()`: `hash()` is salted per process, so the
same id would be sampled in one run and skipped in the next.
"""

import hashlib

SAMPLE_BUCKETS = 100
"""Buckets 0-99, so `sample_percent` reads directly as a percentage."""

MIN_SAMPLE_PERCENT = 0
MAX_SAMPLE_PERCENT = 100


class SamplingError(ValueError):
    """A request id or a sample percentage is not usable."""


def validate_sample_percent(value: object) -> int:
    """Check a sample rate at the boundary.

    Raises:
        SamplingError: not an integer, or outside 0-100 inclusive. `bool` is
            rejected explicitly: `True` would otherwise mean "sample 1%".
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise SamplingError(
            f"sample_percent must be an integer between {MIN_SAMPLE_PERCENT} and "
            f"{MAX_SAMPLE_PERCENT}, got {type(value).__name__}: {value!r}"
        )
    if not MIN_SAMPLE_PERCENT <= value <= MAX_SAMPLE_PERCENT:
        raise SamplingError(
            f"sample_percent must lie in [{MIN_SAMPLE_PERCENT}, {MAX_SAMPLE_PERCENT}], "
            f"got {value}"
        )
    return value


def sample_bucket(request_id: object) -> int:
    """The bucket 0-99 a request id falls in. Stable across processes and machines.

    Raises:
        SamplingError: the id is not a non-empty string.
    """
    if not isinstance(request_id, str) or not request_id.strip():
        raise SamplingError(
            f"request_id must be a non-empty string to be sampled, got {request_id!r}"
        )
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % SAMPLE_BUCKETS


def is_sampled(request_id: object, sample_percent: object) -> bool:
    """Whether this request belongs in the shadow sample.

    Strictly `bucket < sample_percent`, so 0 samples nothing and 100 samples
    everything, and raising the rate can only ever add requests to the sample —
    never swap one set for another. That is what makes two months at different
    rates still comparable on the overlap.
    """
    return sample_bucket(request_id) < validate_sample_percent(sample_percent)
