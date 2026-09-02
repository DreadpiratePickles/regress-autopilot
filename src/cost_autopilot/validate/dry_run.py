"""Fakes for `validate --dry-run`: exercise the pipeline, measure nothing.

A dry run of stage 03 has to answer three different kinds of call — a reference
answer, a criterion verdict, a pairwise comparison — because the runner makes all
three and a single canned string would be a parse error in two of them, which
would test the error path rather than the pipeline.

So the fake answers by looking at which system prompt it was handed. This is a
test double being explicit about the shape it is standing in for, not a heuristic
in production code: the verdicts it returns are constants and say nothing about
any model. Every number a dry run produces is synthetic, and the CLI says so on
the line above the results.
"""

from ..providers.metered import Completion

DRY_RUN_MODEL_SUFFIX = " (dry run)"
DRY_RUN_INPUT_TOKENS = 100
DRY_RUN_OUTPUT_TOKENS = 50
DRY_RUN_LATENCY_MS = 7
"""Small, fixed, and obviously synthetic, matching the routing fakes."""

DRY_RUN_REFERENCE_ANSWER = "This is a dry-run reference answer. No model was called."
DRY_RUN_PAIRWISE = (
    '{"reason": "dry run: no comparison was made", "a_at_least_as_good": true}'
)
DRY_RUN_CRITERION = '{"reason": "dry run: no judgement was made", "passed": true}'

PAIRWISE_MARKER = "a_at_least_as_good"
CRITERION_MARKER = "<criterion>"


class DryRunJudgeProvider:
    """Replies in the shape the caller's system prompt asks for. Calls nothing."""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.calls = 0

    def complete(self, *, system: str, user: str, temperature: float) -> Completion:
        self.calls += 1
        if PAIRWISE_MARKER in system:
            text = DRY_RUN_PAIRWISE
        elif CRITERION_MARKER in system:
            text = DRY_RUN_CRITERION
        else:
            text = DRY_RUN_REFERENCE_ANSWER
        return Completion(
            text=text,
            input_tokens=DRY_RUN_INPUT_TOKENS,
            output_tokens=DRY_RUN_OUTPUT_TOKENS,
            model_id=self.model_id,
            latency_ms=DRY_RUN_LATENCY_MS,
        )


class DryRunJudgeFactory:
    """A `model_id -> provider` factory of dry-run judges, one per model id."""

    def __init__(self) -> None:
        self.built: dict[str, DryRunJudgeProvider] = {}

    def __call__(self, model_id: str) -> DryRunJudgeProvider:
        if model_id not in self.built:
            self.built[model_id] = DryRunJudgeProvider(model_id)
        return self.built[model_id]
