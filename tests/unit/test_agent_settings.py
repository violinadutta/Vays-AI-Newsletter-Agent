"""Agent configuration, and the send-time rule that governs every campaign.

``next_send_after`` is the most consequential function in the automation: get it
wrong and mail goes out at the wrong hour, or a day late, to real customers. It
is pure and therefore worth testing exhaustively here rather than discovering
its behaviour at 09:00 one morning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from config.settings import AgentSettings

IST = ZoneInfo("Asia/Kolkata")


def agent(**overrides: object) -> AgentSettings:
    """Settings built from defaults only — never from the developer's .env."""
    return AgentSettings(_env_file=None, **overrides)  # type: ignore[arg-type]


def daily(**overrides: object) -> AgentSettings:
    """The daily schedule, which is now opt-in.

    These cases still describe real behaviour — ``send_schedule='daily'`` is a
    supported mode — so the tests were pinned to it rather than deleted when
    monthly became the default.
    """
    return agent(send_schedule="daily", **{"send_time": "09:00", **overrides})


# ─────────────────────────────────────────────────────────────────────────────
#  Defaults
# ─────────────────────────────────────────────────────────────────────────────
class TestDefaults:
    def test_the_agent_ships_disabled(self) -> None:
        """A fresh clone must behave exactly as it did before automation existed.
        Nothing autonomous starts until someone deliberately turns it on."""
        assert agent().enabled is False

    def test_no_approval_address_is_configured_by_default(self) -> None:
        """Shipping one would send a stranger's inbox a review request on first
        run. Absent, and the agent refuses to run."""
        assert agent().approval_email == ""

    def test_the_default_schedule_is_monthly(self) -> None:
        """What Vays actually runs: the 3rd Wednesday at 11:00."""
        settings = agent()

        assert settings.send_schedule == "monthly"
        assert settings.send_weekday == "wednesday"
        assert settings.send_week_of_month == 3
        assert settings.send_time == "11:00"

    def test_posts_per_run_is_conservative(self) -> None:
        """Groq's free tier is the binding constraint: ~8-12k tokens/minute
        against ~3,450 per article."""
        assert agent().max_posts_per_run <= 3


# ─────────────────────────────────────────────────────────────────────────────
#  Send time
# ─────────────────────────────────────────────────────────────────────────────
class TestSendTimeValidation:
    @pytest.mark.parametrize(
        ("given", "stored"),
        [("09:00", "09:00"), ("9:00", "09:00"), ("21:30", "21:30"), ("00:00", "00:00")],
    )
    def test_accepted_forms_are_normalised(self, given: str, stored: str) -> None:
        """`9:00` is a reasonable thing to type into a settings box."""
        assert agent(send_time=given).send_time == stored

    @pytest.mark.parametrize("given", ["24:00", "9:5", "9", "noon", "09:00:00", "-1:00", ""])
    def test_malformed_times_are_refused_at_configuration_time(self, given: str) -> None:
        """Not at 09:00 on the morning a campaign was due — a scheduler failure
        is discovered by nobody, because nobody is watching when it fires.

        `24:00` matters especially: ``time.fromisoformat`` accepts it and silently
        means midnight, turning "end of day" into "start of day"."""
        with pytest.raises(ValidationError):
            agent(send_time=given)

    def test_an_unknown_timezone_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            agent(timezone="Mars/Olympus")

    def test_a_blog_url_without_a_scheme_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            agent(blog_url="vaysinfotech.com")

    def test_a_trailing_slash_is_stripped(self) -> None:
        """The discovery source appends its own path; a doubled slash produces a
        404 that reads like the site is down."""
        assert agent(blog_url="https://vaysinfotech.com/").blog_url == "https://vaysinfotech.com"


