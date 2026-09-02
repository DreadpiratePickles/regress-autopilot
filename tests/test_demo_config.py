"""The quota-fit demo: its configuration, its workload, and its call budget.

`docs/runbook-live-demo.md` publishes exact call counts and a claim that the run
fits inside a free-tier key's 20-requests-per-day allowance on the top rung.
Those are numbers in a document, so they are asserted here — a runbook whose
arithmetic has quietly gone stale is worse than no runbook, because somebody will
start a run on its word and burn a day's quota.
"""

from pathlib import Path

from cost_autopilot.classify.scorer import Tier, classify_text
from cost_autopilot.config_file import load_config
from cost_autopilot.workload import load_workload

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_CONFIG = REPO_ROOT / "autopilot.demo.toml"
DEMO_WORKLOAD = REPO_ROOT / "workloads" / "demo_quota_v1.jsonl"

TOP_RUNG_FREE_TIER_DAILY_LIMIT = 20
"""`gemini-3.6-flash`, observed on 2026-09-02. See docs/design.md §9."""

CHEAP_RUNG_FREE_TIER_DAILY_LIMIT = 500
"""`gemini-3.5-flash-lite`, observed on 2026-09-02. See docs/design.md §9."""


def judge_calls_for(criteria_count: int) -> int:
    """Judge calls one sampled record costs: `2 x criteria + 2`.

    Each criterion is judged on the cheap answer and on the reference answer
    separately, and the pair is judged in both orders. Derived rather than
    hard-coded because this workload is not uniform: nine requests carry three
    criteria and three carry four, so a fixed 8 would understate the bill.
    """
    return 2 * criteria_count + 2


class TestDemoConfiguration:
    def test_it_loads(self):
        assert load_config(DEMO_CONFIG) is not None

    def test_the_ladder_is_two_rungs_and_excludes_the_paid_only_model(self):
        config = load_config(DEMO_CONFIG)
        assert len(config.ladder.rungs) == 2
        assert config.ladder.top_rung.model_ref == "MID_MODEL_ID"
        assert all(rung.model_ref != "TOP_MODEL_ID" for rung in config.ladder.rungs)

    def test_the_cheap_rung_can_serve_t2_so_there_is_something_to_validate(self):
        """With a T1-only ceiling every T2 request would start on the top rung,
        and a top-rung answer is never shadow-sampled."""
        config = load_config(DEMO_CONFIG)
        assert config.ladder.rungs[0].max_tier == Tier.T2_STANDARD
        assert config.ladder.first_capable_index(Tier.T2_STANDARD) == 0
        assert config.ladder.first_capable_index(Tier.T3_COMPLEX) == 1

    def test_the_demo_only_settings_are_the_ones_the_file_documents(self):
        config = load_config(DEMO_CONFIG)
        assert config.validate.sample_percent == 50
        assert config.validate.min_samples == 4
        assert config.report.min_comparisons == 4

    def test_it_writes_nowhere_a_real_month_lives(self):
        config = load_config(DEMO_CONFIG)
        for directory in (
            config.ledger.directory,
            config.validate.directory,
            config.validate.shadow_directory,
            config.report.directory,
        ):
            assert directory.name != directory.parent.name
            assert "demo" in str(directory)

    def test_it_paces_itself_even_without_the_flag(self):
        assert load_config(DEMO_CONFIG).run.min_interval_ms == 6500


