"""End-to-end: plan → scored days → field day, against the fake host."""

from __future__ import annotations

import datetime as _dt

import pytest

from flightmanager.season import opportunities, store
from flightmanager.season.planner import PlanError, init_plan, recompute
from flightmanager.season.weather_history import HourlyWeather, HourSample
from tests.season.conftest import FakeHost

FOLDER = "kentta"
#: Sown so that, on TODAY, the N-topdress window (17-29 June) is open. Derived
#: from the shipped barley table against a flat 15 °C series, not guessed.
SOWING = _dt.date(2026, 5, 14)
TODAY = _dt.date(2026, 6, 20)


def _hourly(
    wind=3.0, cloud=25.0, precip=0.0, days=20, start: _dt.date | None = None
) -> HourlyWeather:
    start = start or TODAY
    by_hour = {}
    for offset in range(days):
        day = start + _dt.timedelta(days=offset)
        for hour in range(0, 24):
            key = f"{day.isoformat()}T{hour:02d}"
            by_hour[key] = HourSample(
                hour_key=key,
                wind_ms=wind,
                gust_ms=wind * 1.2,
                cloud_pct=cloud,
                precip_mm=precip,
                temp_c=16.0,
            )
    return HourlyWeather(by_hour=by_hour, utc_offset_s=3 * 3600)


@pytest.fixture
def seeded(host, tables, cfg, monkeypatch):
    """A folder with a planned 2026 season and controllable weather."""
    from flightmanager.season import weather_history as wx
    from tests.season.conftest import constant_series

    def fake_series(lat, lon, start, end, cfg_, cache_dir, **kwargs):
        return constant_series(start, (end - start).days + 1, 15.0, archive_until=TODAY)

    monkeypatch.setattr(wx, "build_series", fake_series)
    init_plan(
        host, FOLDER, 2026, crop_id="spring_barley", sowing_date=SOWING, tables=tables
    )
    recompute(host, tables, cfg, FOLDER, 2026, today=TODAY)
    monkeypatch.setattr(wx, "fetch_hourly", lambda *a, **k: _hourly())
    return host


def _find(host, tables, cfg, **kwargs):
    return opportunities.find_opportunities(
        host, tables, cfg, FOLDER, 2026, today=TODAY, **kwargs
    )


class TestFindOpportunities:
    def test_it_scores_the_whole_horizon(self, seeded, tables, cfg):
        report = _find(seeded, tables, cfg, days=7)
        assert len(report.opportunities) == 7
        assert report.campaigns_considered > 0

    def test_days_come_back_ranked(self, seeded, tables, cfg):
        report = _find(seeded, tables, cfg, days=10)
        scores = [o.score for o in report.opportunities]
        assert scores == sorted(scores, reverse=True)

    def test_the_best_day_is_flyable(self, seeded, tables, cfg):
        best = _find(seeded, tables, cfg, days=10).best()
        assert best is not None
        assert best.score > 0
        assert best.best_hours

    def test_by_date_finds_a_specific_day(self, seeded, tables, cfg):
        report = _find(seeded, tables, cfg, days=5)
        assert report.by_date(TODAY) is not None
        assert report.by_date(TODAY + _dt.timedelta(days=99)) is None

    def test_a_missing_plan_is_an_error(self, host, tables, cfg):
        with pytest.raises(PlanError, match="no season plan"):
            _find(host, tables, cfg)

    def test_a_plan_with_no_open_window_says_so(self, seeded, tables, cfg):
        folder_dir = seeded.folder_dir(FOLDER)
        plan = store.load_plan(folder_dir, 2026)
        for campaign in plan.campaigns:
            campaign.state = "skipped"
        store.save_plan(folder_dir, plan)
        report = _find(seeded, tables, cfg)
        assert report.opportunities == []
        assert any("nothing to schedule" in w for w in report.warnings)

    def test_uncalibrated_windows_are_disclosed(self, seeded, tables, cfg):
        report = _find(seeded, tables, cfg, days=5)
        assert any("UNCALIBRATED" in n for n in report.notices)

    def test_stale_weather_is_reported(self, seeded, tables, cfg, monkeypatch):
        from flightmanager.season import weather_history as wx

        stale = _hourly()
        stale.stale = True
        monkeypatch.setattr(wx, "fetch_hourly", lambda *a, **k: stale)
        report = _find(seeded, tables, cfg, days=3)
        assert report.weather_stale
        assert any("stale cache" in w for w in report.warnings)

    def test_audience_narrows_what_is_scored(self, seeded, tables, cfg):
        everyone = _find(seeded, tables, cfg, days=3).campaigns_considered
        farmer = _find(
            seeded, tables, cfg, days=3, audience="farmer"
        ).campaigns_considered
        assert farmer < everyone


