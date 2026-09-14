"""Weather history: source precedence, caching, and offline degradation."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest
import responses

from flightmanager.season import weather_history as wx
from flightmanager.season.config import SeasonConfig

LAT, LON = 62.79, 22.84
TODAY = _dt.date(2027, 5, 20)
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def _age_cache(path: Path, days: float = 30.0) -> None:
    """Backdate a cache file's mtime so its TTL has genuinely expired."""
    import os
    import time

    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _daily(start: _dt.date, days: int, value: float) -> dict:
    return {
        "daily": {
            "time": [(start + _dt.timedelta(days=i)).isoformat() for i in range(days)],
            "temperature_2m_mean": [value] * days,
        }
    }


class TestGeometry:
    def test_span_of_a_single_point_is_zero(self):
        assert wx.span_km([(LAT, LON)]) == 0.0

    def test_span_matches_a_known_distance(self):
        # One degree of latitude is ~111 km.
        assert wx.span_km([(62.0, 22.0), (63.0, 22.0)]) == pytest.approx(111, abs=2)

    def test_longitude_is_compressed_at_high_latitude(self):
        lat_span = wx.span_km([(62.0, 22.0), (62.5, 22.0)])
        lon_span = wx.span_km([(62.0, 22.0), (62.0, 22.5)])
        assert lon_span < lat_span

    def test_cell_is_the_rounded_mean(self):
        assert wx.cell_for([(62.78, 22.83), (62.80, 22.85)]) == (62.79, 22.84)

    def test_no_points_is_an_error(self):
        with pytest.raises(ValueError):
            wx.cell_for([])


class TestNormals:
    def test_daily_means_average_across_years(self):
        pairs = {"2024-05-14": 10.0, "2025-05-14": 12.0, "2026-05-14": 14.0}
        assert wx._average_by_day_of_year(pairs)["05-14"] == pytest.approx(12.0)

    def test_leap_day_borrows_28_february_when_thin(self):
        pairs = {f"202{y}-02-28": 1.0 for y in range(4)}
        pairs["2024-02-29"] = 99.0
        normals = wx._average_by_day_of_year(pairs)
        assert normals["02-29"] == normals["02-28"] == 1.0

    def test_a_well_sampled_leap_day_keeps_its_own_value(self):
        pairs = {f"20{y}-02-29": 5.0 for y in (16, 20, 24)}
        pairs["2024-02-28"] = 1.0
        assert wx._average_by_day_of_year(pairs)["02-29"] == 5.0


class TestCoverage:
    def test_a_complete_range_is_covered(self):
        days = {(TODAY + _dt.timedelta(days=i)).isoformat(): 10.0 for i in range(5)}
        assert wx._covers(days, TODAY, TODAY + _dt.timedelta(days=4))

    def test_a_gap_breaks_coverage(self):
        days = {
            TODAY.isoformat(): 10.0,
            (TODAY + _dt.timedelta(days=2)).isoformat(): 10.0,
        }
        assert not wx._covers(days, TODAY, TODAY + _dt.timedelta(days=2))


