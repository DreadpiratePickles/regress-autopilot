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

[validate]
enabled = true
sample_percent = 20
judge_model_ref = "CHEAP_MODEL_ID"
max_regret = 0.10
min_samples = 10
dir = "validate"
shadow_dir = "shadow"
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


class TestValidateSection:
    """`[validate]` decides what is sampled, judged, and called safe."""

    def test_it_loads_with_its_defaults(self, tmp_path):
        settings = load_config(write(tmp_path, VALID)).validate
        assert settings.enabled is True
        assert settings.sample_percent == 20
        assert settings.min_samples == 10
        assert settings.max_regret == pytest.approx(0.10)

    def test_the_judge_model_ref_resolves_to_a_model_id(self, tmp_path):
        settings = load_config(write(tmp_path, VALID)).validate
        assert settings.judge_model_id
        assert settings.judge_model_ref == "CHEAP_MODEL_ID"

    def test_the_judge_rung_is_the_ladder_rung_that_prices_the_judge(self, tmp_path):
        config = load_config(write(tmp_path, VALID))
        assert config.judge_rung().model_id == config.validate.judge_model_id

    def test_the_directories_resolve_relative_to_the_config_file(self, tmp_path):
        settings = load_config(write(tmp_path, VALID)).validate
        assert settings.directory == tmp_path / "validate"
        assert settings.shadow_directory == tmp_path / "shadow"

    @pytest.mark.parametrize("value", ["-1", "101", "1000"])
    def test_a_sample_percent_outside_zero_to_one_hundred_is_rejected(self, tmp_path, value):
        text = VALID.replace("sample_percent = 20", f"sample_percent = {value}")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    @pytest.mark.parametrize("value", ["0", "100"])
    def test_the_ends_of_the_sample_range_are_allowed(self, tmp_path, value):
        text = VALID.replace("sample_percent = 20", f"sample_percent = {value}")
        assert load_config(write(tmp_path, text)).validate.sample_percent == int(value)

    def test_a_fractional_sample_percent_is_rejected(self, tmp_path):
        text = VALID.replace("sample_percent = 20", "sample_percent = 20.5")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    @pytest.mark.parametrize("value", ["-0.1", "1.5", '"0.1"'])
    def test_a_max_regret_outside_zero_to_one_is_rejected(self, tmp_path, value):
        text = VALID.replace("max_regret = 0.10", f"max_regret = {value}")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    def test_a_zero_min_samples_is_rejected(self, tmp_path):
        text = VALID.replace("min_samples = 10", "min_samples = 0")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    def test_an_unknown_key_is_rejected(self, tmp_path):
        text = VALID.replace("min_samples = 10", "min_samples = 10\nsample_rate = 5")
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    def test_a_missing_validate_section_is_rejected(self, tmp_path):
        text = VALID.split("[validate]")[0]
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    def test_an_unknown_judge_model_ref_is_rejected(self, tmp_path):
        text = VALID.replace(
            'judge_model_ref = "CHEAP_MODEL_ID"', 'judge_model_ref = "NOT_A_MODEL_REF"'
        )
        with pytest.raises(ConfigFileError):
            load_config(write(tmp_path, text))

    def test_a_judge_model_not_on_the_ladder_cannot_be_priced_and_is_rejected(self, tmp_path):
        two_rungs = VALID.replace(
            """[[ladder.rung]]
model_ref = "TOP_MODEL_ID"
input_price_micro_usd_per_1k_tokens = 2000
output_price_micro_usd_per_1k_tokens = 12000
max_tier = "T3_COMPLEX"
""",
            "",
        ).replace('judge_model_ref = "CHEAP_MODEL_ID"', 'judge_model_ref = "TOP_MODEL_ID"')
        with pytest.raises(ConfigFileError, match="ladder"):
            load_config(write(tmp_path, two_rungs))

    def test_an_absolute_shadow_dir_is_rejected(self, tmp_path):
        text = VALID.replace('shadow_dir = "shadow"', 'shadow_dir = "/tmp/shadow"')
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


class TestValidateSettingsDirectly:
    """The settings object rejects on its own, not only through the file loader."""

    def valid(self, **overrides):
        from pathlib import Path

        from cost_autopilot.validate.settings import ValidateSettings

        return ValidateSettings(
            **{
                "enabled": True,
                "sample_percent": 20,
                "judge_model_ref": "CHEAP_MODEL_ID",
                "judge_model_id": "model-cheap",
                "max_regret": 0.1,
                "min_samples": 10,
                "directory": Path("validate"),
                "shadow_directory": Path("shadow"),
                **overrides,
            }
        )

    def test_a_valid_set_of_settings_builds(self):
        assert self.valid().sample_percent == 20

    def test_enabled_must_be_a_boolean(self):
        with pytest.raises(ValueError):
            self.valid(enabled="yes")

    def test_max_regret_must_be_a_number(self):
        with pytest.raises(ValueError):
            self.valid(max_regret="0.1")

    def test_max_regret_true_is_not_a_number_here(self):
        with pytest.raises(ValueError):
            self.valid(max_regret=True)

    def test_min_samples_must_be_a_positive_integer(self):
        with pytest.raises(ValueError):
            self.valid(min_samples=0)

    def test_a_blank_judge_model_id_is_refused(self):
        with pytest.raises(ValueError):
            self.valid(judge_model_id="  ")

    def test_a_sample_percent_out_of_range_is_refused(self):
        with pytest.raises(ValueError):
            self.valid(sample_percent=101)
