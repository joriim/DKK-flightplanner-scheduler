"""Phenology: thermal time, stage dates, and the uncertainty band (spec §13)."""

from __future__ import annotations

import datetime as _dt

import pytest

from flightmanager.season.config import SeasonConfig
from flightmanager.season.models import CropProfile, Provenance
from flightmanager.season.phenology import (
    accumulate,
    build_timeline,
    date_for_gdd,
    effective_gdd,
    gdd_on,
    thermal_repeat_dates,
    uncertainty_for,
)
from tests.season.conftest import SOWING, TODAY, constant_series


def _crop(**kwargs) -> CropProfile:
    defaults = dict(
        id="test_barley",
        label_fi="Testiohra",
        label_en="Test barley",
        base_temp_c=5.0,
        cutoff_temp_c=30.0,
        model="gdd",
        stages={"sowing": 0, "emergence": 130, "tillering": 260, "harvest_ready": 1150},
    )
    defaults.update(kwargs)
    return CropProfile(**defaults)


class TestEffectiveGdd:
    def test_base_is_subtracted(self):
        assert effective_gdd(15.0, 5.0) == 10.0

    def test_below_base_contributes_nothing(self):
        assert effective_gdd(2.0, 5.0) == 0.0
        assert effective_gdd(-10.0, 5.0) == 0.0

    def test_cutoff_caps_the_daily_mean_before_the_base(self):
        # Finnish convention: cap the mean at 30, then subtract 5 → 25.
        assert effective_gdd(35.0, 5.0, 30.0) == 25.0

    def test_no_cutoff_means_no_cap(self):
        assert effective_gdd(35.0, 5.0, None) == 30.0


class TestAccumulate:
    def test_flat_series_accumulates_linearly(self):
        series = constant_series(SOWING, 20, 15.0)
        cum = accumulate(series, SOWING, 5.0, 30.0)
        assert cum[0].date == SOWING
        assert cum[0].cum == 10.0
        assert cum[12].cum == 130.0

    def test_days_before_the_start_are_ignored_not_zeroed(self):
        """A January-to-December series and a sowing-onward one must agree."""
        long_series = constant_series(_dt.date(2027, 1, 1), 300, 15.0)
        short_series = constant_series(SOWING, 100, 15.0)
        long_cum = accumulate(long_series, SOWING, 5.0, 30.0)
        short_cum = accumulate(short_series, SOWING, 5.0, 30.0)
        assert long_cum[:50] == short_cum[:50]

    def test_gdd_on_returns_the_running_total(self):
        cum = accumulate(constant_series(SOWING, 30, 15.0), SOWING, 5.0)
        assert gdd_on(cum, SOWING) == 10.0
        assert gdd_on(cum, SOWING + _dt.timedelta(days=9)) == 100.0
        assert gdd_on(cum, SOWING - _dt.timedelta(days=1)) is None


class TestDateForGdd:
    def test_crossing_is_the_first_day_that_meets_the_threshold(self):
        cum = accumulate(constant_series(SOWING, 40, 15.0), SOWING, 5.0)
        day, source = date_for_gdd(cum, 130)
        # 10 °C·d/day, so 130 lands on day 13 = sowing + 12.
        assert day == SOWING + _dt.timedelta(days=12)
        assert source == "normal"

    def test_unreached_threshold_returns_none_not_an_exception(self):
        cum = accumulate(constant_series(SOWING, 10, 15.0), SOWING, 5.0)
        assert date_for_gdd(cum, 1150) is None

    def test_zero_threshold_is_the_start_date(self):
        cum = accumulate(constant_series(SOWING, 10, 15.0), SOWING, 5.0)
        assert date_for_gdd(cum, 0)[0] == SOWING


class TestGoldenTimeline:
    """Fixed synthetic series → dates that can be checked by hand."""

    def test_known_stage_dates(self, cfg):
        series = constant_series(SOWING, 200, 15.0, archive_until=TODAY)
        timeline = build_timeline(series, _crop(), SOWING, cfg)
        assert timeline.stage("sowing").date == SOWING
        assert timeline.stage("emergence").date == SOWING + _dt.timedelta(days=12)
        assert timeline.stage("tillering").date == SOWING + _dt.timedelta(days=25)
        assert timeline.stage("harvest_ready").date == SOWING + _dt.timedelta(days=114)

    def test_cold_spring_pushes_every_stage_later(self, cfg):
        warm = build_timeline(constant_series(SOWING, 260, 15.0), _crop(), SOWING, cfg)
        cold = build_timeline(constant_series(SOWING, 260, 9.0), _crop(), SOWING, cfg)
        assert cold.stage("emergence").date > warm.stage("emergence").date
        assert cold.stage("tillering").date > warm.stage("tillering").date

    def test_season_that_never_ripens_reports_it(self, cfg):
        """A cool season is a real outcome, not an error."""
        series = constant_series(SOWING, 120, 7.0)  # 2 °C·d/day → 240 total
        timeline = build_timeline(series, _crop(), SOWING, cfg)
        harvest = timeline.stage("harvest_ready")
        assert harvest.date is None
        assert harvest.reached is False
        assert "1150" in harvest.reason
        # Earlier stages are still dated.
        assert timeline.stage("emergence").date is not None

    def test_leap_year_29_february_is_counted(self, cfg):
        start = _dt.date(2028, 2, 20)
        series = constant_series(start, 20, 15.0)
        cum = accumulate(series, start, 5.0)
        assert any(p.date == _dt.date(2028, 2, 29) for p in cum)
        # 10 days of 10 °C·d reaches 29 Feb at exactly 100.
        assert gdd_on(cum, _dt.date(2028, 2, 29)) == 100.0

    def test_empty_series_is_reported_not_crashed(self, cfg):
        series = constant_series(SOWING - _dt.timedelta(days=40), 10, 15.0)
        timeline = build_timeline(series, _crop(), SOWING, cfg)
        assert timeline.stages == {}
        assert "no temperature data" in " ".join(timeline.notes)


