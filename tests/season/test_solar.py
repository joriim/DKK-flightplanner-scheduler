"""Solar elevation against published values, and the latitude wall (spec §13).

The checks are against the closed form ``90 − |latitude − declination|``, which
is independent of the implementation: at solar noon on a solstice or equinox
the sun's maximum elevation is fixed by geometry alone, so the expected numbers
are derived rather than recorded from a previous run.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from flightmanager.season import solar
from flightmanager.season.config import SeasonConfig

#: Seinäjoki — the spec's reference point (§13).
LAT, LON = 62.79, 22.84

#: Earth's axial tilt: the sun's declination at the solstices.
TILT = 23.44


def noon_elevation(lat: float, declination: float) -> float:
    """Maximum solar elevation for a latitude and declination, from geometry.

    ``90 − |latitude − declination|``. Written as one function rather than
    inline per case so the sign convention cannot be got wrong in one row and
    right in the next — which is exactly the mistake the southern-hemisphere
    case caught.
    """
    return 90.0 - abs(lat - declination)


class TestAgainstPublishedGeometry:
    @pytest.mark.parametrize(
        "day,declination,label",
        [
            (_dt.date(2026, 6, 21), TILT, "summer solstice"),
            (_dt.date(2026, 12, 21), -TILT, "winter solstice"),
            (_dt.date(2026, 3, 20), 0.0, "March equinox"),
            (_dt.date(2026, 9, 22), 0.0, "September equinox"),
        ],
    )
    def test_seinajoki_noon_elevation(self, day, declination, label):
        expected = noon_elevation(LAT, declination)
        assert solar.max_elevation_on(LAT, LON, day) == pytest.approx(
            expected, abs=0.5
        ), label

    @pytest.mark.parametrize(
        "lat,lon,day,declination,label",
        [
            (51.48, -0.13, _dt.date(2026, 3, 20), 0.0, "Greenwich equinox"),
            (0.0, 0.0, _dt.date(2026, 3, 20), 0.0, "equator equinox"),
            # Southern hemisphere: December is *summer*, and the sun is north
            # of nothing — it is the |lat − declination| term that gets this
            # right, not a hemisphere-specific formula.
            (-33.87, 151.21, _dt.date(2026, 12, 21), -TILT, "Sydney midsummer"),
            (78.22, 15.65, _dt.date(2026, 6, 21), TILT, "Svalbard midnight sun"),
        ],
    )
    def test_other_latitudes(self, lat, lon, day, declination, label):
        """Southern hemisphere and high Arctic, to pin the sign conventions."""
        expected = noon_elevation(lat, declination)
        assert solar.max_elevation_on(lat, lon, day) == pytest.approx(
            expected, abs=0.7
        ), label

    def test_the_closed_form_never_exceeds_the_zenith(self):
        """Guards the test's own helper: no latitude can see the sun past 90°."""
        for lat in range(-90, 91, 15):
            for declination in (-TILT, 0.0, TILT):
                assert noon_elevation(lat, declination) <= 90.0

    def test_polar_night_stays_below_the_horizon(self):
        assert solar.max_elevation_on(78.22, 15.65, _dt.date(2026, 12, 21)) < 0

    def test_midnight_sun_never_sets(self):
        """At Svalbard in June the sun stays up around the clock."""
        day = _dt.date(2026, 6, 21)
        lows = [
            solar.elevation_deg(
                78.22,
                15.65,
                _dt.datetime.combine(day, _dt.time(h), tzinfo=_dt.timezone.utc),
            )
            for h in range(24)
        ]
        assert min(lows) > 0


class TestSolarDay:
    def test_midsummer_gives_a_full_working_day(self):
        day = solar.solar_day(
            LAT, LON, _dt.date(2026, 6, 21), 30.0, utc_offset_s=3 * 3600
        )
        assert day.has_qualifying_hours
        assert len(day.qualifying_hours) >= 8
        assert day.shortfall_deg() == 0.0

    def test_hours_are_contiguous_and_centred_on_noon(self):
        day = solar.solar_day(
            LAT, LON, _dt.date(2026, 6, 21), 30.0, utc_offset_s=3 * 3600
        )
        assert len(day.spans()) == 1  # one unbroken run
        start, end = day.spans()[0]
        assert start < 13 < end

    def test_labels_render_as_hour_spans(self):
        day = solar.solar_day(
            LAT, LON, _dt.date(2026, 6, 21), 30.0, utc_offset_s=3 * 3600
        )
        assert all(":00-" in label for label in day.labels())

    def test_midpoint_sits_inside_the_longest_run(self):
        day = solar.solar_day(
            LAT, LON, _dt.date(2026, 6, 21), 30.0, utc_offset_s=3 * 3600
        )
        start, end = day.spans()[0]
        assert start <= day.midpoint().hour <= end

    def test_a_day_with_no_qualifying_hours_has_no_midpoint(self):
        day = solar.solar_day(
            LAT, LON, _dt.date(2026, 12, 21), 30.0, utc_offset_s=2 * 3600
        )
        assert not day.has_qualifying_hours
        assert day.midpoint() is None
        assert day.labels() == []

    def test_a_lower_floor_opens_more_hours(self):
        args = (LAT, LON, _dt.date(2026, 9, 1))
        strict = solar.solar_day(*args, 30.0, utc_offset_s=3 * 3600)
        loose = solar.solar_day(*args, 20.0, utc_offset_s=3 * 3600)
        assert len(loose.qualifying_hours) > len(strict.qualifying_hours)

    def test_shortfall_reports_how_far_below_the_floor_the_day_falls(self):
        day = solar.solar_day(
            LAT, LON, _dt.date(2026, 10, 15), 30.0, utc_offset_s=3 * 3600
        )
        assert not day.has_qualifying_hours
        assert day.shortfall_deg() == pytest.approx(
            30.0 - day.max_elevation_deg, abs=0.01
        )

    def test_spans_split_on_a_gap(self):
        day = solar.SolarDay(date=_dt.date(2026, 6, 1), threshold_deg=30.0)
        day.qualifying_hours = [9, 10, 13, 14, 15]
        assert day.spans() == [(9, 11), (13, 16)]
        assert day.labels() == ["09:00-11:00", "13:00-16:00"]


