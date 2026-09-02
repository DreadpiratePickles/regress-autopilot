"""One shared tolerance in an otherwise strict parser: a markdown code fence.

Three places in this system read a JSON object a model wrote — the optional
classifier, the criterion judge borrowed from project 1, and the pairwise judge.
All three are strict about the payload and all three forgive exactly one thing,
because it is the one deviation models produce constantly and it changes nothing
about what the object says.

It lives here rather than in each of them so the three cannot drift on what
"tolerated" means. Everything else about a reply is still a parse error.
"""

FENCE_CHARACTER = "`"
MIN_FENCE_LENGTH = 3
JSON_LANGUAGE_TAG = "json"


def strip_one_fence(text: str) -> str:
    """Remove a single surrounding markdown fence, if the text is wrapped in one.

    Returns the text unchanged when there is no fence, when the fence names a
    language other than `json`, or when the closing fence is missing — a
    half-fenced reply is malformed, and pretending otherwise would hide it.
    """
    if not text.startswith(FENCE_CHARACTER * MIN_FENCE_LENGTH):
        return text

    opening, _, remainder = text.partition("\n")
    fence = opening[: len(opening) - len(opening.lstrip(FENCE_CHARACTER))]
    language = opening[len(fence) :].strip()
    if language and language != JSON_LANGUAGE_TAG:
        return text
    if not remainder.rstrip().endswith(fence):
        return text
    closed = remainder.rstrip()
    return closed[: len(closed) - len(fence)].strip()