class TestUncertainty:
    """Spec §13: widens monotonically with projection distance, zero for past."""

    def test_archive_backed_date_collapses_to_zero(self, cfg):
        assert (
            uncertainty_for(
                SOWING, source="archive", horizon=TODAY, cfg=cfg, calibrated=False
            )
            == 0.0
        )

    def test_widens_monotonically_with_projection_distance(self, cfg):
        horizon = TODAY
        bands = [
            uncertainty_for(
                horizon + _dt.timedelta(days=d),
                source="normal",
                horizon=horizon,
                cfg=cfg,
                calibrated=True,
            )
            for d in range(0, 90, 7)
        ]
        assert bands == sorted(bands)
        assert bands[-1] > bands[0]

    def test_uncalibrated_crops_carry_a_wider_band(self, cfg):
        day = TODAY + _dt.timedelta(days=30)
        calibrated = uncertainty_for(
            day, source="normal", horizon=TODAY, cfg=cfg, calibrated=True
        )
        placeholder = uncertainty_for(
            day, source="normal", horizon=TODAY, cfg=cfg, calibrated=False
        )
        assert placeholder > calibrated
        assert placeholder - calibrated == pytest.approx(cfg.uncalibrated_extra_days)

    def test_inside_the_forecast_horizon_the_band_is_the_floor(self, cfg):
        band = uncertainty_for(
            TODAY - _dt.timedelta(days=1),
            source="forecast",
            horizon=TODAY,
            cfg=cfg,
            calibrated=True,
        )
        assert band == pytest.approx(cfg.base_uncertainty_days)

    def test_timeline_bands_never_shrink_as_stages_advance(self, cfg):
        series = constant_series(
            SOWING,
            260,
            15.0,
            archive_until=TODAY,
            forecast_until=TODAY + _dt.timedelta(days=14),
        )
        timeline = build_timeline(series, _crop(), SOWING, cfg)
        dated = sorted(
            (s for s in timeline.stages.values() if s.date),
            key=lambda s: s.date,
        )
        bands = [s.uncertainty_days for s in dated]
        assert bands == sorted(bands)

    def test_calibrated_provenance_narrows_the_band(self, cfg):
        series = constant_series(SOWING, 260, 15.0, archive_until=TODAY)
        placeholder = build_timeline(series, _crop(), SOWING, cfg)
        calibrated = build_timeline(
            series,
            _crop(
                provenance=Provenance(
                    source="fitted", confidence="high", calibrated_from=["obs-1"], n=42
                )
            ),
            SOWING,
            cfg,
        )
        assert (
            calibrated.stage("harvest_ready").uncertainty_days
            < placeholder.stage("harvest_ready").uncertainty_days
        )


class TestFixedDaysModel:
    def test_stages_are_plain_day_offsets(self, cfg):
        crop = _crop(model="fixed_days", stages={"emergence": 10, "tillering": 25})
        series = constant_series(SOWING, 60, 15.0, archive_until=TODAY)
        timeline = build_timeline(series, crop, SOWING, cfg)
        assert timeline.stage("emergence").date == SOWING + _dt.timedelta(days=10)
        assert timeline.stage("tillering").date == SOWING + _dt.timedelta(days=25)
        assert "fixed_days" in " ".join(timeline.notes)


class TestManualModel:
    def test_manual_crops_produce_no_dates_and_say_so(self, cfg):
        crop = _crop(model="manual", stages={})
        timeline = build_timeline(constant_series(SOWING, 60, 15.0), crop, SOWING, cfg)
        assert timeline.stages == {}
        assert "manual" in " ".join(timeline.notes)


