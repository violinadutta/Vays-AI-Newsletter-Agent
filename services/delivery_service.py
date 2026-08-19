"""Delivery use case: recipients in, per-recipient outcomes out.

Four guards sit between "click send" and a customer's inbox, each covering a
failure that is cheap to prevent and expensive to explain:

1. **The suppression list**, checked in bulk before anything is sent. Someone who
   unsubscribed stays unsubscribed even if their address is in a freshly uploaded
   CSV. Legal requirement, and the entire point of keeping the list.
2. **The double-send guard** — a conditional UPDATE claiming the campaign. A
   Streamlit rerun firing the handler twice matches zero rows the second time.
3. **Already-delivered filtering on retry.** "Retry failed only" must skip
   recipients who succeeded, or it re-mails everyone — the duplicate-send problem
   reintroduced one layer down.
4. **Render-time compliance** (in the template layer): no unsubscribe link or
   postal address means no email.
"""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Callable

from config import get_logger, get_settings, mask_email
from core.enums import CampaignStatus, SendStatus, SuppressionReason
from core.exceptions import (
    EmailAuthError,
    EmailError,
    EmailQuotaExceeded,
    InvalidCSVError,
    InvalidEmailError,
    ValidationError,
)
from core.models import (
    CampaignSendReport,
    EmailMessage,
    InlineImage,
    NewsletterContent,
    Recipient,
    RecipientValidation,
    RenderedEmail,
    SendResult,
)
from core.validators import validate_email_address
from modules.email.base import unsubscribe_headers
from modules.email.batcher import BatchSender
from modules.email.factory import create_email_provider
from modules.repository.campaign_repo import CampaignRepository
from modules.repository.database import unit_of_work
from modules.repository.recipient_repo import RecipientRepository, SuppressionRepository
from modules.repository.send_repo import SendRepository
from modules.template.brand import LOGO_CID, ResolvedBrand, logo_file, resolve_brand
from modules.template.renderer import LINK_TOKENS, TemplateRenderer, apply_merge_tokens

log = get_logger(__name__)

ProgressCallback = Callable[[int, int, int], None]

#: Column names accepted for the address, in preference order. Marketing exports
#: label it differently in every tool; rejecting "Email Address" would be
#: technically defensible and practically useless.
EMAIL_COLUMNS = ("email", "email address", "e-mail", "mail", "address")
NAME_COLUMNS = ("name", "full name", "first name", "firstname", "contact")
COMPANY_COLUMNS = ("company", "organisation", "organization", "account", "employer")

MAX_RECIPIENTS = 10_000


