"""Tests for the campaign state machine and domain enums."""

from __future__ import annotations

import pytest

from core.enums import (
    AUDIENCE_LABELS,
    EDITABLE_STATES,
    SENDABLE_STATES,
    SENDING_ROLES,
    TONE_LABELS,
    Audience,
    CampaignStatus,
    Category,
    EditableField,
    Tone,
    UserRole,
    allowed_transitions,
    can_transition,
    is_terminal,
)


class TestStateMachine:
    def test_every_status_has_a_transition_rule(self) -> None:
        """A status missing from the table would raise KeyError at runtime —
        in the middle of a send."""
        for status in CampaignStatus:
            assert isinstance(allowed_transitions(status), frozenset)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (CampaignStatus.DRAFT, CampaignStatus.READY),
            (CampaignStatus.DRAFT, CampaignStatus.SENDING),
            (CampaignStatus.READY, CampaignStatus.SENDING),
            (CampaignStatus.READY, CampaignStatus.DRAFT),
            (CampaignStatus.SENDING, CampaignStatus.SENT),
            (CampaignStatus.SENDING, CampaignStatus.PARTIAL_FAILURE),
            (CampaignStatus.PARTIAL_FAILURE, CampaignStatus.SENDING),
            (CampaignStatus.FAILED, CampaignStatus.SENDING),
            (CampaignStatus.SENT, CampaignStatus.ARCHIVED),
        ],
    )
    def test_legal_transitions(self, current: CampaignStatus, target: CampaignStatus) -> None:
        assert can_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (CampaignStatus.DRAFT, CampaignStatus.SENT),  # cannot skip SENDING
            (CampaignStatus.SENT, CampaignStatus.SENDING),  # no resending a sent campaign
            (CampaignStatus.SENT, CampaignStatus.DRAFT),
            (CampaignStatus.ARCHIVED, CampaignStatus.DRAFT),
            (CampaignStatus.SENDING, CampaignStatus.DRAFT),
        ],
    )
    def test_illegal_transitions(self, current: CampaignStatus, target: CampaignStatus) -> None:
        assert not can_transition(current, target)

    def test_a_sent_campaign_can_never_be_sent_again(self) -> None:
        """The most expensive possible bug: mailing 487 customers twice."""
        assert CampaignStatus.SENDING not in allowed_transitions(CampaignStatus.SENT)

    def test_sending_is_only_reachable_from_sendable_states(self) -> None:
        """Backs the repository's guarded UPDATE, which is what makes a
        duplicate Streamlit rerun a no-op rather than a second send."""
        sources = {s for s in CampaignStatus if CampaignStatus.SENDING in allowed_transitions(s)}

        assert sources == SENDABLE_STATES | {
            CampaignStatus.PARTIAL_FAILURE,
            CampaignStatus.FAILED,
        }

    def test_archived_is_terminal(self) -> None:
        assert is_terminal(CampaignStatus.ARCHIVED)

    def test_no_status_can_transition_to_itself(self) -> None:
        for status in CampaignStatus:
            assert not can_transition(status, status)

    def test_settled_campaigns_can_be_archived(self) -> None:
        """Every state except SENDING can be archived. A campaign mid-send must
        reach a settled state first, or archiving would orphan its send records
        while the batch loop is still writing them."""
        for status in CampaignStatus:
            if status in (CampaignStatus.ARCHIVED, CampaignStatus.SENDING):
                continue
            assert can_transition(status, CampaignStatus.ARCHIVED), status

    def test_an_in_flight_campaign_cannot_be_archived(self) -> None:
        assert not can_transition(CampaignStatus.SENDING, CampaignStatus.ARCHIVED)

    def test_editable_states_exclude_in_flight_and_settled(self) -> None:
        assert CampaignStatus.SENDING not in EDITABLE_STATES
        assert CampaignStatus.SENT not in EDITABLE_STATES


class TestEnumValues:
    def test_enums_serialise_as_plain_strings(self) -> None:
        """StrEnum means values round-trip through JSON and SQLite untouched."""
        assert Tone.PROFESSIONAL == "professional"
        assert Category.PRODUCT_LAUNCH == "Product Launch"

    def test_every_tone_has_a_label(self) -> None:
        assert set(TONE_LABELS) == set(Tone)

    def test_every_audience_has_a_label(self) -> None:
        """Labels are interpolated into prompts; a missing one would render an
        empty string and silently degrade the output."""
        assert set(AUDIENCE_LABELS) == set(Audience)

    def test_category_set_is_closed(self) -> None:
        """Emitted under guided decoding, so the model cannot invent a category —
        it can only select one of these."""
        assert len(list(Category)) == 8

    def test_editable_fields_match_the_newsletter_contract(self) -> None:
        from core.models import NewsletterContent

        assert {f.value for f in EditableField} == set(NewsletterContent.model_fields)


class TestRoles:
    def test_editors_cannot_send(self) -> None:
        assert UserRole.EDITOR not in SENDING_ROLES

    def test_approvers_and_admins_can_send(self) -> None:
        assert set(SENDING_ROLES) == {UserRole.APPROVER, UserRole.ADMIN}