class TestThermalRepeat:
    def test_repeats_land_every_interval_of_thermal_time(self, cfg):
        series = constant_series(SOWING, 260, 15.0)
        timeline = build_timeline(series, _crop(), SOWING, cfg)
        repeats = thermal_repeat_dates(
            timeline, anchor_gdd=400, every_gdd=200, max_count=4
        )
        assert len(repeats) == 3
        # 10 °C·d/day → each 200 °C·d step is 20 days.
        gaps = [
            (repeats[i + 1][0] - repeats[i][0]).days for i in range(len(repeats) - 1)
        ]
        assert gaps == [20, 20]

    def test_repeats_stop_where_the_season_does(self, cfg):
        series = constant_series(SOWING, 60, 15.0)  # only 600 °C·d available
        timeline = build_timeline(series, _crop(), SOWING, cfg)
        repeats = thermal_repeat_dates(
            timeline, anchor_gdd=400, every_gdd=200, max_count=5
        )
        assert len(repeats) == 1


class TestSeriesMetadata:
    def test_horizon_is_where_real_information_stops(self):
        series = constant_series(
            SOWING,
            100,
            15.0,
            archive_until=TODAY,
            forecast_until=TODAY + _dt.timedelta(days=14),
        )
        assert series.horizon == TODAY + _dt.timedelta(days=14)

    def test_counts_report_the_source_mix(self):
        series = constant_series(
            SOWING,
            100,
            15.0,
            archive_until=TODAY,
            forecast_until=TODAY + _dt.timedelta(days=14),
        )
        counts = series.counts()
        assert counts["archive"] == (TODAY - SOWING).days + 1
        assert counts["forecast"] == 14
        assert counts["normal"] > 0

    def test_all_normal_series_has_no_horizon(self):
        series = constant_series(SOWING, 30, 15.0)
        assert series.horizon is None


class TestCropValidation:
    def test_gdd_crop_without_stages_is_rejected(self):
        with pytest.raises(ValueError, match="no stages"):
            CropProfile(id="x", label_fi="x", label_en="x", model="gdd", stages={})

    def test_cutoff_below_base_is_rejected(self):
        with pytest.raises(ValueError, match="cutoff_temp_c"):
            _crop(base_temp_c=10.0, cutoff_temp_c=5.0)

    def test_ordered_stages_sorts_by_threshold_not_insertion(self):
        crop = _crop(stages={"harvest_ready": 1150, "sowing": 0, "emergence": 130})
        assert [name for name, _ in crop.ordered_stages()] == [
            "sowing",
            "emergence",
            "harvest_ready",
        ]


def test_seasonconfig_defaults_match_the_spec():
    cfg = SeasonConfig()
    assert cfg.projection_normal_years == 30
    assert cfg.window_uncertainty_days_per_week_projected == 1.5
    assert cfg.min_solar_elevation_deg == 30.0
    assert cfg.min_solar_elevation_rgb_deg == 20.0
    assert cfg.max_field_day_hours == 6.0
    assert cfg.precip_threshold_mm == 0.5


class TestProjectionWithoutAHorizon:
    """Planning next season: every day is a normal, so widen from today."""

    def test_bands_still_widen_when_the_series_holds_no_real_data(self, cfg):
        today = _dt.date(2026, 9, 14)
        future_sowing = _dt.date(2027, 5, 14)
        series = constant_series(future_sowing, 260, 15.0)  # all "normal"
        assert series.horizon is None

        timeline = build_timeline(series, _crop(), future_sowing, cfg, today)
        early = timeline.stage("emergence").uncertainty_days
        late = timeline.stage("harvest_ready").uncertainty_days
        assert late > early, "a date months further out must carry a wider band"

    def test_the_band_is_large_eight_months_out(self, cfg):
        today = _dt.date(2026, 9, 14)
        future_sowing = _dt.date(2027, 5, 14)
        timeline = build_timeline(
            constant_series(future_sowing, 260, 15.0),
            _crop(),
            future_sowing,
            cfg,
            today,
        )
        # ~35 weeks × 1.5 d/week, plus the base and uncalibrated floors.
        assert timeline.stage("emergence").uncertainty_days > 50

    def test_a_real_horizon_still_wins_over_today(self, cfg):
        """With archive + forecast present, widening starts at the horizon."""
        today = _dt.date(2027, 5, 20)
        series = constant_series(
            SOWING,
            260,
            15.0,
            archive_until=today,
            forecast_until=today + _dt.timedelta(days=14),
        )
        timeline = build_timeline(series, _crop(), SOWING, cfg, today)
        # Sowing is archive-backed and already past, so it is known outright.
        assert timeline.stage("sowing").uncertainty_days == 0.0
        # Harvest is ~16 weeks past the horizon, not ~16 weeks past today, so the
        # band is the smaller of the two readings — the horizon is doing the work.
        horizon_weeks = (timeline.stage("harvest_ready").date - series.horizon).days / 7
        today_weeks = (timeline.stage("harvest_ready").date - today).days / 7
        expected = (
            cfg.base_uncertainty_days
            + cfg.uncalibrated_extra_days
            + horizon_weeks * cfg.window_uncertainty_days_per_week_projected
        )
        assert timeline.stage("harvest_ready").uncertainty_days == pytest.approx(
            round(expected, 1)
        )
        assert horizon_weeks < today_weeks