class TestBuildSeries:
    @responses.activate
    def test_sources_are_stitched_in_precedence_order(self, tmp_path: Path):
        """archive > forecast > normal, and the horizon is where real data ends."""
        cfg = SeasonConfig(projection_normal_years=3)
        responses.add(
            responses.GET,
            ARCHIVE_URL,
            json=_daily(_dt.date(2027, 5, 1), 14, 12.0),
            status=200,
        )
        responses.add(
            responses.GET,
            FORECAST_URL,
            json=_daily(_dt.date(2027, 5, 14), 20, 16.0),
            status=200,
        )
        responses.add(
            responses.GET,
            ARCHIVE_URL,
            json=_daily(_dt.date(2024, 1, 1), 1000, 8.0),
            status=200,
        )

        series = wx.build_series(
            LAT,
            LON,
            _dt.date(2027, 5, 1),
            _dt.date(2027, 7, 1),
            cfg,
            tmp_path,
            today=TODAY,
            forecast_url=FORECAST_URL,
        )
        by_date = {d.date: d for d in series.days}
        # 1-14 May is published archive.
        assert by_date[_dt.date(2027, 5, 10)].source == "archive"
        assert by_date[_dt.date(2027, 5, 10)].mean_c == 12.0
        # The forecast covers the archive's publication lag and the horizon.
        assert by_date[_dt.date(2027, 5, 25)].source == "forecast"
        # Beyond that, the climatological normal.
        assert by_date[_dt.date(2027, 6, 20)].source == "normal"
        assert series.horizon == _dt.date(2027, 6, 2)

    @responses.activate
    def test_archive_wins_over_forecast_on_an_overlapping_day(self, tmp_path: Path):
        cfg = SeasonConfig(projection_normal_years=2)
        responses.add(
            responses.GET, ARCHIVE_URL, json=_daily(_dt.date(2027, 5, 1), 14, 12.0)
        )
        responses.add(
            responses.GET, FORECAST_URL, json=_daily(_dt.date(2027, 5, 1), 30, 99.0)
        )
        responses.add(
            responses.GET, ARCHIVE_URL, json=_daily(_dt.date(2025, 1, 1), 700, 8.0)
        )

        series = wx.build_series(
            LAT,
            LON,
            _dt.date(2027, 5, 1),
            _dt.date(2027, 5, 30),
            cfg,
            tmp_path,
            today=TODAY,
            forecast_url=FORECAST_URL,
        )
        overlap = next(d for d in series.days if d.date == _dt.date(2027, 5, 10))
        assert overlap.source == "archive" and overlap.mean_c == 12.0

    @responses.activate
    def test_a_missing_normal_is_reported_not_faked(self, tmp_path: Path):
        cfg = SeasonConfig(projection_normal_years=2)
        responses.add(
            responses.GET, ARCHIVE_URL, json=_daily(_dt.date(2027, 5, 1), 14, 12.0)
        )
        responses.add(
            responses.GET, FORECAST_URL, json=_daily(_dt.date(2027, 5, 14), 20, 16.0)
        )
        responses.add(responses.GET, ARCHIVE_URL, json={}, status=500)

        series = wx.build_series(
            LAT,
            LON,
            _dt.date(2027, 5, 1),
            _dt.date(2027, 8, 1),
            cfg,
            tmp_path,
            today=TODAY,
            forecast_url=FORECAST_URL,
        )
        assert any("no climatological normal" in n for n in series.notes)
        assert any("no temperature from any source" in n for n in series.notes)


class TestCaching:
    @responses.activate
    def test_a_finished_year_is_cached_and_not_refetched(self, tmp_path: Path):
        cfg = SeasonConfig()
        responses.add(
            responses.GET,
            ARCHIVE_URL,
            json=_daily(_dt.date(2025, 5, 1), 31, 11.0),
            status=200,
        )
        args = (LAT, LON, _dt.date(2025, 5, 1), _dt.date(2025, 5, 31), cfg, tmp_path)
        first, _ = wx.fetch_archive(*args)
        second, _ = wx.fetch_archive(*args)
        assert first == second
        assert len(responses.calls) == 1  # the second call was served from cache

    @responses.activate
    def test_the_archive_cache_lands_under_the_season_cache_dir(self, tmp_path: Path):
        responses.add(
            responses.GET, ARCHIVE_URL, json=_daily(_dt.date(2025, 5, 1), 31, 11.0)
        )
        wx.fetch_archive(
            LAT,
            LON,
            _dt.date(2025, 5, 1),
            _dt.date(2025, 5, 31),
            SeasonConfig(),
            tmp_path,
        )
        cached = tmp_path / "season" / "archive" / f"{LAT}_{LON}_2025.json"
        assert cached.exists()
        assert json.loads(cached.read_text())["days"]["2025-05-14"] == 11.0

    @responses.activate
    def test_the_forecast_cache_respects_its_short_ttl(self, tmp_path: Path):
        responses.add(responses.GET, FORECAST_URL, json=_daily(TODAY, 14, 16.0))
        cfg = SeasonConfig()
        wx.fetch_forecast_means(LAT, LON, cfg, tmp_path, forecast_url=FORECAST_URL)
        wx.fetch_forecast_means(LAT, LON, cfg, tmp_path, forecast_url=FORECAST_URL)
        assert len(responses.calls) == 1

    @responses.activate
    def test_a_cache_version_bump_invalidates_old_files(self, tmp_path: Path):
        path = tmp_path / "season" / "forecast" / f"{LAT}_{LON}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"v": 0, "days": {"2027-05-20": 1.0}}))
        responses.add(responses.GET, FORECAST_URL, json=_daily(TODAY, 14, 16.0))
        pairs, _ = wx.fetch_forecast_means(
            LAT, LON, SeasonConfig(), tmp_path, forecast_url=FORECAST_URL
        )
        assert pairs["2027-05-20"] == 16.0