class TestNextSendAfter:
    def test_before_the_window_it_sends_today(self) -> None:
        approved = datetime(2026, 8, 13, 8, 0, tzinfo=IST)

        assert daily().next_send_after(approved) == datetime(2026, 8, 13, 9, 0, tzinfo=IST)

    def test_after_the_window_it_waits_for_tomorrow(self) -> None:
        """The conservative reading, chosen deliberately: a campaign arriving the
        instant it is approved is the behaviour that surprises people."""
        approved = datetime(2026, 8, 13, 9, 5, tzinfo=IST)

        assert daily().next_send_after(approved) == datetime(2026, 8, 14, 9, 0, tzinfo=IST)

    def test_exactly_on_the_window_waits_for_tomorrow(self) -> None:
        """The boundary. Approving at exactly 09:00:00 must not race the sender
        into an immediate dispatch."""
        approved = datetime(2026, 8, 13, 9, 0, tzinfo=IST)

        assert daily().next_send_after(approved).day == 14

    def test_an_approval_from_another_timezone_is_converted(self) -> None:
        """The worker may run in UTC while the send time is expressed in IST.
        Comparing the two without converting sends mail 5.5 hours out."""
        approved_utc = datetime(2026, 8, 13, 2, 0, tzinfo=UTC)  # 07:30 IST

        assert daily().next_send_after(approved_utc) == datetime(2026, 8, 13, 9, 0, tzinfo=IST)

    def test_a_naive_datetime_is_refused(self) -> None:
        """Assuming a naive value is local produces mail sent at the wrong hour —
        a failure that surfaces as a complaint, not an exception."""
        with pytest.raises(ValueError, match="timezone-aware"):
            daily().next_send_after(datetime(2026, 8, 13, 8, 0))  # noqa: DTZ001

    def test_it_respects_a_changed_send_time(self) -> None:
        approved = datetime(2026, 8, 13, 8, 0, tzinfo=IST)

        assert daily(send_time="17:30").next_send_after(approved).hour == 17

    def test_it_respects_a_changed_timezone(self) -> None:
        settings = daily(timezone="UTC")
        approved = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

        assert settings.next_send_after(approved) == datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


class TestSendWindow:
    def test_the_window_is_shut_before_the_send_time(self) -> None:
        approved = datetime(2026, 8, 13, 8, 0, tzinfo=IST)
        now = datetime(2026, 8, 13, 8, 59, tzinfo=IST)

        assert not daily().is_send_window_open(now, approved)

    def test_the_window_opens_at_the_send_time(self) -> None:
        approved = datetime(2026, 8, 13, 8, 0, tzinfo=IST)
        now = datetime(2026, 8, 13, 9, 0, tzinfo=IST)

        assert daily().is_send_window_open(now, approved)

    def test_a_late_approval_is_not_sent_the_same_day(self) -> None:
        """Approved at 09:05, checked at 23:00 — still today, so still waiting."""
        approved = datetime(2026, 8, 13, 9, 5, tzinfo=IST)
        now = datetime(2026, 8, 13, 23, 0, tzinfo=IST)

        assert not daily().is_send_window_open(now, approved)

    def test_a_late_approval_goes_out_the_next_day(self) -> None:
        approved = datetime(2026, 8, 13, 9, 5, tzinfo=IST)
        now = datetime(2026, 8, 14, 9, 0, tzinfo=IST)

        assert daily().is_send_window_open(now, approved)


