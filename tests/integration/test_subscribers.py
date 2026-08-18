"""The master mailing list: persistent, cumulative, and safe to re-import.

The two failures these guard against are both about losing work:

* an upload **replacing** the list instead of adding to it, when nobody has
  another copy of what was there;
* a re-import silently **resurrecting** someone who was deliberately removed.
"""

from __future__ import annotations

import pytest

from core.enums import SuppressionReason
from core.exceptions import InvalidCSVError, InvalidEmailError, ValidationError
from modules.repository.database import unit_of_work
from modules.repository.recipient_repo import SuppressionRepository
from services.subscriber_service import SubscriberService, parse_pasted_addresses

pytestmark = pytest.mark.integration

FIRST = b"email,name,company\npriya@example.com,Priya,Acme\nrahul@example.com,Rahul,Beta\n"
SECOND = b"email,name\nsam@example.com,Sam\npriya@example.com,Priya\n"


@pytest.fixture(autouse=True)
def _env(db_session, set_env) -> None:  # noqa: ANN001, ARG001
    from tests.conftest import MINIMAL_ENV

    set_env(**MINIMAL_ENV)


@pytest.fixture
def service() -> SubscriberService:
    return SubscriberService()


def emails(service: SubscriberService) -> set[str]:
    return {r.email for r in service.active()}


# ─────────────────────────────────────────────────────────────────────────────
#  The list persists and grows
# ─────────────────────────────────────────────────────────────────────────────
class TestImportAppends:
    def test_a_first_import_populates_the_list(self, service: SubscriberService) -> None:
        report = service.import_csv(FIRST)

        assert report.outcome.added == 2
        assert emails(service) == {"priya@example.com", "rahul@example.com"}

    def test_a_second_import_adds_without_replacing(self, service: SubscriberService) -> None:
        """The failure this prevents: an upload wiping a list nobody has another
        copy of."""
        service.import_csv(FIRST)

        report = service.import_csv(SECOND)

        assert report.outcome.added == 1
        assert emails(service) == {
            "priya@example.com",
            "rahul@example.com",
            "sam@example.com",
        }

    def test_reimporting_the_same_file_changes_nothing(self, service: SubscriberService) -> None:
        service.import_csv(FIRST)

        report = service.import_csv(FIRST)

        assert report.outcome.added == 0
        assert report.outcome.already_present == 2
        assert len(service.active()) == 2

    def test_the_list_survives_a_new_service_instance(self, service: SubscriberService) -> None:
        """It is stored, not held in memory — "upload once and it stays"."""
        service.import_csv(FIRST)

        assert len(SubscriberService().active()) == 2

    def test_a_later_import_fills_in_missing_details(self, service: SubscriberService) -> None:
        service.import_csv(b"email\npriya@example.com\n")

        service.import_csv(b"email,name,company\npriya@example.com,Priya,Acme\n")

        [person] = [r for r in service.active() if r.email == "priya@example.com"]
        assert person.name == "Priya"
        assert person.company == "Acme"

    def test_it_reuses_the_existing_csv_validator(self, service: SubscriberService) -> None:
        """Column aliases, bad addresses and duplicates behave exactly as they do
        on the manual send path, because it is the same parser."""
        report = service.import_csv(
            b"Email Address,name\n"
            b"priya@example.com,Priya\n"
            b"PRIYA@example.com,Dupe\n"
            b"not-an-email,Broken\n"
        )

        assert report.outcome.added == 1
        assert report.duplicates_in_file == 1
        assert len(report.invalid) == 1

    def test_a_file_without_an_email_column_is_refused(self, service: SubscriberService) -> None:
        with pytest.raises(InvalidCSVError):
            service.import_csv(b"name,company\nPriya,Acme\n")