class TestWeatherGates:
    def test_a_gale_makes_the_whole_horizon_unflyable(
        self, seeded, tables, cfg, monkeypatch
    ):
        from flightmanager.season import weather_history as wx

        monkeypatch.setattr(wx, "fetch_hourly", lambda *a, **k: _hourly(wind=22.0))
        report = _find(seeded, tables, cfg, days=5)
        assert report.flyable() == []
        assert any("wind is above the drone limit" in n for n in report.notices)

    def test_persistent_rain_is_named_as_the_blocker(
        self, seeded, tables, cfg, monkeypatch
    ):
        from flightmanager.season import weather_history as wx

        monkeypatch.setattr(wx, "fetch_hourly", lambda *a, **k: _hourly(precip=6.0))
        report = _find(seeded, tables, cfg, days=5)
        assert report.flyable() == []
        assert any("rain exceeds" in n for n in report.notices)

    def test_calm_clear_weather_beats_windy_weather(
        self, seeded, tables, cfg, monkeypatch
    ):
        from flightmanager.season import weather_history as wx

        monkeypatch.setattr(
            wx, "fetch_hourly", lambda *a, **k: _hourly(wind=1.0, cloud=5.0)
        )
        good = _find(seeded, tables, cfg, days=3).best().score
        monkeypatch.setattr(
            wx, "fetch_hourly", lambda *a, **k: _hourly(wind=8.0, cloud=90.0)
        )
        poor = _find(seeded, tables, cfg, days=3).best().score
        assert good > poor


class TestTheSolarWall:
    """Spec §7.2: say the sun is the blocker, not just "no good days"."""

    def test_an_autumn_horizon_names_the_latitude_limit(
        self, host, tables, cfg, monkeypatch
    ):
        from flightmanager.season import weather_history as wx
        from tests.season.conftest import constant_series

        autumn = _dt.date(2026, 10, 20)
        monkeypatch.setattr(
            wx,
            "build_series",
            lambda lat, lon, start, end, c, cache, **kw: constant_series(
                start, (end - start).days + 1, 15.0, archive_until=autumn
            ),
        )
        init_plan(
            host,
            FOLDER,
            2026,
            crop_id="spring_barley",
            sowing_date=SOWING,
            tables=tables,
        )
        recompute(host, tables, cfg, FOLDER, 2026, today=autumn)

        # Force every campaign's window open around the autumn date so that the
        # only thing that can block the day is the sun.
        folder_dir = host.folder_dir(FOLDER)
        plan = store.load_plan(folder_dir, 2026)
        for campaign in plan.campaigns:
            if campaign.window:
                campaign.window.earliest = autumn - _dt.timedelta(days=5)
                campaign.window.target = autumn
                campaign.window.latest = autumn + _dt.timedelta(days=5)
                campaign.state = "open"
        store.save_plan(folder_dir, plan)

        monkeypatch.setattr(wx, "fetch_hourly", lambda *a, **k: _hourly(start=autumn))
        report = opportunities.find_opportunities(
            host, tables, cfg, FOLDER, 2026, days=5, today=autumn
        )
        note = " ".join(report.notices)
        assert "sun never clears" in note
        assert "latitude limit, not a weather one" in note


