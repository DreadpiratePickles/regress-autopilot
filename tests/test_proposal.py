"""The tuning proposal, and the one code path that edits `autopilot.toml`.

The gates are the point of this file. A proposal is a suggestion; applying one
changes what the system is allowed to spend, so every refusal below is a
deliberate barrier and each has a test naming it.
"""

import json

import pytest

from conftest import load_test_config, make_verdicts, write_config
from cost_autopilot.config_file import load_config
from cost_autopilot.report.apply import apply_and_record, apply_proposal, replace_scalar
from cost_autopilot.report.build import build_report
from cost_autopilot.report.model import Recommendation, ReportError
from cost_autopilot.report.proposal import (
    STATUS_APPLIED,
    STATUS_AWAITING,
    ProposalError,
    ProposedChange,
    TuningProposal,
    build_proposal,
    load_proposal,
    proposal_from_json_dict,
    write_proposal,
)
from cost_autopilot.report.rules import recommend
from cost_autopilot.validate.regret import build_report as build_regret

NOW = "2026-09-02T12:00:00Z"


def regret_for(items):
    return build_regret(
        items,
        month="2026-09",
        sample_percent=20,
        cheap_routed_count=100,
        shadow_record_count=len(items),
        generated_utc=NOW,
    )


def report_with_recommendations(tmp_path, verdicts, substitutions=()):
    config = load_test_config(tmp_path, substitutions)
    from dataclasses import replace

    report = build_report(
        [],
        regret_for(verdicts),
        [],
        config=config,
        month="2026-09",
        generated_utc=NOW,
    )
    return replace(report, recommendations=recommend(report, config)), config


def proposal_for(tmp_path, verdicts, substitutions=()):
    report, _ = report_with_recommendations(tmp_path, verdicts, substitutions)
    return build_proposal(
        report, config_path=str(tmp_path / "autopilot.toml"), generated_utc=NOW
    )


class TestBuildingAProposal:
    def test_a_regretful_tier_becomes_one_policy_change(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        assert [change.label for change in proposal.changes] == ["[policy] T1_TRIVIAL"]
        assert proposal.changes[0].current_value == 0
        assert proposal.changes[0].proposed_value == 1
        assert proposal.status == STATUS_AWAITING
        assert proposal.approved_by is None

    def test_a_month_with_nothing_to_change_produces_an_empty_proposal(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=200, regret_count=0))
        assert proposal.is_empty
        assert proposal.changes == ()

    def test_advice_that_no_setting_can_express_never_enters_the_proposal(self, tmp_path):
        proposal = proposal_for(
            tmp_path,
            make_verdicts(count=200, regret_count=0),
            substitutions=[("prices_verified = true", "prices_verified = false")],
        )
        assert proposal.is_empty

    def test_two_rules_on_one_key_merge_and_the_higher_rung_wins(self, tmp_path):
        """Raising a policy floor is the conservative direction — it spends more
        and risks less — so a merge must never resolve toward the cheaper rung."""
        from dataclasses import replace

        base, _ = report_with_recommendations(
            tmp_path, make_verdicts(count=200, regret_count=0)
        )
        report = replace(
            base,
            recommendations=(
                Recommendation(
                    rule_id="a",
                    subject="T1_TRIVIAL",
                    evidence="first",
                    action="up one",
                    config_section="policy",
                    config_key="T1_TRIVIAL",
                    current_value=0,
                    proposed_value=1,
                ),
                Recommendation(
                    rule_id="b",
                    subject="T1_TRIVIAL",
                    evidence="second",
                    action="up two",
                    config_section="policy",
                    config_key="T1_TRIVIAL",
                    current_value=0,
                    proposed_value=2,
                ),
            ),
        )
        proposal = build_proposal(report, config_path="autopilot.toml", generated_utc=NOW)
        assert len(proposal.changes) == 1
        assert proposal.changes[0].proposed_value == 2
        assert proposal.changes[0].rule_ids == ("a", "b")
        assert "first" in proposal.changes[0].evidence
        assert "second" in proposal.changes[0].evidence

    def test_a_change_that_changes_nothing_is_refused(self):
        with pytest.raises(ProposalError, match="already 3"):
            ProposedChange(
                section="policy",
                key="T1_TRIVIAL",
                current_value=3,
                proposed_value=3,
                evidence="e",
                rule_ids=("r",),
            )

    def test_a_change_with_no_rule_behind_it_is_refused(self):
        with pytest.raises(ProposalError, match="names no rule"):
            ProposedChange(
                section="policy",
                key="T1_TRIVIAL",
                current_value=0,
                proposed_value=1,
                evidence="e",
                rule_ids=(),
            )

    def test_a_half_specified_recommendation_is_refused(self):
        with pytest.raises(ReportError, match="half-specified"):
            Recommendation(
                rule_id="r",
                subject="T1_TRIVIAL",
                evidence="e",
                action="a",
                config_section="policy",
            )