# ─────────────────────────────────────────────────────────────────────────────
#  Monthly schedule — the 3rd Wednesday at 11:00
# ─────────────────────────────────────────────────────────────────────────────
class TestMonthlySchedule:
    """The cadence Vays actually runs.

    A monthly schedule is unforgiving: a mistake here does not send a newsletter
    an hour late, it sends it a month late or not at all. So the arithmetic is
    checked against real calendar dates rather than trusted.
    """

    @pytest.mark.parametrize(
        ("month", "day"),
        [
            (1, 21),  # Jan 2026
            (2, 18),
            (3, 18),
            (4, 15),
            (5, 20),
            (6, 17),
            (7, 15),
            (8, 19),
            (9, 16),
            (10, 21),
            (11, 18),
            (12, 16),
        ],
    )
    def test_the_third_wednesday_of_every_month_in_2026(self, month: int, day: int) -> None:
        """Verified against a calendar, month by month, for a full year."""
        approved = datetime(2026, month, 1, 0, 1, tzinfo=IST)

        due = agent().next_send_after(approved)

        assert (due.month, due.day) == (month, day)
        assert due.strftime("%A") == "Wednesday"
        assert (due.hour, due.minute) == (11, 0)

    def test_before_the_send_day_it_goes_this_month(self) -> None:
        approved = datetime(2026, 8, 5, 10, 0, tzinfo=IST)

        assert agent().next_send_after(approved) == datetime(2026, 8, 19, 11, 0, tzinfo=IST)

    def test_on_the_send_day_but_earlier_it_still_goes_today(self) -> None:
        approved = datetime(2026, 8, 19, 9, 0, tzinfo=IST)

        assert agent().next_send_after(approved) == datetime(2026, 8, 19, 11, 0, tzinfo=IST)

    def test_after_the_slot_it_waits_a_whole_month(self) -> None:
        """The consequential case: approving at 11:05 on the send day means the
        next window is a month away, not five minutes."""
        approved = datetime(2026, 8, 19, 11, 5, tzinfo=IST)

        assert agent().next_send_after(approved) == datetime(2026, 9, 16, 11, 0, tzinfo=IST)

    def test_exactly_on_the_slot_waits_for_next_month(self) -> None:
        approved = datetime(2026, 8, 19, 11, 0, tzinfo=IST)

        assert agent().next_send_after(approved).month == 9

    def test_late_in_the_month_it_rolls_to_the_next(self) -> None:
        approved = datetime(2026, 8, 31, 23, 0, tzinfo=IST)

        assert agent().next_send_after(approved) == datetime(2026, 9, 16, 11, 0, tzinfo=IST)

    def test_december_rolls_into_january(self) -> None:
        """Year rollover — the arithmetic that breaks if months are added naively."""
        approved = datetime(2026, 12, 20, 12, 0, tzinfo=IST)

        due = agent().next_send_after(approved)

        assert (due.year, due.month, due.day) == (2027, 1, 20)
        assert due.strftime("%A") == "Wednesday"

    def test_an_approval_from_utc_is_converted(self) -> None:
        """The worker may run in UTC while the schedule is expressed in IST."""
        approved = datetime(2026, 8, 19, 5, 0, tzinfo=UTC)  # 10:30 IST, before the slot

        assert agent().next_send_after(approved) == datetime(2026, 8, 19, 11, 0, tzinfo=IST)

    def test_the_fifth_occurrence_means_the_last(self) -> None:
        """Most months have no 5th Wednesday. Returning nothing would silently
        skip those months — a newsletter that never arrives, with no error."""
        settings = agent(send_week_of_month=5)
        # September 2026 has Wednesdays on 2, 9, 16, 23, 30 — five of them.
        assert settings.next_send_after(datetime(2026, 9, 1, 0, 1, tzinfo=IST)).day == 30
        # August 2026 has only four: 5, 12, 19, 26.
        assert settings.next_send_after(datetime(2026, 8, 1, 0, 1, tzinfo=IST)).day == 26

    def test_the_first_occurrence_can_be_the_first_of_the_month(self) -> None:
        """1 July 2026 is a Wednesday — the boundary the offset arithmetic gets
        wrong if it assumes the first occurrence is never day 1."""
        due = agent(send_week_of_month=1).next_send_after(datetime(2026, 6, 30, 12, 0, tzinfo=IST))

        assert (due.month, due.day) == (7, 1)

    @pytest.mark.parametrize(
        "weekday", ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    )
    def test_every_weekday_is_supported(self, weekday: str) -> None:
        due = agent(send_weekday=weekday).next_send_after(datetime(2026, 8, 1, 0, 1, tzinfo=IST))

        assert due.strftime("%A").lower() == weekday

    def test_the_window_opens_on_the_send_day(self) -> None:
        approved = datetime(2026, 8, 5, 10, 0, tzinfo=IST)

        assert not agent().is_send_window_open(datetime(2026, 8, 19, 10, 59, tzinfo=IST), approved)
        assert agent().is_send_window_open(datetime(2026, 8, 19, 11, 0, tzinfo=IST), approved)

    def test_the_schedule_reads_as_a_sentence(self) -> None:
        """It goes into the approval email and the dashboard, where "11:00" alone
        would not tell a reviewer which day."""
        assert agent().describe_schedule() == (
            "the 3rd Wednesday of each month at 11:00 (Asia/Kolkata)"
        )

    def test_the_last_occurrence_is_described_as_last(self) -> None:
        assert "last Wednesday" in agent(send_week_of_month=5).describe_schedule()

    def test_the_daily_schedule_describes_itself(self) -> None:
        assert daily().describe_schedule() == "every day at 09:00 (Asia/Kolkata)"
