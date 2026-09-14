"""End-to-end: init → plan → status, and the idempotence guarantee."""

from __future__ import annotations

import datetime as _dt

import pytest

from flightmanager.season import store
from flightmanager.season.planner import (
    PlanError,
    init_plan,
    parse_sowing_csv,
    recompute,
    status,
    window_summary,
)
from tests.season.conftest import SOWING, TODAY

FOLDER = "kentta"
JOBS = ["kentta/5241087453", "kentta/5241087454", "kentta/5241087455"]


@pytest.fixture
def seeded(host, tables):
    init_plan(
        host, FOLDER, 2027, crop_id="spring_barley", sowing_date=SOWING, tables=tables
    )
    return host


class TestInit:
    def test_every_job_gets_an_assignment(self, host, tables):
        plan = init_plan(
            host,
            FOLDER,
            2027,
            crop_id="spring_barley",
            sowing_date=SOWING,
            tables=tables,
        )
        assert sorted(a.job_path for a in plan.assignments) == JOBS
        assert all(a.crop_id == "spring_barley" for a in plan.assignments)

    def test_the_plan_file_lands_in_the_folder(self, host, tables):
        init_plan(
            host,
            FOLDER,
            2027,
            crop_id="spring_barley",
            sowing_date=SOWING,
            tables=tables,
        )
        assert (host.root / FOLDER / "season_2027.json").exists()

    def test_unknown_crop_is_refused_with_the_list(self, host, tables):
        with pytest.raises(PlanError, match="unknown crop"):
            init_plan(host, FOLDER, 2027, crop_id="bananas", tables=tables)

    def test_empty_folder_is_refused(self, host, tables):
        with pytest.raises(PlanError, match="no jobs"):
            init_plan(host, "empty", 2027, crop_id="spring_barley", tables=tables)

    def test_rerunning_init_does_not_wipe_existing_dates(self, host, tables):
        init_plan(
            host,
            FOLDER,
            2027,
            crop_id="spring_barley",
            sowing_date=SOWING,
            tables=tables,
        )
        plan = init_plan(host, FOLDER, 2027, crop_id="oats", tables=tables)
        assert all(a.sowing_date == SOWING for a in plan.assignments)
        assert all(a.crop_id == "oats" for a in plan.assignments)

    def test_an_unsafe_folder_name_is_rejected(self, host, tables):
        with pytest.raises(ValueError):
            init_plan(host, "../escape", 2027, crop_id="spring_barley", tables=tables)


class TestSowingCsv:
    def test_per_parcel_rows_are_parsed(self):
        rows = parse_sowing_csv(
            "job,crop,sowing\n"
            "kentta/5241087453,spring_barley,2027-05-14\n"
            "kentta/5241087454,oats,2027-05-18\n",
            JOBS,
        )
        assert [r.crop_id for r in rows] == ["spring_barley", "oats"]
        assert rows[1].sowing_date == _dt.date(2027, 5, 18)

    def test_finnish_headers_and_dotted_dates_work(self):
        rows = parse_sowing_csv(
            "lohko;kasvi;kylvöpäivä\n".replace(";", ",")
            + "5241087453,spring_barley,14.5.2027\n",
            JOBS,
        )
        assert rows[0].sowing_date == _dt.date(2027, 5, 14)
        assert rows[0].job_path == "kentta/5241087453"

    def test_a_bare_parcel_name_resolves_to_the_full_path(self):
        rows = parse_sowing_csv("parcel,crop\n5241087455,oats\n", JOBS)
        assert rows[0].job_path == "kentta/5241087455"

    def test_an_unknown_parcel_is_an_error_not_a_silent_skip(self):
        with pytest.raises(PlanError, match="no job named"):
            parse_sowing_csv("parcel,crop\n9999999999,oats\n", JOBS)

    def test_a_bad_date_names_the_line(self):
        with pytest.raises(PlanError, match="line 2"):
            parse_sowing_csv("parcel,sowing\n5241087453,not-a-date\n", JOBS)

    def test_a_missing_job_column_is_an_error(self):
        with pytest.raises(PlanError, match="job/parcel column"):
            parse_sowing_csv("crop,sowing\nspring_barley,2027-05-14\n", JOBS)

    def test_blank_rows_are_skipped(self):
        rows = parse_sowing_csv("parcel,crop\n5241087453,oats\n,\n", JOBS)
        assert len(rows) == 1

    def test_csv_init_reaches_the_plan(self, host, tables):
        plan = init_plan(
            host,
            FOLDER,
            2027,
            csv_text="job,crop,sowing\nkentta/5241087453,oats,2027-05-10\n",
            tables=tables,
        )
        assignment = plan.assignment_for("kentta/5241087453")
        assert assignment.crop_id == "oats"
        assert assignment.sowing_date == _dt.date(2027, 5, 10)


