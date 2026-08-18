"""Approval tokens and decisions.

Most of this file is security. The failure it guards against is a campaign
reaching customers because something followed a link — a mail scanner, a preview
fetcher, a forward to the wrong person — rather than because a human decided.

Each test is named after the attack or mistake it prevents.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from core.enums import CampaignStatus, Category, Tone, UserRole
from core.exceptions import ValidationError
from core.models import NewsletterContent
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from modules.repository.orm_models import ApprovalTokenORM
from services.approval_service import TOKEN_PARAM, ApprovalService, review_url

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _env(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV, AGENT_APPROVAL_EMAIL="management@vaysinfotech.com")


def a_campaign(*, awaiting: bool = True) -> int:
    content = NewsletterContent(
        title="What a rugged industrial firewall actually costs",
        summary="The sticker price is the smallest part of a rugged firewall's cost.",
        newsletter=(
            "A rugged firewall sits where the plant network meets everything else.\n\n"
            "Power, mounting and commissioning outweigh the hardware over five years."
        ),
        subject="What a rugged firewall actually costs",
        preview_text="Total cost of ownership, not sticker price",
        cta="Read the breakdown",
        keywords=["ot", "firewall", "security"],
        category=Category.SECURITY,
        tone=Tone.PROFESSIONAL,
    )
    with unit_of_work() as session:
        repo = CampaignRepository(session)
        campaign_id = int(repo.create(name="Agent draft", content=content).id)
        if awaiting:
            repo.transition_or_raise(campaign_id, CampaignStatus.AWAITING_APPROVAL)
    return campaign_id


def status_of(campaign_id: int) -> str:
    with unit_of_work() as session:
        return str(CampaignRepository(session).get(campaign_id).status)


# ─────────────────────────────────────────────────────────────────────────────
#  Token security
# ─────────────────────────────────────────────────────────────────────────────
class TestTokenSecurity:
    def test_the_raw_token_is_never_stored(self) -> None:
        """A database read, a backup or a screenshot of a query must not yield a
        working link. Same reasoning as passwords."""
        link = ApprovalService().issue(a_campaign())

        with unit_of_work() as session:
            rows = session.execute(select(ApprovalTokenORM)).scalars().all()
            stored = [r.token_hash for r in rows]

        assert link.token not in stored
        assert all(len(h) == 64 for h in stored)  # sha256 hex

    def test_tokens_are_unguessable(self) -> None:
        tokens = {ApprovalService().issue(a_campaign()).token for _ in range(20)}

        assert len(tokens) == 20
        assert all(len(t) >= 40 for t in tokens)

    def test_an_unknown_token_is_refused(self) -> None:
        assert ApprovalService().check("not-a-real-token").rejected

    @pytest.mark.parametrize("token", ["", "   ", None])
    def test_a_missing_token_is_refused(self, token: str | None) -> None:
        assert ApprovalService().check(token or "").rejected

    def test_the_refusal_reason_does_not_distinguish_unknown_from_expired(self) -> None:
        """Telling a prober which guesses were closer is free reconnaissance."""
        unknown = ApprovalService().check("wrong").reason

        assert "expired" not in unknown.lower()
        assert "not valid" in unknown.lower() or "isn't valid" in unknown.lower()

    def test_the_url_carries_the_token_and_nothing_else(self) -> None:
        """No campaign id, no counts, no content. A forwarded message or a proxy
        log must not leak what the campaign is about."""
        from urllib.parse import parse_qs, urlsplit

        campaign_id = a_campaign()
        link = ApprovalService().issue(campaign_id)

        parts = urlsplit(link.url)
        query = parse_qs(parts.query)

        # Exactly one parameter, and it is the opaque token. Checking that the
        # campaign id is absent from the string would be unsound — a small
        # integer appears inside a random base64 token by chance — so the
        # property asserted is the one that actually matters: nothing else is
        # carried at all.
        assert list(query) == [TOKEN_PARAM]
        assert query[TOKEN_PARAM] == [link.token]
        assert parts.path.endswith("/approvals")
        assert str(campaign_id) not in parts.path

    def test_the_url_is_absolute(self) -> None:
        """An email is read on another machine; a relative link is unclickable."""
        assert review_url("abc").startswith("http")


class TestSingleUse:
    def test_a_token_is_spent_by_a_decision(self) -> None:
        """A forwarded link must not let someone reverse the decision already
        made by the person it was sent to."""
        campaign_id = a_campaign()
        service = ApprovalService()
        link = service.issue(campaign_id)

        service.approve(campaign_id, by="admin", role=UserRole.ADMIN, token=link.token)

        assert service.check(link.token).rejected
        assert "already been used" in service.check(link.token).reason

    def test_checking_does_not_spend_it(self) -> None:
        """Opening the page twice, or a preview fetcher touching the link, must
        not consume the reviewer's only chance to decide."""
        service = ApprovalService()
        link = service.issue(a_campaign())

        assert service.check(link.token).valid
        assert service.check(link.token).valid

    def test_issuing_a_new_link_invalidates_the_old_one(self) -> None:
        """Two live links for one campaign means a stale email can still act on
        it after a newer request superseded it."""
        campaign_id = a_campaign()
        service = ApprovalService()
        first = service.issue(campaign_id)

        service.issue(campaign_id)

        assert service.check(first.token).rejected

    def test_every_live_token_is_spent_by_one_decision(self) -> None:
        campaign_id = a_campaign()
        service = ApprovalService()
        link = service.issue(campaign_id)

        service.reject(campaign_id, by="admin", role=UserRole.ADMIN)

        with unit_of_work() as session:
            rows = (
                session.execute(
                    select(ApprovalTokenORM).where(ApprovalTokenORM.campaign_id == campaign_id)
                )
                .scalars()
                .all()
            )
        assert all(r.used_at is not None for r in rows)
        assert service.check(link.token).rejected


