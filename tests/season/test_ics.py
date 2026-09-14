"""iCalendar export: RFC 5545 conformance and what the events actually say."""

from __future__ import annotations

import datetime as _dt

import pytest

from flightmanager.season import ics, store
from flightmanager.season.models import Campaign, Window


def _window(**kwargs) -> Window:
    defaults = dict(
        computed_at="2027-05-20T00:00:00+00:00",
        earliest=_dt.date(2027, 6, 6),
        target=_dt.date(2027, 6, 9),
        latest=_dt.date(2027, 6, 16),
        uncertainty_days=7.0,
        confidence="low",
        basis="forecast",
    )
    defaults.update(kwargs)
    return Window(**defaults)


@pytest.fixture
def plan():
    p = store.new_plan("hiilisyke-2027", 2027)
    p.campaigns.append(
        Campaign(
            campaign_id="2027-emergence_count-5241087453",
            type_id="emergence_count",
            season=2027,
            job_paths=["hiilisyke-2027/5241087453"],
            crop_id="spring_barley",
            state="open",
            window=_window(),
        )
    )
    return p


@pytest.fixture
def calendar(plan, tables):
    return ics.build_calendar(plan, tables)


class TestStructure:
    def test_it_is_a_well_formed_vcalendar(self, calendar):
        assert calendar.startswith("BEGIN:VCALENDAR\r\n")
        assert calendar.rstrip().endswith("END:VCALENDAR")
        assert "VERSION:2.0" in calendar
        assert "PRODID:" in calendar

    def test_every_line_ends_with_crlf(self, calendar):
        assert "\r\n" in calendar
        for line in calendar.split("\r\n"):
            assert "\n" not in line

    def test_begin_and_end_are_balanced(self, calendar):
        assert calendar.count("BEGIN:VEVENT") == calendar.count("END:VEVENT") == 1

    def test_event_count_helper_agrees(self, calendar):
        assert ics.event_count(calendar) == 1

    def test_no_line_exceeds_75_octets(self, plan, tables):
        """RFC 5545 §3.1 folding, counted in bytes — the Finnish labels are UTF-8."""
        plan.campaigns[0].notes = "ä" * 400
        calendar = ics.build_calendar(plan, tables)
        for line in calendar.split("\r\n"):
            assert len(line.encode("utf-8")) <= 75, line[:40]

    def test_folding_never_splits_a_multibyte_character(self, plan, tables):
        """A naive character-count fold would corrupt 'kylvöpäivä'."""
        plan.campaigns[0].job_paths = ["hiilisyke-2027/" + "ö" * 200]
        calendar = ics.build_calendar(plan, tables)
        # If a continuation split a UTF-8 sequence, this would already have
        # raised on encode; unfolding must give the characters back intact.
        unfolded = calendar.replace("\r\n ", "")
        assert "ö" * 200 in unfolded


class TestDates:
    def test_windows_are_all_day_events(self, calendar):
        assert "DTSTART;VALUE=DATE:20270606" in calendar
        assert "VALUE=DATE" in calendar

    def test_dtend_is_exclusive(self, calendar):
        """The window ends 16 June, so DTEND is the 17th."""
        assert "DTEND;VALUE=DATE:20270617" in calendar

    def test_a_single_day_window_still_spans_one_day(self, plan, tables):
        day = _dt.date(2027, 6, 9)
        plan.campaigns[0].window = _window(earliest=day, target=day, latest=day)
        calendar = ics.build_calendar(plan, tables)
        assert "DTSTART;VALUE=DATE:20270609" in calendar
        assert "DTEND;VALUE=DATE:20270610" in calendar

    def test_a_manual_window_overrides_the_derived_one(self, plan, tables):
        plan.campaigns[0].manual_window = _window(
            earliest=_dt.date(2027, 7, 1),
            target=_dt.date(2027, 7, 2),
            latest=_dt.date(2027, 7, 3),
            basis="manual",
            confidence="high",
        )
        calendar = ics.build_calendar(plan, tables)
        assert "DTSTART;VALUE=DATE:20270701" in calendar
        assert "set manually" in calendar