class TestRecompute:
    def test_windows_are_computed_for_every_job(
        self, seeded, tables, cfg, patched_series
    ):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert result.campaigns_total > 0
        assert result.campaigns_with_window > 0
        assert result.path.name == "season_2027.json"

    def test_plan_is_idempotent(self, seeded, tables, cfg, patched_series):
        first = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        ids_before = [c.campaign_id for c in first.plan.campaigns]
        windows_before = [
            (c.campaign_id, c.window.target if c.window else None)
            for c in first.plan.campaigns
        ]

        second = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert [c.campaign_id for c in second.plan.campaigns] == ids_before
        assert [
            (c.campaign_id, c.window.target if c.window else None)
            for c in second.plan.campaigns
        ] == windows_before

    def test_replanning_preserves_a_logged_flight(
        self, seeded, tables, cfg, patched_series
    ):
        recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        folder_dir = seeded.folder_dir(FOLDER)

        plan = store.load_plan(folder_dir, 2027)
        target = next(c for c in plan.campaigns if c.type_id == "emergence_count")
        target.state = "flown"
        target.flown = _dt.date(2027, 5, 26)
        target.notes = "kuvattu sadekuurojen välissä"
        store.save_plan(folder_dir, plan)

        recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        after = store.load_plan(folder_dir, 2027).campaign(target.campaign_id)
        assert after.state == "flown"
        assert after.notes == "kuvattu sadekuurojen välissä"
        assert after.window is not None  # the derived window was still refreshed

    def test_windows_move_as_the_season_progresses(
        self, seeded, tables, cfg, monkeypatch
    ):
        """A cold run must push stages later than a warm one."""
        from flightmanager.season import weather_history as wx
        from tests.season.conftest import constant_series

        def series_at(mean_c):
            def build(lat, lon, start, end, cfg_, cache_dir, **kwargs):
                return constant_series(
                    start, (end - start).days + 1, mean_c, archive_until=TODAY
                )

            return build

        monkeypatch.setattr(wx, "build_series", series_at(16.0))
        warm = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        warm_target = _target_of(warm, "emergence_count")

        monkeypatch.setattr(wx, "build_series", series_at(9.0))
        cold = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert _target_of(cold, "emergence_count") > warm_target

    def test_a_folder_with_no_assignments_is_refused(
        self, host, tables, cfg, patched_series
    ):
        with pytest.raises(PlanError, match="no season assignments"):
            recompute(host, tables, cfg, FOLDER, 2027, today=TODAY)

    def test_a_folder_with_no_sowing_dates_is_refused_not_guessed(
        self, host, tables, cfg, patched_series
    ):
        init_plan(host, FOLDER, 2027, crop_id="spring_barley", tables=tables)
        with pytest.raises(PlanError, match="not guessed"):
            recompute(host, tables, cfg, FOLDER, 2027, today=TODAY)

    def test_the_weather_cell_is_recorded_on_the_plan(
        self, seeded, tables, cfg, patched_series
    ):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert result.plan.centroid_lat == pytest.approx(62.79, abs=0.01)

    def test_a_wide_folder_warns_about_one_grid_cell(
        self, seeded, tables, cfg, patched_series
    ):
        """One weather cell must not silently average two microclimates."""
        seeded.jobs[-1].lat = 61.0  # ~200 km south of the others
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert any("one-cell limit" in w for w in result.warnings)

    def test_a_compact_folder_does_not_warn(self, seeded, tables, cfg, patched_series):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert not any("one-cell limit" in w for w in result.warnings)

    def test_uncalibrated_notice_is_emitted(self, seeded, tables, cfg, patched_series):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert any("UNCALIBRATED" in n for n in result.notices)

    def test_ground_sprayer_notice_is_emitted(
        self, seeded, tables, cfg, patched_series
    ):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert any("GROUND SPRAYER" in n for n in result.notices)

    def test_gsd_flags_reach_the_campaigns(self, seeded, tables, cfg, patched_series):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        weed = [c for c in result.plan.campaigns if c.type_id == "weed_map_early"]
        assert weed and all("low_altitude_workload" in c.flags for c in weed)

    def test_audience_filter_narrows_the_plan(
        self, seeded, tables, cfg, patched_series
    ):
        result = recompute(
            seeded, tables, cfg, FOLDER, 2027, today=TODAY, audience="farmer"
        )
        assert "s2_calibration" not in {c.type_id for c in result.plan.campaigns}

    def test_orphaned_campaigns_with_recorded_work_are_kept(
        self, seeded, tables, cfg, patched_series
    ):
        recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        folder_dir = seeded.folder_dir(FOLDER)
        plan = store.load_plan(folder_dir, 2027)
        ghost = plan.campaigns[0].model_copy(deep=True)
        ghost.campaign_id = "2027-retired_type-5241087453"
        ghost.type_id = "retired_type"
        ghost.state = "flown"
        ghost.notes = "flown before the type was retired"
        plan.campaigns.append(ghost)
        store.save_plan(folder_dir, plan)

        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        kept = result.plan.campaign("2027-retired_type-5241087453")
        assert kept is not None and "orphaned" in kept.flags

    def test_orphaned_campaigns_without_work_are_dropped(
        self, seeded, tables, cfg, patched_series
    ):
        recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        folder_dir = seeded.folder_dir(FOLDER)
        plan = store.load_plan(folder_dir, 2027)
        ghost = plan.campaigns[0].model_copy(deep=True)
        ghost.campaign_id = "2027-retired_type-5241087453"
        ghost.type_id = "retired_type"
        ghost.state = "open"
        plan.campaigns.append(ghost)
        store.save_plan(folder_dir, plan)

        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        assert result.plan.campaign("2027-retired_type-5241087453") is None


