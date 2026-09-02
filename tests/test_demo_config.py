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

CRITERIA_PER_REQUEST = 3
JUDGE_CALLS_PER_SAMPLED = CRITERIA_PER_REQUEST * 2 + 2
"""Each criterion is judged on both answers, and the pair is judged in both
orders: 3 x 2 + 2 = 8 judge calls per sampled record."""


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

    def test_every_request_carries_the_criteria_the_budget_assumes(self):
        for request in load_workload(DEMO_WORKLOAD):
            assert len(request.criteria) >= CRITERIA_PER_REQUEST, request.id

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
        config = load_config(DEMO_CONFIG)
        cheap = sum(
            1
            for request in load_workload(DEMO_WORKLOAD)
            if config.ladder.first_capable_index(request.expected_tier)
            < config.ladder.top_rung.index
        )
        return config, cheap, 12 - cheap

    def test_ten_requests_route_cheap_and_two_route_to_the_top_rung(self):
        _, cheap, top = self._split()
        assert (cheap, top) == (10, 2)

    def test_the_worst_case_stays_inside_the_top_rung_s_daily_allowance(self):
        """Routing costs 2 top-rung calls; validation costs 1 more per sampled
        record, and every one of the 10 cheap-routed requests could be sampled."""
        _, cheap, top = self._split()
        worst_case = top + cheap
        assert worst_case == 12
        assert worst_case <= TOP_RUNG_FREE_TIER_DAILY_LIMIT
        assert TOP_RUNG_FREE_TIER_DAILY_LIMIT - worst_case == 8, "headroom in the runbook"

    def test_the_worst_case_cheap_rung_count_matches_the_runbook(self):
        _, cheap, _ = self._split()
        assert cheap + cheap * JUDGE_CALLS_PER_SAMPLED == 90

    def test_the_expected_case_matches_the_runbook(self):
        """Half of ten sampled: 7 top-rung calls and 50 cheap-rung calls."""
        _, cheap, top = self._split()
        sampled = cheap // 2
        assert top + sampled == 7
        assert cheap + sampled * JUDGE_CALLS_PER_SAMPLED == 50
