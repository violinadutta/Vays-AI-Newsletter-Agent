"""The approval gate, expressed as a state machine.

The single most important property in the automation: **an unapproved campaign
cannot be sent**. It is enforced by ``AWAITING_APPROVAL`` being absent from
``SENDABLE_STATES``, which the repository's existing conditional UPDATE already
consults — the same mechanism that makes double-sending impossible.

That means the guarantee holds without a new check anyone could forget to call,
which is why these tests assert the set membership directly rather than only the
behaviour built on top of it.
"""

from __future__ import annotations

import pytest

from core.enums import (
    PENDING_APPROVAL_STATES,
    SENDABLE_STATES,
    CampaignStatus,
    allowed_transitions,
    can_transition,
    is_terminal,
)


class TestTheApprovalGate:
    def test_an_unapproved_campaign_is_not_sendable(self) -> None:
        """The property the whole human-in-the-loop design rests on."""
        assert CampaignStatus.AWAITING_APPROVAL not in SENDABLE_STATES

    def test_a_rejected_campaign_is_not_sendable(self) -> None:
        assert CampaignStatus.REJECTED not in SENDABLE_STATES

    def test_an_approved_campaign_is_sendable(self) -> None:
        assert CampaignStatus.APPROVED in SENDABLE_STATES

    def test_pending_cannot_jump_straight_to_sending(self) -> None:
        assert not can_transition(CampaignStatus.AWAITING_APPROVAL, CampaignStatus.SENDING)

    def test_rejected_cannot_reach_sending_by_any_route(self) -> None:
        """Rejection must be final. A route back through DRAFT would let a
        rejected campaign quietly become sendable again."""
        reachable = set(allowed_transitions(CampaignStatus.REJECTED))
        for state in list(reachable):
            reachable |= set(allowed_transitions(state))

        assert CampaignStatus.SENDING not in reachable

    def test_approved_cannot_be_edited_back_into_a_draft(self) -> None:
        """An approval that survives later edits guarantees nothing about what
        actually goes out."""
        assert not can_transition(CampaignStatus.APPROVED, CampaignStatus.DRAFT)


class TestTheAgentPath:
    def test_a_draft_can_be_submitted_for_approval(self) -> None:
        assert can_transition(CampaignStatus.DRAFT, CampaignStatus.AWAITING_APPROVAL)

    def test_pending_can_be_approved_or_rejected(self) -> None:
        assert can_transition(CampaignStatus.AWAITING_APPROVAL, CampaignStatus.APPROVED)
        assert can_transition(CampaignStatus.AWAITING_APPROVAL, CampaignStatus.REJECTED)

    def test_approved_can_be_sent(self) -> None:
        assert can_transition(CampaignStatus.APPROVED, CampaignStatus.SENDING)

    def test_the_full_agent_path_is_walkable(self) -> None:
        path = [
            CampaignStatus.DRAFT,
            CampaignStatus.AWAITING_APPROVAL,
            CampaignStatus.APPROVED,
            CampaignStatus.SENDING,
            CampaignStatus.SENT,
        ]

        for current, target in zip(path, path[1:], strict=False):
            assert can_transition(current, target), f"{current} -> {target} is not allowed"

    def test_pending_states_are_declared_for_the_dashboard(self) -> None:
        assert frozenset({CampaignStatus.AWAITING_APPROVAL}) == PENDING_APPROVAL_STATES


class TestTheManualPathIsUnchanged:
    """The existing UI workflow must behave exactly as it did before M10."""

    def test_a_draft_can_still_be_sent_directly(self) -> None:
        """The Send button on the Preview page. Requiring approval for manual
        campaigns too would change existing behaviour, which is out of scope."""
        assert can_transition(CampaignStatus.DRAFT, CampaignStatus.SENDING)

    def test_ready_can_still_be_sent_directly(self) -> None:
        assert can_transition(CampaignStatus.READY, CampaignStatus.SENDING)

    def test_draft_and_ready_are_still_sendable(self) -> None:
        assert {CampaignStatus.DRAFT, CampaignStatus.READY} <= SENDABLE_STATES

    def test_retry_paths_are_unchanged(self) -> None:
        assert can_transition(CampaignStatus.PARTIAL_FAILURE, CampaignStatus.SENDING)
        assert can_transition(CampaignStatus.FAILED, CampaignStatus.SENDING)


class TestInvariants:
    def test_sendable_states_match_the_transition_table(self) -> None:
        """The rule is expressed twice — in ``SENDABLE_STATES`` and in the
        transition table — so they are asserted to agree. Without this, a state
        could become sendable in one place and not the other, and the guarded
        UPDATE would disagree with the state machine."""
        sources = {s for s in CampaignStatus if CampaignStatus.SENDING in allowed_transitions(s)}

        assert sources == SENDABLE_STATES | {
            CampaignStatus.PARTIAL_FAILURE,
            CampaignStatus.FAILED,
        }

    def test_no_new_state_is_accidentally_terminal(self) -> None:
        for state in (CampaignStatus.AWAITING_APPROVAL, CampaignStatus.APPROVED):
            assert not is_terminal(state)

    def test_every_state_has_a_route_to_archived(self) -> None:
        """Otherwise a campaign in that state could never be tidied away."""
        for state in CampaignStatus:
            if state is CampaignStatus.ARCHIVED:
                continue
            reachable = set(allowed_transitions(state))
            for nxt in list(reachable):
                reachable |= set(allowed_transitions(nxt))
            assert CampaignStatus.ARCHIVED in reachable, f"{state} can never be archived"

    @pytest.mark.parametrize(
        "state",
        [CampaignStatus.AWAITING_APPROVAL, CampaignStatus.APPROVED, CampaignStatus.REJECTED],
    )
    def test_new_states_cannot_transition_to_themselves(self, state: CampaignStatus) -> None:
        assert not can_transition(state, state)
