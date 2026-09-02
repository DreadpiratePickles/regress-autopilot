"""`autopilot.toml` is a boundary. Every way it can be wrong is an error here."""

import pytest

from cost_autopilot.classify.scorer import Tier
from cost_autopilot.config_file import ConfigFileError, load_config

VALID = """
[ladder]
prices_verified = true
prices_source = "https://example.invalid/pricing"
prices_read_utc = "2026-09-02"

[[ladder.rung]]
model_ref = "CHEAP_MODEL_ID"
input_price_micro_usd_per_1k_tokens = 300
output_price_micro_usd_per_1k_tokens = 2500
max_tier = "T1_TRIVIAL"

[[ladder.rung]]
model_ref = "MID_MODEL_ID"
input_price_micro_usd_per_1k_tokens = 750
output_price_micro_usd_per_1k_tokens = 3750
max_tier = "T2_STANDARD"

[[ladder.rung]]
model_ref = "TOP_MODEL_ID"
input_price_micro_usd_per_1k_tokens = 2000
output_price_micro_usd_per_1k_tokens = 12000
max_tier = "T3_COMPLEX"

[policy]
T1_TRIVIAL = 0
T2_STANDARD = 0
T3_COMPLEX = 0

[budgets]
default_monthly_cap_micro_usd = 2000000

[budgets.teams]
demo = 5000000

[classifier]
t2_min_score = 15
t3_min_score = 45
llm_classifier = false

[ledger]
dir = "ledger"
log_text = false

[run]
min_interval_ms = 0
temperature = 0.0
"""


def write(tmp_path, text):
    path = tmp_path / "autopilot.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestValidConfig:
    def test_loads(self, tmp_path):
        config = load_config(write(tmp_path, VALID))
        assert len(config.ladder.rungs) == 3
        assert config.ladder.prices_verified is True

    def test_resolves_model_refs_to_model_ids(self, tmp_path):
        config = load_config(write(tmp_path, VALID))
        # config.py owns the ids; the toml never names one.
        assert "CHEAP_MODEL_ID" not in {rung.model_id for rung in config.ladder.rungs}
        assert all(rung.model_id.strip() for rung in config.ladder.rungs)

    def test_the_toml_contains_no_vendor_string(self, tmp_path):
        assert "gemini" not in VALID.lower()

    def test_prices_are_integers(self, tmp_path):
        config = load_config(write(tmp_path, VALID))
        for rung in config.ladder.rungs:
            assert isinstance(rung.input_price_micro_usd_per_1k_tokens, int)
            assert isinstance(rung.output_price_micro_usd_per_1k_tokens, int)

    def test_budgets_and_policy_load(self, tmp_path):
        config = load_config(write(tmp_path, VALID))
        assert config.budgets.cap_for("demo") == 5000000
        assert config.budgets.cap_for("unlisted") == 2000000
        assert config.policy.floor_for(Tier.T3_COMPLEX) == 0

    def test_ledger_dir_is_resolved_against_the_config_file(self, tmp_path):
        config = load_config(write(tmp_path, VALID))
        assert config.ledger.directory == tmp_path / "ledger"

    def test_classifier_thresholds_load(self, tmp_path):
        config = load_config(write(tmp_path, VALID))
        assert config.classifier.thresholds.t2_min_score == 15
        assert config.classifier.llm_classifier is False


class TestMissingAndUnknown:
    def test_a_missing_file_is_an_error(self, tmp_path):
        with pytest.raises(ConfigFileError, match="not found"):
            load_config(tmp_path / "nope.toml")

    def test_invalid_toml_is_an_error(self, tmp_path):
        with pytest.raises(ConfigFileError, match="not valid TOML"):
            load_config(write(tmp_path, "this is [not toml"))

    def test_an_unknown_section_is_rejected(self, tmp_path):
        with pytest.raises(ConfigFileError, match="unknown section"):
            load_config(write(tmp_path, VALID + "\n[surprise]\nx = 1\n"))

    def test_an_unknown_key_in_a_section_is_rejected(self, tmp_path):
        text = VALID.replace("[run]\nmin_interval_ms = 0", "[run]\nmin_interval_ms = 0\nspeed = 9")
        with pytest.raises(ConfigFileError, match="unknown key"):
            load_config(write(tmp_path, text))

    def test_a_misspelled_price_key_is_rejected_not_ignored(self, tmp_path):
        # The exact bug this check exists for: a silently ignored price key
        # would leave the rung at zero and every saving figure wrong.
        text = VALID.replace(
            "output_price_micro_usd_per_1k_tokens = 2500",
            "output_price_micro_usd_per_1k_token = 2500",
        )
        with pytest.raises(ConfigFileError, match="unknown key"):
            load_config(write(tmp_path, text))

    def test_a_missing_section_is_rejected(self, tmp_path):
        text = VALID.replace("[budgets]\ndefault_monthly_cap_micro_usd = 2000000", "")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))