class TestOffline:
    """Spec §13: offline degrades to cached weather and reports staleness."""

    @responses.activate
    def test_offline_serves_the_cache_and_flags_it_stale(self, tmp_path: Path):
        responses.add(
            responses.GET, ARCHIVE_URL, json=_daily(_dt.date(2027, 5, 1), 14, 12.0)
        )
        online = SeasonConfig()
        wx.fetch_archive(
            LAT, LON, _dt.date(2027, 5, 1), _dt.date(2027, 5, 14), online, tmp_path
        )

        _age_cache(tmp_path / "season" / "archive" / f"{LAT}_{LON}_2027.json")
        offline = SeasonConfig(offline=True, history_ttl_days=1)
        pairs, stale = wx.fetch_archive(
            LAT, LON, _dt.date(2027, 5, 1), _dt.date(2027, 5, 14), offline, tmp_path
        )
        assert pairs["2027-05-10"] == 12.0
        assert stale is True

    def test_offline_with_no_cache_is_an_explicit_error(self, tmp_path: Path):
        cfg = SeasonConfig(offline=True)
        with pytest.raises(wx.WeatherHistoryError, match="offline and no cached"):
            wx.fetch_archive(
                LAT, LON, _dt.date(2027, 5, 1), _dt.date(2027, 5, 14), cfg, tmp_path
            )

    def test_offline_forecast_with_no_cache_returns_empty_and_stale(
        self, tmp_path: Path
    ):
        pairs, stale = wx.fetch_forecast_means(
            LAT, LON, SeasonConfig(offline=True), tmp_path, forecast_url=FORECAST_URL
        )
        assert pairs == {} and stale is True

    @responses.activate
    def test_a_stale_series_says_so_in_its_notes(self, tmp_path: Path):
        responses.add(
            responses.GET, ARCHIVE_URL, json=_daily(_dt.date(2027, 5, 1), 14, 12.0)
        )
        wx.fetch_archive(
            LAT,
            LON,
            _dt.date(2027, 5, 1),
            _dt.date(2027, 5, 14),
            SeasonConfig(),
            tmp_path,
        )
        _age_cache(tmp_path / "season" / "archive" / f"{LAT}_{LON}_2027.json")
        series = wx.build_series(
            LAT,
            LON,
            _dt.date(2027, 5, 1),
            _dt.date(2027, 5, 14),
            SeasonConfig(offline=True, history_ttl_days=1),
            tmp_path,
            today=TODAY,
            forecast_url=FORECAST_URL,
        )
        assert series.stale
        assert any("stale cache" in n for n in series.notes)


class TestNetworkFailure:
    @responses.activate
    def test_a_failed_forecast_falls_back_to_the_stale_cache(self, tmp_path: Path):
        responses.add(responses.GET, FORECAST_URL, json=_daily(TODAY, 14, 16.0))
        wx.fetch_forecast_means(
            LAT, LON, SeasonConfig(), tmp_path, forecast_url=FORECAST_URL, ttl_hours=0
        )
        responses.reset()
        responses.add(responses.GET, FORECAST_URL, json={}, status=503)
        pairs, stale = wx.fetch_forecast_means(
            LAT, LON, SeasonConfig(), tmp_path, forecast_url=FORECAST_URL, ttl_hours=0
        )
        assert pairs["2027-05-20"] == 16.0 and stale is True

    @responses.activate
    def test_a_failed_archive_with_no_cache_raises(self, tmp_path: Path):
        responses.add(responses.GET, ARCHIVE_URL, json={}, status=500)
        with pytest.raises(wx.WeatherHistoryError, match="could not fetch"):
            wx.fetch_archive(
                LAT,
                LON,
                _dt.date(2027, 5, 1),
                _dt.date(2027, 5, 14),
                SeasonConfig(),
                tmp_path,
            )

    @responses.activate
    def test_build_series_notes_an_archive_failure_instead_of_dying(
        self, tmp_path: Path
    ):
        responses.add(responses.GET, ARCHIVE_URL, json={}, status=500)
        responses.add(
            responses.GET, FORECAST_URL, json=_daily(_dt.date(2027, 5, 14), 20, 16.0)
        )
        series = wx.build_series(
            LAT,
            LON,
            _dt.date(2027, 5, 1),
            _dt.date(2027, 5, 30),
            SeasonConfig(),
            tmp_path,
            today=TODAY,
            forecast_url=FORECAST_URL,
        )
        assert any("archive unavailable" in n for n in series.notes)
        assert series.days  # the forecast half still came through