class DeliveryService:
    """Validates recipients, renders, and sends."""

    def __init__(
        self, renderer: TemplateRenderer | None = None, sender: BatchSender | None = None
    ) -> None:
        self._renderer = renderer or TemplateRenderer()
        self._sender = sender

    # ── recipients ───────────────────────────────────────────────────────────
    def validate_recipients(self, csv_bytes: bytes) -> RecipientValidation:
        """Parse and validate an uploaded CSV.

        Malformed rows are reported and skipped, never fatal: one bad row in 500
        must not force the user to fix a spreadsheet before they can send.

        Raises:
            InvalidCSVError: The file is unreadable or has no address column.
        """
        text = self._decode(csv_bytes)
        reader = csv.DictReader(io.StringIO(text))

        if not reader.fieldnames:
            raise InvalidCSVError(
                "the CSV has no header row",
                user_message=(
                    "That file has no column headers. The first row must name the columns."
                ),
            )

        columns = {(name or "").strip().lower(): name for name in reader.fieldnames}
        email_column = next((columns[c] for c in EMAIL_COLUMNS if c in columns), None)
        if email_column is None:
            raise InvalidCSVError(
                f"no email column in {reader.fieldnames}",
                user_message=(
                    "Your CSV needs a column named 'email'. "
                    f"Found: {', '.join(str(f) for f in reader.fieldnames)}."
                ),
                context={"columns": list(reader.fieldnames)},
            )

        name_column = next((columns[c] for c in NAME_COLUMNS if c in columns), None)
        company_column = next((columns[c] for c in COMPANY_COLUMNS if c in columns), None)

        validation = RecipientValidation()
        seen: set[str] = set()

        for line_number, row in enumerate(reader, start=2):
            raw = (row.get(email_column) or "").strip()
            if not raw:
                continue  # a blank line, not an error worth reporting
            try:
                address = validate_email_address(raw)
            except InvalidEmailError as exc:
                validation.invalid[f"row {line_number}: {raw}"] = exc.user_message
                continue

            if address in seen:
                validation.duplicates.append(address)
                continue
            seen.add(address)

            validation.valid.append(
                Recipient(
                    email=address,
                    name=(row.get(name_column) or "").strip() or None if name_column else None,
                    company=(row.get(company_column) or "").strip() or None
                    if company_column
                    else None,
                )
            )

        self._apply_suppressions(validation)

        if len(validation.valid) > MAX_RECIPIENTS:
            raise InvalidCSVError(
                f"{len(validation.valid)} recipients exceeds the {MAX_RECIPIENTS} limit",
                user_message=(
                    f"That list has {len(validation.valid):,} recipients. The maximum per "
                    f"campaign is {MAX_RECIPIENTS:,} — split it into several sends."
                ),
            )

        log.info(
            "recipients.validated",
            valid=len(validation.valid),
            invalid=len(validation.invalid),
            duplicates=len(validation.duplicates),
            suppressed=len(validation.suppressed),
        )
        return validation

    @staticmethod
    def _decode(raw: bytes) -> str:
        """Decode an uploaded file, tolerating what spreadsheets actually produce.

        Excel writes UTF-8 with a BOM, and older exports are often cp1252. Failing
        on either would be correct and unhelpful.
        """
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise InvalidCSVError(
            "could not decode the uploaded file",
            user_message="That file isn't readable as text. Export it again as CSV (UTF-8).",
        )

    @staticmethod
    def _apply_suppressions(validation: RecipientValidation) -> None:
        """Move suppressed addresses out of the sendable list.

        One bulk query, not one per address: 10,000 round trips before a send
        could even start would be its own outage.
        """
        if not validation.valid:
            return
        with unit_of_work() as session:
            blocked = SuppressionRepository(session).filter_suppressed(
                [r.email for r in validation.valid]
            )
        if not blocked:
            return
        validation.suppressed.extend(sorted(blocked))
        validation.valid[:] = [r for r in validation.valid if r.email not in blocked]

    def save_recipients(self, campaign_id: int, recipients: list[Recipient]) -> int:
        """Attach a validated list to a campaign, replacing any previous one."""
        with unit_of_work() as session:
            count = RecipientRepository(session).replace_all(campaign_id, recipients)
            CampaignRepository(session).update_delivery_counts(campaign_id, recipients=count)
        return count

    # ── rendering ────────────────────────────────────────────────────────────
    def render(
        self, content: NewsletterContent, template_id: str = "modern", **kwargs: object
    ) -> RenderedEmail:
        return self._renderer.render(content, template_id, **kwargs)  # type: ignore[arg-type]

    # ── sending ──────────────────────────────────────────────────────────────
    def send_test(
        self, content: NewsletterContent, to: str, template_id: str = "modern", **kwargs: object
    ) -> SendResult:
        """Send one message to one address.

        Records nothing: a test send is not a campaign, and counting it would
        corrupt the delivery stats the History page reports.
        """
        address = validate_email_address(to)
        rendered = self.render(content, template_id, **kwargs)
        provider = create_email_provider()
        try:
            result = provider.send(self._build_message(rendered, Recipient(email=address)))
        finally:
            provider.close()

        log.info("email.test_sent", ok=result.ok)
        return result

    def send_campaign(
        self,
        campaign_id: int,
        content: NewsletterContent,
        template_id: str = "modern",
        *,
        cta_url: str | None = None,
        source_urls: list[str] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> CampaignSendReport:
        """Send a campaign to its saved recipient list.

        Raises:
            ValidationError: The campaign is already sending or sent, or has no
                recipients.
        """
        started = time.monotonic()

        with unit_of_work() as session:
            recipients = RecipientRepository(session).list_for_campaign(campaign_id)
            if not recipients:
                raise ValidationError(
                    f"campaign {campaign_id} has no recipients",
                    user_message="Upload a recipient list before sending.",
                )
            # Claim the campaign. False means someone (or a Streamlit rerun) got
            # here first — the guard that makes a double send impossible.
            if not CampaignRepository(session).begin_send(campaign_id):
                raise ValidationError(
                    f"campaign {campaign_id} is not in a sendable state",
                    user_message=(
                        "This campaign is already sending, or has already been sent. "
                        "Refresh the page to see its current status."
                    ),
                )
            targets = [
                (r.id, Recipient(email=r.email, name=r.name, company=r.company)) for r in recipients
            ]

        rendered_by_recipient = self.render(
            content, template_id, cta_url=cta_url, source_urls=source_urls
        )
        results = self._dispatch(campaign_id, targets, rendered_by_recipient, on_progress)

        return self._finalise(campaign_id, results, time.monotonic() - started)

    def retry_failed(
        self,
        campaign_id: int,
        content: NewsletterContent,
        template_id: str = "modern",
        *,
        on_progress: ProgressCallback | None = None,
    ) -> CampaignSendReport:
        """Re-send only to recipients who have not already been delivered to.

        The already-delivered filter is the important part. Without it this
        re-mails everyone who succeeded — exactly the duplicate send the status
        guard exists to prevent, reintroduced one layer down.
        """
        started = time.monotonic()

        with unit_of_work() as session:
            delivered = SendRepository(session).already_sent_recipient_ids(campaign_id)
            rows = RecipientRepository(session).list_for_campaign(campaign_id)
            targets = [
                (r.id, Recipient(email=r.email, name=r.name, company=r.company))
                for r in rows
                if r.id not in delivered
            ]
            if not targets:
                raise ValidationError(
                    f"campaign {campaign_id} has nothing to retry",
                    user_message="Every recipient has already been delivered to.",
                )
            if not CampaignRepository(session).begin_send(campaign_id):
                raise ValidationError(
                    f"campaign {campaign_id} is not in a retryable state",
                    user_message="This campaign is currently sending. Wait for it to finish.",
                )

        log.info("send.retrying_failed", campaign_id=campaign_id, remaining=len(targets))
        rendered = self.render(content, template_id)
        results = self._dispatch(campaign_id, targets, rendered, on_progress)

        return self._finalise(campaign_id, results, time.monotonic() - started)

    # ── internals ────────────────────────────────────────────────────────────
    def _dispatch(
        self,
        campaign_id: int,
        targets: list[tuple[int, Recipient]],
        rendered: RenderedEmail,
        on_progress: ProgressCallback | None,
    ) -> list[tuple[int, SendResult]]:
        """Send, persisting every outcome even if the batch aborts."""
        provider = create_email_provider()
        sender = self._sender or BatchSender(provider)
        messages = [
            self._build_message(rendered, recipient, campaign_id=campaign_id)
            for _, recipient in targets
        ]

        try:
            results = sender.send_many(messages, on_progress=on_progress)
        except (EmailAuthError, EmailQuotaExceeded) as exc:
            # The batch stopped, but what already went out must still be recorded
            # or the retry path would re-send it.
            sent_before = int(exc.context.get("sent_before_failure", 0))
            partial = [
                (targets[i][0], SendResult(email=targets[i][1].email, status=SendStatus.SENT))
                for i in range(min(sent_before, len(targets)))
            ]
            self._persist(campaign_id, partial)
            self._settle(campaign_id, sent=len(partial), failed=0)
            raise
        finally:
            provider.close()

        paired = list(zip([rid for rid, _ in targets], results, strict=False))
        self._persist(campaign_id, paired)
        return paired

    def _build_message(
        self, rendered: RenderedEmail, recipient: Recipient, campaign_id: int | None = None
    ) -> EmailMessage:
        brand = resolve_brand()
        settings = get_settings().email

        # The template is rendered once for the whole campaign and personalised
        # per recipient here. Re-rendering per recipient would cost a full
        # template pass and a CSS inline for every address — minutes, on a list
        # of any size, for a substitution that is a regex.
        # Per-recipient links: each carries a signed identity, so the Like and
        # Unsubscribe buttons know who clicked without a database row per link.
        links = self._recipient_links(recipient, campaign_id, brand)

        html = apply_merge_tokens(rendered.html, recipient, links)
        text = apply_merge_tokens(rendered.text, recipient, links)
        self._assert_no_unresolved_links(html, text, recipient.email)

        return EmailMessage(
            to_email=recipient.email,
            to_name=recipient.name,
            subject=apply_merge_tokens(rendered.subject, recipient),
            html=html,
            text=text,
            # One-click unsubscribe in the mail client's own UI. Points at the
            # tracked link when there is one, so a client-level unsubscribe is
            # recorded exactly like a click in the body.
            headers=unsubscribe_headers(
                links.get("unsubscribe_url") or brand.unsubscribe_url, settings.sender_address
            ),
            tags=[f"campaign-{campaign_id}"] if campaign_id else [],
            inline_images=_logo_images(brand),
        )

    @staticmethod
    def _recipient_links(
        recipient: Recipient, campaign_id: int | None, brand: ResolvedBrand
    ) -> dict[str, str]:
        """Signed Like and Unsubscribe URLs for one recipient.

        **Always returns both keys.** The send-time guard refuses any message
        with an unresolved token, so returning nothing here would block the
        send rather than degrade it.

        A test send has no campaign, and a click on it could not be attributed
        to one, so it falls back to the plain unsubscribe URL: still a valid,
        compliant email, just not a tracked one. The same fallback covers a
        failure to mint — a newsletter that goes out untracked is a lost
        statistic, while one that does not go out is a lost campaign.
        """
        untracked = {
            "like_url": brand.website or brand.unsubscribe_url,
            "unsubscribe_url": brand.unsubscribe_url,
        }
        if campaign_id is None:
            return untracked

        from core.enums import EmailAction
        from services.engagement_service import EngagementService

        service = EngagementService()
        try:
            return {
                "like_url": service.link(recipient.email, campaign_id, EmailAction.LIKED),
                "unsubscribe_url": service.link(
                    recipient.email, campaign_id, EmailAction.UNSUBSCRIBED
                ),
            }
        except Exception:  # noqa: BLE001 - degrade rather than block the send
            log.warning("engagement.link_failed", campaign_id=campaign_id, exc_info=True)
            return untracked

    @staticmethod
    def _assert_no_unresolved_links(html: str, text: str, email: str) -> None:
        """Refuse to send an email still containing a link placeholder.

        The compliance check at render time accepts the unsubscribe *token* in
        place of a URL, which is only safe because of this. Here is the last
        point before the provider, and a literal "{{unsubscribe_url}}" in a
        customer's inbox is both embarrassing and a compliance failure — so it
        fails loudly instead.
        """
        stranded = [
            token
            for token in LINK_TOKENS
            if f"{{{{{token}}}}}" in html or f"{{{{{token}}}}}" in text
        ]
        if stranded:
            raise EmailError(
                f"unresolved link tokens in the message: {', '.join(stranded)}",
                user_message=(
                    "This email could not be personalised correctly and was not sent. "
                    "Check the template's footer links."
                ),
                context={"tokens": stranded, "recipient": mask_email(email)},
            )

    @staticmethod
    def _persist(campaign_id: int, paired: list[tuple[int, SendResult]]) -> None:
        if not paired:
            return
        with unit_of_work() as session:
            repo = SendRepository(session)
            for recipient_id, result in paired:
                repo.record(campaign_id, recipient_id, result)

    @staticmethod
    def _settle(campaign_id: int, *, sent: int, failed: int) -> None:
        """Move the campaign to its final state and store the rollup."""
        if failed == 0 and sent > 0:
            final = CampaignStatus.SENT
        elif sent == 0:
            final = CampaignStatus.FAILED
        else:
            final = CampaignStatus.PARTIAL_FAILURE

        with unit_of_work() as session:
            repo = CampaignRepository(session)
            repo.update_delivery_counts(campaign_id, sent=sent, failed=failed)
            repo.transition_status(campaign_id, final)

    def _finalise(
        self, campaign_id: int, paired: list[tuple[int, SendResult]], elapsed: float
    ) -> CampaignSendReport:
        results = [result for _, result in paired]
        sent = sum(1 for r in results if r.ok)
        failed = len(results) - sent
        self._settle(campaign_id, sent=sent, failed=failed)

        log.info(
            "campaign.sent",
            campaign_id=campaign_id,
            attempted=len(results),
            sent=sent,
            failed=failed,
            duration_s=round(elapsed, 1),
        )
        return CampaignSendReport(
            campaign_id=campaign_id,
            attempted=len(results),
            sent=sent,
            failed=failed,
            duration_s=round(elapsed, 2),
            failures=[r for r in results if not r.ok],
        )

    # ── suppression list ─────────────────────────────────────────────────────
    @staticmethod
    def suppress(email: str, reason: SuppressionReason = SuppressionReason.UNSUBSCRIBED) -> None:
        """Add an address to the global do-not-send list."""
        with unit_of_work() as session:
            SuppressionRepository(session).add(email, reason)
        log.info("recipient.suppressed", reason=reason.value)


#: Cached logo bytes, keyed by path and modification time. A 600-address campaign
#: would otherwise read the same small file 600 times; keying on mtime means
#: replacing the file still takes effect without a restart.
_LOGO_CACHE: dict[tuple[str, float], bytes] = {}


def _logo_images(brand: ResolvedBrand) -> list[InlineImage]:
    """Load the logo for embedding, if the brand uses an embedded one.

    Returns an empty list for a hosted (``http``) logo or no logo at all — and
    also when the file has gone missing, because a broken logo must never be the
    reason a campaign fails to send.
    """
    if brand.logo_url != f"cid:{LOGO_CID}":
        return []

    path = logo_file(get_settings().brand.logo_path)
    if path is None:
        return []

    try:
        key = (str(path), path.stat().st_mtime)
        data = _LOGO_CACHE.get(key)
        if data is None:
            data = path.read_bytes()
            _LOGO_CACHE.clear()  # only ever one logo; do not grow unbounded
            _LOGO_CACHE[key] = data
    except OSError:
        log.warning("email.logo_unreadable", path=str(path))
        return []

    subtype = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif"}.get(
        path.suffix.lower(), "png"
    )
    return [
        InlineImage(
            content_id=LOGO_CID, data=data, subtype=subtype, filename=f"logo{path.suffix.lower()}"
        )
    ]