class TestPriceValidation:
    def test_a_negative_price_is_rejected(self, tmp_path):
        text = VALID.replace(
            "input_price_micro_usd_per_1k_tokens = 300",
            "input_price_micro_usd_per_1k_tokens = -300",
        )
        with pytest.raises(ConfigFileError, match="at least 0"):
            load_config(write(tmp_path, text))

    def test_a_decimal_price_is_rejected(self, tmp_path):
        text = VALID.replace(
            "input_price_micro_usd_per_1k_tokens = 300",
            "input_price_micro_usd_per_1k_tokens = 0.0003",
        )
        with pytest.raises(ConfigFileError, match="integer"):
            load_config(write(tmp_path, text))

    def test_a_zero_price_is_allowed(self, tmp_path):
        text = VALID.replace(
            "input_price_micro_usd_per_1k_tokens = 300",
            "input_price_micro_usd_per_1k_tokens = 0",
        )
        config = load_config(write(tmp_path, text))
        assert config.ladder.rungs[0].input_price_micro_usd_per_1k_tokens == 0

    def test_a_negative_budget_is_rejected(self, tmp_path):
        text = VALID.replace("demo = 5000000", "demo = -1")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    def test_a_decimal_budget_is_rejected(self, tmp_path):
        text = VALID.replace(
            "default_monthly_cap_micro_usd = 2000000",
            "default_monthly_cap_micro_usd = 2.5",
        )
        with pytest.raises(ConfigFileError, match="integer"):
            load_config(write(tmp_path, text))


class TestLadderShapeValidation:
    def test_an_unknown_model_ref_is_rejected(self, tmp_path):
        text = VALID.replace('model_ref = "CHEAP_MODEL_ID"', 'model_ref = "NOT_A_REF"')
        with pytest.raises(ConfigFileError, match="unknown model reference"):
            load_config(write(tmp_path, text))

    def test_an_unknown_tier_is_rejected(self, tmp_path):
        text = VALID.replace('max_tier = "T1_TRIVIAL"', 'max_tier = "T0_FREE"')
        with pytest.raises(ConfigFileError, match="not a known tier"):
            load_config(write(tmp_path, text))

    def test_decreasing_ceilings_are_rejected(self, tmp_path):
        text = VALID.replace('max_tier = "T1_TRIVIAL"', 'max_tier = "T3_COMPLEX"')
        with pytest.raises(ConfigFileError, match="non-decreasing"):
            load_config(write(tmp_path, text))

    def test_a_ladder_with_no_rungs_is_rejected(self, tmp_path):
        head, _, _ = VALID.partition("[[ladder.rung]]")
        text = head + VALID[VALID.index("[policy]"):]
        with pytest.raises(ConfigFileError, match="at least one"):
            load_config(write(tmp_path, text))


class TestOtherValidation:
    def test_an_absolute_ledger_dir_is_rejected(self, tmp_path):
        text = VALID.replace('dir = "ledger"', 'dir = "/var/ledger"')
        with pytest.raises(ConfigFileError, match="relative"):
            load_config(write(tmp_path, text))

    def test_a_non_boolean_log_text_is_rejected(self, tmp_path):
        text = VALID.replace("log_text = false", 'log_text = "no"')
        with pytest.raises(ConfigFileError, match="true or false"):
            load_config(write(tmp_path, text))

    def test_t3_below_t2_is_rejected(self, tmp_path):
        text = VALID.replace("t3_min_score = 45", "t3_min_score = 5")
        with pytest.raises(ConfigFileError, match="t3_min_score"):
            load_config(write(tmp_path, text))

    def test_a_negative_interval_is_rejected(self, tmp_path):
        text = VALID.replace("min_interval_ms = 0", "min_interval_ms = -1")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    def test_an_out_of_range_temperature_is_rejected(self, tmp_path):
        text = VALID.replace("temperature = 0.0", "temperature = 9.0")
        with pytest.raises(ConfigFileError, match="temperature"):
            load_config(write(tmp_path, text))

    def test_an_unknown_policy_tier_is_rejected(self, tmp_path):
        text = VALID.replace("T3_COMPLEX = 0", "T3_COMPLEX = 0\nT9_WILD = 0")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))


class TestTheCommittedConfig:
    """The file the repository actually ships must load and be self-consistent."""

    def test_the_committed_config_loads(self):
        config = load_config("autopilot.toml")
        assert len(config.ladder.rungs) >= 2

    def test_the_committed_ladder_is_cheapest_first(self):
        config = load_config("autopilot.toml")
        prices = [rung.input_price_micro_usd_per_1k_tokens for rung in config.ladder.rungs]
        assert prices == sorted(prices)

    def test_the_committed_config_declares_whether_prices_were_verified(self):
        config = load_config("autopilot.toml")
        assert isinstance(config.ladder.prices_verified, bool)
        assert config.prices_source.startswith("http")

    def test_the_llm_classifier_is_off_by_default(self):
        assert load_config("autopilot.toml").classifier.llm_classifier is False

    def test_request_text_is_not_logged_by_default(self):
        assert load_config("autopilot.toml").ledger.log_text is False