class TestContent:
    def test_the_summary_names_the_campaign_and_parcel(self, calendar):
        assert "SUMMARY:" in calendar
        assert "5241087453" in calendar

    def test_the_description_carries_the_uncertainty(self, calendar):
        assert "Target: 2027-06-09" in calendar
        assert "uncalibrated" in calendar

    def test_an_uncalibrated_window_is_shouted_about(self, calendar):
        assert "WINDOW UNCALIBRATED" in calendar

    def test_acquisition_parameters_are_included(self, calendar):
        assert "0.60 cm/px" in calendar
        assert "rgb" in calendar

    def test_a_normal_basis_warns_the_dates_will_move(self, plan, tables):
        plan.campaigns[0].window = _window(basis="normal")
        assert "climatological normal" in ics.build_calendar(plan, tables)

    def test_the_ground_sprayer_notice_reaches_the_calendar(self, plan, tables):
        plan.campaigns[0].type_id = "weed_map_early"
        calendar = ics.build_calendar(plan, tables)
        assert "GROUND SPRAYER" in calendar

    def test_the_calendar_name_carries_folder_and_season(self, calendar):
        assert "X-WR-CALNAME:" in calendar
        assert "hiilisyke-2027" in calendar

    def test_priority_maps_from_the_campaign_type(self, calendar):
        # emergence_count ships as priority = "high" → RFC value 2.
        assert "PRIORITY:2" in calendar

    def test_events_are_tentative_until_scheduled(self, plan, tables):
        assert "STATUS:TENTATIVE" in ics.build_calendar(plan, tables)
        plan.campaigns[0].state = "scheduled"
        assert "STATUS:CONFIRMED" in ics.build_calendar(plan, tables)


class TestEscaping:
    @pytest.mark.parametrize(
        "raw,escaped",
        [
            ("a,b", "a\\,b"),
            ("a;b", "a\\;b"),
            ("a\nb", "a\\nb"),
            ("a\\b", "a\\\\b"),
        ],
    )
    def test_special_characters_are_escaped(self, raw, escaped):
        assert ics._escape(raw) == escaped

    def test_a_comma_in_a_job_path_does_not_break_the_line(self, plan, tables):
        plan.campaigns[0].job_paths = ["folder/a,b", "folder/c;d"]
        calendar = ics.build_calendar(plan, tables)
        assert "\\," in calendar and "\\;" in calendar


class TestSelection:
    def test_only_schedulable_states_are_exported(self, plan, tables):
        for state in ("flown", "missed", "cancelled", "skipped"):
            plan.campaigns[0].state = state
            assert ics.event_count(ics.build_calendar(plan, tables)) == 0

    @pytest.mark.parametrize("state", ["planned", "open", "scheduled"])
    def test_live_states_are_exported(self, plan, tables, state):
        plan.campaigns[0].state = state
        assert ics.event_count(ics.build_calendar(plan, tables)) == 1

    def test_a_campaign_with_no_window_is_skipped(self, plan, tables):
        plan.campaigns[0].window = None
        assert ics.event_count(ics.build_calendar(plan, tables)) == 0

    def test_an_empty_plan_still_produces_a_valid_calendar(self, tables):
        empty = store.new_plan("kentta", 2027)
        calendar = ics.build_calendar(empty, tables)
        assert "BEGIN:VCALENDAR" in calendar and ics.event_count(calendar) == 0


class TestStableIdentity:
    def test_uids_are_stable_across_exports(self, plan, tables):
        first = ics.build_calendar(plan, tables)
        second = ics.build_calendar(plan, tables)
        assert _uid_of(first) == _uid_of(second)

    def test_re_exporting_after_a_replan_updates_rather_than_duplicates(
        self, plan, tables
    ):
        """Same UID, moved dates: the calendar client replaces the event."""
        before = ics.build_calendar(plan, tables)
        plan.campaigns[0].window = _window(
            earliest=_dt.date(2027, 6, 12),
            target=_dt.date(2027, 6, 15),
            latest=_dt.date(2027, 6, 22),
        )
        after = ics.build_calendar(plan, tables)
        assert _uid_of(before) == _uid_of(after)
        assert "20270612" in after

    def test_different_campaigns_get_different_uids(self, plan, tables):
        second = plan.campaigns[0].model_copy(deep=True)
        second.campaign_id = "2027-canopy_peak-5241087453"
        second.type_id = "canopy_peak"
        plan.campaigns.append(second)
        calendar = ics.build_calendar(plan, tables)
        uids = [line for line in calendar.split("\r\n") if line.startswith("UID:")]
        assert len(uids) == 2 and uids[0] != uids[1]

    def test_the_same_campaign_in_different_folders_differs(self, tables):
        uids = set()
        for folder in ("a", "b"):
            p = store.new_plan(folder, 2027)
            p.campaigns.append(
                Campaign(
                    campaign_id="2027-emergence_count-1",
                    type_id="emergence_count",
                    season=2027,
                    state="open",
                    window=_window(),
                )
            )
            uids.add(_uid_of(ics.build_calendar(p, tables)))
        assert len(uids) == 2


def _uid_of(calendar: str) -> str:
    return next(line for line in calendar.split("\r\n") if line.startswith("UID:"))