class TestProposalFiles:
    def test_a_proposal_round_trips_through_json(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        assert proposal_from_json_dict(proposal.to_json_dict()) == proposal

    def test_write_then_load_returns_the_same_proposal(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        path = write_proposal(tmp_path / "out" / "proposal.json", proposal)
        assert load_proposal(path) == proposal

    def test_a_missing_file_is_a_named_error(self, tmp_path):
        with pytest.raises(ProposalError, match="not found"):
            load_proposal(tmp_path / "nope.json")

    def test_a_file_that_is_not_json_is_a_named_error(self, tmp_path):
        path = tmp_path / "proposal.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ProposalError, match="could not be read"):
            load_proposal(path)

    def test_an_unknown_schema_version_is_refused(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        payload = proposal.to_json_dict()
        payload["schema_version"] = 99
        with pytest.raises(ProposalError, match="schema_version"):
            proposal_from_json_dict(payload)

    def test_an_unknown_field_is_refused(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        payload = proposal.to_json_dict()
        payload["surprise"] = 1
        with pytest.raises(ProposalError, match="unknown field"):
            proposal_from_json_dict(payload)

    def test_a_missing_field_is_refused(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        payload = proposal.to_json_dict()
        del payload["status"]
        with pytest.raises(ProposalError, match="missing field"):
            proposal_from_json_dict(payload)

    def test_a_malformed_change_is_refused(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        payload = proposal.to_json_dict()
        payload["changes"][0]["surprise"] = 1
        with pytest.raises(ProposalError, match="unknown field"):
            proposal_from_json_dict(payload)

    def test_an_applied_proposal_must_name_who_applied_it(self):
        with pytest.raises(ProposalError, match="who approved it"):
            TuningProposal(
                month="2026-09",
                generated_utc=NOW,
                config_path="autopilot.toml",
                changes=(),
                status=STATUS_APPLIED,
            )


class TestReplaceScalar:
    CONFIG = (
        "[policy]\n"
        "# a comment about tiers\n"
        "T1_TRIVIAL = 0\n"
        "T2_STANDARD = 0  # trailing note\n"
        "\n"
        "[validate]\n"
        "sample_percent = 20\n"
    )

    def test_it_replaces_the_value_and_keeps_the_comment(self):
        result = replace_scalar(self.CONFIG, section="policy", key="T2_STANDARD", value=2)
        assert "T2_STANDARD = 2  # trailing note" in result
        assert "# a comment about tiers" in result

    def test_it_leaves_every_other_line_untouched(self):
        result = replace_scalar(self.CONFIG, section="validate", key="sample_percent", value=50)
        assert "T1_TRIVIAL = 0" in result
        assert "sample_percent = 50" in result
        assert result.count("\n") == self.CONFIG.count("\n")

    def test_it_only_matches_inside_the_named_section(self):
        text = "[a]\nx = 1\n\n[b]\nx = 1\n"
        assert replace_scalar(text, section="b", key="x", value=9) == "[a]\nx = 1\n\n[b]\nx = 9\n"

    def test_a_missing_key_is_refused_rather_than_appended(self):
        with pytest.raises(ProposalError, match="could not find 'nope'"):
            replace_scalar(self.CONFIG, section="policy", key="nope", value=1)

    def test_a_missing_section_is_refused(self):
        with pytest.raises(ProposalError, match="could not find"):
            replace_scalar(self.CONFIG, section="nowhere", key="T1_TRIVIAL", value=1)

    def test_a_duplicated_key_is_refused_rather_than_guessed_at(self):
        text = "[policy]\nT1_TRIVIAL = 0\nT1_TRIVIAL = 1\n"
        with pytest.raises(ProposalError, match="appears 2 times"):
            replace_scalar(text, section="policy", key="T1_TRIVIAL", value=2)

    def test_the_committed_configuration_survives_a_real_edit(self, tmp_path):
        path = write_config(tmp_path)
        edited = replace_scalar(
            path.read_text(encoding="utf-8"), section="validate", key="sample_percent", value=55
        )
        path.write_text(edited, encoding="utf-8")
        assert load_config(path).validate.sample_percent == 55


class TestApplyGates:
    @pytest.fixture
    def ready(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        return proposal, tmp_path / "autopilot.toml"

    def test_without_approve_nothing_is_changed(self, ready):
        proposal, config_path = ready
        before = config_path.read_text(encoding="utf-8")
        with pytest.raises(ProposalError, match="without --approve"):
            apply_proposal(
                proposal,
                config_path=config_path,
                approve=False,
                approved_by="someone",
                now_utc=NOW,
            )
        assert config_path.read_text(encoding="utf-8") == before

    def test_an_unnamed_approver_is_refused(self, ready):
        proposal, config_path = ready
        with pytest.raises(ProposalError, match="--approved-by"):
            apply_proposal(
                proposal, config_path=config_path, approve=True, approved_by="  ", now_utc=NOW
            )

    def test_an_empty_proposal_is_refused(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=200, regret_count=0))
        with pytest.raises(ProposalError, match="holds no changes"):
            apply_proposal(
                proposal,
                config_path=tmp_path / "autopilot.toml",
                approve=True,
                approved_by="a",
                now_utc=NOW,
            )

    def test_a_proposal_for_another_config_file_is_refused(self, ready, tmp_path):
        proposal, _ = ready
        other = tmp_path / "elsewhere"
        other.mkdir()
        write_config(other)
        with pytest.raises(ProposalError, match="was built against"):
            apply_proposal(
                proposal,
                config_path=other / "autopilot.toml",
                approve=True,
                approved_by="a",
                now_utc=NOW,
            )

    def test_a_config_that_moved_on_is_refused_and_names_both_values(self, ready):
        proposal, config_path = ready
        config_path.write_text(
            replace_scalar(
                config_path.read_text(encoding="utf-8"),
                section="policy",
                key="T1_TRIVIAL",
                value=2,
            ),
            encoding="utf-8",
        )
        with pytest.raises(ProposalError, match="is 2 in .*but this proposal was built when"):
            apply_proposal(
                proposal, config_path=config_path, approve=True, approved_by="a", now_utc=NOW
            )

    def test_applying_twice_is_refused(self, ready):
        proposal, config_path = ready
        applied = apply_proposal(
            proposal, config_path=config_path, approve=True, approved_by="Bobby", now_utc=NOW
        )
        with pytest.raises(ProposalError, match="already applied"):
            apply_proposal(
                applied, config_path=config_path, approve=True, approved_by="Bobby", now_utc=NOW
            )

    def test_a_key_this_build_may_not_edit_is_refused(self, tmp_path):
        write_config(tmp_path)
        proposal = TuningProposal(
            month="2026-09",
            generated_utc=NOW,
            config_path=str(tmp_path / "autopilot.toml"),
            changes=(
                ProposedChange(
                    section="budgets",
                    key="default_monthly_cap_micro_usd",
                    current_value=2000000,
                    proposed_value=9000000,
                    evidence="e",
                    rule_ids=("r",),
                ),
            ),
        )
        with pytest.raises(ProposalError, match="can only apply"):
            apply_proposal(
                proposal,
                config_path=tmp_path / "autopilot.toml",
                approve=True,
                approved_by="a",
                now_utc=NOW,
            )

    def test_an_unknown_tier_in_the_policy_section_is_refused(self, tmp_path):
        write_config(tmp_path)
        proposal = TuningProposal(
            month="2026-09",
            generated_utc=NOW,
            config_path=str(tmp_path / "autopilot.toml"),
            changes=(
                ProposedChange(
                    section="policy",
                    key="T9_IMAGINARY",
                    current_value=0,
                    proposed_value=1,
                    evidence="e",
                    rule_ids=("r",),
                ),
            ),
        )
        with pytest.raises(ProposalError, match="not a known tier"):
            apply_proposal(
                proposal,
                config_path=tmp_path / "autopilot.toml",
                approve=True,
                approved_by="a",
                now_utc=NOW,
            )


class TestApplySucceeding:
    def test_the_config_really_changes_and_still_loads(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        config_path = tmp_path / "autopilot.toml"
        applied = apply_proposal(
            proposal, config_path=config_path, approve=True, approved_by="Bobby", now_utc=NOW
        )
        reloaded = load_config(config_path)
        from cost_autopilot.classify.scorer import Tier

        assert reloaded.policy.floor_for(Tier.T1_TRIVIAL) == 1
        assert applied.status == STATUS_APPLIED
        assert applied.approved_by == "Bobby"
        assert applied.applied_utc == NOW

    def test_the_comments_in_the_configuration_survive(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        config_path = tmp_path / "autopilot.toml"
        before = config_path.read_text(encoding="utf-8")
        apply_proposal(
            proposal, config_path=config_path, approve=True, approved_by="Bobby", now_utc=NOW
        )
        after = config_path.read_text(encoding="utf-8")
        assert before.count("#") == after.count("#")
        assert "Model identifiers are deliberately absent" in after
        assert before.count("\n") == after.count("\n")

    def test_nothing_but_the_named_line_changes(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        config_path = tmp_path / "autopilot.toml"
        before = config_path.read_text(encoding="utf-8").splitlines()
        apply_proposal(
            proposal, config_path=config_path, approve=True, approved_by="Bobby", now_utc=NOW
        )
        after = config_path.read_text(encoding="utf-8").splitlines()
        differing = [
            (one, two) for one, two in zip(before, after, strict=True) if one != two
        ]
        assert differing == [("T1_TRIVIAL = 0", "T1_TRIVIAL = 1")]

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        apply_proposal(
            proposal,
            config_path=tmp_path / "autopilot.toml",
            approve=True,
            approved_by="Bobby",
            now_utc=NOW,
        )
        assert list(tmp_path.glob("*.proposed")) == []

    def test_the_approval_is_written_back_into_the_proposal_file(self, tmp_path):
        proposal = proposal_for(tmp_path, make_verdicts(count=20, regret_count=10))
        path = write_proposal(tmp_path / "report" / "2026-09" / "proposal.json", proposal)
        apply_and_record(
            path,
            proposal,
            config_path=tmp_path / "autopilot.toml",
            approve=True,
            approved_by="Bobby Meher",
            now_utc=NOW,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["status"] == STATUS_APPLIED
        assert payload["approved_by"] == "Bobby Meher"
        assert payload["approved_utc"] == NOW
        assert payload["applied_utc"] == NOW