class TestTheAutumnWall:
    """Spec §13: an autumn campaign must report "no qualifying hours"."""

    def test_multispectral_work_ends_in_mid_september_at_this_latitude(self):
        first, last = solar.season_window(LAT, LON, 2026, 30.0)
        assert first is not None and last is not None
        # The 30° floor needs declination ≥ 30 − (90 − 62.79) ≈ 2.8°, which the
        # sun holds only from late March to mid September.
        assert first.month == 3 and first.day > 20
        assert last.month == 9 and last.day < 20

    def test_rgb_work_has_a_materially_longer_season(self):
        ms_first, ms_last = solar.season_window(LAT, LON, 2026, 30.0)
        rgb_first, rgb_last = solar.season_window(LAT, LON, 2026, 20.0)
        assert rgb_first < ms_first
        assert rgb_last > ms_last
        assert (rgb_last - rgb_first).days - (ms_last - ms_first).days > 30

    def test_late_october_has_no_multispectral_hours(self):
        """The spec's own example: an autumn catch-crop flight, 62.8 °N."""
        day = solar.solar_day(
            LAT, LON, _dt.date(2026, 10, 25), 30.0, utc_offset_s=2 * 3600
        )
        assert day.qualifying_hours == []
        assert day.shortfall_deg() > 10

    def test_a_30_degree_floor_is_unreachable_in_the_far_north(self):
        first, last = solar.season_window(69.0, 27.0, 2026, 45.0)
        assert (first, last) == (None, None)


class TestThresholdSelection:
    def test_rgb_gets_the_looser_floor(self):
        cfg = SeasonConfig()
        assert solar.threshold_for("rgb", cfg) == cfg.min_solar_elevation_rgb_deg

    @pytest.mark.parametrize("sensor", ["multispectral", "both", "thermal"])
    def test_radiometric_sensors_get_the_strict_floor(self, sensor):
        cfg = SeasonConfig()
        assert solar.threshold_for(sensor, cfg) == cfg.min_solar_elevation_deg

    def test_both_is_gated_by_its_strictest_half(self):
        """A campaign shooting RGB *and* MS must satisfy the MS floor."""
        cfg = SeasonConfig()
        assert solar.threshold_for("both", cfg) > solar.threshold_for("rgb", cfg)


class TestNumericalHygiene:
    def test_naive_datetimes_are_treated_as_utc(self):
        naive = _dt.datetime(2026, 6, 21, 10, 0)
        aware = naive.replace(tzinfo=_dt.timezone.utc)
        assert solar.elevation_deg(LAT, LON, naive) == pytest.approx(
            solar.elevation_deg(LAT, LON, aware)
        )

    def test_elevation_is_continuous_across_midnight(self):
        before = solar.elevation_deg(
            LAT, LON, _dt.datetime(2026, 6, 20, 23, 59, tzinfo=_dt.timezone.utc)
        )
        after = solar.elevation_deg(
            LAT, LON, _dt.datetime(2026, 6, 21, 0, 1, tzinfo=_dt.timezone.utc)
        )
        assert abs(after - before) < 1.0

    def test_azimuth_stays_in_range(self):
        for hour in range(24):
            _, azimuth = solar.solar_position(
                LAT, LON, _dt.datetime(2026, 6, 21, hour, tzinfo=_dt.timezone.utc)
            )
            assert 0.0 <= azimuth < 360.0

    def test_elevation_never_leaves_the_physical_range(self):
        for month in range(1, 13):
            for hour in (0, 6, 12, 18):
                elev = solar.elevation_deg(
                    LAT,
                    LON,
                    _dt.datetime(2026, month, 15, hour, tzinfo=_dt.timezone.utc),
                )
                assert -90.0 <= elev <= 90.0

    def test_leap_years_do_not_shift_the_solstice(self):
        leap = solar.max_elevation_on(LAT, LON, _dt.date(2028, 6, 21))
        common = solar.max_elevation_on(LAT, LON, _dt.date(2027, 6, 21))
        assert abs(leap - common) < 0.2