class TestFieldDayIntegration:
    def test_it_builds_a_route_for_the_best_day(self, seeded, tables, cfg):
        best = _find(seeded, tables, cfg, days=7).best()
        plan, opportunity, report = opportunities.field_day(
            seeded, tables, cfg, FOLDER, 2026, best.date, today=TODAY
        )
        assert plan.jobs
        assert opportunity.date == best.date
        assert [j.route_index for j in plan.jobs] == list(range(1, len(plan.jobs) + 1))

    def test_a_date_outside_the_horizon_is_refused(self, seeded, tables, cfg):
        with pytest.raises(PlanError, match="outside the scored horizon"):
            opportunities.field_day(
                seeded,
                tables,
                cfg,
                FOLDER,
                2026,
                TODAY - _dt.timedelta(days=5),
                today=TODAY,
            )

    def test_max_hours_is_threaded_through(self, seeded, tables, cfg):
        best = _find(seeded, tables, cfg, days=7).best()
        plan, _, _ = opportunities.field_day(
            seeded, tables, cfg, FOLDER, 2026, best.date, today=TODAY, max_hours=0.25
        )
        assert plan.max_field_day_hours == 0.25

    def test_launch_site_clustering_is_offered_the_host_seam(self, seeded, tables, cfg):
        called = {}

        def cluster(cards):
            called["n"] = len(cards)
            return []

        seeded.cluster_launch_sites = cluster
        best = _find(seeded, tables, cfg, days=7).best()
        opportunities.field_day(
            seeded, tables, cfg, FOLDER, 2026, best.date, today=TODAY
        )
        assert called.get("n", 0) >= 1


class TestWindLimitSourcing:
    def test_the_hosts_configured_limit_is_used(self, seeded, tables, cfg, monkeypatch):
        from flightmanager.season import weather_history as wx

        monkeypatch.setattr(wx, "fetch_hourly", lambda *a, **k: _hourly(wind=7.0))
        seeded.drone_wind_limit_ms = 6.0
        assert _find(seeded, tables, cfg, days=3).flyable() == []
        seeded.drone_wind_limit_ms = 12.0
        assert _find(seeded, tables, cfg, days=3).flyable()

    def test_an_unset_limit_falls_back_and_discloses_it(self, seeded, tables, cfg):
        seeded.drone_wind_limit_ms = None
        report = _find(seeded, tables, cfg, days=3)
        flags = [f for o in report.opportunities for f in o.flags]
        assert any("drone_wind_limit_ms is unset" in f for f in flags)


class TestNoJobGeometry:
    def test_a_folder_without_geometry_is_refused_clearly(
        self, tmp_path, tables, cfg, monkeypatch
    ):
        from flightmanager.season import weather_history as wx
        from flightmanager.season.integration import JobRef

        from flightmanager.season.models import Campaign, Window

        host = FakeHost(
            root=tmp_path,
            jobs=[JobRef(path="kentta/a", name="a", folder="kentta")],  # no lat/lon
        )
        plan = store.new_plan(FOLDER, 2026)
        plan.campaigns.append(
            Campaign(
                campaign_id="c1",
                type_id="emergence_count",
                season=2026,
                job_paths=["kentta/a"],
                state="open",
                window=Window(
                    computed_at="2026-06-01T00:00:00+00:00",
                    earliest=TODAY - _dt.timedelta(days=2),
                    target=TODAY,
                    latest=TODAY + _dt.timedelta(days=2),
                ),
            )
        )
        store.save_plan(host.folder_dir(FOLDER), plan)
        monkeypatch.setattr(wx, "fetch_hourly", lambda *a, **k: _hourly())
        with pytest.raises(PlanError, match="no job in folder"):
            opportunities.find_opportunities(
                host, tables, cfg, FOLDER, 2026, today=TODAY
            )
