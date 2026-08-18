"""What the agent sends to, with nobody there to upload anything.

The contract changed when the master list arrived: the **stored list is the
source of truth**, and the CSV folder became a one-time bootstrap. These tests
were rewritten to that contract rather than relaxed — every property the
folder-scanning version asserted still has a test here, because each one still
matters:

* newest file wins, non-CSVs ignored, relative paths resolve from the project
  root — all still true of the bootstrap;
* the upload validator is still what parses the file;
* the suppression list still outranks whatever the list says;
* every failure still names the folder or the page that fixes it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from core.enums import SuppressionReason
from core.exceptions import InvalidCSVError
from modules.repository.database import unit_of_work
from modules.repository.recipient_repo import SuppressionRepository
from services.recipient_source import RecipientSource, latest_csv, recipients_dir
from services.subscriber_service import SubscriberService

pytestmark = pytest.mark.integration

CSV = b"email,name,company\npriya@example.com,Priya,Acme\nrahul@example.com,Rahul,Beta\n"


@pytest.fixture
def folder(tmp_path: Path, set_env, db_session) -> Path:  # noqa: ANN001, ARG001
    """An empty, isolated recipient folder.

    Isolation matters: without it these read the developer's real
    ``data/recipients/`` and pass or fail depending on that machine.
    """
    from config.settings import reset_settings_cache
    from tests.conftest import MINIMAL_ENV

    target = tmp_path / "recipients"
    target.mkdir()
    set_env(**MINIMAL_ENV, AGENT_RECIPIENTS_DIR=str(target))
    reset_settings_cache()
    return target


def touch_newer(path: Path) -> None:
    """Make a file unambiguously the newest, without sleeping."""
    future = time.time() + 60
    os.utime(path, (future, future))


# ─────────────────────────────────────────────────────────────────────────────
#  Finding the bootstrap file
# ─────────────────────────────────────────────────────────────────────────────
class TestFindingTheFile:
    def test_the_configured_folder_is_used(self, folder: Path) -> None:
        assert recipients_dir() == folder

    def test_a_relative_path_resolves_from_the_project_root(self, set_env, db_session) -> None:  # noqa: ANN001, ARG002
        """Not the working directory. ``run_agent.bat``, a terminal and Task
        Scheduler each set a different cwd on Windows."""
        from config.constants import PROJECT_ROOT
        from config.settings import reset_settings_cache
        from tests.conftest import MINIMAL_ENV

        set_env(**MINIMAL_ENV, AGENT_RECIPIENTS_DIR="data/recipients")
        reset_settings_cache()

        assert recipients_dir() == PROJECT_ROOT / "data" / "recipients"

    def test_the_newest_csv_wins(self, folder: Path) -> None:
        (folder / "old.csv").write_bytes(CSV)
        newer = folder / "new.csv"
        newer.write_bytes(CSV)
        touch_newer(newer)

        assert latest_csv() == newer

    def test_non_csv_files_are_ignored(self, folder: Path) -> None:
        (folder / "notes.txt").write_bytes(b"not a list")
        (folder / "list.csv").write_bytes(CSV)

        assert latest_csv().name == "list.csv"

    def test_an_empty_folder_yields_nothing(self, folder: Path) -> None:
        assert latest_csv() is None


# ─────────────────────────────────────────────────────────────────────────────
#  Loading
# ─────────────────────────────────────────────────────────────────────────────
class TestLoading:
    def test_the_stored_list_is_used(self, folder: Path) -> None:  # noqa: ARG002
        """The source of truth, with no file involved at all."""
        SubscriberService().import_csv(CSV)

        assert RecipientSource().load().sendable_count == 2

    def test_a_csv_in_the_folder_bootstraps_an_empty_list(self, folder: Path) -> None:
        """ "Drop a file in and it works" still holds — it just happens once."""
        (folder / "list.csv").write_bytes(CSV)

        assert RecipientSource().load().sendable_count == 2
        assert len(SubscriberService().active()) == 2

    def test_the_stored_list_wins_over_the_folder(self, folder: Path) -> None:
        """Once the list exists it is authoritative, so a stale file left on disk
        cannot quietly re-add people or override an edit."""
        (folder / "list.csv").write_bytes(CSV)
        SubscriberService().add("only@example.com")

        result = RecipientSource().load()

        assert result.sendable_count == 1
        assert {r.email for r in result.validation.valid} == {"only@example.com"}

    def test_it_reuses_the_upload_validator(self, folder: Path) -> None:
        """Column aliases, duplicates and bad rows behave exactly as on the
        manual path, because it is the same parser."""
        (folder / "list.csv").write_bytes(
            b"Email Address,name\n"
            b"priya@example.com,Priya\n"
            b"PRIYA@example.com,Dupe\n"
            b"not-an-email,Broken\n"
            b"rahul@example.com,Rahul\n"
        )

        assert RecipientSource().load().sendable_count == 2

    def test_the_suppression_list_is_respected(self, folder: Path) -> None:
        """Someone who unsubscribed stays unsubscribed, whatever the master list
        says. A legal requirement, not a preference."""
        SubscriberService().import_csv(CSV)
        with unit_of_work() as session:
            SuppressionRepository(session).add("priya@example.com", SuppressionReason.UNSUBSCRIBED)

        result = RecipientSource().load()

        assert result.sendable_count == 1
        assert "priya@example.com" in result.validation.suppressed

    def test_a_windows_excel_export_is_read(self, folder: Path) -> None:
        """Excel writes UTF-8 with a BOM. Failing on that would be correct and
        useless — the decoding cascade already handles it."""
        (folder / "list.csv").write_bytes(b"\xef\xbb\xbfemail\npriya@example.com\n")

        assert RecipientSource().load().sendable_count == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Failing usefully
# ─────────────────────────────────────────────────────────────────────────────
class TestFailingUsefully:
    def test_an_empty_list_and_empty_folder_names_the_fix(self, folder: Path) -> None:  # noqa: ARG002
        with pytest.raises(InvalidCSVError) as exc:
            RecipientSource().load()

        assert "Recipients page" in exc.value.user_message
        assert "only do this once" in exc.value.user_message

    def test_a_missing_folder_is_not_fatal(self, tmp_path: Path, set_env, db_session) -> None:  # noqa: ANN001, ARG002
        """A folder that was never created is the normal state now that the
        dashboard is the primary route — it must read as "add recipients", not
        as a broken installation."""
        from config.settings import reset_settings_cache
        from tests.conftest import MINIMAL_ENV

        set_env(**MINIMAL_ENV, AGENT_RECIPIENTS_DIR=str(tmp_path / "never-created"))
        reset_settings_cache()

        with pytest.raises(InvalidCSVError) as exc:
            RecipientSource().load()

        assert "Recipients page" in exc.value.user_message

    def test_an_unusable_bootstrap_file_says_why(self, folder: Path) -> None:
        """A file that is present but wrong is a different problem from no file,
        and saying which saves someone staring at a folder that looks right."""
        (folder / "list.csv").write_bytes(b"name,company\nPriya,Acme\n")

        with pytest.raises(InvalidCSVError) as exc:
            RecipientSource().load()

        assert "list.csv" in exc.value.user_message
        assert "column named 'email'" in exc.value.user_message

    def test_a_list_of_only_unsubscribed_people_is_refused(self, folder: Path) -> None:  # noqa: ARG002
        """Better to refuse than hand a campaign zero recipients and let it look
        like a successful send to nobody."""
        SubscriberService().import_csv(b"email\npriya@example.com\n")
        with unit_of_work() as session:
            SuppressionRepository(session).add("priya@example.com", SuppressionReason.UNSUBSCRIBED)

        with pytest.raises(InvalidCSVError) as exc:
            RecipientSource().load()

        assert "unsubscribed" in exc.value.user_message
