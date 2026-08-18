"""Finding the recipient list without anyone uploading it.

The manual workflow uploads a CSV through the browser. An unattended agent has
nobody to do that, so it reads the newest ``.csv`` from a configured folder
instead.

**The parsing is not reimplemented.** The bytes go straight into
``DeliveryService.validate_recipients()`` — the same function the upload button
calls, so the same column detection, the same email validation, the same
suppression check and the same encoding tolerance. Two code paths for "read a
recipient list" would eventually disagree, and the one that disagreed would be
the automated one nobody watches.

Newest file wins, by modification time. Marketing exports a fresh list and drops
it in the folder; requiring them to delete the old one first is a step they will
forget, and the failure would be silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import get_logger, get_settings
from config.constants import PROJECT_ROOT
from core.exceptions import InvalidCSVError
from core.models import RecipientValidation

log = get_logger(__name__)


@dataclass(frozen=True)
class RecipientList:
    """A validated recipient list, and where it came from."""

    validation: RecipientValidation
    path: Path

    @property
    def sendable_count(self) -> int:
        return self.validation.sendable_count


def recipients_dir() -> Path:
    """The configured folder, resolved from the project root.

    Relative paths resolve against the project rather than the working
    directory, so the agent behaves the same whether it was started by
    ``run_agent.bat``, from a terminal, or by Task Scheduler — all of which set a
    different cwd on Windows.
    """
    configured = Path(get_settings().agent.recipients_dir.strip() or "data/recipients")
    return configured if configured.is_absolute() else PROJECT_ROOT / configured


def latest_csv() -> Path | None:
    """The most recently modified ``.csv`` in the folder, or ``None``."""
    folder = recipients_dir()
    if not folder.is_dir():
        return None

    candidates = [p for p in folder.glob("*.csv") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


class RecipientSource:
    """Loads the standing recipient list for an unattended run.

    The **master list in the database is the source of truth** — managed on the
    Recipients page, persistent, and cumulative. The CSV folder is a bootstrap
    convenience only: if the list is empty and a file is sitting there, it is
    imported once and then the list takes over. That way "drop a CSV in and it
    just works" stays true without leaving two places that disagree about who
    receives newsletters.
    """

    def load(self) -> RecipientList:
        """Return the current list, importing a bootstrap CSV if needed.

        Raises:
            InvalidCSVError: The list is empty and no file was available to seed
                it from. The message names both routes, because the person
                reading it is deciding what to do next, not debugging.
        """
        from services.subscriber_service import SubscriberService

        service = SubscriberService()
        recipients = service.active()
        source = "master list"
        bootstrap_problem: str | None = None

        if not recipients:
            imported, bootstrap_problem = self._bootstrap(service)
            if imported is not None:
                recipients = service.active()
                source = f"imported from {imported.name}"

        if not recipients:
            # A file that was present but unusable is a different problem from no
            # file at all, and saying which saves someone staring at a folder
            # that looks correctly populated.
            detail = (
                f" A file was found but could not be used: {bootstrap_problem}"
                if bootstrap_problem
                else ""
            )
            raise InvalidCSVError(
                "the recipient list is empty and no usable bootstrap CSV was found",
                user_message=(
                    "There are no recipients yet. Add them on the Recipients page — "
                    "import a CSV with an 'email' column, or type addresses in. The "
                    "list is saved, so you only do this once."
                    f"{detail}"
                ),
                context={"folder": str(recipients_dir())},
            )

        # Suppression is applied by the same bulk check the manual send path
        # uses, so an unsubscribed address cannot slip through by being on the
        # master list.
        from services.delivery_service import DeliveryService

        validation = DeliveryService().validate_recipients(_to_csv_bytes(recipients))

        if not validation.valid:
            raise InvalidCSVError(
                "every recipient on the list is unsubscribed or invalid",
                user_message=(
                    "Nobody on the recipient list can be sent to — they have all "
                    "unsubscribed. Add new recipients on the Recipients page."
                ),
                context={"suppressed": len(validation.suppressed)},
            )

        log.info(
            "agent.recipients_loaded",
            source=source,
            valid=len(validation.valid),
            suppressed=len(validation.suppressed),
        )
        return RecipientList(validation=validation, path=latest_csv() or recipients_dir())

    @staticmethod
    def _bootstrap(service: object) -> tuple[Path | None, str | None]:
        """Seed an empty list from a CSV in the configured folder, once.

        Returns the file used and, if it could not be used, why. Failure is not
        fatal — an unreadable file should leave the caller reporting "no
        recipients yet" *and* the reason, rather than raising a parse error from
        a file the user may not know exists.
        """
        path = latest_csv()
        if path is None:
            return None, None

        try:
            service.import_csv(  # type: ignore[attr-defined]
                path.read_bytes(), source=f"bootstrap:{path.name}"[:64]
            )
        except (OSError, InvalidCSVError) as exc:
            reason = getattr(exc, "user_message", str(exc))
            log.warning("agent.bootstrap_csv_unusable", file=path.name, reason=reason[:200])
            return None, f"{path.name} — {reason}"

        log.info("agent.recipients_bootstrapped", file=path.name)
        return path, None


def _to_csv_bytes(recipients: list) -> bytes:
    """Render the list as CSV so it can go through the existing validator.

    Round-tripping through the shared parser rather than calling the suppression
    repository directly keeps one definition of "is this address sendable" —
    two would eventually disagree, and the automated path is the one nobody
    would be watching when they did.
    """
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["email", "name", "company"])
    for recipient in recipients:
        writer.writerow([recipient.email, recipient.name or "", recipient.company or ""])
    return buffer.getvalue().encode("utf-8")