# ─────────────────────────────────────────────────────────────────────────────
#  Adding by hand
# ─────────────────────────────────────────────────────────────────────────────
class TestAdd:
    def test_one_address_is_added(self, service: SubscriberService) -> None:
        outcome = service.add("priya@example.com", name="Priya", company="Acme")

        assert outcome.added == 1
        assert emails(service) == {"priya@example.com"}

    def test_the_address_is_normalised(self, service: SubscriberService) -> None:
        service.add("  PRIYA@Example.COM ")

        assert emails(service) == {"priya@example.com"}

    def test_adding_an_existing_address_is_harmless(self, service: SubscriberService) -> None:
        service.add("priya@example.com")

        outcome = service.add("priya@example.com")

        assert outcome.added == 0
        assert outcome.already_present == 1

    def test_an_invalid_address_is_refused(self, service: SubscriberService) -> None:
        with pytest.raises(InvalidEmailError):
            service.add("not-an-email")

    def test_an_unsubscribed_address_cannot_be_added(self, service: SubscriberService) -> None:
        """Refused while the person is looking at the screen, rather than
        silently dropped at send time."""
        with unit_of_work() as session:
            SuppressionRepository(session).add("priya@example.com", SuppressionReason.UNSUBSCRIBED)

        with pytest.raises(ValidationError, match="suppression"):
            service.add("priya@example.com")


class TestPastedAddresses:
    @pytest.mark.parametrize(
        "text",
        [
            "a@x.com, b@x.com",
            "a@x.com b@x.com",
            "a@x.com\nb@x.com",
            "a@x.com;b@x.com",
            "<a@x.com>, <b@x.com>",
        ],
    )
    def test_common_separators_are_accepted(self, text: str) -> None:
        """However they arrived — a mail client's To field, a spreadsheet column,
        a chat message."""
        valid, _ = parse_pasted_addresses(text)

        assert valid == ["a@x.com", "b@x.com"]

    def test_duplicates_are_collapsed(self) -> None:
        valid, _ = parse_pasted_addresses("a@x.com, A@X.com")

        assert valid == ["a@x.com"]

    def test_bad_entries_are_reported_with_a_reason(self) -> None:
        valid, invalid = parse_pasted_addresses("good@x.com, rubbish")

        assert valid == ["good@x.com"]
        assert "rubbish" in invalid


# ─────────────────────────────────────────────────────────────────────────────
#  Removal
# ─────────────────────────────────────────────────────────────────────────────
class TestRemoval:
    def test_removing_takes_someone_off_the_send_list(self, service: SubscriberService) -> None:
        service.import_csv(FIRST)

        assert service.remove("priya@example.com")

        assert emails(service) == {"rahul@example.com"}

    def test_a_removed_person_is_kept_on_record(self, service: SubscriberService) -> None:
        service.import_csv(FIRST)
        service.remove("priya@example.com")

        assert service.counts() == {"active": 1, "inactive": 1, "total": 2}

    def test_reimporting_does_not_resurrect_a_removed_person(
        self, service: SubscriberService
    ) -> None:
        """The mistake this prevents: uploading the original file again quietly
        undoing a deliberate removal — and mailing someone who asked not to be."""
        service.import_csv(FIRST)
        service.remove("priya@example.com")

        service.import_csv(FIRST)

        assert emails(service) == {"rahul@example.com"}

    def test_adding_by_hand_does_restore_them(self, service: SubscriberService) -> None:
        """Typing an address in is an explicit intent, unlike a bulk re-upload."""
        service.import_csv(FIRST)
        service.remove("priya@example.com")

        outcome = service.add("priya@example.com")

        assert outcome.reactivated == 1
        assert "priya@example.com" in emails(service)

    def test_removing_someone_absent_is_harmless(self, service: SubscriberService) -> None:
        assert service.remove("nobody@example.com") is False

    def test_restore_puts_them_back(self, service: SubscriberService) -> None:
        service.import_csv(FIRST)
        service.remove("priya@example.com")

        assert service.restore("priya@example.com")
        assert "priya@example.com" in emails(service)


