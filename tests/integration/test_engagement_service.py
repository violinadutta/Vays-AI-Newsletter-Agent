"""Like and unsubscribe links: what a click does, and what a robot must not.

The single most important test in this file is
``test_inspecting_a_link_records_nothing``. Gmail and Outlook prefetch links in
messages with security scanners, so if merely *loading* the page acted, people
would be unsubscribed without asking and the like count would measure mail
infrastructure rather than readers. Everything else here is about a link being
exactly as forgeable as a password, which is to say not at all.
"""

from __future__ import annotations

import pytest

from core.auth import sign_recipient_token
from core.enums import CampaignStatus, EmailAction, SuppressionReason
from modules.repository.orm_models import CampaignORM, SubscriberORM
from services.engagement_service import TOKEN_PARAM, EngagementService

pytestmark = pytest.mark.integration

EMAIL = "dana@client.com"


@pytest.fixture(autouse=True)
def _env(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV, AGENT_APP_BASE_URL="https://news.example.com")


@pytest.fixture
def campaign(db_session) -> int:  # noqa: ANN001
    row = CampaignORM(name="August", status=CampaignStatus.SENT, subject="August newsletter")
    db_session.add(row)
    db_session.commit()
    return row.id


def secret() -> str:
    from config import get_settings

    return get_settings().app.secret_key.get_secret_value()


def token_from(url: str) -> str:
    return url.split(f"{TOKEN_PARAM}=", 1)[1]


class TestTheLink:
    def test_it_uses_the_configured_public_base(self, campaign: int) -> None:
        """The one variable that moves this from localhost to a real domain."""
        link = EngagementService().link(EMAIL, campaign, EmailAction.LIKED)

        assert link.startswith("https://news.example.com/?")

    def test_each_recipient_gets_a_different_link(self, campaign: int) -> None:
        """Identity is what the old static unsubscribe URL lacked entirely."""
        service = EngagementService()

        first = service.link("a@client.com", campaign, EmailAction.LIKED)
        second = service.link("b@client.com", campaign, EmailAction.LIKED)

        assert first != second

    def test_like_and_unsubscribe_differ(self, campaign: int) -> None:
        service = EngagementService()

        like = service.link(EMAIL, campaign, EmailAction.LIKED)
        unsubscribe = service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED)

        assert like != unsubscribe


class TestPrefetchSafety:
    def test_inspecting_a_link_records_nothing(self, campaign: int, db_session) -> None:  # noqa: ANN001
        """The test this whole design exists for.

        A mail scanner fetches the URL. That must read and never write.
        """
        from modules.repository.orm_models import EmailEventORM

        service = EngagementService()
        token = token_from(service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED))

        for _ in range(3):
            assert service.inspect(token).valid

        assert db_session.query(EmailEventORM).count() == 0, "inspect() wrote an event"

    def test_inspecting_an_unsubscribe_does_not_suppress(self, campaign: int) -> None:
        """A scanner prefetching the link must not stop someone's mail."""
        from modules.repository.database import unit_of_work
        from modules.repository.recipient_repo import SuppressionRepository

        service = EngagementService()
        service.inspect(token_from(service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED)))

        with unit_of_work() as session:
            assert SuppressionRepository(session).filter_suppressed([EMAIL]) == set()


class TestTheTokenIsNotForgeable:
    def test_a_tampered_token_is_refused(self, campaign: int) -> None:
        token = token_from(EngagementService().link(EMAIL, campaign, EmailAction.LIKED))

        assert EngagementService().inspect(token[:-4] + "AAAA").valid is False

    def test_a_token_signed_with_another_key_is_refused(self, campaign: int) -> None:
        """Without APP_SECRET_KEY nobody can unsubscribe anybody else."""
        forged = sign_recipient_token(
            "victim@client.com", campaign, str(EmailAction.UNSUBSCRIBED), "w" * 48
        )

        assert EngagementService().inspect(forged).valid is False

    def test_a_like_token_cannot_be_used_as_an_unsubscribe(self, campaign: int) -> None:
        """The action is inside the signature, so editing the URL cannot move it."""
        like = token_from(EngagementService().link(EMAIL, campaign, EmailAction.LIKED))

        assert EngagementService().inspect(like, EmailAction.UNSUBSCRIBED).valid is False

    def test_the_action_is_read_from_the_token_not_the_request(self, campaign: int) -> None:
        service = EngagementService()

        unsubscribe = token_from(service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED))

        assert service.inspect(unsubscribe).action is EmailAction.UNSUBSCRIBED

    def test_garbage_is_refused_without_raising(self, campaign: int) -> None:  # noqa: ARG002
        for value in ("", "not-a-token", "a.b.c", "x" * 400):
            assert EngagementService().inspect(value).valid is False


class TestLiking:
    def test_a_confirmed_like_is_recorded(self, campaign: int) -> None:
        service = EngagementService()
        token = token_from(service.link(EMAIL, campaign, EmailAction.LIKED))

        result = service.apply(token)

        assert result.ok
        assert result.changed
        assert service.inspect(token).already_done

    def test_clicking_twice_counts_once(self, campaign: int) -> None:
        """A second click, or a mail client opening the link again, is still one
        person liking one newsletter."""
        service = EngagementService()
        token = token_from(service.link(EMAIL, campaign, EmailAction.LIKED))

        service.apply(token)
        second = service.apply(token)

        assert second.ok
        assert second.changed is False

    def test_a_like_does_not_suppress_anyone(self, campaign: int) -> None:
        """Obvious, and worth asserting: the two actions share a code path."""
        from modules.repository.database import unit_of_work
        from modules.repository.recipient_repo import SuppressionRepository

        service = EngagementService()
        service.apply(token_from(service.link(EMAIL, campaign, EmailAction.LIKED)))

        with unit_of_work() as session:
            assert SuppressionRepository(session).filter_suppressed([EMAIL]) == set()