class TestDemoWorkload:
    def test_it_holds_twelve_requests_split_five_five_two(self):
        requests = load_workload(DEMO_WORKLOAD)
        assert len(requests) == 12
        counts = {tier: 0 for tier in Tier}
        for request in requests:
            counts[request.expected_tier] += 1
        assert counts == {Tier.T1_TRIVIAL: 5, Tier.T2_STANDARD: 5, Tier.T3_COMPLEX: 2}

    def test_every_request_classifies_to_its_expected_tier(self):
        """12 of 12, unlike mixed_v1's 26 of 30. The demo workload is small enough
        to be chosen deliberately, so a disagreement here is a mistake rather than
        a finding."""
        config = load_config(DEMO_CONFIG)
        for request in load_workload(DEMO_WORKLOAD):
            result = classify_text(request.text, config.classifier.thresholds)
            assert result.tier == request.expected_tier, (
                f"{request.id}: expected {request.expected_tier}, got {result.tier} "
                f"at score {result.complexity_score}"
            )

    def test_every_request_carries_criteria_so_both_judges_run(self):
        """The budget is summed from these counts rather than assuming a fixed
        number, but a request carrying none would skip criteria judging
        altogether and the demo would exercise only the pairwise judge."""
        for request in load_workload(DEMO_WORKLOAD):
            assert request.criteria, request.id

    def test_at_least_one_criterion_per_request_is_negative(self):
        """A negative criterion is the strongest regret detector, because the most
        common way a cheaper model loses is by adding something."""
        for request in load_workload(DEMO_WORKLOAD):
            assert any(
                item.lower().startswith(("does not", "is at most", "stays under"))
                for item in request.criteria
            ), request.id


class TestCallBudget:
    """The arithmetic `docs/runbook-live-demo.md` publishes, asserted."""

    def _split(self):
        """Split the workload by where the ladder starts each request.

        Only a request that starts below the top rung can be cheap-routed, and
        only a cheap-routed request can be shadow-sampled, so this split is
        exactly the set that can cost a reference call and a judge call.
        """
        config = load_config(DEMO_CONFIG)
        cheap, top = [], []
        for request in load_workload(DEMO_WORKLOAD):
            starts_below_top = (
                config.ladder.first_capable_index(request.expected_tier)
                < config.ladder.top_rung.index
            )
            (cheap if starts_below_top else top).append(request)
        return cheap, top

    def _judge_calls(self) -> list[int]:
        """What each cheap-routable request costs in judge calls, if sampled."""
        cheap, _ = self._split()
        return sorted(judge_calls_for(len(request.criteria)) for request in cheap)

    def test_ten_requests_route_cheap_and_two_route_to_the_top_rung(self):
        cheap, top = self._split()
        assert (len(cheap), len(top)) == (10, 2)

    def test_the_worst_case_stays_inside_the_top_rung_s_daily_allowance(self):
        """Routing costs 2 top-rung calls; validation costs 1 more per sampled
        record, and every one of the 10 cheap-routed requests could be sampled."""
        cheap, top = self._split()
        worst_case = len(top) + len(cheap)
        assert worst_case == 12
        assert worst_case <= TOP_RUNG_FREE_TIER_DAILY_LIMIT
        assert TOP_RUNG_FREE_TIER_DAILY_LIMIT - worst_case == 8, "headroom in the runbook"

    def test_the_worst_case_stays_inside_the_cheap_rung_s_daily_allowance(self):
        """Routing costs one cheap-rung call each; validation adds this
        workload's own `2 x criteria + 2` per sampled record, summed rather than
        assumed, because three of the ten carry four criteria and not three."""
        cheap, _ = self._split()
        worst_case = len(cheap) + sum(self._judge_calls())
        assert worst_case == 92
        assert worst_case <= CHEAP_RUNG_FREE_TIER_DAILY_LIMIT
        assert CHEAP_RUNG_FREE_TIER_DAILY_LIMIT - worst_case == 408, "headroom in the runbook"

    def test_the_worst_case_total_call_count_matches_the_runbook(self):
        cheap, top = self._split()
        top_rung = len(top) + len(cheap)
        cheap_rung = len(cheap) + sum(self._judge_calls())
        assert top_rung + cheap_rung == 104

    def test_the_expected_case_is_a_range_because_the_criteria_are_not_uniform(self):
        """Half of ten sampled is 7 top-rung calls, but which five are sampled is
        a hash of a uuid4 request id, and a four-criterion request costs two judge
        calls more than a three-criterion one. So the cheap-rung figure is a range
        and the runbook publishes it as one."""
        cheap, top = self._split()
        sampled = len(cheap) // 2
        assert len(top) + sampled == 7

        per_record = self._judge_calls()
        cheapest = len(cheap) + sum(per_record[:sampled])
        dearest = len(cheap) + sum(per_record[-sampled:])
        assert (cheapest, dearest) == (50, 52)
        assert dearest <= CHEAP_RUNG_FREE_TIER_DAILY_LIMIT