# ─────────────────────────────────────────────────────────────────────────────
#  Reading
# ─────────────────────────────────────────────────────────────────────────────
class TestReading:
    def test_search_matches_email_name_and_company(self, service: SubscriberService) -> None:
        service.import_csv(FIRST)

        assert len(service.search("acme")) == 1
        assert len(service.search("rahul")) == 1
        assert len(service.search("example.com")) == 2

    def test_removed_people_are_hidden_by_default_in_the_send_list(
        self, service: SubscriberService
    ) -> None:
        service.import_csv(FIRST)
        service.remove("priya@example.com")

        assert len(service.search(include_inactive=False)) == 1
        assert len(service.search(include_inactive=True)) == 2

    def test_export_round_trips(self, service: SubscriberService) -> None:
        """The exported file must be importable — it is the backup."""
        service.import_csv(FIRST)
        exported = service.export_csv()

        service.remove("priya@example.com")
        service.remove("rahul@example.com")
        report = SubscriberService().import_csv(exported.encode())

        assert report.outcome.added == 0  # deliberately does not resurrect
        assert "email" in exported.splitlines()[0]

    def test_search_survives_the_session_closing(self, service: SubscriberService) -> None:
        """Rows are returned detached; touching a bound ORM attribute after the
        session closes raises DetachedInstanceError in the UI."""
        service.import_csv(FIRST)

        rows = service.search()

        assert all(isinstance(r["email"], str) for r in rows)


# ─────────────────────────────────────────────────────────────────────────────
#  What the agent uses
# ─────────────────────────────────────────────────────────────────────────────
class TestAgentUsesTheMasterList:
    def test_the_agent_reads_the_stored_list(self, service: SubscriberService) -> None:
        from services.recipient_source import RecipientSource

        service.import_csv(FIRST)

        assert RecipientSource().load().sendable_count == 2

    def test_suppressed_addresses_are_still_skipped(self, service: SubscriberService) -> None:
        """The master list does not outrank an unsubscribe."""
        from services.recipient_source import RecipientSource

        service.import_csv(FIRST)
        with unit_of_work() as session:
            SuppressionRepository(session).add("priya@example.com", SuppressionReason.UNSUBSCRIBED)

        result = RecipientSource().load()

        assert result.sendable_count == 1
        assert "priya@example.com" in result.validation.suppressed

    def test_an_empty_list_names_both_ways_to_fix_it(self, set_env, tmp_path) -> None:  # noqa: ANN001
        from config.settings import reset_settings_cache
        from services.recipient_source import RecipientSource

        # Point at an empty folder explicitly. Without this the test reads the
        # developer's real data/recipients/, so it would pass or fail depending
        # on what happens to be on that machine.
        set_env(AGENT_RECIPIENTS_DIR=str(tmp_path / "empty"))
        reset_settings_cache()

        with pytest.raises(InvalidCSVError) as exc:
            RecipientSource().load()

        assert "Recipients page" in exc.value.user_message

    def test_a_csv_in_the_folder_bootstraps_an_empty_list(self, set_env, tmp_path) -> None:  # noqa: ANN001
        """ "Drop a file in and it works" stays true, and the import happens once
        — after that the stored list is authoritative."""
        from config.settings import reset_settings_cache
        from services.recipient_source import RecipientSource

        folder = tmp_path / "recipients"
        folder.mkdir()
        (folder / "list.csv").write_bytes(FIRST)
        set_env(AGENT_RECIPIENTS_DIR=str(folder))
        reset_settings_cache()

        assert RecipientSource().load().sendable_count == 2
        assert len(SubscriberService().active()) == 2

    def test_a_bootstrap_removal_is_not_undone_by_the_file_still_being_there(
        self, set_env, tmp_path
    ) -> None:  # noqa: ANN001
        """The file stays on disk after bootstrapping. Removing someone must not
        be undone on the next run just because the CSV still lists them."""
        from config.settings import reset_settings_cache
        from services.recipient_source import RecipientSource

        folder = tmp_path / "recipients"
        folder.mkdir()
        (folder / "list.csv").write_bytes(FIRST)
        set_env(AGENT_RECIPIENTS_DIR=str(folder))
        reset_settings_cache()

        RecipientSource().load()
        SubscriberService().remove("priya@example.com")

        assert RecipientSource().load().sendable_count == 1