class TestUnsubscribing:
    def test_it_suppresses_the_address(self, campaign: int) -> None:
        """The suppression list is what the send guard actually checks, so this
        is the assertion that proves the person stops receiving email."""
        from modules.repository.database import unit_of_work
        from modules.repository.recipient_repo import SuppressionRepository

        service = EngagementService()
        service.apply(token_from(service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED)))

        with unit_of_work() as session:
            assert SuppressionRepository(session).filter_suppressed([EMAIL]) == {EMAIL}

    def test_it_records_the_reason(self, campaign: int) -> None:
        from modules.repository.database import unit_of_work
        from modules.repository.orm_models import SuppressionORM

        service = EngagementService()
        service.apply(token_from(service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED)))

        with unit_of_work() as session:
            row = session.get(SuppressionORM, EMAIL)
            assert row is not None
            assert row.reason == SuppressionReason.UNSUBSCRIBED

    def test_it_deactivates_the_subscriber(self, campaign: int, db_session) -> None:  # noqa: ANN001
        db_session.add(SubscriberORM(email=EMAIL, name="Dana"))
        db_session.commit()

        service = EngagementService()
        service.apply(token_from(service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED)))

        db_session.expire_all()
        assert db_session.get(SubscriberORM, EMAIL).is_active is False

    def test_unsubscribing_twice_stays_unsubscribed(self, campaign: int) -> None:
        from modules.repository.database import unit_of_work
        from modules.repository.recipient_repo import SuppressionRepository

        service = EngagementService()
        token = token_from(service.link(EMAIL, campaign, EmailAction.UNSUBSCRIBED))

        service.apply(token)
        second = service.apply(token)

        assert second.ok
        assert second.changed is False
        with unit_of_work() as session:
            assert SuppressionRepository(session).filter_suppressed([EMAIL]) == {EMAIL}

    def test_a_forged_token_cannot_unsubscribe_someone(self, campaign: int) -> None:
        """The attack this design has to stop: opting out a competitor's
        contacts by editing an address into a URL."""
        from modules.repository.database import unit_of_work
        from modules.repository.recipient_repo import SuppressionRepository

        forged = sign_recipient_token(
            "victim@client.com", campaign, str(EmailAction.UNSUBSCRIBED), "w" * 48
        )

        result = EngagementService().apply(forged)

        assert result.ok is False
        with unit_of_work() as session:
            assert SuppressionRepository(session).filter_suppressed(["victim@client.com"]) == set()


class TestEmailNormalisation:
    def test_case_differences_resolve_to_one_person(self, campaign: int) -> None:
        """A link minted for Dana@ must match the row stored for dana@, or the
        same person could like twice and unsubscribe without it sticking."""
        service = EngagementService()
        service.apply(token_from(service.link("Dana@Client.com", campaign, EmailAction.LIKED)))

        lower = token_from(service.link("dana@client.com", campaign, EmailAction.LIKED))

        assert service.inspect(lower).already_done


class TestLinksReachTheEmail:
    """The join between minting a link and it actually being in the message."""

    def test_a_campaign_send_carries_tracked_links(self, campaign: int) -> None:
        """Both footer links must resolve to the signed, per-recipient URLs."""
        from core.models import Recipient
        from modules.template.brand import resolve_brand
        from services.delivery_service import DeliveryService

        links = DeliveryService._recipient_links(Recipient(email=EMAIL), campaign, resolve_brand())

        assert "?t=" in links["like_url"]
        assert "?t=" in links["unsubscribe_url"]
        assert links["like_url"] != links["unsubscribe_url"]

    def test_a_test_send_falls_back_to_the_plain_unsubscribe(self) -> None:
        """No campaign means a click could not be attributed to one. The email
        must still go, and must still be compliant — so it degrades to the
        static URL rather than being blocked."""
        from core.models import Recipient
        from modules.template.brand import resolve_brand
        from services.delivery_service import DeliveryService

        brand = resolve_brand()
        links = DeliveryService._recipient_links(Recipient(email=EMAIL), None, brand)

        assert links["unsubscribe_url"] == brand.unsubscribe_url
        assert "?t=" not in links["unsubscribe_url"]

    def test_an_unresolved_token_is_refused_before_sending(self) -> None:
        """The last guard before the provider.

        The render-time compliance check accepts the unsubscribe *token* in
        place of a URL, which is only safe because of this. A literal
        "{{unsubscribe_url}}" in a customer's inbox would be both embarrassing
        and a compliance failure.
        """
        from core.exceptions import EmailError
        from services.delivery_service import DeliveryService

        with pytest.raises(EmailError, match="unresolved link tokens"):
            DeliveryService._assert_no_unresolved_links(
                "<a href='{{unsubscribe_url}}'>Unsubscribe</a>", "text", EMAIL
            )

    def test_a_fully_resolved_message_passes_the_guard(self) -> None:
        from services.delivery_service import DeliveryService

        DeliveryService._assert_no_unresolved_links(
            "<a href='https://x/?t=abc'>Unsubscribe</a>", "Unsubscribe: https://x/?t=abc", EMAIL
        )