class TestExpiry:
    def test_an_expired_link_is_refused(self) -> None:
        """Expired means the campaign is *not* sent — silence is the safe
        outcome when nobody got round to deciding."""
        campaign_id = a_campaign()
        service = ApprovalService()
        link = service.issue(campaign_id)

        with unit_of_work() as session:
            row = session.execute(
                select(ApprovalTokenORM).where(ApprovalTokenORM.campaign_id == campaign_id)
            ).scalar_one()
            row.expires_at = datetime.now(UTC) - timedelta(minutes=1)

        check = service.check(link.token)
        assert check.rejected
        assert "not sent" in check.reason

    def test_the_ttl_comes_from_settings(self, set_env) -> None:  # noqa: ANN001
        from config.settings import reset_settings_cache

        set_env(AGENT_APPROVAL_TOKEN_TTL_HOURS="1")
        reset_settings_cache()

        link = ApprovalService().issue(a_campaign())

        assert link.expires_at <= datetime.now(UTC) + timedelta(hours=1, seconds=5)


# ─────────────────────────────────────────────────────────────────────────────
#  Authorisation — the token is not permission
# ─────────────────────────────────────────────────────────────────────────────
class TestAuthorisation:
    def test_an_editor_cannot_approve_even_with_a_valid_link(self) -> None:
        """Holding the link is not permission to mail customers. Authorisation
        is the signed-in role, always."""
        campaign_id = a_campaign()
        link = ApprovalService().issue(campaign_id)

        with pytest.raises(ValidationError, match="may not approve"):
            ApprovalService().approve(
                campaign_id, by="editor", role=UserRole.EDITOR, token=link.token
            )

        assert status_of(campaign_id) == CampaignStatus.AWAITING_APPROVAL

    @pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.APPROVER])
    def test_approvers_and_admins_can_approve(self, role: UserRole) -> None:
        campaign_id = a_campaign()

        ApprovalService().approve(campaign_id, by="someone", role=role)

        assert status_of(campaign_id) == CampaignStatus.APPROVED

    def test_an_editor_cannot_reject_either(self) -> None:
        campaign_id = a_campaign()

        with pytest.raises(ValidationError):
            ApprovalService().reject(campaign_id, by="editor", role=UserRole.EDITOR)


# ─────────────────────────────────────────────────────────────────────────────
#  Decisions
# ─────────────────────────────────────────────────────────────────────────────
class TestDecisions:
    def test_approval_makes_the_campaign_sendable(self) -> None:
        campaign_id = a_campaign()

        ApprovalService().approve(campaign_id, by="admin", role=UserRole.ADMIN)

        with unit_of_work() as session:
            assert CampaignRepository(session).begin_send(campaign_id) is True

    def test_rejection_leaves_it_unsendable_forever(self) -> None:
        """The decision that must stick. A rejected campaign is never sent."""
        campaign_id = a_campaign()

        ApprovalService().reject(campaign_id, by="admin", role=UserRole.ADMIN)

        assert status_of(campaign_id) == CampaignStatus.REJECTED
        with unit_of_work() as session:
            assert CampaignRepository(session).begin_send(campaign_id) is False

    def test_deciding_twice_is_refused(self) -> None:
        """Two reviewers opening the same email must not fight over it."""
        campaign_id = a_campaign()
        service = ApprovalService()
        service.approve(campaign_id, by="admin", role=UserRole.ADMIN)

        with pytest.raises(ValidationError, match="not awaiting approval") as exc:
            service.reject(campaign_id, by="other", role=UserRole.ADMIN)
        assert "already been approved" in exc.value.user_message

        assert status_of(campaign_id) == CampaignStatus.APPROVED

    def test_a_draft_not_awaiting_approval_is_refused(self) -> None:
        """A manually created campaign is not the agent's to decide on."""
        campaign_id = a_campaign(awaiting=False)

        with pytest.raises(ValidationError, match="not awaiting approval") as exc:
            ApprovalService().approve(campaign_id, by="admin", role=UserRole.ADMIN)
        assert "still a draft" in exc.value.user_message

    def test_a_missing_campaign_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="not found"):
            ApprovalService().approve(9999, by="admin", role=UserRole.ADMIN)

    def test_pending_campaigns_are_listed_oldest_first(self) -> None:
        first, second = a_campaign(), a_campaign()

        assert ApprovalService().pending_campaigns() == [first, second]

    def test_a_decided_campaign_leaves_the_pending_list(self) -> None:
        campaign_id = a_campaign()
        ApprovalService().approve(campaign_id, by="admin", role=UserRole.ADMIN)

        assert ApprovalService().pending_campaigns() == []
