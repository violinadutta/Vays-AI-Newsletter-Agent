"""Managing the master mailing list: import once, then add and remove.

The list is **persistent and cumulative**. A CSV is an import, not a
replacement: uploading a second file adds the new addresses to the existing
list rather than swapping it out. Replacing on upload would be one mis-click
away from losing a list nobody has a copy of.

**Validation is not reimplemented.** Uploaded bytes go through
``DeliveryService.validate_recipients()`` — the same column detection, email
normalisation, duplicate removal and encoding tolerance the manual send path
uses. A second parser would eventually disagree, and the disagreement would show
up as mail sent to an address one of them thought was invalid.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import get_logger
from core.exceptions import InvalidEmailError, ValidationError
from core.models import Recipient
from core.validators import validate_email_address
from modules.repository.database import unit_of_work
from modules.repository.subscriber_repo import ImportOutcome, SubscriberRepository

log = get_logger(__name__)


@dataclass
class ImportReport:
    """The outcome of importing a file, phrased for the person who uploaded it."""

    outcome: ImportOutcome
    invalid: dict[str, str]
    duplicates_in_file: int
    suppressed: list[str]
    source: str

    @property
    def summary(self) -> str:
        parts = [f"{self.outcome.added} added"]
        if self.outcome.reactivated:
            parts.append(f"{self.outcome.reactivated} restored")
        if self.outcome.already_present:
            parts.append(f"{self.outcome.already_present} already on the list")
        if self.duplicates_in_file:
            parts.append(f"{self.duplicates_in_file} repeated in the file")
        if self.invalid:
            parts.append(f"{len(self.invalid)} rejected")
        if self.suppressed:
            parts.append(f"{len(self.suppressed)} unsubscribed")
        return " · ".join(parts)


class SubscriberService:
    """The master mailing list as a use case."""

    # ── importing ────────────────────────────────────────────────────────────
    def import_csv(
        self, csv_bytes: bytes, *, source: str = "csv", added_by: str | None = None
    ) -> ImportReport:
        """Append the addresses in a CSV to the list.

        Existing entries are kept, not overwritten, and someone previously
        removed is **not** brought back — a re-upload of the original file must
        not quietly undo a deliberate removal.

        Raises:
            InvalidCSVError: The file is unreadable or has no email column.
        """
        from services.delivery_service import DeliveryService

        validation = DeliveryService().validate_recipients(csv_bytes)

        with unit_of_work() as session:
            outcome = SubscriberRepository(session).add_many(
                validation.valid, source=source, added_by=added_by, reactivate=False
            )

        report = ImportReport(
            outcome=outcome,
            invalid=validation.invalid,
            duplicates_in_file=len(validation.duplicates),
            suppressed=validation.suppressed,
            source=source,
        )
        log.info(
            "subscribers.imported",
            source=source,
            added=outcome.added,
            already_present=outcome.already_present,
            invalid=len(validation.invalid),
            by=added_by,
        )
        return report

    # ── one at a time ────────────────────────────────────────────────────────
    def add(
        self,
        email: str,
        *,
        name: str | None = None,
        company: str | None = None,
        added_by: str | None = None,
    ) -> ImportOutcome:
        """Add one person by hand.

        Unlike an import, this **does** reactivate someone previously removed:
        typing an address in is an explicit intent to have them on the list.

        Raises:
            InvalidEmailError: The address is not valid.
            ValidationError: The address is on the suppression list.
        """
        address = validate_email_address(email)

        from modules.repository.recipient_repo import SuppressionRepository

        with unit_of_work() as session:
            if SuppressionRepository(session).filter_suppressed([address]):
                # Adding an unsubscribed address would be refused at send time
                # anyway; refusing here says so while the person is looking at
                # the screen, rather than silently dropping them later.
                raise ValidationError(
                    f"{address} is on the suppression list",
                    user_message=(
                        f"{address} has unsubscribed, so they can't be added back. "
                        "They would be skipped at send time in any case."
                    ),
                )

            outcome = SubscriberRepository(session).add_many(
                [Recipient(email=address, name=name or None, company=company or None)],
                source="manual",
                added_by=added_by,
                reactivate=True,
            )

        log.info(
            "subscribers.added", added=outcome.added, restored=outcome.reactivated, by=added_by
        )
        return outcome

    def remove(self, email: str, *, removed_by: str | None = None) -> bool:
        """Take someone off the list, keeping the record.

        Deactivation rather than deletion, so a later CSV import cannot put them
        back without someone deciding to.
        """
        with unit_of_work() as session:
            changed = SubscriberRepository(session).deactivate(email)
        if changed:
            log.info("subscribers.removed", by=removed_by)
        return changed

    def restore(self, email: str, *, restored_by: str | None = None) -> bool:
        with unit_of_work() as session:
            changed = SubscriberRepository(session).reactivate(email)
        if changed:
            log.info("subscribers.restored", by=restored_by)
        return changed

    # ── reading ──────────────────────────────────────────────────────────────
    @staticmethod
    def active() -> list[Recipient]:
        """Everyone the next campaign would go to, before suppression."""
        with unit_of_work() as session:
            return SubscriberRepository(session).active()

    @staticmethod
    def counts() -> dict[str, int]:
        with unit_of_work() as session:
            return SubscriberRepository(session).counts()

    @staticmethod
    def search(term: str = "", *, include_inactive: bool = True, limit: int = 200) -> list[object]:
        with unit_of_work() as session:
            rows = SubscriberRepository(session).search(
                term, include_inactive=include_inactive, limit=limit
            )
            # Detached copies: the session closes on exit, and touching a bound
            # ORM attribute afterwards raises DetachedInstanceError in the UI.
            return [
                {
                    "email": r.email,
                    "name": r.name,
                    "company": r.company,
                    "is_active": r.is_active,
                    "source": r.source,
                    "added_at": r.added_at,
                }
                for r in rows
            ]

    def export_csv(self) -> str:
        """The whole active list as CSV, for a backup or another tool."""
        import csv
        import io

        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["email", "name", "company"])
        for recipient in self.active():
            writer.writerow([recipient.email, recipient.name or "", recipient.company or ""])
        return buffer.getvalue()


def parse_pasted_addresses(text: str) -> tuple[list[str], dict[str, str]]:
    """Split pasted text into valid addresses and reasons for the rest.

    Accepts commas, semicolons, spaces and newlines, because that is what comes
    out of a mail client's "to" field, a spreadsheet column, and a chat message
    respectively.
    """
    import re

    valid: list[str] = []
    invalid: dict[str, str] = {}
    seen: set[str] = set()

    for raw in re.split(r"[,\s;]+", text or ""):
        candidate = raw.strip().strip("<>")
        if not candidate:
            continue
        try:
            address = validate_email_address(candidate)
        except InvalidEmailError as exc:
            invalid[candidate] = exc.user_message
            continue
        if address not in seen:
            seen.add(address)
            valid.append(address)

    return valid, invalid
