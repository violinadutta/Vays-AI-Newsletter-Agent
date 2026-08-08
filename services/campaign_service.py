"""Campaign lifecycle: drafts, edits, history, duplication.

Thin over the repository on purpose — the interesting logic (the send guard, the
state machine) lives where it can be enforced atomically, in SQL. What this layer
adds is the transaction boundary and the translation between ORM rows and the
DTOs the UI works with.
"""

from __future__ import annotations

from config import get_logger
from core.enums import EDITABLE_STATES, CampaignStatus
from core.exceptions import ValidationError
from core.models import (
    CampaignFilter,
    CampaignSummary,
    ContentPatch,
    NewsletterContent,
    Page,
)
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from modules.repository.recipient_repo import RecipientRepository
from modules.repository.send_repo import SendRepository

log = get_logger(__name__)


class CampaignService:
    """Use cases for the History and Preview pages."""

    # ── reads ────────────────────────────────────────────────────────────────
    def get_content(self, campaign_id: int) -> NewsletterContent:
        """The editable content of a campaign.

        Raises:
            ValidationError: The campaign does not exist.
        """
        with unit_of_work() as session:
            row = CampaignRepository(session).get(campaign_id)
            if row is None:
                raise ValidationError(
                    f"campaign {campaign_id} not found",
                    user_message="That campaign no longer exists.",
                )
            return NewsletterContent(
                title=row.title or "",
                summary=row.summary or "",
                newsletter=row.newsletter or "",
                subject=row.subject or "",
                preview_text=row.preview_text or "",
                cta=row.cta or "",
                keywords=row.keywords or [],
                category=row.category,
                tone=row.tone,
            )

    def list_campaigns(self, filters: CampaignFilter | None = None) -> Page[CampaignSummary]:
        with unit_of_work() as session:
            return CampaignRepository(session).list_page(filters)

    def recent(self, limit: int = 5) -> list[CampaignSummary]:
        with unit_of_work() as session:
            return CampaignRepository(session).recent(limit)

    def status_counts(self) -> dict[str, int]:
        with unit_of_work() as session:
            return CampaignRepository(session).count_by_status()

    def failures(self, campaign_id: int) -> list[tuple[str, str]]:
        """Failed recipients with their reasons, for the retry table."""
        with unit_of_work() as session:
            return [
                (recipient.email, record.error_message or "Unknown error")
                for record, recipient in SendRepository(session).failed_recipients(campaign_id)
            ]

    # ── writes ───────────────────────────────────────────────────────────────
    def update_content(self, campaign_id: int, patch: ContentPatch) -> None:
        """Apply a partial edit.

        Refuses once a campaign is sending or sent. Editing content mid-send would
        mean two recipients received materially different emails from the same
        campaign, and the History record could match neither.

        Raises:
            ValidationError: The campaign is missing, or no longer editable.
        """
        with unit_of_work() as session:
            repo = CampaignRepository(session)
            row = repo.get(campaign_id)
            if row is None:
                raise ValidationError(
                    f"campaign {campaign_id} not found",
                    user_message="That campaign no longer exists.",
                )
            if CampaignStatus(row.status) not in EDITABLE_STATES:
                raise ValidationError(
                    f"campaign {campaign_id} is {row.status} and cannot be edited",
                    user_message=(
                        "This campaign has already been sent, so its content is locked. "
                        "Duplicate it to make a new version."
                    ),
                    context={"status": str(row.status)},
                )
            repo.update_content(campaign_id, patch)

    def mark_ready(self, campaign_id: int) -> None:
        with unit_of_work() as session:
            CampaignRepository(session).transition_or_raise(campaign_id, CampaignStatus.READY)

    def archive(self, campaign_id: int) -> None:
        with unit_of_work() as session:
            CampaignRepository(session).transition_or_raise(campaign_id, CampaignStatus.ARCHIVED)

    def duplicate(self, campaign_id: int) -> int:
        """Copy a campaign as a new DRAFT (FR-7.4).

        Content and template are copied; recipients and send history are not. A
        duplicate is a new campaign that happens to start from the same copy —
        inheriting the old list would be a surprising way to mail 500 people.
        """
        with unit_of_work() as session:
            repo = CampaignRepository(session)
            source = repo.get(campaign_id)
            if source is None:
                raise ValidationError(
                    f"campaign {campaign_id} not found",
                    user_message="That campaign no longer exists.",
                )

            content = NewsletterContent(
                title=source.title or "",
                summary=source.summary or "",
                newsletter=source.newsletter or "",
                subject=source.subject or "",
                preview_text=source.preview_text or "",
                cta=source.cta or "",
                keywords=source.keywords or [],
                category=source.category,
                tone=source.tone,
            )
            copy = repo.create(
                name=f"{source.name} (copy)",
                content=content,
                provenance={
                    "model_name": source.model_name,
                    "provider": source.provider,
                    "prompt_version": source.prompt_version,
                    "generation_params": source.generation_params,
                    "template_id": source.template_id,
                },
                created_by=source.created_by,
            )
            new_id = int(copy.id)

        log.info("campaign.duplicated", source=campaign_id, copy=new_id)
        return new_id

    def delete(self, campaign_id: int) -> None:
        """Delete a campaign. Only drafts — sent campaigns are the audit trail.

        Raises:
            ValidationError: The campaign has been sent.
        """
        with unit_of_work() as session:
            repo = CampaignRepository(session)
            row = repo.get(campaign_id)
            if row is None:
                return  # already gone; deleting twice is not an error
            if CampaignStatus(row.status) not in EDITABLE_STATES:
                raise ValidationError(
                    f"campaign {campaign_id} is {row.status} and cannot be deleted",
                    user_message=(
                        "Sent campaigns can't be deleted — they are the record of what "
                        "went out. Archive it instead."
                    ),
                )
            repo.delete(campaign_id)

        log.info("campaign.deleted", campaign_id=campaign_id)

    def recipient_count(self, campaign_id: int) -> int:
        with unit_of_work() as session:
            return RecipientRepository(session).count_for_campaign(campaign_id)