def _target_of(result, type_id):
    return next(
        c.window.target
        for c in result.plan.campaigns
        if c.type_id == type_id and c.window
    )


class TestStatus:
    def test_groups_partition_sensibly(self, seeded, tables, cfg, patched_series):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        report = status(result.plan, tables, today=TODAY)
        assert report.rows
        for row in report.open_now():
            assert row.earliest <= TODAY <= row.latest
        for row in report.upcoming():
            assert row.earliest > TODAY

    def test_missed_view_reports_a_closed_window(
        self, seeded, tables, cfg, patched_series
    ):
        late = _dt.date(2027, 11, 1)
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=late)
        report = status(result.plan, tables, today=late)
        assert report.missed()
        assert all(r.state == "missed" for r in report.missed())

    def test_blocked_rows_carry_a_reason(self, host, tables, cfg, patched_series):
        init_plan(
            host,
            FOLDER,
            2027,
            csv_text="job,crop,sowing\nkentta/5241087453,spring_barley,2027-05-14\n",
            tables=tables,
        )
        result = recompute(host, tables, cfg, FOLDER, 2027, today=TODAY)
        report = status(result.plan, tables, today=TODAY)
        # The two jobs with no sowing date are blocked with a stated reason.
        assert report.blocked()
        assert all(r.reasons for r in report.blocked())

    def test_rows_are_sorted_by_target_date(self, seeded, tables, cfg, patched_series):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        report = status(result.plan, tables, today=TODAY)
        targets = [r.target for r in report.rows if r.target]
        assert targets == sorted(targets)

    def test_closing_is_a_subset_of_open(self, seeded, tables, cfg, patched_series):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        report = status(result.plan, tables, today=TODAY)
        assert all(r in report.open_now() for r in report.closing())

    def test_ground_sprayer_notice_reaches_the_row(
        self, seeded, tables, cfg, patched_series
    ):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        report = status(result.plan, tables, today=TODAY)
        weed = [r for r in report.rows if r.type_id == "weed_map_early"]
        assert weed and all("GROUND SPRAYER" in r.ground_sprayer_notice for r in weed)


class TestWindowSummary:
    def test_it_always_shows_the_band(self, seeded, tables, cfg, patched_series):
        result = recompute(seeded, tables, cfg, FOLDER, 2027, today=TODAY)
        window = next(c.window for c in result.plan.campaigns if c.window)
        text = window_summary(window)
        assert "→" in text and "±" in text

    def test_no_window_says_so(self):
        assert window_summary(None) == "no window"
